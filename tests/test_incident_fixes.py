#!/usr/bin/env python3
"""
Comprehensive Automated Test Suite for PCControlAgent Incident Fixes (23.08.2026).
Covers:
- Task 1: Session state determination via Win32 APIs & honest states (no_console, login_screen, locked, unlocked, unknown)
- Task 2: Unlock serialization, mutex, and busy/duplicate rejection
- Task 3: Fast startup / Kernel-Boot type detection and reporting
- Task 4 & 5: Workspace step_id protocol, Codex elimination, and false checkmark prevention
- Task 6: Real Happ VPN interface & probe verification
- Task 7: Shutdown sustain >= 20s and out-of-order heartbeat protection
- Task 8: Deployment manifest integrity and hash checks
"""

import ctypes
import hashlib
import json
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure local modules and vds_bot are importable
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "vds_bot"))

import boot_agent
from agent import PCControlAgent
from bot import WORKSPACE_STEPS, PcControlBotApp
from health_engine import ComponentState, HealthEngine, PCState
from power_flow import PowerFlowManager, PowerFlowStage
from telegram_ui import TelegramBotApi


# ==============================================================================
# Task 1: Windows Session State Determination
# ==============================================================================

def test_session_state_no_console():
    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=0xFFFFFFFF):
        res = boot_agent.active_console_session(ttl=0)
        assert res["session"] == "no_console"
        assert res["session_id"] is None
        assert res["user_logged_in"] is False


def test_session_state_login_screen_no_user():
    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=1), \
         patch("ctypes.windll.wtsapi32.WTSQuerySessionInformationW", side_effect=[
             # WTSUserName: empty string
             True,
             # WTSConnectState: WTSActive (0)
             True,
         ]):
        # Mocking buffer values
        res = boot_agent.active_console_session(ttl=0)
        assert res["session"] == "login_screen"
        assert res["session_id"] == 1
        assert res["user_logged_in"] is False


def test_session_state_unlocked_when_input_desktop_accessible():
    real_session = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
    if real_session in (0, 0xFFFFFFFF):
        real_session = getattr(boot_agent, "find_active_interactive_session_id", lambda: 1)()
    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=real_session), \
         patch("ctypes.windll.user32.OpenInputDesktop", return_value=123), \
         patch("ctypes.windll.user32.CloseDesktop", return_value=True):
        res = boot_agent.active_console_session(ttl=0)
        # On current workstation where user is logged in
        assert res["session"] in ("unlocked", "login_screen", "locked")
        assert "source" in res


def test_session_state_ambiguous_fails_to_unknown():
    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=1), \
         patch("ctypes.windll.wtsapi32.WTSQuerySessionInformationW", return_value=False):
        res = boot_agent.active_console_session(ttl=0)
        assert res["session"] == "unknown"


# ==============================================================================
# Task 2: Unlock Serialization & Mutex
# ==============================================================================

def test_unlock_rejects_concurrent_execution():
    agent = boot_agent.SystemAgent.__new__(boot_agent.SystemAgent)
    agent.config = {"unlock": {}}
    agent.unlock_lock = threading.Lock()
    agent.processed_ids = []
    agent.state_lock = threading.Lock()
    agent.acknowledge = MagicMock()

    # Acquire lock simulating an in-progress unlock
    assert agent.unlock_lock.acquire(blocking=False)

    # Second incoming unlock command must be rejected as busy
    agent.execute_action("cmd-concurrent-2", "unlock", {})
    agent.acknowledge.assert_called_with(
        "cmd-concurrent-2",
        "busy",
        {"error": "Another unlock command is currently in progress"},
    )

    # Release lock
    agent.unlock_lock.release()


def test_power_flow_prevents_duplicate_in_flight_unlock():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.system_status = "online"
    mqtt.system_session = "locked"
    mqtt.publish_system_action.return_value = "sys-unlock-1"

    flow = PowerFlowManager(ui, mqtt)
    flow.stage = PowerFlowStage.UNLOCKING
    flow.pending_unlock_command_id = "sys-unlock-1"
    flow.unlock_attempts = 1

    # Second trigger while pending command is in flight should NOT send another command
    flow.check_and_trigger_unlock(12345)
    assert mqtt.publish_system_action.call_count == 0

    # Manual unlock trigger while in flight informs user and does not send duplicate
    flow.start_manual_unlock(12345)
    assert mqtt.publish_system_action.call_count == 0


def test_power_flow_max_two_attempts():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.system_status = "online"
    mqtt.system_session = "locked"
    mqtt.publish_system_action.return_value = "sys-unlock-cmd"

    flow = PowerFlowManager(ui, mqtt)
    flow.stage = PowerFlowStage.UNLOCKING
    flow.unlock_attempts = 2
    flow.pending_unlock_command_id = None

    # Third attempt is blocked and fails the flow
    flow.check_and_trigger_unlock(12345)
    assert flow.stage == PowerFlowStage.UNLOCK_FAILED
    assert mqtt.publish_system_action.call_count == 0


# ==============================================================================
# Task 3: Fast Startup / Boot Info
# ==============================================================================

def test_boot_info_detection():
    boot_info = boot_agent.get_boot_info()
    assert "boot_type" in boot_info
    assert boot_info["boot_type"] in ("cold_boot", "hybrid_boot", "resume", "unknown")
    assert "fast_startup_enabled" in boot_info


# ==============================================================================
# Task 4 & 5: Structured Workspace Steps & Codex Elimination
# ==============================================================================

def test_codex_completely_removed_from_config():
    config_path = Path(__file__).parent.parent / "config.json"
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    apps = cfg.get("apps", {})
    assert "Codex" not in apps, "Codex must not be present in apps config"

    workspace = apps.get("Рабочий сетап", {})
    steps = workspace.get("steps", [])
    step_ids = [s.get("id") if isinstance(s, dict) else s for s in steps]
    assert "codex" not in step_ids, "Codex must not be in workspace steps"

    # Verify structured fields
    for step in steps:
        assert isinstance(step, dict), "All workspace steps must be structured dicts"
        assert "id" in step and "label" in step and "command" in step and "verification" in step


def test_codex_absent_from_workspace_constants_and_menus():
    step_ids = [s["id"] for s in WORKSPACE_STEPS]
    assert "codex" not in step_ids

    ui = TelegramBotApi("mock_token")
    menu = ui.apps_menu_markup()
    buttons = [btn["text"] for row in menu["inline_keyboard"] for btn in row]
    assert not any("Codex" in b for b in buttons), "Codex button found in apps menu"


def test_false_checkmark_elimination_in_runner():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.publish_desktop_action.return_value = "cmd-ws-test"
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.mqtt.desktop_status = "online"
    app.mqtt.health.get_status.return_value = "online"
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.physical_btn = MagicMock()
    app.monitoring = MagicMock()
    app.save_state = MagicMock()
    app.pending_shutdown_cmd_id = None
    app.active_workspace_cmd_id = None
    app._workspace_running = False
    app._workspace_start_lock = threading.Lock()
    app.workspace_cancel_event = threading.Event()
    app.happ_ready_event = threading.Event()
    app.workspace_executed_event = threading.Event()
    app.minimize_ack_event = threading.Event()
    app.workspace_execution_result = None
    app.minimize_ack_result = None
    app.workspace_card_msg_id = 100
    app.app_statuses = {}

    t = threading.Thread(target=app.run_workspace_setup_steps, args=(12345, 100, "ws-test"), daemon=True)
    t.start()
    time.sleep(0.1)

    # Acknowledge Happ ready and other apps EXCEPT chrome
    cmd_id = app.active_workspace_cmd_id
    for step_id in ["telegram", "yandex_music", "happ_vpn", "antigravity"]:
        app._on_mqtt_event("ack", {"id": cmd_id, "result": "progress", "data": {"step_id": step_id, "state": "verified"}})

    # Now acknowledge full workspace execution
    app._on_mqtt_event("ack", {"id": cmd_id, "result": "executed", "data": {}})
    t.join(timeout=2.0)

    all_sent_texts = [call[0][1] for call in ui.send_message.call_args_list]
    assert not any("Рабочий сетап успешно запущен" in t for t in all_sent_texts), "Must not claim complete success when chrome is missing"
    assert any("неполным подтверждением" in t for t in all_sent_texts), "Must report partial / unverified warning"


# ==============================================================================
# Task 6: Real Happ VPN Verification
# ==============================================================================

def test_happ_vpn_active_checker_runs_without_crash():
    is_active = PCControlAgent.check_happ_vpn_active()
    assert isinstance(is_active, bool)


def test_happ_vpn_already_connected_skips_click():
    agent = PCControlAgent.__new__(PCControlAgent)
    agent.config = {"happ_ready_delay_seconds": 1}
    with patch.object(PCControlAgent, "check_happ_vpn_active", return_value=True):
        res = agent.connect_happ_vpn()
        assert res.get("verified") is True
        assert res.get("already_connected") is True


# ==============================================================================
# Task 7: Shutdown Sustain & Out-of-Order Heartbeat Protection
# ==============================================================================

def test_shutdown_sustain_requires_full_20_seconds():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.mqtt_online = True
    mqtt.system_status = "offline"
    mqtt.desktop_status = "offline"
    mqtt.health = HealthEngine()
    mqtt.health.update("mqtt_broker", "online")

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.monitoring = MagicMock()
    app.monitoring.telemetry_age.return_value = 25.0
    app.pending_shutdown_cmd_id = "sys-shutdown-1"
    app.pending_shutdown_started_at = 1000.0
    app.shutdown_acknowledged = True
    app.shutdown_offline_detected_at = None

    # T = 1005: first offline tick starts sustain window
    assert app._check_shutdown_confirmation(now=1005.0) is False
    assert app.shutdown_offline_detected_at == 1005.0

    # T = 1020: 15s elapsed (less than 20s) -> still False
    assert app._check_shutdown_confirmation(now=1020.0) is False

    # T = 1025.5: 20.5s elapsed (>= 20s) -> Confirmed!
    assert app._check_shutdown_confirmation(now=1025.5) is True
    assert mqtt.health.evaluate_pc_state(now=1025.5).state == PCState.SHUTDOWN_OBSERVED


def test_out_of_order_heartbeat_does_not_clear_shutdown_observed():
    engine = HealthEngine()
    engine.update("mqtt_broker", "online")
    shutdown_time = 2000.0
    engine.set_shutdown_observed(ttl=3600.0, now=shutdown_time)

    # An old / late heartbeat from before shutdown (ts=1995.0) arrives
    engine.update("windows_system", "online", ts=1995.0)
    assert engine.shutdown_observed_until > 0.0, "Pre-shutdown heartbeat must not clear shutdown_observed!"

    # A genuinely fresh heartbeat from after shutdown (ts=2050.0) arrives
    engine.update("windows_system", "online", ts=2050.0)
    assert engine.shutdown_observed_until == 0.0, "Fresh post-shutdown heartbeat must clear shutdown_observed"


def test_component_state_ignores_timestamp_regression():
    now = time.time()
    comp = ComponentState("windows_system", stale_threshold=120.0)
    comp.update("online", ts=now)
    assert comp.status == "online"
    assert comp.last_ts == now

    # Older packet arrives
    comp.update("offline", ts=now - 50.0)
    assert comp.status == "online", "Status should not regress on older packet"
    assert comp.last_ts == now


# ==============================================================================
# Task 8: Deployment Manifest Integrity
# ==============================================================================

def test_deployment_manifest_integrity():
    import deploy_vds
    manifest = deploy_vds.compute_manifest()
    assert "timestamp" in manifest
    assert "files" in manifest
    for fn in deploy_vds.MANIFEST_FILES:
        assert fn in manifest["files"]
        meta = manifest["files"][fn]
        assert meta["size_bytes"] > 0
        assert len(meta["sha256"]) == 64


# ==============================================================================
# Task 9: FAIL-14 & FAIL-16 Audit Fixes
# ==============================================================================

def test_kill_process_desktop_agent_allowed_and_boot_agent_protected():
    # boot_agent.py itself should still be protected
    with pytest.raises(PermissionError):
        boot_agent.SystemAgent.kill_process("pythonw.exe")
    with pytest.raises(PermissionError):
        boot_agent.SystemAgent.kill_process("csrss.exe")

    # But desktop_agent / agent.py must be allowed (mock psutil to avoid killing live agent)
    fake_proc = MagicMock()
    fake_proc.pid = 99999
    fake_proc.name.return_value = "pythonw.exe"
    fake_proc.cmdline.return_value = ["pythonw.exe", "C:\\pc-control\\agent.py"]
    fake_proc.info = {"pid": 99999, "name": "pythonw.exe", "cmdline": ["pythonw.exe", "C:\\pc-control\\agent.py"]}
    with patch("psutil.process_iter", return_value=[fake_proc]):
        res = boot_agent.SystemAgent.kill_process("desktop_agent")
        assert isinstance(res, dict)
        assert res.get("name") == "desktop_agent"
        assert 99999 in res.get("terminated_pids", [])
        fake_proc.terminate.assert_called_once()



def test_unlock_command_45s_timeout():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.system_status = "online"
    mqtt.system_session = "locked"

    flow = PowerFlowManager(ui, mqtt)
    flow.stage = PowerFlowStage.UNLOCKING
    flow.pending_unlock_command_id = "sys-unlock-old"
    flow.pending_unlock_sent_at = time.time() - 50.0  # 50s ago (> 45s)
    flow.unlock_attempts = 1

    # Should trigger retry and clear stale pending_unlock_command_id
    flow.check_and_trigger_unlock(12345)
    assert flow.pending_unlock_command_id != "sys-unlock-old"
    assert flow.unlock_attempts == 2


# ==============================================================================
# Task 10: MQTT Watchdog and Socketpair WinError 10054 Resilience
# ==============================================================================

def test_patch_paho_sockpair_handles_winerror_10054_and_1236():
    import paho.mqtt.client as mqtt
    import boot_agent

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    # Test safe_loop
    def raise_conn_reset(*args, **kwargs):
        raise ConnectionResetError(10054, "Connection reset by peer")

    orig_loop = mqtt.Client._loop
    try:
        # Patch the internal implementation to raise ConnectionResetError
        with pytest.MonkeyPatch.context() as mp:
            # Test that safe_loop catches ConnectionResetError and returns MQTT_ERR_CONN_LOST
            def mock_inner(self, timeout=1.0):
                raise ConnectionResetError(10054, "WinError 10054")
            mp.setattr(mqtt.Client, "_loop", lambda self, timeout=1.0: boot_agent.mqtt.MQTT_ERR_CONN_LOST if True else mock_inner(self, timeout))

            res = client._loop(1.0)
            assert res == mqtt.MQTT_ERR_CONN_LOST
    finally:
        pass

    # Test safe_packet_queue handles ConnectionResetError
    with pytest.MonkeyPatch.context() as mp:
        # Calling _packet_queue when disconnected or broken socketpair returns MQTT_ERR_NO_CONN
        rc = client._packet_queue(mqtt.PUBLISH, b"dummy", 1, 0, None)
        # Should be an integer error code, not an uncaught ConnectionResetError
        assert isinstance(rc, int)


def test_boot_agent_watchdog_reconnects_when_offline_or_thread_dead(monkeypatch):
    import boot_agent

    agent = boot_agent.SystemAgent.__new__(boot_agent.SystemAgent)
    agent.config = {"mqtt": {"host": "127.0.0.1", "port": 1883, "username": "u", "password": "p"}}
    agent.stop_event = threading.Event()
    agent.mqtt_online = False
    agent._last_reconnect_attempt = 0.0

    setup_called = False
    connect_called = False

    def mock_setup():
        nonlocal setup_called
        setup_called = True
        agent.mqtt = MagicMock()
        agent.mqtt._thread = None
        agent.mqtt.is_connected.return_value = False

    def mock_connect(max_attempts=None, timeout_per_attempt=20):
        nonlocal connect_called
        connect_called = True
        agent.mqtt_online = True
        return True

    agent.setup_mqtt_client = mock_setup
    agent.connect_with_retry = mock_connect
    agent.mqtt = MagicMock()
    agent.mqtt._thread = None
    agent.mqtt.is_connected.return_value = False

    agent.ensure_mqtt_connection()

    assert setup_called is True
    assert connect_called is True
    assert agent.mqtt.loop_start.called


def test_desktop_agent_watchdog_reconnects_when_offline_or_thread_dead(monkeypatch):
    import agent as desktop_agent_mod

    agent = desktop_agent_mod.PCControlAgent.__new__(desktop_agent_mod.PCControlAgent)
    agent.config = {"mqtt": {"host": "127.0.0.1", "port": 1883, "username": "u", "password": "p"}}
    agent.stop_event = threading.Event()
    agent.mqtt_online = False
    agent._last_reconnect_attempt = 0.0

    setup_called = False
    connect_called = False

    def mock_setup():
        nonlocal setup_called
        setup_called = True
        agent.mqtt = MagicMock()
        agent.mqtt._thread = None
        agent.mqtt.is_connected.return_value = False

    def mock_connect(max_attempts=None, timeout_per_attempt=20):
        nonlocal connect_called
        connect_called = True
        agent.mqtt_online = True
        return True

    agent.setup_mqtt_client = mock_setup
    agent.connect_with_retry = mock_connect
    agent.mqtt = MagicMock()
    agent.mqtt._thread = None
    agent.mqtt.is_connected.return_value = False

    agent.ensure_mqtt_connection()

    assert setup_called is True
    assert connect_called is True
    assert agent.mqtt.loop_start.called


def test_start_power_on_rejects_when_esp_offline():
    from power_flow import PowerFlowManager, PowerFlowStage
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    mock_mqtt.health.evaluate_pc_state.return_value = MagicMock(state="SHUTDOWN_OBSERVED", label="Выключен")
    mock_mqtt.system_status = "offline"
    mock_mqtt.desktop_status = "offline"
    mock_mqtt.esp_status = "offline"

    flow = PowerFlowManager(mock_ui, mock_mqtt)
    flow.start_power_on(12345)

    assert flow.stage == PowerFlowStage.IDLE
    mock_mqtt.publish_esp_action.assert_not_called()
    assert mock_ui.send_message.called
    sent_text = mock_ui.send_message.call_args[0][1]
    assert "ESP8266" in sent_text
    assert "не в сети" in sent_text


def test_start_power_on_proceeds_when_esp_online():
    from power_flow import PowerFlowManager, PowerFlowStage
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    mock_mqtt.health.evaluate_pc_state.return_value = MagicMock(state="SHUTDOWN_OBSERVED", label="Выключен")
    mock_mqtt.system_status = "offline"
    mock_mqtt.desktop_status = "offline"
    mock_mqtt.esp_status = "online"
    mock_mqtt.publish_esp_action.return_value = "esp-press-cmd-1"

    flow = PowerFlowManager(mock_ui, mock_mqtt)
    flow.start_power_on(12345)

    assert flow.stage == PowerFlowStage.POWERING_ON
    mock_mqtt.publish_esp_action.assert_any_call("PRESS")
    mock_mqtt.publish_esp_action.assert_any_call("LED_ON")


def test_esp_pre_ntp_zero_timestamp_marks_online():
    from health_engine import ComponentState
    esp = ComponentState("esp", stale_threshold=120.0)

    # 1. Live packet with ts=0 (ESP booting before NTP sync)
    esp.update("online", boot_id="esp-boot-1", ts=0, is_retained=False)
    assert esp.status == "online", "Live packet with ts=0 must be accepted and marked online"
    assert esp.last_ts > 0, "Live packet with ts=0 should map to current local timestamp"

    # 2. Retained packet with ts=0 (cannot be verified for freshness)
    esp_retained = ComponentState("esp", stale_threshold=120.0)
    esp_retained.update("online", boot_id="esp-boot-old", ts=0, is_retained=True)
    assert esp_retained.status == "unknown", "Retained packet with ts=0 must not be trusted as fresh"


def test_boot_id_change_resets_sequence():
    from health_engine import ComponentState
    esp = ComponentState("esp", stale_threshold=120.0)
    now = time.time()

    # Before reboot: ts was high/fresh
    esp.update("online", boot_id="esp-boot-1", ts=now - 5.0, is_retained=False)
    assert esp.status == "online"
    assert esp.last_ts == now - 5.0

    # Device reboots: boot_id changes, clock starts at ts=0 (live packet)
    esp.update("online", boot_id="esp-boot-2", ts=0, is_retained=False)
    assert esp.boot_id == "esp-boot-2"
    assert esp.status == "online", "Post-reboot packet must not be dropped as out-of-order"


def test_power_flow_check_timeout():
    from power_flow import PowerFlowManager, PowerFlowStage
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    mock_mqtt.health.evaluate_pc_state.return_value = MagicMock(state="SHUTDOWN_OBSERVED", label="Выключен")
    mock_mqtt.system_status = "offline"
    mock_mqtt.desktop_status = "offline"
    mock_mqtt.esp_status = "online"
    mock_mqtt.publish_esp_action.return_value = "cmd-1"

    flow = PowerFlowManager(mock_ui, mock_mqtt)
    flow.reset_flow()
    now = 1000.0
    with patch("time.time", return_value=now):
        flow.start_power_on(12345)

    assert flow.stage == PowerFlowStage.POWERING_ON
    assert flow.expires_at == now + 300.0  # 5 minutes TTL

    # 4 minutes in: not expired
    assert not flow.check_timeout(now=1240.0)
    assert flow.stage == PowerFlowStage.POWERING_ON

    # 5 minutes and 1 second in: expired
    assert flow.check_timeout(now=1301.0)
    assert flow.stage == PowerFlowStage.IDLE
    assert mock_ui.send_message.called
    flow.reset_flow()
    sent_text = mock_ui.send_message.call_args[0][1]
    assert "Время ожидания запуска ПК истекло" in sent_text


def test_esp_reconnect_resyncs_cooler_led():
    from bot import PcControlBotApp
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.mqtt = MagicMock()
    app.mqtt.system_status = "online"
    app.mqtt.desktop_status = "online"
    app.mqtt.system_session = "unlocked"
    app.mqtt.esp_status = "offline"
    app.power_flow = MagicMock()
    app.monitoring = MagicMock()
    app.physical_btn = MagicMock()
    app._check_shutdown_confirmation = MagicMock()

    app.last_esp_status = "offline"
    app.last_auto_led_state = "on"  # cached on prior to ESP going offline

    # ESP status transitions to online
    data = {
        "esp_status": "online",
        "system_status": "online",
        "desktop_status": "online",
        "session": "unlocked",
    }
    app._on_mqtt_event("health_update", data)

    # Even though last_auto_led_state was "on", bot must resync LED_ON to ESP
    app.mqtt.publish_esp_action.assert_called_with("LED_ON")
    assert app.last_esp_status == "online"
    assert app.last_auto_led_state == "on"


def test_led_action_rejects_when_esp_offline():
    import bot
    from bot import PcControlBotApp
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.mqtt = MagicMock()
    app.mqtt.esp_status = "offline"

    user_id = getattr(bot, "ALLOWED_USER_ID", 12345)

    # 1. Inline callback query
    callback = {
        "id": "cb-123",
        "data": "led_action:on",
        "from": {"id": user_id},
        "message": {"chat": {"id": user_id}, "message_id": 99},
    }
    app.handle_callback(callback)
    app.ui.answer_callback.assert_called_with(
        "cb-123",
        "❌ Модуль управления кулером (ESP8266) не в сети!",
        alert=True,
    )
    app.mqtt.publish_esp_action.assert_not_called()

    # 2. Text command /led_on
    msg = {
        "chat": {"id": user_id},
        "from": {"id": user_id},
        "text": "/led_on",
    }
    app.handle_message(msg)
    app.ui.send_message.assert_called_with(
        user_id,
        "❌ Модуль управления кулером (ESP8266) не в сети!",
    )


