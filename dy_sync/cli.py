from __future__ import annotations

import argparse
import asyncio
import random
import sys

from rich.console import Console
from rich.table import Table

from .browser_bridge import DouyinBrowserBridge, resolve_profile_url
from .config import AppConfig, load_config
from .downloader import AsyncDownloader
from .storage import CreatorRecord, Storage
from .utils import extract_url_from_text

console = Console()


async def _cmd_login(cfg: AppConfig) -> int:
    bridge = DouyinBrowserBridge(cfg)
    ok = await bridge.login()
    if ok:
        console.print("[green]✓ 检测到登录态 Cookie，会话已保存到本地目录。[/green]")
        return 0
    console.print(
        "[yellow]! 未检测到明确的登录会话 Cookie（可能未完成登录），如果同步时受限请重新执行 login。[/yellow]"
    )
    return 0


async def _cmd_add(cfg: AppConfig, raw_input: str, name: str = "") -> int:
    url = extract_url_from_text(raw_input)
    try:
        sec_uid, canonical_url = await resolve_profile_url(url)
    except Exception as exc:
        console.print(f"[red]添加失败：{exc}[/red]")
        return 1

    with Storage(cfg.db_path) as storage:
        creator, created = storage.upsert_creator(
            sec_uid=sec_uid, profile_url=canonical_url, nickname=name
        )
    if created:
        console.print(
            f"[green]✓ 已添加博主 #{creator.id}[/green] | "
            f"昵称: [bold]{creator.nickname or '(首次同步时自动获取)'}[/bold] | "
            f"sec_uid: {creator.sec_uid}"
        )
    else:
        console.print(
            f"[yellow]该博主已在列表中 (#{creator.id})[/yellow] | "
            f"昵称: [bold]{creator.nickname or '未知'}[/bold] | "
            f"sec_uid: {creator.sec_uid}"
        )
    return 0


def _cmd_list(cfg: AppConfig) -> int:
    with Storage(cfg.db_path) as storage:
        creators = storage.list_creators()

    if not creators:
        console.print("[yellow]当前没有记录任何博主，请先用 add 命令添加。[/yellow]")
        return 0

    table = Table(title="已记录的抖音博主")
    table.add_column("ID", justify="right", style="cyan")
    table.add_column("昵称", style="bold")
    table.add_column("sec_uid", style="dim")
    table.add_column("已下载", justify="right", style="green")
    table.add_column("失败", justify="right", style="red")
    table.add_column("上次同步时间")

    for c in creators:
        short_uid = c.sec_uid if len(c.sec_uid) <= 22 else f"{c.sec_uid[:18]}..."
        table.add_row(
            str(c.id),
            c.nickname or "(未同步)",
            short_uid,
            str(c.downloaded_count),
            str(c.failed_count),
            c.last_sync_at or "-",
        )
    console.print(table)
    return 0


def _cmd_remove(cfg: AppConfig, identifier: str) -> int:
    with Storage(cfg.db_path) as storage:
        creator = storage.get_creator(identifier)
        if not creator:
            console.print(f"[red]未找到博主：{identifier}[/red]")
            return 1
        storage.remove_creator(creator.sec_uid)
    console.print(
        f"[green]✓ 已从数据库移除博主 #{creator.id} ({creator.nickname or creator.sec_uid})[/green] "
        f"[dim](本地已下载的视频文件保留不变)[/dim]"
    )
    return 0


def _cmd_status(cfg: AppConfig) -> int:
    with Storage(cfg.db_path) as storage:
        s = storage.get_summary()
    console.print(
        f"订阅博主数: [cyan]{s['creators']}[/cyan] | "
        f"总记录作品: [bold]{s['total_videos']}[/bold] | "
        f"已下载: [green]{s['downloaded']}[/green] | "
        f"待下载: [yellow]{s['pending']}[/yellow] | "
        f"失败: [red]{s['failed']}[/red]"
    )
    console.print(f"下载目录: [dim]{cfg.download_dir}[/dim]")
    console.print(f"数据库路径: [dim]{cfg.db_path}[/dim]")
    return 0


async def _sync_one_creator(
    cfg: AppConfig,
    storage: Storage,
    bridge: DouyinBrowserBridge,
    creator: CreatorRecord,
    *,
    full_sync: bool,
) -> bool:
    label = creator.nickname or creator.sec_uid[:18]
    mode_text = "全量扫描" if full_sync else "增量更新"
    console.rule(f"[bold cyan]开始{mode_text}：#{creator.id} {label}[/bold cyan]")

    def _is_downloaded(aweme_id: str) -> bool:
        return storage.is_video_downloaded(
            aweme_id, redownload_missing=cfg.redownload_missing
        )

    def _on_page(page_no: int, new_cnt: int, skipped_cnt: int) -> None:
        console.print(
            f"  [dim]已扫描第 {page_no} 页 | 发现新作品: {new_cnt} | 已跳过: {skipped_cnt}[/dim]"
        )

    scan = await bridge.scan_creator_posts(
        sec_uid=creator.sec_uid,
        profile_url=creator.profile_url,
        is_downloaded_fn=_is_downloaded,
        full_sync=full_sync,
        on_page_callback=_on_page,
    )

    nickname = scan.nickname or creator.nickname or creator.sec_uid[:16]
    max_time = max((item.create_time for item in scan.new_items), default=0)

    stop_reason = "已追平上次同步点（增量早停）" if scan.stopped_by_incremental else "已扫描完毕"
    console.print(
        f"扫描完成（{stop_reason}）：共扫描 {scan.total_scanned} 条，"
        f"新增待下载 [bold green]{len(scan.new_items)}[/bold green] 条，"
        f"跳过已下载 {scan.skipped_existing} 条。"
    )

    if scan.new_items:
        downloader = AsyncDownloader(
            cfg=cfg,
            storage=storage,
            cookie_header=scan.cookie_header,
            on_notice=lambda msg: console.print(f"[yellow]{msg}[/yellow]"),
        )
        dl_summary = await downloader.download_items(nickname, scan.new_items)
        console.print(
            f"下载完成：成功 [green]{dl_summary.succeeded}[/green] 条，"
            f"失败 [red]{dl_summary.failed}[/red] 条"
            + (
                f"，因熔断暂停 [yellow]{dl_summary.skipped_by_breaker}[/yellow] 条"
                if dl_summary.skipped_by_breaker
                else ""
            )
            + "。"
        )
        if dl_summary.breaker_tripped:
            console.print(f"[bold red]⚠ {dl_summary.breaker_reason}[/bold red]")
            return False
    else:
        console.print("[green]✓ 没有需要下载的新作品。[/green]")

    storage.update_creator_sync(
        sec_uid=creator.sec_uid, nickname=nickname, last_video_time=max_time
    )
    return True


async def _cmd_sync(
    cfg: AppConfig, creator_ident: str | None = None, full_sync: bool = False
) -> int:
    with Storage(cfg.db_path) as storage:
        if creator_ident:
            target = storage.get_creator(creator_ident)
            if not target:
                console.print(f"[red]未找到博主：{creator_ident}[/red]")
                return 1
            targets = [target]
        else:
            targets = storage.list_creators()

        if not targets:
            console.print("[yellow]还没有添加任何博主，请先执行 add 命令。[/yellow]")
            return 0

        bridge = DouyinBrowserBridge(cfg)
        for idx, creator in enumerate(targets):
            if idx > 0:
                gap = random.uniform(
                    cfg.anti_risk.creator_delay_min,
                    cfg.anti_risk.creator_delay_max,
                )
                console.print(
                    f"[dim][防风控] 切换下一个博主前随机休眠 {gap:.1f} 秒...[/dim]"
                )
                await asyncio.sleep(gap)
            try:
                ok = await _sync_one_creator(
                    cfg, storage, bridge, creator, full_sync=full_sync
                )
                if not ok:
                    console.print("[red]检测到风控熔断，停止后续博主同步。[/red]")
                    return 2
            except Exception as exc:
                console.print(
                    f"[red]同步博主 #{creator.id} ({creator.nickname or creator.sec_uid}) 出错：{exc}[/red]"
                )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dy-sync",
        description="本地抖音博主视频订阅与增量下载工具（Playwright + aiohttp + SQLite）",
    )
    parser.add_argument(
        "-c", "--config", default="config.yml", help="配置文件路径（默认 config.yml）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="打开浏览器扫码登录抖音并保存本地会话")

    p_add = sub.add_parser("add", help="记录一个抖音博主主页链接到数据库")
    p_add.add_argument("url", help="博主主页链接、短链或包含链接的分享文案")
    p_add.add_argument("--name", default="", help="可选：自定义初始备注名")

    sub.add_parser("list", help="列出所有已记录的博主及同步统计")

    p_rm = sub.add_parser("remove", help="从数据库中移除指定博主")
    p_rm.add_argument("creator", help="博主 ID 或 sec_uid")

    p_sync = sub.add_parser("sync", help="同步并下载博主视频（默认增量更新）")
    p_sync.add_argument(
        "--creator", default=None, help="只同步指定博主（填 ID 或 sec_uid）；留空则同步全部"
    )
    p_sync.add_argument(
        "--full",
        action="store_true",
        help="忽略增量早停，全量扫描历史作品（已下载的仍会自动跳过）",
    )

    sub.add_parser("status", help="查看下载库整体统计")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "login":
        return asyncio.run(_cmd_login(cfg))
    if args.command == "add":
        return asyncio.run(_cmd_add(cfg, args.url, name=args.name))
    if args.command == "list":
        return _cmd_list(cfg)
    if args.command == "remove":
        return _cmd_remove(cfg, args.creator)
    if args.command == "sync":
        return asyncio.run(
            _cmd_sync(cfg, creator_ident=args.creator, full_sync=args.full)
        )
    if args.command == "status":
        return _cmd_status(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
