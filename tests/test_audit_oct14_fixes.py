"""
Regression tests for Audit Findings (October 14, 2026 - Basket 1).
Covers:
- media_manager asyncio.to_thread offload for toggle_like
- stealth_guard socket cleanup on connection error
- antigravity_switcher wait_procs on kill_ide and env secret override
- activity_logger None-guard for IP addresses
- launcher & vpn_manager ShellExecuteW 'runas' on WinError 740
- vpn_manager 64-bit ctypes declarations for desktops
- sensors dead attribute removal
- bot.py single-instance locking and complete setMyCommands
- power_flow startup safety in load_state
- health_engine clock step detection and sequence reset
- physical_button mode names fix
- command_router exact token match for /esc
- monitoring_router filename='chart.png' in send_photo
"""

import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest


# 1. media_manager toggle_like asyncio.to_thread
def test_media_manager_toggle_like_is_async_thread_offloaded():
    from core.media_manager import MediaManager
    mm = MediaManager.__new__(MediaManager)
    mm._toggle_like = MagicMock(return_value=True)

    res = asyncio.run(mm._execute_control_async("toggle_like"))
    assert res is True
    assert mm._toggle_like.called


# 2. stealth_guard socket cleanup
def test_stealth_guard_check_network_connectivity_closes_socket_on_error():
    from core.stealth_guard import check_network_connectivity

    mock_sock = MagicMock()
    mock_sock.connect.side_effect = socket.timeout("Timed out")

    with patch("core.stealth_guard.socket.socket", return_value=mock_sock):
        connected = check_network_connectivity(timeout=0.1)
        assert connected is False
        assert mock_sock.close.called


# 3. antigravity_switcher wait_procs on kill_ide
def test_antigravity_switcher_kill_ide_waits_for_killed_procs():
    from core.antigravity_switcher import AntigravityAccountManager
    mock_proc1 = MagicMock()
    mock_proc1.info = {"name": "antigravity.exe"}
    mock_proc2 = MagicMock()
    mock_proc2.info = {"name": "antigravity.exe"}

    # First call: proc1 gone, proc2 alive
    # Second call (after kill): proc2 gone
    with patch("core.antigravity_switcher.psutil.process_iter", return_value=[mock_proc1, mock_proc2]), \
         patch("core.antigravity_switcher.psutil.wait_procs", side_effect=[
             ([mock_proc1], [mock_proc2]),
             ([mock_proc2], []),
         ]) as mock_wait:
        AntigravityAccountManager.kill_ide()
        assert mock_proc2.kill.called
        assert mock_wait.call_count == 2


# 4. antigravity_switcher client secret from env
def test_antigravity_switcher_client_secret_env_override(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_CLIENT_SECRET", "custom-secret-123")
    import importlib
    import core.antigravity_switcher
    importlib.reload(core.antigravity_switcher)
    assert core.antigravity_switcher.DEFAULT_CLIENT_SECRET == "custom-secret-123"


# 5. activity_logger None-guard for interface address
def test_activity_logger_network_state_handles_none_address():
    from core.activity_logger import ActivityTracker

    class MockAddr:
        family = socket.AF_INET
        address = None

    class MockStat:
        isup = True

    with patch("core.activity_logger.psutil.net_if_stats", return_value={"eth0": MockStat()}), \
         patch("core.activity_logger.psutil.net_if_addrs", return_value={"eth0": [MockAddr()]}):
        tracker = ActivityTracker.__new__(ActivityTracker)
        state = tracker._network_state()
        assert state == []


# 6. launcher ShellExecuteW 'runas' on WinError 740
def test_launcher_winerror_740_elevates_via_shellexecute():
    from desktop.launcher import LauncherMixin

    class TestApp(LauncherMixin):
        def __init__(self):
            self.config = {"apps": {"DummyApp": r"C:\Tools\tool.exe"}}

    app = TestApp()
    err_740 = OSError()
    err_740.winerror = 740

    mock_running_proc = MagicMock()
    mock_running_proc.info = {"name": "tool.exe"}

    with patch("desktop.launcher.shutil.which", return_value=r"C:\Tools\tool.exe"), \
         patch("desktop.launcher.Path.exists", return_value=True), \
         patch("subprocess.Popen", side_effect=err_740), \
         patch("desktop.launcher.psutil.process_iter", side_effect=[[], [mock_running_proc]]), \
         patch("ctypes.windll.shell32.ShellExecuteW", return_value=42) as mock_shell:
        res = app.launch_app("DummyApp", r"C:\Tools\tool.exe")
        assert res.get("success") is True
        mock_shell.assert_called_once()
        assert mock_shell.call_args[0][1] == "runas"


# 7. vpn_manager connect_incy_vpn ShellExecuteW 'runas' on WinError 740
def test_vpn_manager_connect_incy_vpn_elevates_on_740():
    from desktop.vpn_manager import VpnManagerMixin

    class TestVpn(VpnManagerMixin):
        def __init__(self):
            self.config = {"apps": {"INCY": r"C:\Program Files\INCY\incy.exe"}}

    vpn = TestVpn()
    err_740 = OSError()
    err_740.winerror = 740

    with patch.object(vpn, "check_incy_vpn_active", side_effect=[False, True]), \
         patch("desktop.vpn_manager.psutil.process_iter", return_value=[]), \
         patch("desktop.vpn_manager.Path.exists", return_value=True), \
         patch("subprocess.Popen", side_effect=err_740), \
         patch("ctypes.windll.shell32.ShellExecuteW", return_value=42) as mock_shell:
        res = vpn.connect_incy_vpn(timeout=2)
        assert res.get("verified") is True
        mock_shell.assert_called_once()
        assert mock_shell.call_args[0][1] == "runas"


# 8. vpn_manager 64-bit desktop types
def test_vpn_manager_desktop_types_are_handles():
    import desktop.vpn_manager
    user32 = ctypes.windll.user32
    assert user32.GetThreadDesktop.restype == wintypes.HANDLE
    assert user32.OpenInputDesktop.restype == wintypes.HANDLE
    assert user32.SetThreadDesktop.argtypes == [wintypes.HANDLE]
    assert user32.SetThreadDesktop.restype == wintypes.BOOL


# 9. sensors dead attribute removed
def test_sensors_dead_attribute_removed(tmp_path):
    from desktop.sensors import SensorHelper
    helper = SensorHelper(tmp_path / "dummy.exe")
    assert not hasattr(helper, "admin_snapshot")


# 10. bot single-instance locking and release
def test_bot_single_instance_lock(tmp_path, monkeypatch):
    from vds_bot.bot import PcControlBotApp
    lock_file = tmp_path / "bot.lock"
    monkeypatch.setattr("vds_bot.bot.LOCK_PATH", lock_file)

    app1 = PcControlBotApp.__new__(PcControlBotApp)
    app1._acquire_lock()
    assert lock_file.exists()
    assert lock_file.read_text().strip() == str(os.getpid())

    app1._release_lock()
    assert not lock_file.exists()


# 11. bot register_commands includes all required commands
def test_bot_register_commands_includes_all_slash_commands():
    from vds_bot.bot import PcControlBotApp
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()

    app.register_commands()
    assert app.ui.call.called
    call_args = app.ui.call.call_args
    assert call_args[0][0] == "setMyCommands"
    registered_cmds = [c["command"] for c in call_args[0][1]["commands"]]
    for expected in ("menu", "player", "stealth", "activity", "antigravity", "esc", "console", "cmd", "ps", "shutdown", "start"):
        assert expected in registered_cmds


# 12. power_flow startup safety in load_state
def test_power_flow_load_state_handles_notification_error(tmp_path, monkeypatch):
    from vds_bot.power_flow import PowerFlowManager, PowerFlowStage
    state_file = tmp_path / "flow_state.json"
    state_data = {
        "stage": "UNLOCKING",
        "chat_id": 12345,
        "message_id": 67890,
        "expires_at": time.time() + 300,
    }
    state_file.write_text(json.dumps(state_data))
    monkeypatch.setattr("vds_bot.power_flow.FLOW_STATE_PATH", state_file)

    fake_ui = MagicMock()
    fake_ui.send_message.side_effect = ConnectionError("Telegram API down")
    fake_mqtt = MagicMock()

    flow = PowerFlowManager(fake_ui, fake_mqtt)
    # Should not crash and stage should be UNLOCK_FAILED
    assert flow.stage == PowerFlowStage.UNLOCK_FAILED


# 13. health_engine boot_id reboot reset
def test_health_engine_boot_id_resets_sequence_on_reboot():
    from vds_bot.health_engine import ComponentState
    comp = ComponentState("telemetry", stale_threshold=60)
    now = time.time()

    comp.update("fresh", ts=now, boot_id="boot-1")
    assert comp.status == "fresh"
    assert comp.last_ts == now

    # Machine rebooted: boot_id changed to boot-2, and timestamp is slightly smaller
    reboot_ts = now - 5.0
    comp.update("fresh", ts=reboot_ts, boot_id="boot-2")
    assert comp.boot_id == "boot-2"
    assert comp.last_ts == reboot_ts
    assert comp.status in ("fresh", "stale")


# 14. physical_button mode names check
def test_physical_button_failed_event_suppressed_in_test_mode():
    from vds_bot.physical_button import PhysicalButtonManager
    fake_ui = MagicMock()
    fake_mqtt = MagicMock()
    mgr = PhysicalButtonManager(fake_ui, fake_mqtt)

    mgr.mode = "test"
    mgr.handle_event({"event": "failed", "failed_attempts": 1}, 12345)
    assert fake_mqtt.publish_esp_action.called
    assert fake_mqtt.publish_esp_action.call_args[0][0] == "UNLOCK_PHYSICAL_INPUT"
    assert not fake_ui.send_message.called


# 15. command_router exact esc token match
def test_command_router_esc_does_not_trigger_on_words():
    from vds_bot.routers.command_router import route_message
    app = MagicMock()
    app.allowed_user_id = 12345

    # Word 'escape' should not trigger ESC
    route_message(app, {"chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}, "text": "escape from tarkov"})
    assert not app.mqtt.publish_desktop_action.called

    # Exact '/esc 3' should trigger ESC
    route_message(app, {"chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}, "text": "/esc 3"})
    assert app.mqtt.publish_desktop_action.called
    assert app.mqtt.publish_desktop_action.call_args[0][1] == {"count": 3}


# 16. monitoring_router chart send_photo filename
def test_monitoring_router_sends_chart_with_filename():
    from vds_bot.routers.monitoring_router import route_monitoring_callback
    app = MagicMock()
    app.monitoring = MagicMock()
    app.monitoring.history.render_chart.return_value = (b"\x89PNG\r\n\x1a\nfake", 10)

    with patch("vds_bot.routers.monitoring_router.threading.Thread") as mock_thread:
        route_monitoring_callback(app, "history:24h", 12345, 999, "cb_id")
        # Run thread target directly
        thread_target = mock_thread.call_args[1]["target"]
        thread_target()

        assert app.ui.send_photo.called
        kwargs = app.ui.send_photo.call_args[1]
        assert kwargs.get("filename") == "chart.png"
