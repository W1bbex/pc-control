"""Regression and validation tests for fixes identified in CRITIQUE_REPORT.md."""

from pathlib import Path
from unittest.mock import MagicMock
import pytest


def test_media_manager_has_ctypes_imported():
    """Verify core/media_manager imports ctypes and defines fallback control without NameError."""
    import core.media_manager as mm
    assert hasattr(mm, "ctypes"), "ctypes must be imported in core/media_manager"
    assert hasattr(mm.ctypes, "windll"), "ctypes.windll must be accessible"


def test_icon_extractor_high_dpi_structs_and_getobject():
    """Verify icon_extractor defines BITMAP struct and GetObjectW signature."""
    import core.icon_extractor as ie
    assert hasattr(ie, "BITMAP"), "BITMAP structure must be declared in icon_extractor"
    assert hasattr(ie.gdi32, "GetObjectW"), "GetObjectW must be bound in gdi32"
    assert ie.gdi32.GetObjectW.argtypes is not None


def test_core_init_has_no_utf8_bom():
    """Verify core/__init__.py does not contain UTF-8 BOM."""
    init_path = Path(__file__).resolve().parent.parent / "core" / "__init__.py"
    raw_bytes = init_path.read_bytes()
    assert not raw_bytes.startswith(b"\xef\xbb\xbf"), "core/__init__.py must not have UTF-8 BOM"


def test_physical_button_resets_consumed_count_after_timeout():
    """Verify physical_button resets last_unlock_consumed_count after gesture timeout (6s)."""
    import time
    from vds_bot.physical_button import PhysicalButtonManager

    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    mgr = PhysicalButtonManager(mock_ui, mock_mqtt)
    unlock_calls = []
    mgr.on_trigger_unlock_cb = lambda cid: unlock_calls.append(cid)

    # 1. First successful Morse + unlock via CC
    mgr.handle_event({"event": "success"}, chat_id=123)
    mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
    mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 110}, chat_id=123)
    assert len(unlock_calls) == 1
    assert mgr.last_unlock_consumed_count == 2

    # 2. Simulate 10 seconds passing (buffer prune + timeout)
    mgr.physical_morse_unlock_eligible = True
    mgr.gesture_buffer = [(s, t - 10.0) for (s, t) in mgr.gesture_buffer]
    mgr.last_unlock_triggered_at -= 10.0

    # Next click after timeout: should reset consumed count and require only 2 clicks
    mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
    assert mgr.last_unlock_consumed_count == 0
    assert len(mgr.gesture_buffer) == 1

    # Second click completes CC and triggers second unlock
    mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
    assert len(unlock_calls) == 2, "Must trigger second unlock on 2 clicks after timeout!"


def test_telegram_ui_session_has_http_adapter_and_lock():
    """Verify TelegramBotApi has dedicated HTTPAdapter pool and thread-safe lock."""
    from vds_bot.telegram_ui import TelegramBotApi
    api = TelegramBotApi("123456:FAKE_TOKEN")
    assert hasattr(api, "_lock"), "TelegramBotApi must have a threading lock"
    assert "https://" in api.session.adapters
    adapter = api.session.adapters["https://"]
    assert getattr(adapter, "_pool_connections", 0) >= 10


def test_vds_bot_has_dedicated_polling_session():
    """Verify vds_bot uses dedicated polling_session and no base64 import."""
    bot_file = Path(__file__).resolve().parent.parent / "vds_bot" / "bot.py"
    source = bot_file.read_text(encoding="utf-8")
    assert "import base64" not in source, "Unused import base64 must be removed"
    assert "self.polling_session = requests.Session()" in source
    assert "res = self.polling_session.post(" in source


def test_vds_bot_command_timeout_cleans_pending_telemetry():
    """Verify command_timeout evicts pending_telemetry_cmd_ids to prevent memory leaks."""
    dispatcher_file = Path(__file__).resolve().parent.parent / "vds_bot" / "mqtt_dispatcher.py"
    bot_file = Path(__file__).resolve().parent.parent / "vds_bot" / "bot.py"
    source = (dispatcher_file.read_text(encoding="utf-8") if dispatcher_file.exists() else "") + bot_file.read_text(encoding="utf-8")
    assert 'getattr(self, "pending_telemetry_cmd_ids", {}).pop(cmd_id, None)' in source


def test_agent_top_processes_not_duplicated():
    """Verify action == 'top_processes' occurs exactly once in agent.py."""
    agent_file = Path(__file__).resolve().parent.parent / "agent.py"
    source = agent_file.read_text(encoding="utf-8")
    count = source.count('if action == "top_processes":')
    assert count == 1, f"Expected 1 occurrence of 'if action == \"top_processes\":', found {count}"


def test_type_password_mutex_handling():
    """Verify type_password.py safely closes handle and clears mutex on ERROR_ALREADY_EXISTS."""
    script_file = Path(__file__).resolve().parent.parent / "scripts" / "type_password.py"
    source = script_file.read_text(encoding="utf-8")
    assert "CreateMutexW(None, True," in source
    assert "kernel32.CloseHandle(mutex)" in source
    assert "mutex = None" in source


def test_auto_backup_excludes_env_files():
    """Verify auto_backup.py strictly excludes .env and *.env files."""
    import scripts.auto_backup as ab
    assert ".env" not in ab.CODE_EXTENSIONS
    assert ".env" in ab.EXCLUDE_FILES
    assert "*.env" in ab.EXCLUDE_FILES
    assert "pc-control-bot.env" not in ab.VDS_MANIFEST_FILES

    # Verify is_excluded_file rejects .env and my.env
    assert ab.is_excluded_file(Path(".env"))
    assert ab.is_excluded_file(Path("sub/.env"))
    assert ab.is_excluded_file(Path("config.env"))
    assert not ab.is_excluded_file(Path(".gitignore"))
    assert not ab.is_excluded_file(Path("pc-control-bot.env.example"))


def test_activity_db_auto_migration(tmp_path):
    """Verify ActivityDB automatically migrates legacy schema without emoji_id column."""
    import sqlite3
    import time
    from core.activity_db import ActivityDB

    test_db = tmp_path / "legacy_activity.db"
    # Create legacy table without emoji_id
    conn = sqlite3.connect(str(test_db))
    conn.execute("""
        CREATE TABLE app_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_name TEXT NOT NULL,
            window_title TEXT,
            exe_path TEXT,
            started_at REAL NOT NULL,
            ended_at REAL NOT NULL,
            duration_sec REAL NOT NULL,
            started_iso TEXT NOT NULL,
            ended_iso TEXT NOT NULL,
            date_str TEXT NOT NULL,
            time_str TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

    # Now open with ActivityDB - it should auto-migrate emoji_id
    now = time.time()
    db = ActivityDB(test_db)
    rec_id = db.record_session(
        app_name="Code.exe",
        window_title="test",
        exe_path="C:/Code.exe",
        started_at=now - 10.0,
        ended_at=now,
        emoji_id="12345"
    )
    assert rec_id is not None
    db.close()


def test_telegram_ui_safe_float_none():
    """Verify player cards handle None values for position and duration without TypeError."""
    from vds_bot.telegram_ui import TelegramBotApi
    ui = TelegramBotApi("123456:FAKE_TOKEN")

    null_state = {
        "active": True,
        "title": "Song",
        "artist": "Artist",
        "position": None,
        "duration": None,
        "is_playing": False
    }
    text = ui.player_card_text(null_state)
    caption = ui.player_card_caption(null_state)
    assert "Song" in text
    assert "Song" in caption


def test_telegram_ui_token_sanitization():
    """Verify TelegramBotApi redacts secret tokens from error messages and logs."""
    from vds_bot.telegram_ui import TelegramBotApi
    ui = TelegramBotApi("123456:SECRET_TEST_TOKEN")
    err_msg = "Failed at https://api.telegram.org/bot123456:SECRET_TEST_TOKEN/sendMessage?chat_id=123"
    sanitized = ui.sanitize_error(err_msg)
    assert "SECRET_TEST_TOKEN" not in sanitized
    assert "[REDACTED_BOT_TOKEN]" in sanitized


def test_bot_group_chat_authorization():
    """Verify unauthorized users in group chats cannot trigger player commands."""
    from unittest.mock import MagicMock
    from vds_bot.bot import PcControlBotApp

    bot = PcControlBotApp.__new__(PcControlBotApp)
    bot.ui = MagicMock()
    bot.mqtt = MagicMock()
    bot.latest_media_state = None
    bot._send_player_card = MagicMock()

    # Group message from unauthorized user
    bot.handle_message({
        "chat": {"id": -100999999, "type": "supergroup"},
        "from": {"id": 9999999},
        "text": "/player"
    })
    bot._send_player_card.assert_not_called()


def test_bot_scheduler_discards_stale_shutdown():
    """Verify scheduler discards shutdowns scheduled more than 10 minutes ago."""
    import time
    from unittest.mock import MagicMock
    from vds_bot.bot import PcControlBotApp

    bot = PcControlBotApp.__new__(PcControlBotApp)
    bot.ui = MagicMock()
    bot.mqtt = MagicMock()
    bot.monitoring = MagicMock()
    bot.monitoring.telemetry_age.return_value = 100.0
    bot.pending_screenshots = {}
    bot.pending_shutdown_cmd_id = None
    bot.scheduled_shutdown_ts = time.time() - 700.0  # 11 minutes ago
    bot.last_schedule_attempt_at = 0.0
    bot.last_dashboard_refresh_at = time.time()
    bot.dashboard_refresh_interval = 60.0
    bot.save_state = MagicMock()
    bot._request_shutdown = MagicMock()

    bot.scheduler_tick()

    assert bot.scheduled_shutdown_ts is None
    bot.save_state.assert_called_once()
    bot._request_shutdown.assert_not_called()


def test_boot_agent_network_rates_none_safe():
    """Verify boot_agent network_rates returns zeros safely if psutil returns None."""
    from unittest.mock import patch
    from boot_agent import SystemAgent

    agent = SystemAgent.__new__(SystemAgent)
    agent.previous_net = None
    agent.previous_net_time = 0.0

    with patch("boot_agent.psutil.net_io_counters", return_value=None):
        rates = agent.network_rates()
        assert rates["download_mbps"] == 0.0
        assert rates["upload_mbps"] == 0.0

