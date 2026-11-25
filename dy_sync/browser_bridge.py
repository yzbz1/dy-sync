from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import random
from typing import Callable

import aiohttp
from playwright.async_api import BrowserContext, Response, async_playwright

from .config import AppConfig
from .utils import extract_sec_uid, is_short_url


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class VideoItem:
    aweme_id: str
    sec_uid: str
    author_nickname: str
    desc: str
    create_time: int
    is_top: bool
    media_type: str  # "video" | "images"
    video_urls: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)


@dataclass
class SyncScanResult:
    sec_uid: str
    nickname: str
    new_items: list[VideoItem]
    total_scanned: int
    skipped_existing: int
    pages_fetched: int
    stopped_by_incremental: bool
    cookie_header: str


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u and u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def parse_aweme_item(raw: dict, fallback_sec_uid: str) -> VideoItem | None:
    """从抖音 /aweme/v1/web/aweme/post/ 单条作品 JSON 中提取结构化信息。"""
    aweme_id = str(raw.get("aweme_id") or "").strip()
    if not aweme_id:
        return None

    author = raw.get("author") or {}
    sec_uid = str(author.get("sec_uid") or fallback_sec_uid or "").strip()
    nickname = str(author.get("nickname") or "").strip()
    desc = str(raw.get("desc") or "").strip()
    create_time = int(raw.get("create_time") or 0)
    is_top = bool(raw.get("is_top") == 1)

    # 判断图文作品
    images = raw.get("images") or []
    if images and isinstance(images, list):
        img_urls: list[str] = []
        for img in images:
            if not isinstance(img, dict):
                continue
            url_list = img.get("url_list") or img.get("download_url_list") or []
            if url_list:
                img_urls.append(str(url_list[0]))
        if img_urls:
            return VideoItem(
                aweme_id=aweme_id,
                sec_uid=sec_uid,
                author_nickname=nickname,
                desc=desc,
                create_time=create_time,
                is_top=is_top,
                media_type="images",
                image_urls=img_urls,
            )

    # 提取最高码率无水印视频直链
    video = raw.get("video") or {}
    candidate_urls: list[str] = []

    bit_rates = video.get("bit_rate") or []
    if isinstance(bit_rates, list) and bit_rates:
        sorted_br = sorted(
            [b for b in bit_rates if isinstance(b, dict)],
            key=lambda b: int(b.get("bit_rate") or 0),
            reverse=True,
        )
        for br in sorted_br:
            play_addr = br.get("play_addr") or {}
            for u in play_addr.get("url_list") or []:
                if isinstance(u, str):
                    candidate_urls.append(u.replace("playwm", "play"))

    play_addr = video.get("play_addr") or {}
    for u in play_addr.get("url_list") or []:
        if isinstance(u, str):
            candidate_urls.append(u.replace("playwm", "play"))

    play_addr_h264 = video.get("play_addr_h264") or {}
    for u in play_addr_h264.get("url_list") or []:
        if isinstance(u, str):
            candidate_urls.append(u.replace("playwm", "play"))

    video_urls = _dedupe_urls(candidate_urls)
    if not video_urls:
        return None

    return VideoItem(
        aweme_id=aweme_id,
        sec_uid=sec_uid,
        author_nickname=nickname,
        desc=desc,
        create_time=create_time,
        is_top=is_top,
        media_type="video",
        video_urls=video_urls,
    )


async def resolve_profile_url(raw_url: str) -> tuple[str, str]:
    """解析标准主页链接或 v.douyin.com 短链，返回 (sec_uid, canonical_url)。"""
    sec_uid = extract_sec_uid(raw_url)
    if sec_uid:
        return sec_uid, f"https://www.douyin.com/user/{sec_uid}"

    if is_short_url(raw_url):
        headers = {"User-Agent": DEFAULT_UA}
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(raw_url, allow_redirects=True) as resp:
                final_url = str(resp.url)
                sec_uid = extract_sec_uid(final_url)
                if sec_uid:
                    return sec_uid, f"https://www.douyin.com/user/{sec_uid}"
                raise ValueError(f"短链重定向后未找到用户 sec_uid: {final_url}")

    raise ValueError(
        "无法从链接中识别抖音博主 sec_uid，请提供形如 "
        "https://www.douyin.com/user/MS4wLjAB... 或 https://v.douyin.com/... 的主页链接"
    )


class DouyinBrowserBridge:
    """
    借鉴 Page Bridge 思路：通过 Playwright 持久化浏览器上下文复用真实页面环境与登录态，
    直接监听网页原生发出的 /aweme/v1/web/aweme/post/ 响应，免去手写易失效的前端签名算法。
    """

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

    async def _launch_context(
        self, p, *, headless: bool
    ) -> BrowserContext:
        launch_kwargs: dict = {
            "user_data_dir": str(self.cfg.browser_profile_dir),
            "headless": headless,
            "viewport": {"width": 1280, "height": 900},
            "user_agent": DEFAULT_UA,
            "locale": "zh-CN",
            "args": [
                "--disable-blink-features=AutomationControlled",
            ],
        }
        if self.cfg.browser_channel:
            launch_kwargs["channel"] = self.cfg.browser_channel
        return await p.chromium.launch_persistent_context(**launch_kwargs)

    async def login(self) -> bool:
        """打开可见浏览器窗口，供用户扫码登录并自动保存持久化会话。"""
        async with async_playwright() as p:
            ctx = await self._launch_context(p, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(
                    "https://www.douyin.com/", wait_until="domcontentloaded"
                )
                print(
                    "\n[登录提示] 请在弹出的浏览器窗口中完成抖音扫码/手机号登录。"
                )
                print(
                    "[登录提示] 登录成功后回到终端按 Enter 键保存并关闭窗口..."
                )
                await asyncio.to_thread(input)
                cookies = await ctx.cookies(["https://www.douyin.com"])
                names = {c["name"] for c in cookies}
                logged_in = bool(
                    {"sessionid", "sessionid_ss", "sid_guard"} & names
                )
                return logged_in
            finally:
                await ctx.close()

    async def scan_creator_posts(
        self,
        sec_uid: str,
        profile_url: str,
        is_downloaded_fn: Callable[[str], bool],
        *,
        full_sync: bool = False,
        on_page_callback: Callable[[int, int, int], None] | None = None,
    ) -> SyncScanResult:
        """
        打开博主主页并滚动翻页，监听原生作品列表接口，支持增量早停。
        """
        new_items: list[VideoItem] = []
        seen_aweme_ids: set[str] = set()
        author_nickname = ""
        total_scanned = 0
        skipped_existing = 0
        pages_fetched = 0
        consecutive_known_non_top = 0
        has_more = True
        stopped_by_incremental = False
        page_event = asyncio.Event()

        async def handle_response(resp: Response) -> None:
            nonlocal author_nickname, total_scanned, skipped_existing
            nonlocal pages_fetched, consecutive_known_non_top
            nonlocal has_more, stopped_by_incremental

            if "/aweme/v1/web/aweme/post/" not in resp.url:
                return
            if resp.status != 200:
                return

            try:
                raw_text = await resp.text()
                if not raw_text:
                    return
                payload = json.loads(raw_text)
            except Exception:
                return

            aweme_list = payload.get("aweme_list") or []
            pages_fetched += 1
            if payload.get("has_more") == 0:
                has_more = False

            for raw in aweme_list:
                item = parse_aweme_item(raw, sec_uid)
                if not item or item.aweme_id in seen_aweme_ids:
                    continue
                seen_aweme_ids.add(item.aweme_id)
                total_scanned += 1
                if item.author_nickname and not author_nickname:
                    author_nickname = item.author_nickname

                if is_downloaded_fn(item.aweme_id):
                    skipped_existing += 1
                    # 置顶视频不参与增量连续计数，防止误停
                    if not item.is_top:
                        consecutive_known_non_top += 1
                        if (
                            not full_sync
                            and consecutive_known_non_top
                            >= self.cfg.incremental_stop_threshold
                        ):
                            stopped_by_incremental = True
                else:
                    if not item.is_top:
                        consecutive_known_non_top = 0
                    new_items.append(item)

            if on_page_callback:
                on_page_callback(pages_fetched, len(new_items), skipped_existing)
            page_event.set()

        async with async_playwright() as p:
            ctx = await self._launch_context(p, headless=self.cfg.headless)
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                page.on("response", handle_response)

                await page.goto(
                    profile_url, wait_until="domcontentloaded", timeout=45000
                )

                # 等待首屏作品接口返回
                try:
                    await asyncio.wait_for(page_event.wait(), timeout=20.0)
                except asyncio.TimeoutError:
                    pass

                # 若接口里没拿到昵称，尝试从页面标题补充
                if not author_nickname:
                    try:
                        title = await page.title()
                        if "的抖音" in title:
                            author_nickname = title.split("的抖音")[0].strip()
                    except Exception:
                        pass

                # 滚动翻页，直到增量早停 / 无更多作品 / 达到页数上限 / 连续空转
                idle_scrolls = 0
                max_pages = self.cfg.max_scroll_pages
                while has_more and not stopped_by_incremental:
                    if max_pages > 0 and pages_fetched >= max_pages:
                        break
                    if idle_scrolls >= 4:
                        break

                    # 检测是否触发验证码/安全验证弹窗，避免在风控状态下继续无效滚动
                    try:
                        title_now = await page.title()
                        if any(k in title_now for k in ("验证码", "安全验证", "验证")):
                            break
                    except Exception:
                        pass

                    page_event.clear()
                    # 随机滚动距离与随机抖动间隔，模拟真人浏览节奏
                    await page.evaluate(
                        "window.scrollTo(0, document.body.scrollHeight)"
                    )
                    scroll_wait = random.uniform(
                        self.cfg.anti_risk.scroll_delay_min,
                        self.cfg.anti_risk.scroll_delay_max,
                    )
                    await asyncio.sleep(scroll_wait)

                    try:
                        await asyncio.wait_for(page_event.wait(), timeout=8.0)
                        idle_scrolls = 0
                    except asyncio.TimeoutError:
                        idle_scrolls += 1

                cookies = await ctx.cookies(["https://www.douyin.com"])
                cookie_header = "; ".join(
                    f"{c['name']}={c['value']}" for c in cookies if c.get("name")
                )
            finally:
                await ctx.close()

        return SyncScanResult(
            sec_uid=sec_uid,
            nickname=author_nickname,
            new_items=new_items,
            total_scanned=total_scanned,
            skipped_existing=skipped_existing,
            pages_fetched=pages_fetched,
            stopped_by_incremental=stopped_by_incremental,
            cookie_header=cookie_header,
        )
