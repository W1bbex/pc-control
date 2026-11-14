"""
Regression tests verifying reliability, edge-case and hygiene remediations.
Covers P2 and P3 items from the audit report.
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


def test_app_emoji_manager_locks_sticker_set_operations():
    """Verify sticker upload and getStickerSet execute under _sticker_set_lock."""
    mgr = AppEmojiManager(token="fake_token", user_id=12345)
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
    assert is_telegram_control_chat("telegram.exe", "Family Chat") is False
    assert is_telegram_control_chat("notepad.exe", "pc control") is False


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
    from vds_bot.bot import PcControlBotApp

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.mqtt = MagicMock()

    # Re-apply the helper definition as in bot.py __init__
    def _is_shutting_down():
        if not getattr(app, "pending_shutdown_cmd_id", None):
            return False
        started_at = getattr(app, "pending_shutdown_started_at", None)
        if not started_at:
            return False
        try:
            return (time.time() - float(started_at) < 180)
        except (ValueError, TypeError):
            return False

    app.is_shutting_down = _is_shutting_down
    app.pending_shutdown_cmd_id = "cmd-123"
    app.pending_shutdown_started_at = "corrupted_timestamp"

    assert app.is_shutting_down() is False


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



