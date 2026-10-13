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
    """Verify action == 'top_processes' occurs exactly once across agent dispatchers."""
    dispatcher_file = Path(__file__).resolve().parent.parent / "desktop" / "action_dispatcher.py"
    agent_file = Path(__file__).resolve().parent.parent / "agent.py"
    source = (dispatcher_file.read_text(encoding="utf-8") if dispatcher_file.exists() else "") + agent_file.read_text(encoding="utf-8")
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


def test_boot_agent_wtsinfoex_struct_offsets():
    """Verify WTSINFOEX_LEVEL1_W has correct field alignment without redundant State field."""
    import boot_agent
    import ctypes

    struct_cls = boot_agent.WTSINFOEX_LEVEL1_W
    assert ctypes.sizeof(struct_cls) == 12, f"Expected 12 bytes, got {ctypes.sizeof(struct_cls)}"
    assert struct_cls.SessionFlags.offset == 8, f"Expected SessionFlags offset 8, got {struct_cls.SessionFlags.offset}"


def test_stealth_guard_vk_codes_mapping():
    """Verify VK codes for Shift and Ctrl are not inverted."""
    from core.stealth_guard import MODIFIER_VK_MAP

    assert MODIFIER_VK_MAP[0xA0] == "shift"  # VK_LSHIFT
    assert MODIFIER_VK_MAP[0xA1] == "shift"  # VK_RSHIFT
    assert MODIFIER_VK_MAP[0xA2] == "ctrl"   # VK_LCONTROL
    assert MODIFIER_VK_MAP[0xA3] == "ctrl"   # VK_RCONTROL


def test_media_manager_none_artist_title_safety():
    """Verify _resolve_track does not crash with TypeError when title or artist is None."""
    import threading
    from core.media_manager import MediaManager

    mm = MediaManager.__new__(MediaManager)
    mm._lock = threading.Lock()
    mm._sync_likes_if_needed = lambda: None
    mm.liked_track_ids = set()

    res1 = mm._resolve_track("Прямой эфир", None)
    assert res1[0] == "Прямой эфир"

    res2 = mm._resolve_track(None, "Исполнитель")
    assert res2[1] == "Исполнитель"


def test_ui_markups_antigravity_menu_callback():
    """Verify back button in Antigravity menu sends 'menu' callback instead of dead 'main_menu'."""
    from vds_bot.ui_markups import UiMarkupsMixin

    ui = UiMarkupsMixin()
    markup = ui.antigravity_card_markup({"accounts": []})
    keyboard = markup.get("inline_keyboard", [])
    all_callbacks = [btn.get("callback_data") for row in keyboard for btn in row]
    assert "menu" in all_callbacks, f"'menu' callback not found in {all_callbacks}"
    assert "main_menu" not in all_callbacks


def test_telegram_ui_edit_media_sanitizes_reply_markup():
    """Verify edit_message_media sanitizes invalid custom emoji fields in reply_markup."""
    import json
    from unittest.mock import MagicMock
    from vds_bot.telegram_ui import TelegramBotApi

    ui = TelegramBotApi.__new__(TelegramBotApi)
    ui.api_url = "https://api.telegram.org/bot123456:fake"
    ui.session = MagicMock()
    mock_post = MagicMock()
    mock_post.json.return_value = {"ok": True, "result": {}}
    ui.session.post.return_value = mock_post

    dirty_markup = {
        "inline_keyboard": [[
            {"text": "Test", "callback_data": "t", "icon_custom_emoji_id": None}
        ]]
    }
    ui.edit_message_media(12345, 67890, "https://example.com/pic.jpg", reply_markup=dirty_markup)
    called_data = ui.session.post.call_args[1]["data"]
    parsed_markup = json.loads(called_data["reply_markup"])
    btn = parsed_markup["inline_keyboard"][0][0]
    assert "icon_custom_emoji_id" not in btn


def test_command_router_antigravity_isolated_import():
    """Verify route_message does not crash with ModuleNotFoundError when processing /antigravity."""
    from unittest.mock import MagicMock
    from vds_bot.routers.command_router import route_message

    app = MagicMock()
    app.allowed_user_id = 12345
    app.ui = MagicMock()
    app.latest_media_state = None
    app.mqtt = MagicMock()
    msg = {"chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}, "text": "/antigravity"}
    route_message(app, msg)


def test_wtsinfoexw_memory_alignment_offset_8():
    """Verify WTSINFOEXW has Data field at offset 8 in 64-bit SDK while LEVEL1_W is 12 bytes."""
    import ctypes
    import boot_agent

    struct_l1 = boot_agent.WTSINFOEX_LEVEL1_W
    struct_ex = boot_agent.WTSINFOEXW
    assert ctypes.sizeof(struct_l1) == 12, f"Expected 12 bytes, got {ctypes.sizeof(struct_l1)}"
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert struct_ex.Data.offset == 8, f"Expected Data offset 8 on 64-bit Windows, got {struct_ex.Data.offset}"
        assert struct_ex.Data.offset + struct_l1.SessionFlags.offset == 16


def test_antigravity_card_renders_valid_tg_emoji_and_none_safe():
    """Verify antigravity_card_text wraps an emoji in tg-emoji and handles None values safely."""
    from vds_bot.ui_templates import UiTemplatesMixin

    ui = UiTemplatesMixin()
    # 1. Null data test
    null_data = {
        "accounts": [
            {"email": None, "name": None, "is_active": True},
            {"email": None, "name": None, "is_active": False},
        ],
        "active": {"email": None, "name": None, "reset_time": None},
        "is_running": False,
    }
    card = ui.antigravity_card_text(null_data)
    assert "Antigravity • Управление аккаунтами" in card
    assert "Без имени" in card or "—" in card

    # 2. Check tg-emoji tag has inner character (not empty <tg-emoji ...></tg-emoji>)
    if "<tg-emoji" in card:
        assert "></tg-emoji>" not in card, "Custom emoji must wrap a fallback character!"


def test_bot_callback_none_message_safe():
    """Verify handle_callback does not crash with AttributeError when callback['message'] is None."""
    from unittest.mock import MagicMock
    from vds_bot.bot import PcControlBotApp

    bot = PcControlBotApp.__new__(PcControlBotApp)
    bot.allowed_user_id = 12345
    bot.ui = MagicMock()
    bot.current_menu_view = "menu"
    bot.monitoring = MagicMock()
    bot.save_state = MagicMock()
    bot._main_menu_markup = MagicMock(return_value={})

    # Message is None (inline callback from inline query message)
    callback_payload = {
        "id": "cb_999",
        "data": "menu",
        "message": None,
        "from": {"id": 12345},
    }
    bot.handle_callback(callback_payload)
    bot.ui.answer_callback.assert_called_with("cb_999")


def test_player_card_caption_delegates_to_player_card_text():
    """Verify player_card_caption returns the exact same string as player_card_text."""
    from vds_bot.ui_templates import UiTemplatesMixin

    ui = UiTemplatesMixin()
    state = {"active": True, "title": "Song", "artist": "Artist", "position": 10, "duration": 100}
    assert ui.player_card_caption(state) == ui.player_card_text(state)


def test_power_flow_double_click_relay_protection():
    """Verify power_flow ignores duplicate start_power_on calls while in POWERING_ON stage."""
    from vds_bot.power_flow import PowerFlowManager, PowerFlowStage
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    mock_mqtt.system_status = "offline"
    mock_mqtt.desktop_status = "offline"
    mock_mqtt.esp_status = "online"
    mock_mqtt.publish_esp_action = MagicMock(return_value="cmd_1")
    pc_state = MagicMock()
    pc_state.state = "OFFLINE"
    mock_mqtt.health.evaluate_pc_state = MagicMock(return_value=pc_state)

    flow = PowerFlowManager(mock_ui, mock_mqtt)
    flow.reset_flow()
    flow.start_power_on(123)
    assert flow.stage == PowerFlowStage.POWERING_ON
    # 1 PRESS and 1 LED_ON
    assert mock_mqtt.publish_esp_action.call_count == 2

    # Second call while stage is POWERING_ON
    flow.start_power_on(123)
    # publish_esp_action count must not increase (no second PRESS command!)
    assert mock_mqtt.publish_esp_action.call_count == 2
    flow.reset_flow()


def test_power_flow_game_cancel_workspace_id():
    """Verify auto_launch_game initializes active_workspace_id so cancel_workspace sends cancel command."""
    from vds_bot.power_flow import PowerFlowManager, PowerFlowStage
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    flow = PowerFlowManager(mock_ui, mock_mqtt)
    flow.reset_flow()
    flow.auto_launch_game = True
    flow.target_game = "gta5"
    flow.message_id = 999
    flow.on_unlock_success(123)

    assert flow.stage == PowerFlowStage.WORKSPACE_RUNNING
    assert flow.active_workspace_id is not None
    assert flow.active_workspace_id.startswith("game-")

    flow.cancel_workspace(123)
    mock_mqtt.publish_desktop_action.assert_called_with("cancel_workspace", {"workspace_id": flow.active_workspace_id})
    flow.reset_flow()


def test_command_router_time_input_cancellation():
    """Verify command_router aborts awaiting_shutdown_time on cancellation keywords."""
    from vds_bot.routers.command_router import route_message
    app = MagicMock()
    app.allowed_user_id = 123
    app.awaiting_shutdown_time_chat_id = 123
    app._main_menu_markup = MagicMock(return_value={})

    msg = {"chat": {"id": 123}, "text": "отмена", "from": {"id": 123}}
    route_message(app, msg)
    assert app.awaiting_shutdown_time_chat_id is None
    app.ui.send_message.assert_called_with(123, "Ввод времени отменён.", reply_markup={})


def test_history_records_ts_and_timestamp(tmp_path):
    """Verify TelemetryHistory correctly extracts and records ts from payload."""
    import time
    from vds_bot.history import TelemetryHistory
    db_file = tmp_path / "telem.db"
    hist = TelemetryHistory(db_file)

    test_ts = int(time.time()) - 30
    hist.insert({"ts": test_ts, "host": "test-pc", "cpu": {"load_percent": 15.0}})

    conn = hist._connect()
    try:
        cur = conn.execute("SELECT ts, host, cpu_load FROM telemetry").fetchall()
        assert len(cur) == 1
        assert cur[0][0] == test_ts
        assert cur[0][1] == "test-pc"
        assert cur[0][2] == 15.0
    finally:
        conn.close()


def test_power_scheduler_evicts_stale_cmd_ids():
    """Verify scheduler_tick evicts stale pending_telemetry and antigravity cmd_ids and None keys."""
    import time
    from vds_bot.power_scheduler import PowerSchedulerMixin

    class DummyBot(PowerSchedulerMixin):
        def __init__(self):
            self.running = True
            self.monitoring = MagicMock()
            self.monitoring.telemetry_age = MagicMock(return_value=1.0)
            self.mqtt = MagicMock()
            self.mqtt.system_status = "online"
            self.pending_screenshots = {}
            self.pending_shutdown_cmd_id = None
            self.scheduled_shutdown_ts = None
            self.pending_telemetry_cmd_ids = {
                None: (123, 456),
                "stale_telem": (123, 456),
                "fresh_telem": (123, 456),
            }
            self.pending_antigravity_cmd_ids = {
                None: (123, 456, "list"),
                "stale_ag": (123, 456, "list"),
                "fresh_ag": (123, 456, "list"),
            }
            self.pending_cmd_timestamps = {
                "stale_telem": time.time() - 120.0,
                "fresh_telem": time.time() - 5.0,
                "stale_ag": time.time() - 120.0,
                "fresh_ag": time.time() - 5.0,
            }

    bot = DummyBot()
    bot.scheduler_tick()

    assert None not in bot.pending_telemetry_cmd_ids
    assert "stale_telem" not in bot.pending_telemetry_cmd_ids
    assert "fresh_telem" in bot.pending_telemetry_cmd_ids

    assert None not in bot.pending_antigravity_cmd_ids
    assert "stale_ag" not in bot.pending_antigravity_cmd_ids
    assert "fresh_ag" in bot.pending_antigravity_cmd_ids

    assert None not in bot.pending_antigravity_cmd_ids
    assert "stale_ag" not in bot.pending_antigravity_cmd_ids
    assert "fresh_ag" in bot.pending_antigravity_cmd_ids


def test_workspace_router_escapes_html():
    """Verify workspace_router escapes dangerous HTML characters in label and reason."""
    from vds_bot.routers.workspace_router import route_workspace_callback

    app = MagicMock()
    app.monitoring.telemetry_age = MagicMock(return_value=5.0)
    pc_state = MagicMock()
    pc_state.session_status = "locked"
    pc_state.state = "OFFLINE"
    pc_state.label = "Bad <Tag> & Label"
    pc_state.reason = "Crash <Error>"
    app.mqtt.health.evaluate_pc_state = MagicMock(return_value=pc_state)
    app.ui.back_button = MagicMock(return_value=[])

    handled = route_workspace_callback(app, "game_start_direct", chat_id=123, message_id=456, callback_id="cb_1")
    assert handled is True
    call_args = app.ui.send_message.call_args[0]
    sent_text = call_args[1]
    assert "&lt;Tag&gt;" in sent_text
    assert "&amp;" in sent_text
    assert "<Tag>" not in sent_text
    assert "<Error>" not in sent_text



