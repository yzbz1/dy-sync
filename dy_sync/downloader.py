from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import random
import time
from typing import Callable

import aiohttp
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TransferSpeedColumn,
)

from .browser_bridge import DEFAULT_UA, VideoItem
from .config import AntiRiskConfig, AppConfig
from .storage import Storage
from .utils import format_video_filename, sanitize_filename


class CircuitBreakerTripped(RuntimeError):
    """连续失败或触发风控限流达到阈值，触发熔断保护。"""


class AntiRiskController:
    """
    下载阶段防风控控制器：
    1. 速率上限 + 随机抖动间隔（避免固定机械节奏）
    2. 批次冷却休眠（每完成 N 个作品主动暂停休息）
    3. 403/429 自适应指数退避
    4. 连续失败熔断器（及时止损，避免持续触发风控）
    """

    def __init__(
        self,
        rate_per_sec: float,
        anti_risk: AntiRiskConfig,
        on_notice: Callable[[str], None] | None = None,
    ) -> None:
        self._min_interval = 1.0 / max(0.1, rate_per_sec)
        self.cfg = anti_risk
        self.on_notice = on_notice
        self._lock = asyncio.Lock()
        self._last_ts = 0.0
        self._completed_since_cooldown = 0
        self._consecutive_failures = 0
        self._tripped = False
        self._trip_reason = ""

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def trip_reason(self) -> str:
        return self._trip_reason

    def _emit(self, msg: str) -> None:
        if self.on_notice:
            self.on_notice(msg)

    async def before_request(self) -> None:
        """每次发起下载请求前执行：校验熔断 + 限速 + 随机抖动。"""
        async with self._lock:
            if self._tripped:
                raise CircuitBreakerTripped(self._trip_reason)

            jitter = random.uniform(
                self.cfg.download_delay_min, self.cfg.download_delay_max
            )
            required_gap = max(self._min_interval, jitter)
            now = time.monotonic()
            wait = required_gap - (now - self._last_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_ts = time.monotonic()

    async def record_success(self) -> None:
        """作品下载成功：清零连续失败计数，按需触发批次冷却。"""
        async with self._lock:
            self._consecutive_failures = 0
            self._completed_since_cooldown += 1
            every = self.cfg.batch_cooldown_every
            if every > 0 and self._completed_since_cooldown >= every:
                self._completed_since_cooldown = 0
                cooldown = random.uniform(
                    self.cfg.batch_cooldown_min, self.cfg.batch_cooldown_max
                )
                self._emit(
                    f"[防风控] 已连续下载 {every} 个作品，进入批次冷却休眠 {cooldown:.1f} 秒..."
                )
                await asyncio.sleep(cooldown)
                self._last_ts = time.monotonic()

    async def record_failure(self, reason: str, *, is_risk_http: bool = False) -> None:
        """记录失败：遇到 403/429 时加大退避；连续失败达阈值触发熔断。"""
        async with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.cfg.circuit_breaker_failures:
                self._tripped = True
                self._trip_reason = (
                    f"连续失败 {self._consecutive_failures} 次（最近错误: {reason}），"
                    "已触发防风控自动熔断，请稍后再试"
                )
                self._emit(f"[防风控熔断] {self._trip_reason}")
                return

            if is_risk_http:
                backoff = self.cfg.rate_limit_backoff_base * (
                    2 ** (self._consecutive_failures - 1)
                )
                backoff = min(backoff, 120.0) + random.uniform(1.0, 4.0)
                self._emit(
                    f"[防风控退避] 检测到限流/拒绝响应 ({reason})，冷却 {backoff:.1f} 秒..."
                )
                await asyncio.sleep(backoff)
                self._last_ts = time.monotonic()


@dataclass
class DownloadSummary:
    succeeded: int = 0
    failed: int = 0
    skipped_by_breaker: int = 0
    breaker_tripped: bool = False
    breaker_reason: str = ""


class AsyncDownloader:
    """支持并发控制、防风控节奏调节、多候选直链回退与原子落盘的异步下载器。"""

    def __init__(
        self,
        cfg: AppConfig,
        storage: Storage,
        cookie_header: str = "",
        on_notice: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.storage = storage
        self.cookie_header = cookie_header
        self.controller = AntiRiskController(
            rate_per_sec=cfg.rate_limit,
            anti_risk=cfg.anti_risk,
            on_notice=on_notice,
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": DEFAULT_UA,
            "Referer": "https://www.douyin.com/",
        }
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        return headers

    async def download_items(
        self, creator_folder_name: str, items: list[VideoItem]
    ) -> DownloadSummary:
        summary = DownloadSummary()
        if not items:
            return summary

        creator_dir = self.cfg.download_dir / sanitize_filename(
            creator_folder_name, max_length=60, fallback="unknown_creator"
        )
        creator_dir.mkdir(parents=True, exist_ok=True)

        sem = asyncio.Semaphore(self.cfg.concurrency)
        timeout = aiohttp.ClientTimeout(total=self.cfg.timeout_seconds)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
        ) as progress:
            async with aiohttp.ClientSession(
                headers=self._headers(), timeout=timeout
            ) as session:
                tasks = [
                    self._download_one(
                        session, sem, creator_dir, item, progress, summary
                    )
                    for item in items
                ]
                await asyncio.gather(*tasks)

        summary.breaker_tripped = self.controller.tripped
        summary.breaker_reason = self.controller.trip_reason
        return summary

    async def _download_one(
        self,
        session: aiohttp.ClientSession,
        sem: asyncio.Semaphore,
        creator_dir: Path,
        item: VideoItem,
        progress: Progress,
        summary: DownloadSummary,
    ) -> None:
        async with sem:
            if self.controller.tripped:
                summary.skipped_by_breaker += 1
                return

            self.storage.record_discovered_video(
                aweme_id=item.aweme_id,
                sec_uid=item.sec_uid,
                desc=item.desc,
                create_time=item.create_time,
                is_top=item.is_top,
                media_type=item.media_type,
            )

            short_title = sanitize_filename(
                item.desc, max_length=24, fallback=item.aweme_id
            )
            task_id = progress.add_task(f"[cyan]{short_title}", total=None)

            try:
                if item.media_type == "images":
                    dest_dir, total_size = await self._download_images(
                        session, creator_dir, item, progress, task_id
                    )
                    self.storage.mark_downloaded(
                        item.aweme_id, str(dest_dir), total_size
                    )
                else:
                    dest_file, total_size = await self._download_video(
                        session, creator_dir, item, progress, task_id
                    )
                    self.storage.mark_downloaded(
                        item.aweme_id, str(dest_file), total_size
                    )
                summary.succeeded += 1
                progress.update(task_id, description=f"[green]✓ {short_title}")
                await self.controller.record_success()
            except CircuitBreakerTripped:
                summary.skipped_by_breaker += 1
                progress.update(task_id, description=f"[yellow]⏸ {short_title}")
            except Exception as exc:
                self.storage.mark_failed(item.aweme_id, str(exc))
                summary.failed += 1
                progress.update(task_id, description=f"[red]✗ {short_title}")
                is_risk = any(code in str(exc) for code in ("HTTP 403", "HTTP 429"))
                await self.controller.record_failure(str(exc), is_risk_http=is_risk)

    async def _download_video(
        self,
        session: aiohttp.ClientSession,
        creator_dir: Path,
        item: VideoItem,
        progress: Progress,
        task_id: int,
    ) -> tuple[Path, int]:
        filename = format_video_filename(
            template=self.cfg.filename_template,
            aweme_id=item.aweme_id,
            desc=item.desc,
            create_time=item.create_time,
            max_title_length=self.cfg.max_title_length,
            ext=".mp4",
        )
        dest_file = creator_dir / filename
        part_file = creator_dir / f"{filename}.part"

        last_err: Exception | None = None
        for attempt in range(self.cfg.retry_times):
            for url in item.video_urls:
                await self.controller.before_request()
                try:
                    size = await self._stream_to_file(
                        session, url, part_file, progress, task_id
                    )
                    os.replace(part_file, dest_file)
                    return dest_file, size
                except Exception as exc:
                    last_err = exc
                    if part_file.exists():
                        part_file.unlink(missing_ok=True)
            await asyncio.sleep(min(2 ** attempt, 8) + random.uniform(0.3, 1.2))

        raise RuntimeError(f"视频下载失败 ({item.aweme_id}): {last_err}")

    async def _download_images(
        self,
        session: aiohttp.ClientSession,
        creator_dir: Path,
        item: VideoItem,
        progress: Progress,
        task_id: int,
    ) -> tuple[Path, int]:
        folder_name = format_video_filename(
            template=self.cfg.filename_template,
            aweme_id=item.aweme_id,
            desc=item.desc,
            create_time=item.create_time,
            max_title_length=self.cfg.max_title_length,
            ext="",
        )
        dest_dir = creator_dir / folder_name
        dest_dir.mkdir(parents=True, exist_ok=True)

        total_size = 0
        for idx, url in enumerate(item.image_urls, start=1):
            img_file = dest_dir / f"{idx:02d}.jpg"
            part_file = dest_dir / f"{idx:02d}.jpg.part"
            last_err: Exception | None = None
            for attempt in range(self.cfg.retry_times):
                await self.controller.before_request()
                try:
                    size = await self._stream_to_file(
                        session, url, part_file, progress, task_id
                    )
                    os.replace(part_file, img_file)
                    total_size += size
                    last_err = None
                    break
                except Exception as exc:
                    last_err = exc
                    if part_file.exists():
                        part_file.unlink(missing_ok=True)
                    await asyncio.sleep(min(2 ** attempt, 5) + random.uniform(0.2, 1.0))
            if last_err:
                raise RuntimeError(f"图集第 {idx} 张下载失败: {last_err}")

        return dest_dir, total_size

    async def _stream_to_file(
        self,
        session: aiohttp.ClientSession,
        url: str,
        part_file: Path,
        progress: Progress,
        task_id: int,
    ) -> int:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            total = int(resp.headers.get("Content-Length") or 0)
            if total > 0:
                progress.update(task_id, total=total, completed=0)

            written = 0
            with part_file.open("wb") as f:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
                        progress.update(task_id, completed=written)

            if written <= 0:
                raise RuntimeError("响应内容为空")
            return written
