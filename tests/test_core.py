from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from dy_sync.browser_bridge import parse_aweme_item
from dy_sync.config import AntiRiskConfig
from dy_sync.downloader import AntiRiskController, CircuitBreakerTripped
from dy_sync.storage import Storage
from dy_sync.utils import (
    extract_sec_uid,
    extract_url_from_text,
    format_video_filename,
    is_short_url,
    sanitize_filename,
)


class TestCoreLogic(unittest.TestCase):
    def test_url_extraction(self) -> None:
        share = "长按复制此条消息，打开抖音搜索 https://v.douyin.com/iRNBho6u/ 查看TA的主页"
        url = extract_url_from_text(share)
        self.assertEqual(url, "https://v.douyin.com/iRNBho6u/")
        self.assertTrue(is_short_url(url))

        profile = "https://www.douyin.com/user/MS4wLjABAAAA_test_uid_123?from_tab_name=main"
        self.assertEqual(extract_sec_uid(profile), "MS4wLjABAAAA_test_uid_123")
        self.assertIsNone(extract_sec_uid("https://www.douyin.com/user/self"))

    def test_filename_formatting(self) -> None:
        name = format_video_filename(
            template="{date}_{title}_{id}",
            aweme_id="7400000000000000001",
            desc="测试视频:包含/非法*字符?",
            create_time=1735689600,
        )
        self.assertTrue(name.endswith("_7400000000000000001.mp4"))
        self.assertNotIn(":", name)
        self.assertNotIn("/", name)
        self.assertEqual(sanitize_filename("   "), "untitled")

    def test_parse_aweme_item_video_and_top(self) -> None:
        raw = {
            "aweme_id": "7412345678901234567",
            "desc": "测试高码率视频",
            "create_time": 1735689600,
            "is_top": 1,
            "author": {"sec_uid": "MS4wLjAB_abc", "nickname": "测试博主"},
            "video": {
                "bit_rate": [
                    {
                        "bit_rate": 500000,
                        "play_addr": {"url_list": ["https://cdn.example.com/low_playwm.mp4"]},
                    },
                    {
                        "bit_rate": 2500000,
                        "play_addr": {"url_list": ["https://cdn.example.com/high_playwm.mp4"]},
                    },
                ]
            },
        }
        item = parse_aweme_item(raw, "fallback")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertTrue(item.is_top)
        self.assertEqual(item.media_type, "video")
        # 应优先最高码率且替换 playwm -> play
        self.assertEqual(item.video_urls[0], "https://cdn.example.com/high_play.mp4")

    def test_storage_incremental_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "test.db"
            media_file = tmp_path / "v1.mp4"
            media_file.write_bytes(b"fake mp4 data")

            with Storage(db_path) as st:
                creator, created = st.upsert_creator(
                    sec_uid="MS4wLjAB_creator_1",
                    profile_url="https://www.douyin.com/user/MS4wLjAB_creator_1",
                    nickname="博主A",
                )
                self.assertTrue(created)
                self.assertEqual(creator.id, 1)

                # 再次添加同一博主应去重
                _, created_again = st.upsert_creator(
                    sec_uid="MS4wLjAB_creator_1",
                    profile_url="https://www.douyin.com/user/MS4wLjAB_creator_1",
                )
                self.assertFalse(created_again)

                st.record_discovered_video(
                    aweme_id="1001",
                    sec_uid="MS4wLjAB_creator_1",
                    desc="视频1",
                    create_time=1735689600,
                    is_top=False,
                    media_type="video",
                )
                self.assertFalse(st.is_video_downloaded("1001"))

                st.mark_downloaded("1001", str(media_file), media_file.stat().st_size)
                self.assertTrue(st.is_video_downloaded("1001", redownload_missing=True))

                # 删除本地文件后，开启 redownload_missing 应返回 False（触发补下）
                media_file.unlink()
                self.assertFalse(st.is_video_downloaded("1001", redownload_missing=True))
                self.assertTrue(st.is_video_downloaded("1001", redownload_missing=False))

                # 按序号和前缀查询
                self.assertIsNotNone(st.get_creator("1"))
                self.assertIsNotNone(st.get_creator("MS4wLjAB_cre"))

    def test_anti_risk_cooldown_and_circuit_breaker(self) -> None:
        notices: list[str] = []
        ar_cfg = AntiRiskConfig(
            download_delay_min=0.0,
            download_delay_max=0.0,
            batch_cooldown_every=2,
            batch_cooldown_min=0.01,
            batch_cooldown_max=0.02,
            circuit_breaker_failures=2,
            rate_limit_backoff_base=0.01,
        )
        ctrl = AntiRiskController(
            rate_per_sec=100.0, anti_risk=ar_cfg, on_notice=notices.append
        )

        async def _run() -> None:
            await ctrl.before_request()
            await ctrl.record_success()
            await ctrl.record_success()
            # 达到 batch_cooldown_every=2 应触发批次冷却通知
            self.assertTrue(any("批次冷却" in n for n in notices))

            # 连续失败 2 次应触发熔断
            await ctrl.record_failure("HTTP 429", is_risk_http=True)
            self.assertFalse(ctrl.tripped)
            await ctrl.record_failure("HTTP 403", is_risk_http=True)
            self.assertTrue(ctrl.tripped)

            with self.assertRaises(CircuitBreakerTripped):
                await ctrl.before_request()

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
