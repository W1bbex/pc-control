import unittest
import tempfile
from pathlib import Path
import time
from activity_db import ActivityDB

class TestActivityDB(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_activity.db"
        self.db = ActivityDB(self.db_path)

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def test_record_and_query_session(self):
        t0 = 1788590000.0
        t1 = 1788590300.0  # 300 seconds (5 min)
        
        row_id = self.db.record_session(
            app_name="Telegram.exe",
            window_title="Saved Messages",
            exe_path="C:\\Users\\Telegram\\Telegram.exe",
            started_at=t0,
            ended_at=t1,
            emoji_id="5551234"
        )
        self.assertIsNotNone(row_id)

        # Query exact point in time (t0 + 100)
        item = self.db.get_activity_at(t0 + 100)
        self.assertIsNotNone(item)
        self.assertEqual(item["app_name"], "Telegram.exe")
        self.assertEqual(item["window_title"], "Saved Messages")
        self.assertEqual(item["duration_sec"], 300.0)
        self.assertEqual(item["emoji_id"], "5551234")

        # Query time outside interval should return None
        outside = self.db.get_activity_at(t0 - 10)
        self.assertIsNone(outside)

    def test_day_summary_and_timeline(self):
        t0 = 1788590000.0
        # Session 1: Telegram 200s
        self.db.record_session("Telegram.exe", "Chat 1", "path", t0, t0 + 200, "eid1")
        # Session 2: Code 500s
        self.db.record_session("Code.exe", "agent.py", "path", t0 + 200, t0 + 700, "eid2")
        # Session 3: Telegram 100s
        self.db.record_session("Telegram.exe", "Chat 2", "path", t0 + 700, t0 + 800, "eid1")

        # Date string from timestamp
        from datetime import datetime
        date_str = datetime.fromtimestamp(t0).astimezone().strftime("%Y-%m-%d")

        summary = self.db.get_day_summary(date_str)
        self.assertEqual(len(summary), 2)
        # Top app should be Code.exe (500s)
        self.assertEqual(summary[0]["app_name"], "Code.exe")
        self.assertEqual(summary[0]["total_duration"], 500.0)
        # Second app should be Telegram.exe (200 + 100 = 300s, 2 sessions)
        self.assertEqual(summary[1]["app_name"], "Telegram.exe")
        self.assertEqual(summary[1]["total_duration"], 300.0)
        self.assertEqual(summary[1]["sessions_count"], 2)

        # Timeline
        timeline = self.db.get_day_timeline(date_str)
        self.assertEqual(len(timeline), 3)

    def test_midnight_crossing_session_splits_across_days(self):
        from datetime import datetime

        day1 = datetime(2026, 11, 15, 23, 30, 0).astimezone()
        day2_end = datetime(2026, 11, 16, 0, 30, 0).astimezone()
        span_start = datetime(2026, 11, 18, 23, 0, 0).astimezone()
        span_end = datetime(2026, 11, 20, 1, 0, 0).astimezone()

        # Single midnight crossing: 23:30 -> 00:30 (1800s + 1800s)
        self.db.record_session(
            "Code.exe", "Late Work", "C:\\Code.exe", day1.timestamp(), day2_end.timestamp()
        )
        # Multi-day crossing: 23:00 Nov 18 -> 01:00 Nov 20 (3600s, 86400s, 3600s)
        self.db.record_session(
            "Browser.exe", "Docs", "C:\\Browser.exe", span_start.timestamp(), span_end.timestamp()
        )

        sum1 = {r["app_name"]: r for r in self.db.get_day_summary("2026-11-15")}
        sum2 = {r["app_name"]: r for r in self.db.get_day_summary("2026-11-16")}
        sum_m1 = {r["app_name"]: r for r in self.db.get_day_summary("2026-11-18")}
        sum_m2 = {r["app_name"]: r for r in self.db.get_day_summary("2026-11-19")}
        sum_m3 = {r["app_name"]: r for r in self.db.get_day_summary("2026-11-20")}

        self.assertEqual(sum1["Code.exe"]["total_duration"], 1800.0)
        self.assertEqual(sum2["Code.exe"]["total_duration"], 1800.0)
        self.assertEqual(sum_m1["Browser.exe"]["total_duration"], 3600.0)
        self.assertEqual(sum_m2["Browser.exe"]["total_duration"], 86400.0)
        self.assertEqual(sum_m3["Browser.exe"]["total_duration"], 3600.0)

        tl2 = self.db.get_day_timeline("2026-11-16")
        by_app = {r["app_name"]: r for r in tl2}
        self.assertEqual(by_app["Code.exe"]["duration_sec"], 1800.0)
        self.assertEqual(by_app["Code.exe"]["time_str"], "00:00:00")

if __name__ == "__main__":
    unittest.main()
