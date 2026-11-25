from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3
from typing import Any


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class CreatorRecord:
    id: int
    sec_uid: str
    nickname: str
    profile_url: str
    added_at: str
    last_sync_at: str | None
    last_video_time: int
    downloaded_count: int = 0
    failed_count: int = 0


class Storage:
    """基于 SQLite (WAL) 的博主订阅与作品增量去重存储层。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _init_schema(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS creators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sec_uid TEXT UNIQUE NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                profile_url TEXT NOT NULL,
                added_at TEXT NOT NULL,
                last_sync_at TEXT,
                last_video_time INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS videos (
                aweme_id TEXT PRIMARY KEY,
                sec_uid TEXT NOT NULL,
                desc TEXT NOT NULL DEFAULT '',
                create_time INTEGER NOT NULL DEFAULT 0,
                is_top INTEGER NOT NULL DEFAULT 0,
                media_type TEXT NOT NULL DEFAULT 'video',
                local_path TEXT NOT NULL DEFAULT '',
                file_size INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                error_msg TEXT NOT NULL DEFAULT '',
                discovered_at TEXT NOT NULL,
                downloaded_at TEXT,
                FOREIGN KEY (sec_uid) REFERENCES creators(sec_uid) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_videos_sec_uid_status
                ON videos(sec_uid, status);
            CREATE INDEX IF NOT EXISTS idx_videos_sec_uid_time
                ON videos(sec_uid, create_time DESC);
            """
        )
        self.conn.commit()

    def upsert_creator(
        self, sec_uid: str, profile_url: str, nickname: str = ""
    ) -> tuple[CreatorRecord, bool]:
        """添加或更新博主记录，返回 (记录, 是否为新添加)。"""
        existing = self.conn.execute(
            "SELECT * FROM creators WHERE sec_uid = ?", (sec_uid,)
        ).fetchone()
        if existing:
            if nickname and nickname != existing["nickname"]:
                self.conn.execute(
                    "UPDATE creators SET nickname = ?, profile_url = ? WHERE sec_uid = ?",
                    (nickname, profile_url, sec_uid),
                )
                self.conn.commit()
            return self.get_creator(sec_uid), False  # type: ignore[return-value]

        self.conn.execute(
            """
            INSERT INTO creators (sec_uid, nickname, profile_url, added_at)
            VALUES (?, ?, ?, ?)
            """,
            (sec_uid, nickname or "", profile_url, _now_iso()),
        )
        self.conn.commit()
        return self.get_creator(sec_uid), True  # type: ignore[return-value]

    def get_creator(self, identifier: str) -> CreatorRecord | None:
        """支持按数字序号 id、完整 sec_uid 或 sec_uid 前缀查找博主。"""
        ident = identifier.strip()
        row = None
        if ident.isdigit():
            row = self.conn.execute(
                "SELECT * FROM creators WHERE id = ?", (int(ident),)
            ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT * FROM creators WHERE sec_uid = ?", (ident,)
            ).fetchone()
        if row is None and len(ident) >= 6:
            row = self.conn.execute(
                "SELECT * FROM creators WHERE sec_uid LIKE ? LIMIT 1",
                (f"{ident}%",),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_creator(row)

    def list_creators(self) -> list[CreatorRecord]:
        rows = self.conn.execute(
            "SELECT * FROM creators ORDER BY id ASC"
        ).fetchall()
        return [self._row_to_creator(r) for r in rows]

    def _row_to_creator(self, row: sqlite3.Row) -> CreatorRecord:
        stats = self.conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'downloaded' THEN 1 ELSE 0 END) AS dl_cnt,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS fail_cnt
            FROM videos
            WHERE sec_uid = ?
            """,
            (row["sec_uid"],),
        ).fetchone()
        return CreatorRecord(
            id=row["id"],
            sec_uid=row["sec_uid"],
            nickname=row["nickname"],
            profile_url=row["profile_url"],
            added_at=row["added_at"],
            last_sync_at=row["last_sync_at"],
            last_video_time=row["last_video_time"],
            downloaded_count=int(stats["dl_cnt"] or 0),
            failed_count=int(stats["fail_cnt"] or 0),
        )

    def remove_creator(self, sec_uid: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM creators WHERE sec_uid = ?", (sec_uid,)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def is_video_downloaded(
        self, aweme_id: str, redownload_missing: bool = True
    ) -> bool:
        """判断作品是否已下载完成（可选校验本地文件是否仍存在）。"""
        row = self.conn.execute(
            "SELECT status, local_path FROM videos WHERE aweme_id = ?",
            (aweme_id,),
        ).fetchone()
        if not row or row["status"] != "downloaded":
            return False
        if redownload_missing:
            local_path = row["local_path"]
            if not local_path:
                return False
            p = Path(local_path)
            if not p.exists():
                return False
            if p.is_file() and p.stat().st_size <= 0:
                return False
        return True

    def record_discovered_video(
        self,
        aweme_id: str,
        sec_uid: str,
        desc: str,
        create_time: int,
        is_top: bool,
        media_type: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO videos (
                aweme_id, sec_uid, desc, create_time, is_top, media_type, status, discovered_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(aweme_id) DO UPDATE SET
                desc = excluded.desc,
                create_time = CASE WHEN excluded.create_time > 0 THEN excluded.create_time ELSE videos.create_time END,
                is_top = excluded.is_top,
                media_type = excluded.media_type
            """,
            (
                aweme_id,
                sec_uid,
                desc,
                create_time,
                1 if is_top else 0,
                media_type,
                _now_iso(),
            ),
        )
        self.conn.commit()

    def mark_downloaded(
        self, aweme_id: str, local_path: str, file_size: int
    ) -> None:
        self.conn.execute(
            """
            UPDATE videos
            SET status = 'downloaded',
                local_path = ?,
                file_size = ?,
                error_msg = '',
                downloaded_at = ?
            WHERE aweme_id = ?
            """,
            (local_path, file_size, _now_iso(), aweme_id),
        )
        self.conn.commit()

    def mark_failed(self, aweme_id: str, error_msg: str) -> None:
        self.conn.execute(
            """
            UPDATE videos
            SET status = 'failed',
                error_msg = ?
            WHERE aweme_id = ?
            """,
            (error_msg[:500], aweme_id),
        )
        self.conn.commit()

    def update_creator_sync(
        self, sec_uid: str, nickname: str = "", last_video_time: int = 0
    ) -> None:
        if nickname:
            self.conn.execute(
                """
                UPDATE creators
                SET nickname = ?,
                    last_sync_at = ?,
                    last_video_time = MAX(last_video_time, ?)
                WHERE sec_uid = ?
                """,
                (nickname, _now_iso(), last_video_time, sec_uid),
            )
        else:
            self.conn.execute(
                """
                UPDATE creators
                SET last_sync_at = ?,
                    last_video_time = MAX(last_video_time, ?)
                WHERE sec_uid = ?
                """,
                (_now_iso(), last_video_time, sec_uid),
            )
        self.conn.commit()

    def get_summary(self) -> dict[str, int]:
        creators_cnt = self.conn.execute(
            "SELECT COUNT(*) FROM creators"
        ).fetchone()[0]
        row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'downloaded' THEN 1 ELSE 0 END) AS downloaded,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM videos
            """
        ).fetchone()
        return {
            "creators": int(creators_cnt or 0),
            "total_videos": int(row["total"] or 0),
            "downloaded": int(row["downloaded"] or 0),
            "pending": int(row["pending"] or 0),
            "failed": int(row["failed"] or 0),
        }
