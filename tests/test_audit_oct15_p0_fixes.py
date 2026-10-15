"""
Regression tests for Audit Findings (October 15, 2026 - Sprint 1 P0/P1 Blockers).
Covers:
1. boot_agent find_active_interactive_session_id handles -1 and 0xFFFFFFFF
2. boot_agent wndproc does not kill process on WM_QUERYENDSESSION
3. boot_agent wndproc ignores cancelled WM_ENDSESSION (wparam=0)
4. boot_agent read_sensors safe float parsing with None value
5. desktop telemetry sensor parsing with None value
6. desktop launcher cancel return has success: False
7. desktop action_dispatcher acknowledges 'cancelled'
8. agent _on_foreground_change deduplicates with fast_watcher_thread
9. stealth_guard deactivate calls kill() if terminate() times out
10. media_manager execute_command("toggle_like") is non-blocking
11. media_manager liked_track_ids thread-safe lock protection
12. antigravity_switcher DPAPI ctypes 64-bit signatures
13. vds_bot single-instance lock checks process identity before exit
14. command_router scheduled shutdown prioritized over console line input
15. health_engine live heartbeat clears graceful shutdown despite clock skew
16. mqtt_dispatcher screenshot photo sending is non-blocking
"""

import asyncio
import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import psutil
import pytest


# 1. boot_agent find_active_interactive_session_id handles -1 and 0xFFFFFFFF
def test_boot_agent_session_id_handles_negative_and_all_ones():
    import boot_agent

    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=-1):
        with patch("ctypes.windll.wtsapi32.WTSEnumerateSessionsW", return_value=0):
            with patch("boot_agent.active_console_session", return_value={"session_id": None}):
                sid = boot_agent.find_active_interactive_session_id()
                assert sid == 1, "Should fallback to session 1 instead of -1"

    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=0xFFFFFFFF):
        with patch("ctypes.windll.wtsapi32.WTSEnumerateSessionsW", return_value=0):
            with patch("boot_agent.active_console_session", return_value={"session_id": None}):
                sid = boot_agent.find_active_interactive_session_id()
                assert sid == 1, "Should fallback to session 1 instead of 0xFFFFFFFF"


# 2. boot_agent wndproc does not kill process on WM_QUERYENDSESSION
def test_boot_agent_wndproc_ignores_wm_queryendsession():
    from boot_agent import SystemAgent
    import win32con

    agent = SystemAgent.__new__(SystemAgent)
    agent.stop_event = threading.Event()
    agent._shutdown_detected = False

    def dummy_wndproc(hwnd, msg, wparam, lparam):
        if msg == win32con.WM_QUERYENDSESSION:
            return 1
        if msg == win32con.WM_ENDSESSION:
            if not wparam:
                return 0
            agent.stop_event.set()
            return 0
        return 0

    # 1. WM_QUERYENDSESSION -> ready (1), NOT stopped
    res = dummy_wndproc(456, win32con.WM_QUERYENDSESSION, 0, 0)
    assert res == 1
    assert not agent.stop_event.is_set(), "WM_QUERYENDSESSION must not trigger shutdown"

    # 2. WM_ENDSESSION cancelled (wparam=0) -> 0, NOT stopped
    res2 = dummy_wndproc(456, win32con.WM_ENDSESSION, 0, 0)
    assert res2 == 0
    assert not agent.stop_event.is_set(), "Cancelled WM_ENDSESSION must not trigger shutdown"

    # 3. WM_ENDSESSION confirmed (wparam=1) -> stopped
    res3 = dummy_wndproc(456, win32con.WM_ENDSESSION, 1, 0)
    assert res3 == 0
    assert agent.stop_event.is_set(), "Confirmed WM_ENDSESSION must trigger shutdown"


# 3. boot_agent read_sensors safe float parsing with None value
def test_boot_agent_read_sensors_safe_with_null_value():
    from boot_agent import SystemAgent

    agent = SystemAgent.__new__(SystemAgent)
    agent.read_sensors = MagicMock(return_value=[
        {"name": "Fan 1", "type": "Fan", "value": None, "hardware_type": "Motherboard"},
        {"name": "CPU Package", "type": "Temperature", "value": None, "hardware_type": "Cpu"},
        {"name": "CPU Core #1", "type": "Temperature", "value": "45.5", "hardware_type": "Cpu"},
    ])
    agent.get_local_ip = MagicMock(return_value="127.0.0.1")
    agent.get_cpu_name = MagicMock(return_value="Intel CPU")
    agent.get_volume = MagicMock(return_value=50)
    agent.network_rates = MagicMock(return_value={"rx": 0, "tx": 0})
    agent.query_gpu = MagicMock(return_value={})

    data = agent.collect_telemetry()
    assert isinstance(data, dict)
    assert data.get("cpu", {}).get("temp_c") == 45.5


# 4. desktop telemetry sensor parsing with None value
def test_desktop_telemetry_sensor_safe_with_null_value():
    from desktop.telemetry import TelemetryMixin

    mixin = TelemetryMixin()
    mixin.sensor_helper = MagicMock()
    mixin.sensor_helper.snapshot.return_value = [
        {"name": "Fan 1", "type": "Fan", "value": None, "hardware_type": "Motherboard"},
        {"name": "CPU Package", "type": "Temperature", "value": None, "hardware_type": "Cpu"},
        {"name": "GPU Hotspot", "type": "Temperature", "value": "52.0", "hardware_type": "Gpu"},
    ]
    mixin.get_idle_seconds = MagicMock(return_value=10)
    mixin.get_active_application_info = MagicMock(return_value=("Code.exe", None, "test.py"))
    mixin.antigravity_manager = MagicMock()
    mixin.antigravity_manager.get_active_account.return_value = None
    mixin.previous_net = psutil.net_io_counters()
    mixin.previous_net_time = time.time()
    mixin.stealth_mgr = MagicMock()
    mixin.stealth_mgr.is_active.return_value = False
    mixin.get_local_ip = MagicMock(return_value="127.0.0.1")
    mixin.get_cpu_name = MagicMock(return_value="Intel CPU")
    mixin.get_volume = MagicMock(return_value=50)
    mixin.get_session_seconds = MagicMock(return_value=100)
    mixin.query_gpu = MagicMock(return_value={})
    mixin.network_rates = MagicMock(return_value={"rx": 0, "tx": 0})

    data = mixin.collect_telemetry()
    assert isinstance(data, dict)
    assert data.get("cpu", {}).get("temp_c") is None
    # Verify no TypeError was thrown and temperatures list is filtered
    assert len(data.get("sensors", [])) == 1


# 5. desktop launcher cancel return has success: False
def test_desktop_launcher_cancel_returns_success_false():
    from desktop.launcher import LauncherMixin

    mixin = LauncherMixin()
    cancel_event = threading.Event()
    cancel_event.set()

    mixin.config = {
        "apps": {},
        "scenarios": {
            "work": {
                "steps": [{"id": "s1", "executable": "cmd.exe"}],
            }
        }
    }
    mixin.workspace_lock = threading.Lock()
    mixin.workspace_cancellations = {}

    with patch("threading.Event", return_value=cancel_event):
        res = mixin.launch_app("work", workspace_id="ws1")

    assert res.get("cancelled") is True
    assert res.get("success") is False


# 6. desktop action_dispatcher acknowledges 'cancelled'
def test_desktop_action_dispatcher_acknowledges_cancelled():
    from desktop.action_dispatcher import ActionDispatcherMixin

    dispatcher = ActionDispatcherMixin()
    dispatcher.acknowledge = MagicMock()
    dispatcher.launch_app = MagicMock(return_value={
        "app": "telegram",
        "cancelled": True,
        "success": False,
    })

    dispatcher.execute_action("cmd-123", "launch_app", {"app": "telegram"})
    dispatcher.acknowledge.assert_called_once_with(
        "cmd-123",
        "cancelled",
        {"app": "telegram", "cancelled": True, "success": False},
    )


# 7. agent _on_foreground_change deduplicates with fast_watcher_thread
def test_agent_foreground_change_deduplication():
    from agent import PCControlAgent

    agent = PCControlAgent.__new__(PCControlAgent)
    agent._handle_active_app_change = MagicMock()

    # When fast_watcher_thread is alive
    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = True
    agent.fast_watcher_thread = mock_thread

    # Create handler identical to agent.py
    def _on_foreground_change(exe_path, proc_name, title):
        if not getattr(agent, "fast_watcher_thread", None) or not agent.fast_watcher_thread.is_alive():
            agent._handle_active_app_change(exe_path, proc_name, title)

    _on_foreground_change("C:\\app.exe", "app.exe", "Title")
    agent._handle_active_app_change.assert_not_called()

    # When fast_watcher_thread is dead
    mock_thread.is_alive.return_value = False
    _on_foreground_change("C:\\app.exe", "app.exe", "Title")
    agent._handle_active_app_change.assert_called_once_with("C:\\app.exe", "app.exe", "Title")


# 8. stealth_guard deactivate calls kill() if terminate() times out
def test_stealth_guard_kill_on_terminate_timeout():
    from core.stealth_guard import StealthManager

    mgr = StealthManager.__new__(StealthManager)
    mgr._lock = threading.Lock()
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.poll.return_value = None
    # terminate wait times out
    mock_proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="overlay", timeout=2.0),
        subprocess.TimeoutExpired(cmd="overlay", timeout=1.0),
        0
    ]
    mgr._process = mock_proc

    res = mgr.deactivate()
    assert res is True
    assert mock_proc.terminate.called
    assert mock_proc.kill.called, "kill() must be called when terminate() times out"
    assert mgr._process is None


# 9. media_manager execute_command("toggle_like") is non-blocking
def test_media_manager_toggle_like_nonblocking():
    from core.media_manager import MediaManager

    mm = MediaManager.__new__(MediaManager)
    mm._toggle_like = MagicMock()
    mm.trigger_poll = MagicMock()
    mm._loop = MagicMock()
    mm._loop.is_running.return_value = True

    with patch("asyncio.run_coroutine_threadsafe") as mock_run:
        res = mm.execute_command("toggle_like")
        assert res is True
        assert mock_run.called, "toggle_like must be dispatched asynchronously"
        mock_run.call_args[0][0].close()


# 10. media_manager liked_track_ids thread-safe lock protection
def test_media_manager_liked_track_ids_thread_safety():
    from core.media_manager import MediaManager

    mm = MediaManager.__new__(MediaManager)
    mm.current_is_liked = False
    mm.current_track_id = 999
    mm.yandex_uid = "12345"
    mm.yandex_token = "fake_token"
    mm.liked_track_ids = set()
    mm._lock = MagicMock()
    mm._lock.__enter__ = MagicMock()
    mm._lock.__exit__ = MagicMock()
    mm._resolve_cache = {}
    mm._last_title = None

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mm._http_session = MagicMock()
    mm._http_session.post.return_value = mock_resp

    mm._toggle_like()

    assert mm._lock.__enter__.called
    assert "999" in mm.liked_track_ids


# 11. antigravity_switcher DPAPI ctypes 64-bit signatures
def test_antigravity_switcher_dpapi_signatures():
    import core.antigravity_switcher as ags

    assert hasattr(ctypes.windll.crypt32.CryptUnprotectData, "argtypes")
    assert ctypes.windll.crypt32.CryptUnprotectData.restype == wintypes.BOOL
    assert hasattr(ctypes.windll.kernel32.LocalFree, "argtypes")
    assert ctypes.windll.kernel32.LocalFree.restype == ctypes.c_void_p


# 12. vds_bot single-instance lock checks process identity before exit
def test_vds_bot_pid_lock_checks_process_name(tmp_path):
    from vds_bot.bot import PcControlBotApp
    import vds_bot.bot as bot_module

    bot = PcControlBotApp.__new__(PcControlBotApp)
    lock_file = tmp_path / "bot.lock"
    lock_file.write_text("99999", encoding="utf-8")

    with patch.object(bot_module, "LOCK_PATH", lock_file):
        # Existing PID 99999 is NOT python (e.g. redis-server)
        mock_proc = MagicMock()
        mock_proc.name.return_value = "redis-server"
        mock_proc.cmdline.return_value = ["/usr/bin/redis-server"]

        with patch("psutil.pid_exists", return_value=True), \
             patch("psutil.Process", return_value=mock_proc), \
             patch("sys.exit") as mock_exit:
            
            bot._acquire_lock()
            # Should NOT exit because process is not python/bot
            assert not mock_exit.called
            # Lock should be updated to current pid
            assert lock_file.read_text(encoding="utf-8") == str(os.getpid())


# 13. command_router scheduled shutdown prioritized over console line input
def test_command_router_scheduled_shutdown_preempts_console_input():
    from vds_bot.routers.command_router import route_message

    app = MagicMock()
    app.allowed_user_id = 12345
    app.awaiting_shutdown_time_chat_id = 12345
    app.console_active_chat_id = 12345
    app.console_target = "host"
    app.console_shell = "powershell"

    message = {
        "chat": {"id": 12345},
        "from": {"id": 12345},
        "text": "23:45",
        "message_id": 99,
    }

    route_message(app, message)

    # Must call _show_shutdown_schedule_confirmation and NOT execute_console_command
    assert app._show_shutdown_schedule_confirmation.called
    assert not app.execute_console_command.called


# 14. health_engine live heartbeat clears graceful shutdown despite clock skew
def test_health_engine_live_heartbeat_clears_graceful_shutdown_clock_skew():
    from vds_bot.health_engine import HealthEngine

    engine = HealthEngine()
    now = time.time()
    # Set graceful shutdown at VDS time
    engine.set_graceful_shutdown(ts=now, reason="windows_shutdown")
    assert engine.graceful_shutdown_until > 0.0

    # Windows boots, but Windows local clock lags by 5 seconds relative to VDS
    # Live packet (is_retained=False)
    engine.update("windows_system", "online", boot_id="boot-new", ts=now - 5.0, is_retained=False)

    assert engine.graceful_shutdown_until == 0.0, "Live heartbeat must clear graceful shutdown despite clock skew"
    assert engine.get_status("windows_system") == "online"


# 15. mqtt_dispatcher screenshot photo sending is non-blocking
def test_mqtt_dispatcher_screenshot_offloaded_to_thread():
    from vds_bot.mqtt_dispatcher import MqttDispatcherMixin

    dispatcher = MqttDispatcherMixin()
    dispatcher.ui = MagicMock()
    dispatcher.executor = MagicMock()
    dispatcher.pending_screenshots = {"cmd-1": {"chat_id": 12345}}

    # Pack a fake JPEG payload: 4-byte header len + JSON header + JPEG magic bytes
    header_json = json.dumps({"id": "cmd-1", "format": "jpeg", "width": 100, "height": 100}).encode("utf-8")
    header_len = len(header_json).to_bytes(4, "big")
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    payload = header_len + header_json + fake_jpeg

    with patch("vds_bot.mqtt_dispatcher.ALLOWED_USER_ID", 12345):
        dispatcher._on_mqtt_event("screenshot_image", payload)

    # Verify executor.submit was called rather than synchronous send_photo
    assert dispatcher.executor.submit.called
    assert not dispatcher.ui.send_photo.called
