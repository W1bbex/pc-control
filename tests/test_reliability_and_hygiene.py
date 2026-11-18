"""
Regression tests verifying reliability, edge-case resilience and state transitions.
"""

import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import boot_agent
from core.activity_db import ActivityDB
from core.antigravity_crypto import AntigravityCrypto
from core.antigravity_switcher import AntigravityAccountManager
from core.app_emoji_manager import AppEmojiManager
from core.media_manager import MediaManager
from desktop.stealth import StealthMixin
from scripts import deploy_vds
from system import session_manager
from vds_bot.power_flow import PowerFlowManager
from vds_bot.routers.power_router import route_power_callback
from vds_bot.telegram_ui import TelegramBotApi


def test_antigravity_crypto_master_key_fallback_to_portable(tmp_path, monkeypatch):
    """Verify AntigravityCrypto falls back to portable master_key.hex when Local State is absent."""
    fake_home = tmp_path / "home"
    portable_dir = fake_home / ".antigravity-agent"
    portable_dir.mkdir(parents=True)
    portable_key = portable_dir / "master_key.hex"
    expected_hex = "0102030405060708090a0b0c0d0e0f10" * 2
    portable_key.write_text(expected_hex, encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata_empty"))

    crypto = AntigravityCrypto()
    key_bytes = crypto.get_master_key()
    assert key_bytes == bytes.fromhex(expected_hex)


def test_antigravity_manager_recognizes_ide_process_variant():
    """Verify is_ide_running and kill_ide handle 'Antigravity IDE.exe' process name."""
    mock_proc = MagicMock()
    mock_proc.info = {"pid": 5555, "name": "Antigravity IDE.exe"}

    with patch("psutil.process_iter", return_value=[mock_proc]):
        assert AntigravityAccountManager.is_ide_running() is True


def test_antigravity_manager_switch_account_raises_when_vscdb_missing(tmp_path):
    """Verify switch_account raises FileNotFoundError when state.vscdb does not exist."""
    db_path = tmp_path / "cloud_accounts.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, email TEXT, token_json TEXT, is_active INTEGER, last_used INTEGER)"
    )
    conn.execute(
        "INSERT INTO accounts VALUES ('acc1', 'a@b.com', '{\"access_token\": \"abc\"}', 0, 0)"
    )
    conn.commit()
    conn.close()

    mgr = AntigravityAccountManager()
    mgr.get_cloud_db_path = MagicMock(return_value=db_path)
    mgr.refresh_token_if_needed = MagicMock(return_value={"access_token": "valid_token"})
    mgr.get_ide_state_paths = MagicMock(return_value=(tmp_path / "missing.vscdb", tmp_path / "storage.json"))
    mgr.kill_ide = MagicMock()
    mgr.is_ide_running = MagicMock(return_value=False)

    with pytest.raises(FileNotFoundError, match="state.vscdb"):
        mgr.switch_account("acc1", restart_ide=False)
    mgr.kill_ide.assert_not_called()


def test_app_emoji_manager_returns_none_when_token_or_user_id_missing():
    """Verify get_or_create_emoji_id returns None immediately without network calls when credentials omitted."""
    mgr = AppEmojiManager(token="", user_id=0)
    mgr.cache = {}
    with patch("requests.post") as mock_post:
        result = mgr.get_or_create_emoji_id("C:\\Tools\\app_unique.exe", "app_unique.exe")
        assert result is None
        mock_post.assert_not_called()


def test_app_emoji_manager_locks_sticker_set_operations(tmp_path, monkeypatch):
    """Verify sticker upload and getStickerSet execute under _sticker_set_lock."""
    monkeypatch.setattr("core.app_emoji_manager.CACHE_FILE", tmp_path / "app_icon_cache.json")
    mgr = AppEmojiManager(token="fake_token", user_id=12345, pack_name="pc_apps_pack_by_test_bot")
    mgr.cache = {}
    lock_used = []

    with patch("os.path.exists", return_value=True), patch(
        "core.app_emoji_manager.extract_app_icon_png", return_value=b"fake_png_data"
    ), patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        def verify_lock(*args, **kwargs):
            lock_used.append(mgr._get_sticker_set_lock().locked())
            return MagicMock(json=lambda: {"ok": True})

        mock_post.side_effect = verify_lock
        mock_get.return_value = MagicMock(json=lambda: {"ok": True, "result": {"stickers": [{"custom_emoji_id": "999"}]}})

        emoji_id = mgr.get_or_create_emoji_id("C:\\Tools\\unique_test_proc.exe", "unique_test_proc.exe")
        assert emoji_id == "999"
        assert lock_used == [True]
        mock_get.assert_called_once_with(
            "https://api.telegram.org/botfake_token/getStickerSet",
            params={"name": "pc_apps_pack_by_test_bot"}, timeout=5,
        )


def test_media_manager_sync_likes_skipped_when_credentials_missing():
    """Verify _sync_likes_if_needed returns early when yandex_token or yandex_uid are missing."""
    mm = MediaManager.__new__(MediaManager)
    mm.yandex_token = ""
    mm.yandex_uid = 0
    mm._get_lock = lambda: threading.Lock()
    mm._request_with_retry = MagicMock()

    mm._sync_likes_if_needed(force=True)
    mm._request_with_retry.assert_not_called()


def test_ui_sound_menu_markup_has_two_by_two_grid():
    """Verify sound menu organizes volume options into 2 rows of 2 buttons for mobile ergonomics."""
    ui = TelegramBotApi("dummy_token")
    markup = ui.sound_menu_markup()
    keyboard = markup["inline_keyboard"]

    # Row 0: Toggle
    # Row 1: 25%, 50%
    # Row 2: 75%, 100%
    # Row 3: Back
    assert len(keyboard) == 4
    assert len(keyboard[1]) == 2
    assert keyboard[1][0]["text"] == "25%"
    assert keyboard[1][1]["text"] == "50%"
    assert len(keyboard[2]) == 2
    assert keyboard[2][0]["text"] == "75%"
    assert keyboard[2][1]["text"] == "100%"


def test_ui_antigravity_card_markup_caps_accounts():
    """Verify antigravity_card_markup caps accounts grid at 25 buttons to prevent Telegram 400 Bad Request."""
    ui = TelegramBotApi("dummy_token")
    large_account_list = [{"id": f"acc_{i}", "email": f"acc{i}@test.com", "min_quota": 80} for i in range(50)]
    markup = ui.antigravity_card_markup({"accounts": large_account_list})
    rows = markup["inline_keyboard"]

    # Filter switch rows for accounts (containing ag_switch:acc_)
    switch_rows = [r for r in rows if any("ag_switch:acc_" in str(btn.get("callback_data", "")) for btn in r)]
    assert len(switch_rows) == 25
    assert any("Показаны 25 из 50" in str(btn.get("text", "")) for r in rows for btn in r)


def test_power_router_passes_message_id_to_start_power_on():
    """Verify route_power_callback passes message_id to power_flow.start_power_on for in-place edit."""
    app = SimpleNamespace()
    app.monitoring = SimpleNamespace(telemetry_age=MagicMock(return_value=None))
    app.mqtt = SimpleNamespace(
        esp_status="online",
        system_status="offline",
        desktop_status="offline",
        health=MagicMock(),
    )
    app.mqtt.health.evaluate_pc_state.return_value = MagicMock(state="SHUTDOWN_OBSERVED")
    app.physical_btn = SimpleNamespace(revoke_morse_eligibility=MagicMock())
    app.save_state = MagicMock()
    app.ui = SimpleNamespace(answer_callback=MagicMock())
    app.power_flow = SimpleNamespace(start_power_on=MagicMock())

    handled = route_power_callback(app, data="power_on", chat_id=123, message_id=777, callback_id="cb1")
    assert handled is True
    app.power_flow.start_power_on.assert_called_once_with(123, message_id=777)


def test_deploy_vds_pull_remote_handles_empty_files(tmp_path, monkeypatch):
    """Verify deploy_vds.pull_remote does not fail when remote file is empty (0 bytes)."""
    monkeypatch.setattr(deploy_vds, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(deploy_vds, "MANIFEST_FILES", ["empty.py"])

    fake_res = MagicMock(returncode=0, stdout="")
    with patch.object(deploy_vds, "run_ssh", return_value=fake_res):
        deploy_vds.pull_remote()
        assert (tmp_path / "empty.py").exists()
        assert (tmp_path / "empty.py").read_text(encoding="utf-8") == ""


def test_stealth_send_monitor_command_uses_lparam():
    """Verify StealthMixin._send_monitor_command sends LPARAM correctly typed."""
    with patch("ctypes.windll.user32.SendMessageTimeoutW", return_value=1) as mock_send:
        res = StealthMixin._send_monitor_command(-1, 1000)
        assert res is True
        mock_send.assert_called_once()
        # Verify lparam argument value
        args = mock_send.call_args[0]
        assert args[3].value == -1


def test_session_manager_logonui_failure_continues_search():
    """Verify active_console_session does not set logonui_found when ProcessIdToSessionId fails."""
    import ctypes
    from ctypes import wintypes

    mock_kernel32 = MagicMock()
    mock_kernel32.WTSGetActiveConsoleSessionId.return_value = 1
    mock_kernel32.ProcessIdToSessionId.return_value = 0
    mock_kernel32.GetLastError.return_value = 5  # AccessDenied

    user_buf = ctypes.create_unicode_buffer("TestUser\x00")

    def fake_wts_query(h_server, sess_id, info_class, p_buf, p_bytes):
        if info_class == 5:  # WTSUserName
            ctypes.cast(p_buf, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(user_buf)
            return 1
        return 0

    mock_wtsapi32 = MagicMock()
    mock_wtsapi32.WTSQuerySessionInformationW.side_effect = fake_wts_query

    mock_user32 = MagicMock()
    mock_user32.OpenInputDesktop.return_value = 0

    mock_proc = MagicMock()
    mock_proc.info = {"name": "logonui.exe", "pid": 4321}

    with patch("ctypes.windll.kernel32", mock_kernel32), patch("ctypes.windll.wtsapi32", mock_wtsapi32), patch(
        "ctypes.windll.user32", mock_user32
    ), patch("psutil.process_iter", return_value=[mock_proc]):
        res = session_manager.active_console_session(ttl=0)
        assert res["session"] == "unlocked"
        assert res["source"] == "logonui_not_running"


def test_activity_db_inserts_unknown_fallback_when_app_name_empty(tmp_path):
    """Verify ActivityDB uses 'unknown' fallback to avoid NOT NULL constraint violations."""
    from datetime import datetime
    db_path = tmp_path / "test_activity.db"
    db = ActivityDB(db_path=db_path)
    now = 1700000000.0
    db.record_session("", window_title="Some Window", exe_path="", started_at=now, ended_at=now + 10.0)

    date_str = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    summary = db.get_day_summary(date_str)
    assert any(s["app_name"] == "unknown" for s in summary)


def test_boot_agent_acknowledge_catches_publish_error():
    """Verify boot_agent.acknowledge safely catches MQTT publish errors without raising."""
    agent = boot_agent.SystemAgent.__new__(boot_agent.SystemAgent)
    agent.mqtt = MagicMock()
    agent.mqtt.publish.side_effect = RuntimeError("Broker disconnected")

    # Must not raise
    agent.acknowledge("cmd-99", "failed", {"error": "test"})
    agent.mqtt.publish.assert_called_once()


def test_extracted_antigravity_crypto_and_protobuf_helpers():
    from core.antigravity_crypto import ProtobufHelper
    encoded = ProtobufHelper.encode_varint(300)
    val, pos = ProtobufHelper.read_varint(encoded, 0)
    assert val == 300
    assert pos == len(encoded)

    oauth_bytes = ProtobufHelper.create_oauth_info("token123", "refresh123", 1800000000)
    assert b"token123" in oauth_bytes
    assert b"Bearer" in oauth_bytes


def test_extracted_antigravity_process_helpers():
    from core.antigravity_process import is_ide_running, kill_ide
    mock_proc = MagicMock()
    mock_proc.info = {"pid": 9999, "name": "antigravity.exe"}

    with patch("core.antigravity_process.psutil.process_iter", return_value=[mock_proc]):
        assert is_ide_running() is True

    with patch("core.antigravity_process.psutil.process_iter", return_value=[mock_proc]), \
         patch("core.antigravity_process.psutil.wait_procs", return_value=([mock_proc], [])):
        kill_ide(timeout_sec=0.1)
        assert mock_proc.terminate.called


def test_extracted_power_state_atomic_persistence(tmp_path):
    from vds_bot.power_state import clear_persisted_flow_state, load_persisted_flow_state, save_persisted_flow_state
    state_file = tmp_path / "flow.json"
    data = {"stage": "POWERING_ON", "created_at": 100.0, "expires_at": 400.0}

    assert save_persisted_flow_state(data, path=state_file) is True
    loaded = load_persisted_flow_state(path=state_file)
    assert loaded == data

    clear_persisted_flow_state(path=state_file)
    assert not state_file.exists()


def test_extracted_power_ui_presentation():
    from vds_bot.power_ui import (
        build_flow_btn,
        get_unlock_failed_markup,
        get_unlock_success_markup,
        get_unlocking_markup,
        resolve_target_mode_info,
    )
    btn = build_flow_btn("Test", "primary", "cb_test", icon="123")
    assert btn["text"] == "Test"
    assert btn["style"] == "primary"
    assert btn["icon_custom_emoji_id"] == "123"

    mode, detail, label = resolve_target_mode_info(
        auto_launch_workspace=True,
        auto_launch_game=False,
        target_workspace_vpn="happ",
        target_game=None,
        target_game_vpn=None,
        stage="UNLOCKING",
    )
    assert mode == "workspace"
    assert "Happ" in label

    markup = get_unlocking_markup(auto_workspace=True)
    assert len(markup["inline_keyboard"]) == 2

    text, s_markup = get_unlock_success_markup(manual=True)
    assert "вручную" in text
    assert len(s_markup["inline_keyboard"]) == 2

    f_text, f_markup = get_unlock_failed_markup("Password rejected")
    assert "Password rejected" in f_text
    assert len(f_markup["inline_keyboard"]) == 2


def test_extracted_desktop_os_events_helpers():
    from desktop.os_events import is_telegram_control_chat
    assert is_telegram_control_chat("telegram.exe", "pc control bot") is True
    assert is_telegram_control_chat("Telegram.exe", "pc control bot") is True
    assert is_telegram_control_chat(r"C:\Program Files\Telegram Desktop\Telegram.exe", "pc control") is True
    assert is_telegram_control_chat("telegram.exe", "Family Chat") is False
    assert is_telegram_control_chat("notepad.exe", "pc control") is False
    assert is_telegram_control_chat(None, "pc control") is False
    assert is_telegram_control_chat("telegram.exe", None) is False


def test_power_flow_unlock_fails_immediately_when_two_attempts_exhausted():
    """Verify on_system_session_update transitions to UNLOCK_FAILED when 2 unlock attempts are exhausted."""
    from vds_bot.power_flow import PowerFlowManager, PowerFlowStage

    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    flow = PowerFlowManager(mock_ui, mock_mqtt)
    flow.stage = PowerFlowStage.UNLOCKING
    flow.unlock_attempts = 2
    flow.pending_unlock_command_id = None
    flow.chat_id = 12345
    flow.message_id = 999

    flow.on_system_session_update("login_screen", 12345)
    assert flow.stage == PowerFlowStage.UNLOCK_FAILED
    mock_ui.send_message.assert_called_once()
    args, kwargs = mock_ui.send_message.call_args
    assert "Не удалось разблокировать Windows за 2 попытки" in args[1]


def test_power_flow_load_state_resets_workspace_running(tmp_path, monkeypatch):
    """Verify load_state resets flow to IDLE if bot restarted while a workspace was running."""
    import vds_bot.power_flow as pf
    from vds_bot.power_flow import PowerFlowManager, PowerFlowStage
    from vds_bot.power_state import save_persisted_flow_state

    state_file = tmp_path / "flow_state.json"
    monkeypatch.setattr(pf, "FLOW_STATE_PATH", state_file)
    monkeypatch.setattr("vds_bot.power_state.FLOW_STATE_PATH", state_file)

    save_persisted_flow_state({
        "flow_id": "test-flow",
        "chat_id": 123,
        "stage": PowerFlowStage.WORKSPACE_RUNNING,
        "expires_at": time.time() + 300,
    }, path=state_file)

    flow = PowerFlowManager(MagicMock(), MagicMock())
    assert flow.stage == PowerFlowStage.IDLE


def test_power_flow_check_timeout_expires_workspace_running():
    """Verify check_timeout cleans up expired WORKSPACE_RUNNING flows."""
    from vds_bot.power_flow import PowerFlowManager, PowerFlowStage

    mock_ui = MagicMock()
    flow = PowerFlowManager(mock_ui, MagicMock())
    flow.stage = PowerFlowStage.WORKSPACE_RUNNING
    flow.flow_id = "ws-expired"
    flow.chat_id = 123
    flow.message_id = 456
    flow.created_at = time.time() - 400
    flow.expires_at = time.time() - 50

    expired = flow.check_timeout(now=time.time())
    assert expired is True
    assert flow.stage == PowerFlowStage.IDLE
    mock_ui.send_message.assert_called_once()


def test_is_pc_online_for_auto_led_preserves_cooling_at_login_screen():
    """Verify cooling Auto-LED stays powered on at Windows login screen despite stale desktop telemetry."""
    from vds_bot.power_scheduler import is_pc_online_for_auto_led

    app = MagicMock()
    app.monitoring.telemetry_age.return_value = 120.0  # desktop agent not yet launched

    # When boot_agent is online in Session 0, cooler must stay ON
    pc_online, telem_age = is_pc_online_for_auto_led(app, "online", "offline", "login_screen")
    assert pc_online is True
    assert telem_age == 120.0

    # When PC is offline and session is unknown, stale telemetry turns cooler OFF
    pc_online, _ = is_pc_online_for_auto_led(app, "offline", "offline", "unknown")
    assert pc_online is False


def test_health_engine_preserves_graceful_shutdown_when_state_is_shutdown():
    """Verify evaluate_pc_state treats shutdown/graceful_shutdown as offline without clearing graceful flag."""
    from vds_bot.health_engine import HealthEngine, PCState

    engine = HealthEngine()
    engine.components["telegram"].update("online")
    engine.components["mqtt_broker"].update("online")
    engine.components["esp"].update("online")

    now = time.time()
    engine.set_graceful_shutdown(ts=now, ttl=3600.0, reason="windows_shutdown")
    engine.components["windows_system"].update("shutdown", ts=now)
    engine.components["windows_session"].update("unknown", ts=now)

    pc_state = engine.evaluate_pc_state()
    assert pc_state.state == PCState.SHUTDOWN_OBSERVED
    assert "штатно" in pc_state.label
    assert engine.graceful_shutdown_until > 0.0


def test_health_engine_stale_detection_includes_login_screen():
    """Verify check_stale marks login_screen session as stale when threshold is exceeded."""
    from vds_bot.health_engine import HealthEngine

    engine = HealthEngine()
    engine.components["windows_session"].status = "login_screen"
    engine.components["windows_session"].local_updated_at = time.time() - 100.0
    engine.components["windows_session"].stale_threshold = 45.0

    stale = engine.components["windows_session"].check_stale()
    assert stale is True
    assert engine.components["windows_session"].status == "stale"


def test_vds_bot_mqtt_gateway_relative_import():
    """Verify vds_bot.mqtt_gateway is importable as a module from project root."""
    import importlib
    mod = importlib.import_module("vds_bot.mqtt_gateway")
    assert hasattr(mod, "HealthEngine")
    assert hasattr(mod, "TOPIC_SYSTEM_COMMAND")


def test_save_persisted_flow_state_handles_mkdir_permission_error_safely(tmp_path):
    """Verify save_persisted_flow_state returns False without UnboundLocalError when mkdir fails."""
    from vds_bot.power_state import save_persisted_flow_state

    failing_path = tmp_path / "sub" / "flow_state.json"
    with patch.object(Path, "mkdir", side_effect=PermissionError("Permission denied")):
        ok = save_persisted_flow_state({"stage": "IDLE"}, path=failing_path)
        assert ok is False


def test_history_insert_sanitizes_string_float_ts_and_string_values(tmp_path):
    """Verify history insert safely handles string float ts and non-dict payload structures without crashing."""
    from vds_bot.history import TelemetryHistory

    db_path = tmp_path / "telemetry_test.db"
    hm = TelemetryHistory(str(db_path))

    now_ts = int(time.time())
    # String float timestamp, non-dict cpu, disks string
    bad_payload = {
        "ts": f"{now_ts}.5",
        "host": "test-pc",
        "cpu": "N/A",
        "gpu": None,
        "memory": "bad_memory",
        "disks": "N/A",
        "network": {},
        "idle_seconds": 10,
        "user_active": True,
    }

    # Should not raise ValueError or AttributeError
    hm.insert(bad_payload)

    with hm.lock, hm._connect() as conn:
        row = conn.execute("SELECT ts, host, cpu_load FROM telemetry WHERE ts = ?", (now_ts,)).fetchone()
        assert row is not None
        assert row[0] == now_ts
        assert row[1] == "test-pc"
        assert row[2] is None


def test_history_render_chart_handles_future_clock_skew(tmp_path):
    """Verify render_chart expands bucket range properly even if DB timestamps are in future relative to VDS."""
    import io
    from vds_bot.history import TelemetryHistory

    db_path = tmp_path / "skew_test.db"
    hm = TelemetryHistory(str(db_path))

    # Insert two timestamps slightly in the future relative to VDS time.time()
    future_now = int(time.time()) + 180
    hm.insert({"ts": future_now - 60, "cpu": {"load_percent": 15}})
    hm.insert({"ts": future_now, "cpu": {"load_percent": 25}})

    mock_fig = MagicMock()
    mock_axes = [MagicMock() for _ in range(4)]
    mock_plt = MagicMock()
    mock_plt.subplots.return_value = (mock_fig, mock_axes)

    def fake_savefig(buf, *args, **kwargs):
        buf.write(b"FAKE_PNG_BYTES")

    mock_fig.savefig.side_effect = fake_savefig

    with patch("vds_bot.history.HAS_MATPLOTLIB", True), \
         patch("vds_bot.history.plt", mock_plt, create=True), \
         patch("vds_bot.history.mdates", MagicMock(), create=True):
        chart_png, points_count = hm.render_chart(3600, "Тест")
        assert chart_png == b"FAKE_PNG_BYTES"
        assert points_count >= 2
        # Verify times array passed to plot was not empty
        assert mock_axes[0].plot.called
        plot_times = mock_axes[0].plot.call_args[0][0]
        assert len(plot_times) >= 2


def test_power_scheduler_evicts_stale_console_commands():
    """Verify scheduler_tick evicts pending_console_cmds older than 60s and notifies the user."""
    from vds_bot.bot import PcControlBotApp

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.mqtt = MagicMock()
    app.state_lock = threading.Lock()
    app.pending_console_cmds = {
        "cmd-stale": {
            "chat_id": 111,
            "message_id": 222,
            "command": "dir /s",
            "started_at": time.time() - 90.0,
        },
        "cmd-fresh": {
            "chat_id": 111,
            "message_id": 333,
            "command": "echo 1",
            "started_at": time.time() - 10.0,
        },
    }
    app.pending_screenshots = {}
    app.pending_telemetry_cmd_ids = {}
    app.pending_antigravity_cmd_ids = {}
    app.pending_cmd_timestamps = {}
    app.scheduled_shutdown_ts = None
    app.power_flow = MagicMock()

    app.scheduler_tick(time.time())

    assert "cmd-stale" not in app.pending_console_cmds
    assert "cmd-fresh" in app.pending_console_cmds
    app.ui.send_message.assert_called_once()
    args, kwargs = app.ui.send_message.call_args
    assert "Таймаут выполнения команды" in args[1]
    assert kwargs.get("message_id") == 222


def test_physical_unlock_and_workspace_edits_existing_message_on_unlocked_session():
    """Verify handle_physical_unlock_and_workspace_trigger edits existing message instead of sending duplicate."""
    from vds_bot.bot import PcControlBotApp

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.ui.send_message.return_value = {"message_id": 777}
    app.mqtt = MagicMock()
    app.mqtt.system_session = "unlocked"
    app.mqtt.system_status = "online"
    app.power_flow = MagicMock()
    app.power_flow.stage = "IDLE"
    app.power_flow.start_direct_setup.return_value = "ws_test"
    app.start_async_workspace_setup = MagicMock()

    # 1. Trigger unlock when unlocked -> sends "Windows уже разблокирована"
    app.handle_physical_unlock_trigger(12345)
    assert app.last_unlock_gesture_msg_id == 777
    assert app.ui.send_message.call_count == 1

    # 2. Trigger combo 1 second later -> must edit message 777 rather than sending a new one
    app.handle_physical_unlock_and_workspace_trigger(12345)
    assert app.ui.send_message.call_count == 2
    args, kwargs = app.ui.send_message.call_args
    assert kwargs.get("message_id") == 777
    assert "запускаю рабочий сетап" in args[1]


def test_stealth_guard_deactivate_releases_lock_before_process_wait():
    """Verify StealthManager.deactivate releases internal lock before calling wait/terminate on process."""
    from core.stealth_guard import StealthManager

    sm = StealthManager()
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    lock_held_during_wait = []

    def fake_wait(timeout=None):
        lock_held_during_wait.append(sm._lock.acquire(blocking=False))
        if lock_held_during_wait[-1]:
            sm._lock.release()

    mock_proc.wait.side_effect = fake_wait
    sm._process = mock_proc

    ok = sm.deactivate()
    assert ok is True
    assert sm._process is None
    assert lock_held_during_wait == [True]  # Lock was free during wait!


def test_media_manager_resolve_cache_bounded_fifo():
    """Verify MediaManager._store_resolve_cache evicts oldest entries when reaching maxsize."""
    import core.media_manager as mm
    from core.media_manager import MediaManager

    manager = MediaManager.__new__(MediaManager)
    manager._resolve_cache = {}

    with patch.object(mm, "MAX_RESOLVE_CACHE_SIZE", 3):
        manager._store_resolve_cache(("t1", "a1", 100), ("T1", "A1", "1", False, None))
        manager._store_resolve_cache(("t2", "a2", 100), ("T2", "A2", "2", False, None))
        manager._store_resolve_cache(("t3", "a3", 100), ("T3", "A3", "3", False, None))
        assert len(manager._resolve_cache) == 3
        assert ("t1", "a1", 100) in manager._resolve_cache

        # Adding 4th item must evict 1st item (t1)
        manager._store_resolve_cache(("t4", "a4", 100), ("T4", "A4", "4", False, None))
        assert len(manager._resolve_cache) == 3
        assert ("t1", "a1", 100) not in manager._resolve_cache
        assert ("t4", "a4", 100) in manager._resolve_cache


def test_bot_is_shutting_down_handles_malformed_timestamp_safely():
    """Verify is_shutting_down safely returns False if pending_shutdown_started_at is malformed."""
    import bot as bot_mod

    with patch.object(bot_mod, "TELEGRAM_TOKEN", "test_tok"), \
         patch.object(bot_mod, "ALLOWED_USER_ID", 123456), \
         patch.object(bot_mod, "TelegramBotApi"), \
         patch.object(bot_mod, "MqttGateway"):
        app = bot_mod.PcControlBotApp()
        app.pending_shutdown_cmd_id = "cmd-123"
        app.pending_shutdown_started_at = "corrupted_timestamp"

        assert app.monitoring.is_shutting_down() is False

        app.pending_shutdown_started_at = time.time()
        assert app.monitoring.is_shutting_down() is True


def test_monitoring_router_led_action_unknown_answers_callback():
    """Verify unhandled led_action triggers callback answer instead of leaving Telegram spinner hanging."""
    from vds_bot.routers.monitoring_router import route_monitoring_callback

    app = MagicMock()
    app.mqtt.esp_status = "online"
    answered = []
    app.ui.answer_callback.side_effect = lambda cid, text=None, alert=False: answered.append((cid, text, alert))

    handled = route_monitoring_callback(app, "led_action:unsupported_subcommand", chat_id=123, message_id=456, callback_id="cb_456")
    assert handled is True
    assert len(answered) == 1
    assert answered[0][0] == "cb_456"
    assert "Неизвестное действие" in answered[0][1]


def test_antigravity_router_ag_heal_includes_reply_markup():
    """Verify ag_heal callback sends message with navigation reply markup attached."""
    from vds_bot.routers.antigravity_router import route_antigravity_callback

    app = MagicMock()
    app.mqtt.desktop_status = "online"
    app.mqtt.publish_desktop_action.return_value = "cmd-heal"
    app.pending_antigravity_cmd_ids = {}

    handled = route_antigravity_callback(app, "ag_heal", chat_id=123, message_id=456, callback_id="cb_heal")
    assert handled is True
    app.ui.send_message.assert_called_once()
    args, kwargs = app.ui.send_message.call_args
    assert "reply_markup" in kwargs
    assert kwargs["reply_markup"] is not None
    assert kwargs.get("message_id") == 456


def test_stealth_mode_idempotent_volume_mute():
    """Verify multiple stealth activations keep the original unmute state and restore it upon exit."""
    class DummyAgent(StealthMixin):
        def __init__(self):
            self.config = {"stealth_mode": {"offline_timeout_minutes": 10}}
            self.stealth_mgr = MagicMock()
            self.stealth_mgr.mute_audio = True
            self.stealth_mgr.turn_off_displays = False
            self.stealth_mgr.activate.return_value = True
            self.stealth_mgr.deactivate.return_value = True
            self.vol_state = {"percent": 75, "muted": False}

        def get_volume(self):
            return dict(self.vol_state)

        def set_volume(self, mute=None, percent=None, toggle=False):
            if mute is not None:
                self.vol_state["muted"] = bool(mute)
            return dict(self.vol_state)

    agent = DummyAgent()
    # First activation: mutes audio and records original muted=False
    res1 = agent.handle_stealth_action("on")
    assert res1["active"] is True
    assert agent.vol_state["muted"] is True
    assert agent._prev_mute_state is False

    # Second activation (duplicate "on" call): should preserve original False instead of overwriting with True
    res2 = agent.handle_stealth_action("on")
    assert res2["active"] is True
    assert agent._prev_mute_state is False

    # Exit stealth: restores unmuted state
    agent.handle_stealth_action("off")
    assert agent.vol_state["muted"] is False


def test_stealth_offline_watchdog_zero_timeout_disabled():
    """Verify offline_timeout_minutes: 0 disables emergency exit instead of forcing 60s exit."""
    class DummyAgent(StealthMixin):
        def __init__(self):
            self.config = {"stealth_mode": {"offline_timeout_minutes": 0}}
            self._stealth_is_active = True
            self._stealth_activating = False
            self.mqtt_online = False
            self.stealth_mgr = MagicMock()

    agent = DummyAgent()
    agent.check_stealth_offline_watchdog()
    # Watchdog should not start when timeout <= 0
    assert getattr(agent, "_stealth_offline_since", None) is None
    assert agent._stealth_is_active is True


def test_health_engine_ignores_heartbeat_older_than_shutdown():
    """Verify health engine rejects pre-shutdown packets without clearing shutdown flags or setting online."""
    from vds_bot.health_engine import HealthEngine

    engine = HealthEngine()
    now = time.time()
    shutdown_time = now - 15.0
    engine.set_graceful_shutdown(ts=shutdown_time)

    assert engine.graceful_shutdown_at == shutdown_time
    assert engine.get_status("windows_desktop") == "offline"

    # Packet dated before graceful_shutdown
    accepted = engine.update("windows_desktop", "online", boot_id=None, ts=shutdown_time - 5.0)
    assert accepted is False
    assert engine.get_status("windows_desktop") == "offline"
    assert engine.graceful_shutdown_at == shutdown_time

    # Fresh packet dated after shutdown from reboot clears graceful shutdown
    accepted_new = engine.update("windows_desktop", "online", boot_id="boot-1", ts=now)
    assert accepted_new is True
    assert engine.get_status("windows_desktop") == "online"
    assert engine.graceful_shutdown_at == 0.0


def test_command_router_russian_commands_before_console_input():
    """Verify Russian bot commands (плеер, активность) take precedence over console command execution."""
    from vds_bot.routers.command_router import route_message

    app = MagicMock()
    app.allowed_user_id = 12345
    app.console_active_chat_id = 12345
    app.console_target = "desktop"
    app.console_shell = "cmd"
    app.mqtt.desktop_status = "online"
    app.mqtt.query_activity.return_value = True

    # User types "активность" while in console mode: should query activity, NOT execute shell command
    msg = {"message_id": 99, "chat": {"id": 12345}, "text": "активность"}
    route_message(app, msg)
    app.execute_console_command.assert_not_called()
    app.mqtt.query_activity.assert_called_once()


def test_command_router_exit_updates_main_menu_message_id():
    """Verify /exit updates active_main_menu_message_id and calls save_state."""
    from vds_bot.routers.command_router import route_message

    app = MagicMock()
    app.allowed_user_id = 12345
    app.console_active_chat_id = 12345
    app.ui.send_message.return_value = {"message_id": 777}

    msg = {"message_id": 100, "chat": {"id": 12345}, "text": "/exit"}
    route_message(app, msg)

    assert app.console_active_chat_id is None
    assert app.active_main_menu_message_id == 777
    app.save_state.assert_called_once()


def test_monitoring_router_menu_clears_console_and_activity_state():
    """Verify pressing 'menu' clears console_active_chat_id and pending_activity_*."""
    from vds_bot.routers.monitoring_router import route_monitoring_callback

    app = MagicMock()
    app.console_active_chat_id = 12345
    app.pending_activity_chat_id = 12345
    app.pending_activity_msg_id = 555
    app.pending_activity_requested_at = time.time()
    app.monitoring.dashboard_text.return_value = "Dashboard"
    app._main_menu_markup.return_value = {}

    handled = route_monitoring_callback(app, "menu", chat_id=12345, message_id=888, callback_id="cb_menu")
    assert handled is True
    assert app.console_active_chat_id is None
    assert app.pending_activity_chat_id is None
    assert app.pending_activity_msg_id is None
    assert app.pending_activity_requested_at is None


def test_power_scheduler_escapes_console_timeout_html():
    """Verify power_scheduler escapes html in command timeout notifications."""
    from vds_bot.power_scheduler import PowerSchedulerMixin

    class DummyScheduler(PowerSchedulerMixin):
        def __init__(self):
            self.pending_console_cmds = {
                "cmd-1": {
                    "started_at": time.time() - 70.0,
                    "chat_id": 123,
                    "message_id": 456,
                    "command": "dir <test> & more",
                }
            }
            self.ui = MagicMock()
            self.mqtt = MagicMock()
            self._dashboard_refresh_in_flight = False

    scheduler = DummyScheduler()
    scheduler.scheduler_tick()

    scheduler.ui.send_message.assert_called_once()
    args, kwargs = scheduler.ui.send_message.call_args
    sent_text = args[1]
    assert "&lt;test&gt;" in sent_text
    assert "&amp;" in sent_text
    assert "<test>" not in sent_text


def test_action_dispatcher_rejects_empty_or_oversized_command_id():
    """Verify action dispatcher drops commands with empty or oversized id."""
    from desktop.action_dispatcher import ActionDispatcherMixin

    class DummyDispatcher(ActionDispatcherMixin):
        def __init__(self):
            self.processed_ids = []
            self.acknowledge = MagicMock()

    dispatcher = DummyDispatcher()
    # Empty ID
    dispatcher.process_command({"id": "", "action": "test", "expires": int(time.time()) + 10})
    assert "" not in dispatcher.processed_ids
    dispatcher.acknowledge.assert_not_called()

    # Oversized ID (> 80 chars)
    huge_id = "x" * 100
    dispatcher.process_command({"id": huge_id, "action": "test", "expires": int(time.time()) + 10})
    assert huge_id not in dispatcher.processed_ids
    dispatcher.acknowledge.assert_not_called()


def test_vpn_and_hardware_locks_thread_safe_init():
    """Verify _get_incy_lock and _get_net_state_lock return valid locks under concurrent access."""
    from desktop.vpn_manager import VpnManagerMixin
    from system.hardware_monitor import HardwareMonitorMixin

    vpn_inst = VpnManagerMixin()
    hw_inst = HardwareMonitorMixin()

    incy_locks = []
    net_locks = []

    def _worker():
        incy_locks.append(vpn_inst._get_incy_lock())
        net_locks.append(hw_inst._get_net_state_lock())

    threads = [threading.Thread(target=_worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(l is incy_locks[0] for l in incy_locks)
    assert all(l is net_locks[0] for l in net_locks)


def test_power_flow_pre_registers_unlock_cmd_id_and_reverts_on_failure():
    """Verify power_flow registers pending_unlock_command_id before publish and rolls back on publish failure."""
    from vds_bot.power_state import PowerFlowStage

    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.publish_system_action.return_value = None  # simulate MQTT failure

    flow = PowerFlowManager(ui, mqtt)
    flow.stage = PowerFlowStage.UNLOCKING
    flow.unlock_attempts = 0

    flow.check_and_trigger_unlock(12345)

    # Attempts should be rolled back to 0 because publish failed
    assert flow.unlock_attempts == 0
    assert flow.pending_unlock_command_id is None


def test_power_flow_set_auto_launch_preserves_active_unlock_state():
    """Verify set_auto_launch_workspace and set_auto_launch_game do not reset unlock attempts or pending unlock cmd."""
    from vds_bot.power_state import PowerFlowStage

    ui = MagicMock()
    mqtt = MagicMock()
    flow = PowerFlowManager(ui, mqtt)
    flow.stage = PowerFlowStage.UNLOCKING
    flow.pending_unlock_command_id = "cmd-unlock-existing"
    flow.unlock_attempts = 1

    flow.set_auto_launch_workspace(chat_id=12345)
    assert flow.auto_launch_workspace is True
    assert flow.stage == PowerFlowStage.UNLOCKING
    assert flow.pending_unlock_command_id == "cmd-unlock-existing"
    assert flow.unlock_attempts == 1
    mqtt.publish_system_action.assert_not_called()

    flow.set_auto_launch_game(game="gta5", vpn="incy", chat_id=12345)
    assert flow.auto_launch_game is True
    assert flow.target_game == "gta5"
    assert flow.stage == PowerFlowStage.UNLOCKING
    assert flow.pending_unlock_command_id == "cmd-unlock-existing"
    assert flow.unlock_attempts == 1
    mqtt.publish_system_action.assert_not_called()


def test_mqtt_dispatcher_pop_console_command_under_state_lock():
    """Verify console command response handling safely pops from pending_console_cmds under state_lock."""
    from vds_bot.mqtt_dispatcher import MqttDispatcherMixin

    app = SimpleNamespace()
    app.state_lock = threading.RLock()
    app.pending_console_cmds = {"cmd-1": {"chat_id": 123, "command": "dir", "target": "desktop"}}
    app.ui = MagicMock()
    app.allowed_user_id = 12345
    app.power_flow = MagicMock()
    app.physical_btn = MagicMock()
    app._handle_console_ack = MagicMock()

    # Call _on_mqtt_event on app via mixin
    MqttDispatcherMixin._on_mqtt_event(app, "command_timeout", {"id": "cmd-1"})
    assert "cmd-1" not in app.pending_console_cmds
    app.ui.send_message.assert_called_once()

    # Test unknown/already-popped command does not crash
    app.ui.send_message.reset_mock()
    MqttDispatcherMixin._on_mqtt_event(app, "command_timeout", {"id": "cmd-already-gone"})
    app.ui.send_message.assert_not_called()


def test_power_scheduler_tick_snapshot_protects_pending_structures():
    """Verify scheduler_tick iterates pending dicts safely and does not crash when modified concurrently."""
    from vds_bot.power_scheduler import PowerSchedulerMixin

    class DummyScheduler(PowerSchedulerMixin):
        def __init__(self):
            self.state_lock = threading.Lock()
            self.ui = MagicMock()
            self.monitoring = SimpleNamespace(telemetry_age=MagicMock(return_value=1.0))
            self.mqtt = SimpleNamespace(
                system_status="online",
                desktop_status="online",
                system_session="unlocked",
                health=MagicMock(),
            )
            self.allowed_user_id = 12345
            self.user_led_state = "off"
            self.last_auto_led_state = "off"
            now = time.time()
            self.pending_screenshots = {"s1": {"requested_at": now - 40.0, "chat_id": 12345}}
            self.pending_console_cmds = {"c1": {"started_at": now - 70.0, "chat_id": 12345, "command": "dir"}}
            self.pending_telemetry_cmd_ids = {"t1": "status"}
            self.pending_antigravity_cmd_ids = {"a1": "switch"}
            self.pending_cmd_timestamps = {"t1": now - 70.0, "a1": now - 70.0}
            self.pending_activity_chat_id = 12345
            self.pending_activity_requested_at = now - 20.0
            self.pending_shutdown_cmd_id = None
            self.scheduled_shutdown_ts = None

    sched = DummyScheduler()
    sched.scheduler_tick()

    assert "s1" not in sched.pending_screenshots
    assert "c1" not in sched.pending_console_cmds
    assert "t1" not in sched.pending_telemetry_cmd_ids
    assert "a1" not in sched.pending_antigravity_cmd_ids
    assert sched.pending_activity_chat_id is None
    assert sched.ui.send_message.call_count >= 2


def test_workspace_runner_resets_cmd_ids_on_publish_failure():
    """Verify workspace and game runners reset active_workspace_cmd_id in finally block when publish fails."""
    from vds_bot.workspace_runner import WorkspaceRunnerMixin

    class DummyRunner(WorkspaceRunnerMixin):
        def __init__(self):
            self.ui = MagicMock()
            self.mqtt = SimpleNamespace(
                desktop_status="online",
                publish_desktop_action=MagicMock(return_value=None),  # MQTT failure
            )
            self.power_flow = SimpleNamespace(reset_flow=MagicMock())
            self.workspace_cancel_event = threading.Event()
            self.workspace_executed_event = threading.Event()
            self.minimize_ack_event = threading.Event()
            self.current_workspace_steps = []
            self.app_statuses = {}
            self.active_workspace_cmd_id = None
            self.pending_minimize_cmd_id = None

    runner = DummyRunner()
    runner.run_workspace_setup_steps(12345)
    assert runner.active_workspace_cmd_id is None
    assert runner.pending_minimize_cmd_id is None
    runner.power_flow.reset_flow.assert_called_once()

    runner.power_flow.reset_flow.reset_mock()
    runner.run_game_setup_steps(12345, game="gta5")
    assert runner.active_workspace_cmd_id is None
    assert runner.pending_minimize_cmd_id is None
    runner.power_flow.reset_flow.assert_called_once()


def test_player_router_optimistic_update_and_business_id():
    """Verify route_player_callback passes business_connection_id on failure and merges optimistic updates."""
    from vds_bot.routers.player_router import route_player_callback

    app = SimpleNamespace()
    app.allowed_user_id = 99999
    app.active_player_message_id = None
    app.active_player_chat_id = None
    app.active_player_business_connection_id = None
    app.player_edit_lock = threading.Lock()
    app.latest_media_state = {"active": True, "title": "Song", "is_playing": True}
    app.last_player_edit_time = 0.0
    app._render_and_send_player = MagicMock()
    app.ui = SimpleNamespace(answer_callback=MagicMock(), send_message=MagicMock())
    app.mqtt = SimpleNamespace(
        last_media_state={},
        send_media_command=MagicMock(return_value=True),
    )

    # Success case: toggle_play updates is_playing optimistically while keeping title
    res = route_player_callback(app, "player_toggle_play", chat_id=111, message_id=222, callback_id="cb_play", business_connection_id="biz_1")
    assert res is True
    assert app.latest_media_state["is_playing"] is False
    assert app.latest_media_state["title"] == "Song"
    assert app.active_player_business_connection_id == "biz_1"
    app._render_and_send_player.assert_called_once()

    # Failure case: publish fails -> sends message with business_connection_id
    app.mqtt.send_media_command.return_value = False
    route_player_callback(app, "player_toggle_play", chat_id=111, message_id=222, callback_id="cb_fail", business_connection_id="biz_2")
    app.ui.send_message.assert_called_once()
    assert app.ui.send_message.call_args[1].get("business_connection_id") == "biz_2"


def test_ui_templates_antigravity_accounts_does_not_split_html_entities():
    """Verify antigravity_card_text trims whole lines to fit in 4000 chars without slicing HTML tags."""
    from vds_bot.ui_templates import UiTemplatesMixin

    ui = UiTemplatesMixin()
    # Create 80 dummy accounts with HTML names
    accounts = [
        {"id": f"acc_{i}", "email": f"user{i}@example.com", "name": f"<b>User {i}</b>", "min_quota": 80, "is_active": (i == 0)}
        for i in range(80)
    ]
    active_acc = accounts[0]
    active_acc["models_quota"] = {f"model_{j}": {"percentage": 90} for j in range(10)}

    text = ui.antigravity_card_text({"accounts": accounts, "active": active_acc, "is_running": True})
    assert len(text) <= 4000
    assert text.endswith("<i>⚡ Выберите аккаунт ниже для мгновенного переключения:</i>")
    # Verify no unclosed tags from slicing
    assert text.count("<b>") == text.count("</b>")
    assert text.count("<i>") == text.count("</i>")


def test_media_manager_poll_step_drops_concurrent_calls():
    """Verify _poll_step drops redundant calls when a poll step is already in flight."""
    import asyncio
    from core.media_manager import MediaManager

    async def _test():
        mgr = MediaManager(token="fake", user_id=1)
        call_count = 0

        async def slow_get_state():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return {"active": False}

        mgr.get_current_state = slow_get_state

        # Run two polls concurrently
        t1 = asyncio.create_task(mgr._poll_step(force=True))
        await asyncio.sleep(0.005)  # ensure t1 acquires the lock
        t2 = asyncio.create_task(mgr._poll_step(force=True))

        await asyncio.gather(t1, t2)
        # The second poll should have dropped without calling get_current_state
        assert call_count == 1

    asyncio.run(_test())


def test_vpn_manager_connect_incy_does_not_hold_lock_during_network_probe():
    """Verify connect_incy_vpn does not hold _get_incy_lock while performing network probe."""
    from desktop.vpn_manager import VpnManagerMixin

    vpn = VpnManagerMixin()
    vpn.config = {"vpn_ready_delay_seconds": 1}

    lock_held_during_probe = None

    def probe_wrapper(*args, **kwargs):
        nonlocal lock_held_during_probe
        lock = vpn._get_incy_lock()
        if hasattr(lock, "_is_owned"):
            lock_held_during_probe = lock._is_owned()
        else:
            acquired = lock.acquire(blocking=False)
            if acquired:
                lock.release()
                lock_held_during_probe = False
            else:
                lock_held_during_probe = True
        return True

    vpn.check_incy_vpn_active = probe_wrapper
    res = vpn.connect_incy_vpn(verify_google=False)
    assert res.get("verified") is True
    assert lock_held_during_probe is False


def test_vpn_manager_switch_next_server_superseded_by_newer_request():
    """Verify an active switch_next_incy_server wait loop aborts if superseded by a newer server switch."""
    from desktop.vpn_manager import VpnManagerMixin

    vpn = VpnManagerMixin()
    vpn.config = {}
    servers = [{"id": "s1", "name": "Server 1", "latency": 50}, {"id": "s2", "name": "Server 2", "latency": 60}]
    vpn.get_incy_servers = MagicMock(return_value=servers)
    vpn._stop_incy_processes = MagicMock()
    vpn._load_incy_preferences = MagicMock(return_value={"selectedServerId": "s1"})
    vpn._write_incy_preferences_atomic = MagicMock()
    vpn.check_incy_vpn_active = MagicMock(return_value=False)

    with patch("subprocess.Popen"), patch("pathlib.Path.exists", return_value=True):
        result_holder = []
        def _run_switch():
            res = vpn.switch_next_incy_server(target_server_id="s2")
            result_holder.append(res)

        t = threading.Thread(target=_run_switch)
        t.start()
        time.sleep(0.1)

        # Trigger switch 2 which increments _incy_generation
        vpn.switch_next_incy_server(target_server_id="s1")
        t.join(timeout=2.0)

        assert len(result_holder) == 1
        assert result_holder[0]["success"] is False
        assert "Superseded" in result_holder[0]["error"]


def test_activity_card_text_safe_truncation_without_broken_html_tags():
    """Verify activity_card_text drops whole lines when character count exceeds Telegram limits instead of slicing tags."""
    from vds_bot.ui_templates import UiTemplatesMixin

    tmpl = UiTemplatesMixin()
    # Create 12 items with long names that exceed 4000 characters combined
    summary_data = [
        {"app_name": "A" * 70 + f"_{i}", "total_duration": 3600.0, "emoji_id": f"emoji_{i}"}
        for i in range(12)
    ]
    card_text = tmpl.activity_card_text(summary_data, "15.11.2026")
    assert len(card_text) <= 4096
    assert card_text.count("<b>") == card_text.count("</b>")
    assert card_text.count("<tg-emoji") == card_text.count("</tg-emoji>")


def test_power_flow_handle_unlock_decision_when_pc_is_already_running():
    """Verify handle_unlock_decision with flow_unlock_no does not claim PC is starting when PC was already running."""
    from vds_bot.power_flow import PowerFlowManager, PowerFlowStage

    mock_ui = MagicMock()
    mock_ui.back_button.return_value = [{"text": "Назад", "callback_data": "menu"}]
    mock_mqtt = MagicMock()
    flow = PowerFlowManager(mock_ui, mock_mqtt)
    flow.stage = PowerFlowStage.WAITING_WORKSPACE_DECISION
    flow.message_id = 100

    flow.handle_unlock_decision("flow_unlock_no", chat_id=123, message_id=100)

    mock_ui.send_message.assert_called_once()
    sent_text = mock_ui.send_message.call_args[0][1]
    assert "ПК запускается" not in sent_text
    assert "Разблокировка Windows пропущена" in sent_text
    assert "Запуск сценария отменён" in sent_text
    assert flow.stage == PowerFlowStage.IDLE


def test_antigravity_router_heal_back_button_and_settings_menu_entry():
    """Verify heal_antigravity returns to apps_menu when launched from apps view, and settings menu has antigravity button."""
    from vds_bot.routers.antigravity_router import route_antigravity_callback
    from vds_bot.ui_markups import UiMarkupsMixin

    markups = UiMarkupsMixin()
    settings_kb = markups.settings_menu_markup()["inline_keyboard"]
    callbacks = [btn.get("callback_data") for row in settings_kb for btn in row]
    assert "antigravity_menu" in callbacks

    app = MagicMock()
    app.ui = markups
    app.ui.send_message = MagicMock()
    app.ui.answer_callback = MagicMock()
    app.mqtt.desktop_status = "online"
    app.mqtt.publish_desktop_action.return_value = "cmd-heal-1"
    app.current_menu_view = "apps_menu"
    app.pending_antigravity_cmd_ids = {}

    handled = route_antigravity_callback(app, "ag_heal", 123, 456, "cb1")
    assert handled is True
    app.ui.send_message.assert_called_once()
    call_kwargs = app.ui.send_message.call_args[1]
    reply_markup = call_kwargs.get("reply_markup", {})
    back_btn = reply_markup["inline_keyboard"][0][0]
    assert back_btn["callback_data"] == "apps_menu"


def test_power_scheduler_pending_antigravity_timeout_notification():
    """Verify scheduler_tick evicts timed-out antigravity commands and sends notification with back button."""
    from vds_bot.bot import PcControlBotApp

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.ui.back_button.return_value = [{"text": "Назад", "callback_data": "antigravity_menu"}]
    app.mqtt = MagicMock()
    app.mqtt.mqtt_online = True
    app.mqtt.system_status = "online"
    app.mqtt.desktop_status = "online"
    app.state_lock = threading.Lock()
    app.pending_console_cmds = {}
    app.pending_telemetry_cmd_ids = {}
    app.pending_screenshots = {}
    app.pending_activity_cmd_ids = {}
    app.pending_antigravity_cmd_ids = {
        "cmd-ag-stale": (123, 456, "switch")
    }
    app.pending_cmd_timestamps = {
        "cmd-ag-stale": time.time() - 75.0
    }
    app.pending_shutdown_cmd_id = None
    app.scheduled_shutdown_ts = None
    app.shutdown_offline_detected_at = None

    with patch("vds_bot.power_scheduler.sync_auto_led"):
        app.scheduler_tick()

    assert "cmd-ag-stale" not in app.pending_antigravity_cmd_ids
    assert "cmd-ag-stale" not in app.pending_cmd_timestamps
    app.ui.send_message.assert_called_once()
    sent_text = app.ui.send_message.call_args[0][1]
    assert "Таймаут: переключение профиля" in sent_text


def test_apply_stealth_state_preserves_message_id_in_menu():
    """Verify apply_stealth_state edits existing message in-place when message_id is provided."""
    from vds_bot.action_helpers import apply_stealth_state

    app = MagicMock()
    app.ui.stealth_menu_markup.return_value = {"inline_keyboard": []}
    app.ui.send_message.return_value = {"message_id": 999}

    apply_stealth_state(app, chat_id=123, state="on", message_id=999)

    app.mqtt.publish_desktop_action.assert_called_once_with("stealth_mode", {"state": "on"})
    app.ui.send_message.assert_called_once()
    assert app.ui.send_message.call_args[1].get("message_id") == 999
    assert app.active_stealth_card == (123, 999)


def test_workspace_runner_card_ui_uses_scenario_title():
    """Verify _update_workspace_card_ui renders the current scenario title in card header."""
    from vds_bot.workspace_runner import WorkspaceRunnerMixin

    runner = WorkspaceRunnerMixin()
    runner.workspace_card_msg_id = 888
    runner.current_scenario_title = "Запуск игры GTA V..."
    runner.current_workspace_steps = [{"id": "step1", "label": "Launcher", "emoji_id": None}]
    runner.app_statuses = {"step1": "✅"}
    runner.allowed_user_id = 777
    runner.ui = MagicMock()

    runner._update_workspace_card_ui()

    runner.ui.send_message.assert_called_once()
    args, kwargs = runner.ui.send_message.call_args
    assert args[0] == 777
    assert "<b>Запуск игры GTA V...</b>" in args[1]
    assert kwargs.get("message_id") == 888


def test_console_service_command_header_truncation():
    """Verify ConsoleService safely truncates long command lines in Telegram header to prevent message overflow."""
    from vds_bot.console_service import ConsoleServiceMixin

    class DummyBot(ConsoleServiceMixin):
        def __init__(self):
            self.ui = MagicMock()
            self.state_lock = threading.Lock()

    bot = DummyBot()
    pending = {
        "chat_id": 123,
        "message_id": 456,
        "command": "powershell " + ("Get-Process -Name TestProcessVeryLongName " * 20),
        "started_at": time.time() - 1.0,
        "target": "desktop",
        "shell": "powershell",
    }
    data = {"id": "cmd-long", "result": "executed", "data": {"exit_code": 0, "stdout": "Success output"}}
    bot._handle_console_ack(pending, data)

    bot.ui.send_message.assert_called_once()
    sent_text = bot.ui.send_message.call_args[0][1]
    assert "..." in sent_text
    assert len(sent_text) < 4096


def test_monitoring_router_led_and_telemetry_publish_failure_toast():
    """Verify monitoring router answers callback with error toast when MQTT publish returns None."""
    from vds_bot.routers.monitoring_router import route_monitoring_callback

    app = MagicMock()
    app.mqtt.desktop_status = "online"
    app.mqtt.esp_status = "online"
    app.mqtt.publish_desktop_action.return_value = None
    app.mqtt.publish_esp_action.return_value = None
    app.ui.answer_callback = MagicMock()

    # Telemetry failure
    handled = route_monitoring_callback(app, "telemetry_now", 123, 456, callback_id="cb-telem")
    assert handled is True
    app.ui.answer_callback.assert_called_with("cb-telem", "❌ Не удалось запросить телеметрию: сбой MQTT", alert=True)

    # LED Power failure
    handled = route_monitoring_callback(app, "led_action:on", 123, 456, callback_id="cb-led")
    assert handled is True
    app.ui.answer_callback.assert_called_with("cb-led", "❌ Не удалось включить подсветку: сбой MQTT", alert=True)


def test_power_router_publish_failure_answers_toast():
    """Verify power router notifies user with alert toast when desktop/system actions fail to publish."""
    from vds_bot.routers.power_router import route_power_callback

    app = MagicMock()
    app.mqtt.publish_desktop_action.return_value = None
    app.mqtt.publish_system_action.return_value = None
    app.mqtt.publish_esp_action.return_value = None
    app.mqtt.esp_status = "online"
    app.ui.answer_callback = MagicMock()

    # Lock failure
    handled = route_power_callback(app, "lock_windows", 123, 456, callback_id="cb-lock")
    assert handled is True
    app.ui.answer_callback.assert_called_with("cb-lock", "❌ Не удалось отправить команду блокировки: MQTT недоступен", alert=True)

    # Emergency power off failure
    handled = route_power_callback(app, "emergency_power_off_confirm", 123, 456, callback_id="cb-emerg")
    assert handled is True
    app.ui.answer_callback.assert_called_with("cb-emerg", "❌ Сбой отправки команды на ESP8266: MQTT недоступен", alert=True)


def test_workspace_router_app_launch_publish_failure_alerts_user():
    """Verify workspace router alerts user when publish_desktop_action fails to return a command ID."""
    from vds_bot.routers.workspace_router import route_workspace_callback

    app = MagicMock()
    app.mqtt.desktop_status = "online"
    app.mqtt.publish_desktop_action.return_value = None
    app.ui.answer_callback = MagicMock()

    handled = route_workspace_callback(app, "happ_launch:connect", 123, 456, callback_id="cb-happ")
    assert handled is True
    app.ui.answer_callback.assert_called_with("cb-happ", "❌ Не удалось отправить команду: сбой MQTT", alert=True)


def test_flow_workspace_cancel_signals_cancel_event():
    """Verify flow_workspace_cancel callback sets workspace_cancel_event before calling cancel_workspace."""
    from vds_bot.routers.workspace_router import route_workspace_callback

    app = MagicMock()
    app.workspace_cancel_event = threading.Event()
    app.power_flow = MagicMock()
    app.ui.answer_callback = MagicMock()

    handled = route_workspace_callback(app, "flow_workspace_cancel", 123, 456, callback_id="cb-cancel")
    assert handled is True
    assert app.workspace_cancel_event.is_set()
    app.power_flow.cancel_workspace.assert_called_once_with(123)


def test_physical_router_recording_guards_and_esp_offline():
    """Verify physical router prevents starting recording if ESP is offline or publish fails, and cancel publishes to ESP."""
    from vds_bot.routers.physical_router import route_physical_callback

    app = MagicMock()
    app.mqtt.esp_status = "offline"
    app.physical_btn = MagicMock()
    app.physical_btn._lock = threading.Lock()
    app.ui.answer_callback = MagicMock()

    # ESP offline guard
    handled = route_physical_callback(app, "physical_record_begin", 123, 456, callback_id="cb-rec")
    assert handled is True
    app.ui.answer_callback.assert_called_with("cb-rec", "❌ Контроллер ESP8266 не в сети!", alert=True)

    # Cancel recording notifies ESP
    app.mqtt.esp_status = "online"
    handled = route_physical_callback(app, "physical_cancel_record", 123, 456, callback_id="cb-cancel")
    assert handled is True
    app.mqtt.publish_esp_action.assert_called_with("CANCEL_PHYSICAL_RECORDING")


def test_bot_app_idempotent_stop_and_signal_handling():
    """Verify bot app stop() is idempotent and avoids redundant teardown."""
    from vds_bot.bot import PcControlBotApp

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.running = True
    app.mqtt = MagicMock()
    app.ui = MagicMock()
    app._release_lock = MagicMock()

    app.stop()
    assert app.running is False
    assert getattr(app, "_stopped", False) is True
    assert app.mqtt.stop.call_count == 1

    # Second call must be a no-op
    app.stop()
    assert app.mqtt.stop.call_count == 1


def test_core_module_import_resilience_without_native_windows_binaries():
    """Verify core package can be imported safely even if platform icon extractor / media modules fail."""
    import importlib
    import sys

    with patch.dict(sys.modules, {"core.icon_extractor": None, "core.media_manager": None}):
        core_mod = importlib.import_module("core")
        assert hasattr(core_mod, "ActivityTracker")
        assert hasattr(core_mod, "AntigravityCrypto")


def test_desktop_launcher_cancel_event_returns_cancelled_reason():
    """Verify launch_app returns reason='cancelled' when cancel_event is triggered during step verification."""
    from desktop.launcher import LauncherMixin

    class DummyLauncher(LauncherMixin):
        def __init__(self):
            self.config = {
                "apps": {
                    "TestApp": [r"C:\Windows\notepad.exe"]
                }
            }
            self.workspace_cancellations = {}
            self.workspace_lock = threading.Lock()

    launcher = DummyLauncher()
    ws_id = "ws-cancel-1"

    with patch("subprocess.Popen") as mock_popen, \
         patch("pathlib.Path.exists", return_value=True), \
         patch("psutil.process_iter") as mock_iter:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        def _fake_process_iter(*args, **kwargs):
            launcher.workspace_cancellations[ws_id].set()
            return []

        mock_iter.side_effect = _fake_process_iter

        res = launcher.launch_app("TestApp", workspace_id=ws_id)
        assert res.get("success") is False
        assert res.get("reason") == "cancelled"


def test_os_events_win32_ctypes_signatures_configured():
    """Verify desktop.os_events initializes 64-bit safe ctypes signatures for Win32 API calls."""
    import sys
    if sys.platform != "win32":
        pytest.skip("Win32 ctypes test applies only to Windows platform")

    import ctypes
    from ctypes import wintypes
    from desktop.os_events import _ensure_win32_signatures
    _ensure_win32_signatures()

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    assert user32.GetForegroundWindow.restype == wintypes.HWND
    assert user32.GetWindowThreadProcessId.argtypes == [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    assert user32.GetWindowThreadProcessId.restype == wintypes.DWORD
    assert kernel32.OpenProcess.restype == wintypes.HANDLE
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]


def test_auto_backup_refuses_partial_backup_on_vds_failure(tmp_path):
    """Verify auto_backup refuses to overwrite full backup history when VDS files cannot be fetched."""
    from scripts.auto_backup import run_backup

    with patch("scripts.auto_backup.collect_vds_code_files", side_effect=RuntimeError("VDS SSH down")), \
         patch("scripts.auto_backup.collect_local_code_files", return_value={"test.py": b"print(1)"}):
        exit_code = run_backup(force=False, dry_run=False, skip_vds=False, scheduled=False)
        assert exit_code == 1


def test_type_password_win32_ctypes_signatures_configured():
    """Verify scripts.type_password registers safe 64-bit ctypes argtypes and restypes."""
    import sys
    if sys.platform != "win32":
        pytest.skip("Win32 ctypes test applies only to Windows platform")

    import ctypes
    from ctypes import wintypes
    import scripts.type_password as tp

    assert tp.user32.GetWindowThreadProcessId.argtypes == [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    assert tp.user32.GetWindowThreadProcessId.restype == wintypes.DWORD
    assert tp.user32.GetKeyboardLayout.argtypes == [wintypes.DWORD]
    assert tp.user32.GetKeyboardLayout.restype == wintypes.HKL


def test_premium_checker_exported_and_attached_to_bot_app():
    """Verify PremiumChecker is exported from core and hooked into bot update pipeline."""
    import core
    assert "PremiumChecker" in core.__all__
    assert hasattr(core, "PremiumChecker")

    from vds_bot.bot import PcControlBotApp
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.allowed_user_id = 12345
    mock_checker = MagicMock()
    app.premium_checker = mock_checker

    app.handle_callback({"id": "cb1", "from": {"id": 12345, "is_premium": True}, "data": "noop"})
    mock_checker.record_user.assert_called_with({"id": 12345, "is_premium": True})


def test_physical_button_dead_builder_methods_purged():
    """Verify dead physical_builder methods have been removed from PhysicalButtonManager."""
    from vds_bot.physical_button import PhysicalButtonManager
    pbtn = PhysicalButtonManager(MagicMock(), MagicMock())
    assert not hasattr(pbtn, "physical_builder_text")
    assert not hasattr(pbtn, "physical_builder_markup")


def test_antigravity_crypto_topic_entry_buffer_bounds_check():
    """Verify replace_unified_topic_entry handles malformed varint length without raising IndexError."""
    from core.antigravity_crypto import ProtobufHelper
    corrupted_entry = bytes([0x0A, 100, 0x01, 0x02])
    res = ProtobufHelper.replace_unified_topic_entry(corrupted_entry, "test_sentinel", b"payload")
    assert len(res) > 0


def test_stealth_guard_win32_ctypes_signatures_configured():
    """Verify core.stealth_guard declares 64-bit safe ctypes signatures for foreground and cursor APIs."""
    import sys
    if sys.platform != "win32":
        pytest.skip("Win32 ctypes test applies only to Windows platform")

    import ctypes
    from ctypes import wintypes
    import core.stealth_guard as sg

    assert sg.user32.GetForegroundWindow.restype == wintypes.HWND
    assert sg.user32.IsWindow.argtypes == [wintypes.HWND]
    assert sg.user32.SetForegroundWindow.restype == wintypes.BOOL
    assert sg.user32.SetCursor.restype == wintypes.HANDLE


def test_media_manager_toggle_like_missing_track_id_returns_false():
    """Verify MediaManager._toggle_like returns False when track ID cannot be resolved."""
    from core.media_manager import MediaManager
    mm = MediaManager.__new__(MediaManager)
    mm.current_track_id = None
    mm.current_is_liked = True
    mm._resolve_track = MagicMock(return_value=(None, None, None, False))
    mm._sync_lock = threading.RLock()
    mm._state_lock = threading.RLock()

    result = mm._toggle_like()
    assert result is False


def test_telemetry_bg_notify_uses_current_session_title():
    """Verify telemetry background notification publishes fresh current_session_title under state_lock."""
    from desktop.telemetry import TelemetryMixin

    telem = TelemetryMixin()
    telem.current_session_app = "old_app.exe"
    telem.current_session_title = "Old Title"
    telem.current_session_exe = r"C:\test.exe"
    telem.current_session_emoji_id = None
    telem.current_session_start = time.time()
    telem.emoji_manager = MagicMock()
    telem.emoji_manager.notify_app_opened.return_value = "emoji-123"
    telem.publish_active_app_update = MagicMock()

    def _fake_notify(exe_path, exe_name, title):
        telem.current_session_title = "Updated Window Title"
        return "emoji-123"

    telem.emoji_manager.notify_app_opened.side_effect = _fake_notify

    with patch("threading.Thread") as mock_thread:
        def fake_start():
            worker_fn = mock_thread.call_args[1]["target"]
            worker_fn()
        mock_thread_inst = MagicMock()
        mock_thread_inst.start = fake_start
        mock_thread.return_value = mock_thread_inst

        telem._handle_active_app_change(r"C:\test.exe", "test.exe", "Initial Title")

    telem.publish_active_app_update.assert_called_with("test.exe", "emoji-123", "Updated Window Title")


def test_sensors_read_streams_survives_closure():
    """Verify _read_stdout and _read_stderr handle stream ValueError/OSError without throwing exceptions."""
    from desktop.sensors import SensorHelper

    sh = SensorHelper.__new__(SensorHelper)
    sh.lock = threading.Lock()
    sh.latest = []
    sh.process = MagicMock()

    class FailingStream:
        def __iter__(self):
            return self
        def __next__(self):
            raise ValueError("I/O operation on closed file")

    sh.process.stdout = FailingStream()
    sh.process.stderr = FailingStream()

    sh._read_stdout()
    sh._read_stderr()
    assert sh.latest == []


def test_action_dispatcher_diagnose_codex_launch_safely_handles_non_string_chatgpt_config():
    """Verify diagnose_codex_launch action safely extracts string from list or dict app configuration."""
    from desktop.action_dispatcher import ActionDispatcherMixin

    dispatcher = ActionDispatcherMixin()
    dispatcher.config = {"apps": {"ChatGPT": [r"C:\Program Files\OpenAI\ChatGPT.exe"]}}
    dispatcher.acknowledge = MagicMock()
    dispatcher._has_visible_window = MagicMock(return_value=False)

    with patch("desktop.action_dispatcher.diagnose_codex_launch", return_value={"state": "checked"}):
        dispatcher.execute_action("cmd-1", "diagnose_codex_launch", {"variant": "1"})
        dispatcher.acknowledge.assert_called_once()
        assert dispatcher.acknowledge.call_args[0][1] == "executed"


def test_vpn_manager_get_incy_servers_filters_dead_servers(tmp_path):
    """Verify get_incy_servers assigns fallback weight 9999 to dead servers with negative latency."""
    from desktop.vpn_manager import VpnManagerMixin

    db_path = tmp_path / "incy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE servers (id TEXT, name TEXT, latency INTEGER)")
    conn.execute("INSERT INTO servers VALUES ('s1', 'Germany', 45)")
    conn.execute("INSERT INTO servers VALUES ('s2', 'Россия Москва', 10)")
    conn.execute("INSERT INTO servers VALUES ('s3', 'Dead Server', -1)")
    conn.execute("INSERT INTO servers VALUES ('s4', 'Unchecked Server', NULL)")
    conn.commit()
    conn.close()

    with patch("pathlib.Path.exists", return_value=True), \
         patch("sqlite3.connect", return_value=sqlite3.connect(str(db_path))):
        servers = VpnManagerMixin.get_incy_servers()
        ids = [s["id"] for s in servers]
        assert "s1" in ids
        assert "s4" in ids
        assert "s3" in ids
        assert "s2" not in ids
        dead_srv = next(s for s in servers if s["id"] == "s3")
        assert dead_srv["latency"] == 9999
        # s1 (45ms) must come before s3 (9999ms)
        assert ids.index("s1") < ids.index("s3")


def test_system_process_manager_restart_desktop_agent_stderr_priority():
    """Verify restart_desktop_agent surfaces stderr text when schtasks execution fails."""
    from system.process_manager import ProcessManagerMixin

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=1, stdout="   \n", stderr="Access is denied.\r\n")
        with pytest.raises(RuntimeError) as exc_info:
            ProcessManagerMixin.restart_desktop_agent()
        assert "Access is denied." in str(exc_info.value)


def test_power_ui_get_unlock_failed_markup_escapes_html():
    """Verify get_unlock_failed_markup escapes reason string to prevent unclosed HTML tags."""
    from vds_bot.power_ui import get_unlock_failed_markup

    text, markup = get_unlock_failed_markup("Failed due to <b>XSS</b> <script>")
    assert "&lt;script&gt;" in text
    assert "&lt;b&gt;XSS&lt;/b&gt;" in text
    assert "<script>" not in text


def test_ui_templates_power_menu_escapes_session_name():
    """Verify power_menu_text properly escapes unknown session name in HTML."""
    from vds_bot.ui_templates import UiTemplatesMixin

    tmpl = UiTemplatesMixin()
    text = tmpl.power_menu_text(SimpleNamespace(label="Готов к работе"), session="custom<session>_state")
    assert "&lt;session&gt;" in text
    assert "<session>" not in text


def test_telegram_ui_answer_callback_truncates_long_toast():
    """Verify TelegramBotApi.answer_callback truncates toast text exceeding 200 characters."""
    from vds_bot.telegram_ui import TelegramBotApi

    bot_api = TelegramBotApi("fake_token")
    bot_api.call = MagicMock()

    long_toast = "A" * 250
    bot_api.answer_callback("cb123", text=long_toast)

    bot_api.call.assert_called_once()
    payload = bot_api.call.call_args[0][1]
    assert len(payload["text"]) <= 200
    assert payload["text"].endswith("...")


def test_mqtt_gateway_publish_esp_action_allows_zero_value():
    """Verify publish_esp_action serializes numeric 0 value into command payload."""
    from vds_bot.mqtt_gateway import MqttGateway

    gw = MqttGateway.__new__(MqttGateway)
    gw.pending_lock = threading.Lock()
    gw.pending_commands = {}
    gw.publish = MagicMock(return_value=True)

    gw.publish_esp_action("SET_BRIGHTNESS", 0)

    gw.publish.assert_called_once()
    topic, payload = gw.publish.call_args[0]
    parts = payload.split("|")
    assert parts[0] == "SET_BRIGHTNESS"
    assert parts[-1] == "0"


def test_type_password_character_typing_ascii_digits_only():
    """Verify type_character treats only ASCII 0..9 as direct number keys and rejects non-ASCII digits."""
    from scripts.type_password import type_character

    with patch("scripts.type_password._send_vk_with_modifiers") as mock_send, \
         patch("scripts.type_password.user32.VkKeyScanW", return_value=-1):
        type_character("5")
        mock_send.assert_called_once_with(0x35, 0)

        with pytest.raises(ValueError):
            type_character("²")


def test_apps_menu_markup_two_buttons_per_row_ergonomics():
    """Verify apps_menu_markup has at most 2 buttons per row to prevent text truncation on mobile screens."""
    from vds_bot.ui_markups import UiMarkupsMixin

    markups = UiMarkupsMixin()
    kb = markups.apps_menu_markup()["inline_keyboard"]
    for row_idx, row in enumerate(kb):
        assert len(row) <= 2, f"Row {row_idx} exceeds 2 buttons: {[b.get('text') for b in row]}"


def test_led_menu_and_palette_markup_structure():
    """Verify led_menu_markup delegates full numbered grid to led_palette_markup."""
    from vds_bot.ui_markups import UiMarkupsMixin

    markups = UiMarkupsMixin()
    led_kb = markups.led_menu_markup()["inline_keyboard"]
    all_callbacks = [btn["callback_data"] for row in led_kb for btn in row]

    # Main menu must have on/off, quick presets, brightness, palette link, back
    assert "led_action:on" in all_callbacks
    assert "led_action:off" in all_callbacks
    assert "led_action:preset:eye" in all_callbacks
    assert "led_action:preset:photo" in all_callbacks
    assert "led_palette" in all_callbacks

    # Numbered 1..12 buttons must NOT clutter the main led_menu
    for i in range(1, 13):
        assert f"led_action:preset:{i}" not in all_callbacks

    # Palette markup must contain all 1..12 numbered buttons and back to led_menu
    palette_kb = markups.led_palette_markup()["inline_keyboard"]
    palette_callbacks = [btn["callback_data"] for row in palette_kb for btn in row]
    for i in range(1, 13):
        assert f"led_action:preset:{i}" in palette_callbacks
    assert "led_menu" in palette_callbacks


def test_monitoring_router_led_palette_callback():
    """Verify monitoring router displays palette text and markup on led_palette callback."""
    from vds_bot.routers.monitoring_router import route_monitoring_callback

    mock_app = MagicMock()
    mock_app.ui.led_palette_markup.return_value = {"inline_keyboard": []}

    handled = route_monitoring_callback(mock_app, "led_palette", 12345, 99, "42")
    assert handled is True
    assert mock_app.current_menu_view == "led_palette"
    mock_app.ui.answer_callback.assert_called_once_with("42")
    mock_app.ui.send_message.assert_called_once()
    msg_call = mock_app.ui.send_message.call_args
    assert "Все оттенки белого (1..12)" in msg_call[0][1]
    assert msg_call[1]["message_id"] == 99


def test_console_command_resolves_fast_early_ack():
    """Verify execute_console_command immediately processes an ACK that arrived early during dispatch."""
    from vds_bot.console_service import ConsoleServiceMixin

    app = ConsoleServiceMixin()
    app.ui = MagicMock()
    app.ui.send_message.return_value = {"message_id": 555}
    app.mqtt = MagicMock()
    app.mqtt.desktop_status = "online"
    app.pending_console_cmds = {}
    app.early_console_acks = {}
    app.state_lock = threading.RLock()
    app._handle_console_ack = MagicMock()

    def mock_publish(action, args=None, timeout=60):
        # Simulate fast ACK arriving on MQTT thread during publish
        app.early_console_acks["cmd-fast-1"] = {"result": "executed", "data": {"stdout": "Fast result"}}
        return "cmd-fast-1"

    app.mqtt.publish_desktop_action.side_effect = mock_publish

    app.execute_console_command(chat_id=123, cmd_text="Get-Process", target="desktop", shell="powershell")

    # The early ACK must have been processed immediately without waiting in pending_console_cmds
    app._handle_console_ack.assert_called_once()
    assert "cmd-fast-1" not in app.pending_console_cmds
    assert "cmd-fast-1" not in app.early_console_acks


def test_console_command_cleans_up_when_mqtt_publish_fails():
    """Verify execute_console_command does not register pending_console_cmds entry when MQTT returns None."""
    from vds_bot.console_service import ConsoleServiceMixin

    app = ConsoleServiceMixin()
    app.ui = MagicMock()
    app.ui.send_message.return_value = {"message_id": 555}
    app.mqtt = MagicMock()
    app.mqtt.desktop_status = "online"
    app.mqtt.publish_desktop_action.return_value = None
    app.pending_console_cmds = {}
    app.early_console_acks = {}
    app.state_lock = threading.RLock()

    app.execute_console_command(chat_id=123, cmd_text="dir", target="desktop")

    assert len(app.pending_console_cmds) == 0
    from vds_bot.console_service import ConsoleServiceMixin

    app = ConsoleServiceMixin()
    app.ui = MagicMock()
    app.ui.send_message.return_value = {"message_id": 555}
    app.mqtt = MagicMock()
    app.mqtt.desktop_status = "online"
    app.mqtt.publish_desktop_action.return_value = None
    app.pending_console_cmds = {}
    app.state_lock = threading.RLock()

    app.execute_console_command(chat_id=123, cmd_text="dir", target="desktop")

    assert len(app.pending_console_cmds) == 0


def test_acquire_lock_fails_fast_on_permission_error(tmp_path, monkeypatch):
    """Verify _acquire_lock exits with error code 1 instead of continuing on PermissionError."""
    import vds_bot.bot as bot_mod
    from vds_bot.bot import PcControlBotApp

    lock_file = tmp_path / "bot.lock"
    monkeypatch.setattr(bot_mod, "LOCK_PATH", lock_file)

    app = PcControlBotApp.__new__(PcControlBotApp)

    with patch("os.open", side_effect=PermissionError("Permission denied: bot.lock")), \
         pytest.raises(SystemExit) as exc_info:
        app._acquire_lock()

    assert exc_info.value.code == 1


def test_send_key_esc_reports_false_when_both_methods_fail():
    """Verify send_key_esc returns success=False when both Win32 keybd_event and fallback fail."""
    from desktop.launcher import LauncherMixin

    with patch("ctypes.windll.user32.keybd_event", side_effect=OSError("Access denied")), \
         patch.dict("sys.modules", {"pyautogui": None}):
        res = LauncherMixin.send_key_esc(count=1)
        assert res["success"] is False
        assert res["sent"] == 0


def test_import_vds_bot_package_clean_resolution():
    """Verify vds_bot and monitoring modules resolve internal imports cleanly when imported as a package."""
    import vds_bot
    import vds_bot.monitoring
    import vds_bot.bot

    assert hasattr(vds_bot.monitoring, "MonitoringManager")
    assert hasattr(vds_bot.bot, "PcControlBotApp")


def test_desktop_and_system_power_ack_only_after_launch_succeeds():
    """Verify shutdown, restart, and lock acknowledge failed rather than executed when launch fails."""
    from desktop.action_dispatcher import ActionDispatcherMixin as DesktopDispatcher
    from system.action_dispatcher import ActionDispatcherMixin as SystemDispatcher

    d = DesktopDispatcher()
    d.mqtt = MagicMock()
    d.acknowledge = MagicMock()

    with patch("desktop.action_dispatcher.subprocess.Popen", side_effect=OSError("missing shutdown.exe")):
        d.execute_action("cmd-shut", "shutdown", {})
    assert d.acknowledge.call_args[0][1] == "failed"

    d.acknowledge.reset_mock()
    with patch("desktop.action_dispatcher.ctypes.windll.user32.LockWorkStation", return_value=0):
        d.execute_action("cmd-lock", "lock", {})
    assert d.acknowledge.call_args[0][1] == "failed"

    d.acknowledge.reset_mock()
    call_order = []

    def fake_popen(*args, **kwargs):
        call_order.append("popen")
        return MagicMock()

    def fake_ack(*args, **kwargs):
        call_order.append(("ack", args[1]))

    d.acknowledge = fake_ack
    with patch("desktop.action_dispatcher.subprocess.Popen", side_effect=fake_popen):
        d.execute_action("cmd-rst-ok", "restart", {})
    assert call_order == ["popen", ("ack", "executed")]

    s = SystemDispatcher()
    s.config = {}
    s.acknowledge = MagicMock()
    s._turn_off_led_esp = MagicMock()
    with patch("system.action_dispatcher.subprocess.Popen", side_effect=OSError("spawn failed")):
        s.execute_action("sys-shut", "shutdown", {})
    assert s.acknowledge.call_args[0][1] == "failed"
    s._turn_off_led_esp.assert_not_called()


def test_agent_command_dispatch_uses_bounded_executors_and_queues():
    """Verify desktop and system agents use bounded ThreadPoolExecutors and handle full queues."""
    import concurrent.futures
    import queue as py_queue
    import agent as desktop_agent_mod
    import boot_agent

    d = desktop_agent_mod.PCControlAgent.__new__(desktop_agent_mod.PCControlAgent)
    d.command_queue = py_queue.Queue(maxsize=1)
    d.remember_command = MagicMock(return_value=True)
    d.acknowledge = MagicMock()
    d.execute_action = MagicMock()
    d.media_manager = MagicMock()
    d.mqtt = MagicMock()

    fut_expire = int(time.time()) + 60
    d.process_command({"id": "c1", "action": "telemetry", "expires": fut_expire, "args": {}})
    d.process_command({"id": "c2", "action": "telemetry", "expires": fut_expire, "args": {}})
    exec_desktop = d._get_action_executor()
    assert isinstance(exec_desktop, concurrent.futures.ThreadPoolExecutor)
    d._shutdown_action_executor()
    assert d._action_executor is None

    msg_cmd_1 = MagicMock(topic=desktop_agent_mod.TOPIC_COMMAND, payload=b'{"id":"q1"}')
    msg_cmd_2 = MagicMock(topic=desktop_agent_mod.TOPIC_COMMAND, payload=b'{"id":"q2"}')
    d.on_message(None, None, msg_cmd_1)
    d.on_message(None, None, msg_cmd_2)
    assert d.command_queue.qsize() == 1

    msg_media = MagicMock(topic=desktop_agent_mod.TOPIC_MEDIA_COMMAND, payload=b'{"action":"play_pause"}')
    d.on_message(None, None, msg_media)
    assert isinstance(d._get_aux_executor(), concurrent.futures.ThreadPoolExecutor)
    d._shutdown_aux_executor()

    sa = boot_agent.SystemAgent.__new__(boot_agent.SystemAgent)
    sa.command_queue = py_queue.Queue(maxsize=1)
    sa.remember_command = MagicMock(return_value=True)
    sa.acknowledge = MagicMock()
    sa.execute_action = MagicMock()
    sa.process_command({"id": "s1", "action": "telemetry", "expires": fut_expire, "args": {}})
    assert isinstance(sa._get_action_executor(), concurrent.futures.ThreadPoolExecutor)
    sa._shutdown_action_executor()

    sa.on_message(None, None, MagicMock(payload=b'{"id":"sq1"}'))
    sa.on_message(None, None, MagicMock(payload=b'{"id":"sq2"}'))
    assert sa.command_queue.qsize() == 1








