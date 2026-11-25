from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import urlparse


_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WHITESPACE = re.compile(r"\s+")
_USER_URL_RE = re.compile(r"/user/([A-Za-z0-9_-]+)")
_SHORT_URL_RE = re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9_-]+/?")
_ANY_URL_RE = re.compile(r"https?://[^\s]+")


def extract_url_from_text(text: str) -> str:
    """从抖音分享文案或纯链接中提取 URL。"""
    text = text.strip()
    m = _ANY_URL_RE.search(text)
    if m:
        return m.group(0).rstrip(".,;!?，。；！？")
    return text


def extract_sec_uid(url: str) -> str | None:
    """从标准抖音用户主页链接中提取 sec_uid。"""
    m = _USER_URL_RE.search(url)
    if m:
        sec_uid = m.group(1)
        if sec_uid and sec_uid.lower() != "self":
            return sec_uid
    return None


def is_short_url(url: str) -> bool:
    return bool(_SHORT_URL_RE.search(url))


def canonical_profile_url(sec_uid: str) -> str:
    return f"https://www.douyin.com/user/{sec_uid}"


def sanitize_filename(name: str, max_length: int = 50, fallback: str = "untitled") -> str:
    """清理 Windows/Linux 非法文件名字符并截断长度。"""
    cleaned = _INVALID_FS_CHARS.sub("_", name or "")
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" ._")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" ._")
    return cleaned or fallback


def format_video_filename(
    template: str,
    aweme_id: str,
    desc: str,
    create_time: int,
    max_title_length: int = 50,
    ext: str = ".mp4",
) -> str:
    date_str = (
        datetime.fromtimestamp(create_time).strftime("%Y-%m-%d")
        if create_time and create_time > 0
        else "unknown-date"
    )
    safe_title = sanitize_filename(desc, max_length=max_title_length, fallback="video")
    raw = template.format(date=date_str, title=safe_title, id=aweme_id)
    stem = sanitize_filename(raw, max_length=max_title_length + 40, fallback=aweme_id)
    return f"{stem}{ext}"


def normalize_url_host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""
