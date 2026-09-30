import json
import os
from pathlib import Path
import time
import unittest

from activity_db import ActivityDB
from app_emoji_manager import AppEmojiManager
from media_manager import MediaManager


class TestAuditFixes(unittest.TestCase):
    def test_config_json_has_no_plaintext_passwords(self):
        config_path = Path(__file__).resolve().parent.parent / "config.json"
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg.get("mqtt", {}).get("password"), "")
        self.assertEqual(cfg.get("unlock", {}).get("password"), "")

    def test_app_emoji_manager_negative_cache_timeout_is_60s(self):
        mgr = AppEmojiManager(token="fake", user_id=123)
        mgr.cache["test_app.exe"] = {"emoji_id": None, "failed_at": time.time() - 61.0}
        # After 61s, get_or_create_emoji_id should proceed past the negative cache check
        self.assertIsNone(mgr.get_or_create_emoji_id("non_existent.exe", "test_app.exe"))
        # Verify negative cache blocked within 30s
        mgr.cache["test_app2.exe"] = {"emoji_id": None, "failed_at": time.time() - 30.0}
        self.assertIsNone(mgr.get_or_create_emoji_id("non_existent.exe", "test_app2.exe"))

    def test_activity_db_persistent_connection(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test_persist.db"
            db = ActivityDB(db_path)
            c1 = db._get_connection()
            c2 = db._get_connection()
            self.assertIs(c1, c2, "ActivityDB should reuse persistent connection")
            db.close()
            self.assertIsNone(db._conn, "ActivityDB close should reset _conn")

    def test_media_manager_execute_command_is_nonblocking(self):
        mgr = MediaManager()
        # When loop is not running, returns False immediately
        res = mgr.execute_command("play")
        self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
