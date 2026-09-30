import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from activity_logger import ActivityRecorder
from activity_report import load_events, parse_local


class TestActivityLogging(unittest.TestCase):
    def test_recorder_writes_structured_json_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity.jsonl"
            recorder = ActivityRecorder(path, retention_days=2)
            recorder.emit("foreground", process="example.exe", title="Example")
            recorder.close()

            event = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(event["event"], "foreground")
            self.assertEqual(event["process"], "example.exe")
            self.assertEqual(event["title"], "Example")
            self.assertIn("ts", event)
            self.assertIn("epoch", event)

    def test_report_filters_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity.jsonl"
            now = datetime.now().astimezone().replace(microsecond=0)
            rows = [
                {"ts": (now - timedelta(minutes=2)).isoformat(), "epoch": 1, "event": "old"},
                {"ts": now.isoformat(), "epoch": 2, "event": "wanted"},
                {"ts": (now + timedelta(minutes=2)).isoformat(), "epoch": 3, "event": "new"},
            ]
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            events = load_events(
                Path(directory),
                now - timedelta(seconds=1),
                now + timedelta(seconds=1),
            )
            self.assertEqual([event["event"] for event in events], ["wanted"])

    def test_parse_local_adds_timezone(self):
        self.assertIsNotNone(parse_local("2026-08-14T20:42").tzinfo)


if __name__ == "__main__":
    unittest.main()
