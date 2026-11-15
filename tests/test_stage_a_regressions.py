"""
Regression tests verifying core process management, network fault tolerance and IPC contracts.
"""

import asyncio
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent
import boot_agent
from core.antigravity_switcher import AntigravityAccountManager
from core.media_manager import MediaManager
from core.process_utils import shell_execute_runas
from core.stealth_guard import StealthOverlay
from desktop.launcher import LauncherMixin
from desktop.telemetry import TelemetryMixin
from desktop.vpn_manager import VpnManagerMixin
from scripts.type_password import _switch_layout_us_english
from vds_bot.console_service import ConsoleServiceMixin
from vds_bot.monitoring import MonitoringManager
from vds_bot.power_scheduler import PowerSchedulerMixin
from vds_bot.routers.workspace_router import route_workspace_callback


def test_monitoring_media_escape_does_not_break_entities():
    """Verify truncating long track names does not break HTML entities like &amp;."""
    mgr = MonitoringManager.__new__(MonitoringManager)
    mgr.mqtt = MagicMock()
    mgr.mqtt.health.evaluate_pc_state.return_value = MagicMock(state="READY", label="В сети")
    mgr.latest_telemetry = {"cpu": {"load": 10}, "timestamp": int(time.time())}
    mgr.latest_received_at = time.time()
    mgr.is_shutting_down = lambda: False
    mgr.get_os_type = lambda t=None: "windows"
    mgr.header_os_tag = lambda t=None: "🪟"

    long_title = "A & B & C & D & E & F & G & H & I & J & K & L & M & N & O & P & Q"
    media_state = {
        "active": True,
        "title": long_title,
        "artist": "Artist & Band <Rock>",
        "is_playing": True,
        "is_liked": True,
    }
    card = mgr.dashboard_text(media_state=media_state)
    assert "&amp;" in card
    # Should not contain severed entity fragments like &am or &a
    assert "&am " not in card
    assert "& " not in card


def test_console_service_preview_escape_does_not_break_entities():
    """Verify large console output preview does not cut HTML entities in half."""
    ui = MagicMock()
    class DummyConsole(ConsoleServiceMixin):
        def __init__(self, ui):
            self.ui = ui

    service = DummyConsole(ui=ui)
    full_output = "<tag>&amp;special_chars&amp;</tag>\n" * 300
    service._handle_console_ack(
        pending_cmd={"chat_id": 123, "command": "test", "target": "desktop", "shell": "cmd"},
        data={"data": {"stdout": full_output, "exit_code": 0}},
    )

    assert ui.send_message.called
    sent_text = ui.send_message.call_args[0][1]
    assert "<pre><code>" in sent_text
    assert "&amp;" in sent_text
    assert "&am " not in sent_text


def test_boot_agent_mqtt_auth_optional():
    """Verify boot_agent initializes MQTT client safely when username/password are omitted."""
    fake_config = {
        "mqtt": {"host": "localhost", "port": 8883},
    }
    sys_agent = boot_agent.SystemAgent.__new__(boot_agent.SystemAgent)
    sys_agent.config = fake_config
    with patch("paho.mqtt.client.Client.tls_set"):
        sys_agent.setup_mqtt_client()
        assert sys_agent.mqtt is not None


def test_codex_launch_handles_bare_executable_without_dir(monkeypatch):
    """Verify codex_launch passes cwd=None instead of empty string for bare executable names."""
    from desktop import codex_launch

    called_cwd = []

    def fake_popen(cmd, cwd=None, **kwargs):
        called_cwd.append(cwd)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        return mock_proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(codex_launch, "_codex_processes", lambda: [])

    with patch("os.path.isfile", return_value=True):
        res = codex_launch.diagnose_codex_launch(variant="1", alias="chatgpt.exe", has_visible_window=lambda names: True, timeout=0.1)
        assert res.get("codex_diagnostic") is True
        assert called_cwd == [None]


def test_shell_execute_runas_success_and_failure(monkeypatch):
    """Verify shell_execute_runas handles 64-bit HINSTANCE returns correctly."""
    mock_shell32 = MagicMock()
    # Success handle with high bit set in 32-bit (e.g. 0x80000040)
    mock_shell32.ShellExecuteW.return_value = 0x80000040
    monkeypatch.setattr("ctypes.windll.shell32", mock_shell32)

    ret = shell_execute_runas("test.exe")
    assert ret > 32

    # Error code (e.g. 5 = Access Denied)
    mock_shell32.ShellExecuteW.return_value = 5
    ret_err = shell_execute_runas("test.exe")
    assert ret_err <= 32


def test_workspace_router_blocks_when_status_is_none():
    """Verify workspace router rejects launch when session status is None or locked."""
    app = MagicMock()
    app.mqtt.desktop_status = None
    app.mqtt.system_session = None

    handled = route_workspace_callback(app, "workspace_vpn:incy", chat_id=1, message_id=10, callback_id="cb1")
    assert handled is True
    assert app.ui.answer_callback.called
    toast_msg = app.ui.answer_callback.call_args[0][1]
    assert "заблокирована" in toast_msg

    app.mqtt.desktop_status = "online"
    app.mqtt.system_session = "locked"
    handled_locked = route_workspace_callback(app, "workspace_vpn:incy", chat_id=1, message_id=10, callback_id="cb2")
    assert handled_locked is True
    toast_locked = app.ui.answer_callback.call_args[0][1]
    assert "заблокирована" in toast_locked


def test_telemetry_get_idle_seconds_tick_count_handling(monkeypatch):
    """Verify get_idle_seconds uses unsigned 64-bit ticks without integer overflow."""
    mixin = TelemetryMixin()
    mock_user32 = MagicMock()
    # Simulate GetLastInputInfo returning dwTime = 10000
    def fake_get_last_input(info_ptr):
        info_ptr._obj.dwTime = 10000
        return 1

    mock_user32.GetLastInputInfo = fake_get_last_input
    mock_kernel32 = MagicMock()
    # 25 days uptime in ms: ~2,160,000,000 ms (> 2^31 - 1)
    mock_kernel32.GetTickCount64.return_value = 2160000000
    mock_kernel32.GetTickCount.return_value = -2134967296  # overflowed signed 32-bit

    monkeypatch.setattr("ctypes.windll.user32", mock_user32)
    monkeypatch.setattr("ctypes.windll.kernel32", mock_kernel32)

    idle = mixin.get_idle_seconds()
    assert idle is not None
    assert idle > 0


def test_launcher_respects_connect_vpn_false_for_incy():
    """Verify launcher does not invoke INCY VPN when connect_vpn=False."""
    class DummyLauncher(LauncherMixin):
        def __init__(self):
            self.config = {
                "apps": {"INCY": "C:\\Program Files\\INCY\\incy.exe"},
                "scenarios": {},
            }
            self.connect_incy_vpn = MagicMock()

    dl = DummyLauncher()
    with patch("shutil.which", return_value="C:\\Program Files\\INCY\\incy.exe"), patch(
        "pathlib.Path.exists", return_value=True
    ), patch("subprocess.Popen") as mock_popen, patch("psutil.process_iter", return_value=[]):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        dl.launch_app("INCY", connect_vpn=False)
        dl.connect_incy_vpn.assert_not_called()


def test_stealth_overlay_raises_if_hooks_fail():
    """Verify StealthOverlay aborts and raises RuntimeError when low-level hooks fail."""
    overlay = StealthOverlay.__new__(StealthOverlay)
    overlay._mouse_hook = None
    overlay._keyboard_hook = None
    overlay._cleanup = MagicMock()

    with patch("ctypes.windll.user32.SetWindowsHookExW", return_value=None):
        with pytest.raises(RuntimeError, match="Failed to install low-level"):
            overlay.run()
        overlay._cleanup.assert_called_once()


def test_antigravity_find_best_account_checks_primary_quota():
    """Verify account with high quota on primary model is selected despite 0% on secondary model."""
    mgr = AntigravityAccountManager.__new__(AntigravityAccountManager)
    mgr.list_accounts = MagicMock(return_value=[
        {
            "id": 1,
            "status": "active",
            "has_valid_token": True,
            "has_quota_data": True,
            "min_quota": 0,  # min over all models is 0 because old flash model expired
            "models_quota": {
                "gemini-3.1-pro-high": {"percentage": 100},
                "gemini-legacy": {"percentage": 0},
            },
            "is_active": False,
            "last_used": 0,
        }
    ])

    best = mgr.find_best_account(preferred_model="gemini-3.1-pro-high")
    assert best is not None
    assert best["id"] == 1


def test_antigravity_refresh_token_refuses_expired_token_on_network_failure():
    """Verify refresh_token_if_needed raises when token is expired and network call fails."""
    mgr = AntigravityAccountManager.__new__(AntigravityAccountManager)
    mgr.crypto = MagicMock()
    # Token expired 10 seconds ago
    expired_token = {
        "access_token": "old_expired_tok",
        "refresh_token": "valid_refresh_tok",
        "expiry_timestamp": int(time.time()) - 10,
    }
    mgr.crypto.decrypt_payload.return_value = expired_token

    with patch("urllib.request.urlopen", side_effect=OSError("Network unreachable")):
        with pytest.raises(OSError):
            mgr.refresh_token_if_needed({"email": "test@gmail.com", "token_json": "{}"})


def test_media_manager_current_state_updates_under_lock():
    """Verify get_current_state updates track title atomically after resolution."""
    mm = MediaManager.__new__(MediaManager)
    mm._lock = threading.Lock()
    mm._get_lock = lambda: mm._lock
    mm._last_title = "Old Song"
    mm._last_artist = "Old Artist"
    mm._last_duration = 180
    mm._last_playing = True
    mm._last_raw_position = 10.0
    mm._anchor_time = time.time()
    mm._anchor_pos = 10.0
    mm.liked_track_ids = set()

    mm._get_manager = AsyncMock(return_value=MagicMock())
    mock_session = MagicMock()
    mock_props = MagicMock()
    mock_props.title = "New Song"
    mock_props.artist = "New Artist"
    mock_props.album_title = ""
    mock_session.try_get_media_properties_async = AsyncMock(return_value=mock_props)
    mock_session.source_app_user_model_id = "test.app"
    mock_session.get_playback_info.return_value = MagicMock(playback_status=4)
    mock_session.get_timeline_properties.return_value = MagicMock(position=None, end_time=None)
    mm._get_target_session = MagicMock(return_value=mock_session)

    mm._resolve_track = MagicMock(return_value=("Resolved New", "Resolved Artist", 999, False, None))

    state = asyncio.run(mm.get_current_state())
    assert state["title"] == "Resolved New"
    assert mm._last_title == "New Song"


def test_power_scheduler_tick_triggers_when_desktop_online():
    """Verify scheduler_tick sends shutdown when desktop agent is online even if system is offline."""
    class DummyApp(PowerSchedulerMixin):
        def __init__(self):
            self.running = False
            self.scheduled_shutdown_ts = time.time() - 5
            self.last_schedule_attempt_at = 0.0
            self.monitoring = SimpleNamespace(telemetry_age=lambda: 5.0)
            self.mqtt = SimpleNamespace(
                system_status="offline",
                desktop_status="online",
                health=SimpleNamespace(evaluate_pc_state=lambda age, now=None: SimpleNamespace(state="READY")),
            )
            self.ui = MagicMock()
            self.save_state = MagicMock()
            self._request_shutdown = MagicMock(return_value=True)

    app = DummyApp()
    app.scheduler_tick()
    app._request_shutdown.assert_called_once()


def test_vpn_manager_switch_server_returns_immediately_when_exe_missing():
    """Verify switch_next_incy_server returns error immediately without 25s polling when exe missing."""
    mgr = VpnManagerMixin()
    mgr._get_incy_lock = lambda: threading.Lock()
    mgr.get_incy_servers = MagicMock(return_value=[{"id": "101", "name": "NL-1", "latency": 50}])
    mgr._stop_incy_processes = MagicMock()
    mgr._load_incy_preferences = MagicMock(return_value={"selectedServerId": "101"})
    mgr._write_incy_preferences_atomic = MagicMock()
    mgr.check_incy_vpn_active = MagicMock(return_value=False)
    mgr.config = {"apps": {"INCY": "C:\\Missing\\incy.exe"}}

    start = time.monotonic()
    with patch("pathlib.Path.exists", return_value=False):
        res = mgr.switch_next_incy_server(target_server_id="101")
        elapsed = time.monotonic() - start
        assert elapsed < 2.0
        assert res["success"] is False
        assert "not found" in res["error"]


def test_launcher_verifies_configured_targets():
    """Verify launcher verifies that configured target process names are present."""
    class DummyLauncher(LauncherMixin):
        def __init__(self):
            self.config = {
                "scenarios": {
                    "test_scen": {
                        "steps": [{
                            "id": "test_app",
                            "label": "Test App",
                            "command": ["launch.exe"],
                            "verification": {"type": "process_name", "targets": ["target_app.exe"], "timeout": 0.1},
                        }]
                    }
                }
            }

    dl = DummyLauncher()
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # Wrapper process is still alive

    # But psutil process_iter does NOT have target app
    with patch("psutil.process_iter", return_value=[]), patch("time.sleep"), patch(
        "subprocess.Popen", return_value=fake_proc
    ), patch("shutil.which", return_value="C:\\launch.exe"):
        res = dl.launch_app("test_scen")
        # Since target_app.exe is not in psutil, it must fail verification
        assert res["success"] is False
        assert "test_app" not in res.get("verified_steps", [])
        assert res.get("failed_step") == "test_app"


def test_launcher_registers_cancel_event_for_single_app():
    """Verify launcher registers cancel_event in workspace_cancellations for single app launch."""
    class DummyLauncher(LauncherMixin):
        def __init__(self):
            self.config = {"apps": {"TestApp": "C:\\test.exe"}}
            self.workspace_lock = threading.Lock()
            self.workspace_cancellations = {}

    dl = DummyLauncher()
    registered = []

    with patch("shutil.which", return_value="C:\\test.exe"), patch("pathlib.Path.exists", return_value=True), patch(
        "subprocess.Popen"
    ) as mock_popen, patch("psutil.process_iter", return_value=[]):
        def check_cancellation(*args, **kwargs):
            registered.append(list(dl.workspace_cancellations.keys()))
            m = MagicMock()
            m.poll.return_value = 0
            return m

        mock_popen.side_effect = check_cancellation
        dl.launch_app("TestApp", workspace_id="ws_single_99")

    assert "ws_single_99" in registered[0]
    # In finally block, it cleans up
    assert "ws_single_99" not in dl.workspace_cancellations


def test_type_password_switch_layout_toggles_when_not_english(monkeypatch):
    """Verify _switch_layout_us_english sends Alt+Shift if foreground window has Russian layout."""
    mock_user32 = MagicMock()
    mock_user32.LoadKeyboardLayoutW.return_value = 0x04090409
    mock_user32.GetForegroundWindow.return_value = 9999
    mock_user32.GetWindowThreadProcessId.return_value = 8888
    # Russian layout: 0x04190419
    mock_user32.GetKeyboardLayout.return_value = 0x04190419

    key_events = []
    mock_user32.keybd_event = lambda vk, scan, flags, extra: key_events.append((vk, flags))

    monkeypatch.setattr("scripts.type_password.user32", mock_user32)

    _switch_layout_us_english()
    # Should have sent Alt (0x12) and Shift (0x10) keybd_events
    vks = [ev[0] for ev in key_events]
    assert 0x12 in vks
    assert 0x10 in vks


def test_agent_fail_fast_on_missing_mqtt():
    """Verify agent fails fast with sys.exit(1) on invalid or missing mqtt configuration."""
    with patch("agent.load_agent_config", return_value={"mqtt": {}}), pytest.raises(SystemExit):
        agent.PCControlAgent()
