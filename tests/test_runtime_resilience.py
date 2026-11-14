import asyncio
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from core.app_emoji_manager import AppEmojiManager
from core.media_manager import MediaManager
from desktop.telemetry import TelemetryMixin
from desktop.vpn_manager import VpnManagerMixin
from vds_bot.health_engine import HealthEngine
from vds_bot.routers.command_router import route_message


class DummyTelemetry(TelemetryMixin):
    def __init__(self):
        self.state_lock = threading.Lock()
        self.current_session_app = "notepad.exe"
        self.current_session_title = "Untitled"
        self.current_session_emoji_id = None
        self.published_updates = []

    def publish_active_app_update(self, exe_name, emoji_id, title):
        with self.state_lock:
            self.published_updates.append((exe_name, emoji_id, title))


def test_health_engine_preserves_graceful_shutdown_on_same_boot_id():
    engine = HealthEngine()
    engine.update("windows_system", "online", boot_id="boot-100", ts=100.0)
    engine.set_graceful_shutdown(ts=105.0, ttl=3600.0)

    assert engine.graceful_shutdown_until > 0.0

    # Incoming packet with the same boot_id and older timestamp should not cancel graceful shutdown
    engine.update("windows_system", "online", boot_id="boot-100", ts=102.0)
    assert engine.graceful_shutdown_until > 0.0

    # Only a confirmed reboot with a different boot_id should cancel graceful shutdown
    engine.update("windows_system", "online", boot_id="boot-200", ts=110.0)
    assert engine.graceful_shutdown_until == 0.0


def test_vpn_manager_sqlite_handle_closed_on_query_error(tmp_path):
    incy_dir = tmp_path / "incy"
    incy_dir.mkdir(parents=True, exist_ok=True)
    fake_db = incy_dir / "incy.db"
    fake_db.touch()

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = sqlite3.DatabaseError("Corrupt disk image")
    mock_conn.cursor.return_value = mock_cursor

    with patch("desktop.vpn_manager.sqlite3.connect", return_value=mock_conn):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(tmp_path)}):
            servers = VpnManagerMixin.get_incy_servers()
            assert servers == []
            mock_conn.close.assert_called_once()


def test_media_manager_http_session_closed_before_recreation():
    mgr = MediaManager(yandex_token="token", yandex_uid="123")
    old_session = MagicMock()
    old_session.get.side_effect = OSError("Network unreachable")
    mgr._http_session = old_session

    with patch("core.media_manager.requests.Session") as mock_new_session_cls:
        new_session = MagicMock()
        mock_new_session_cls.return_value = new_session

        mgr._request_with_retry("GET", "https://api.music.yandex.net/test")

        old_session.close.assert_called_once()
        assert mgr._http_session is new_session


def test_app_emoji_manager_clear_all_thread_safety(tmp_path):
    with patch("core.app_emoji_manager.CACHE_FILE", tmp_path / "cache.json"):
        mgr = AppEmojiManager(token="dummy")
        mgr.cache = {"app.exe": {"emoji_id": 123}}

        mock_lock = MagicMock()
        mgr.upload_lock = mock_lock
        with patch.object(mgr, "_save_cache"):
            mgr.clear_all_emojis()
            mock_lock.__enter__.assert_called_once()
            assert mgr.cache == {}


def test_telemetry_active_app_drift_prevention():
    dummy = DummyTelemetry()
    mock_emoji_mgr = MagicMock()
    mock_emoji_mgr.cache = {}
    mock_emoji_mgr.notify_app_opened.return_value = "emoji-notepad"
    dummy.emoji_manager = mock_emoji_mgr

    # Simulate race where user switched to game.exe while notify was querying Telegram
    def switched_notify(exe_path, exe_name, title):
        dummy.current_session_app = "game.exe"
        return "emoji-notepad"

    mock_emoji_mgr.notify_app_opened.side_effect = switched_notify

    dummy._handle_active_app_change(r"C:\notepad.exe", "notepad.exe", "Notes")

    # Wait for the background thread to finish
    for th in threading.enumerate():
        if th.name == "emoji-notify-fast":
            th.join(timeout=2.0)

    # current_session_emoji_id must not be overridden with notepad's emoji
    assert dummy.current_session_emoji_id != "emoji-notepad"


def test_media_manager_loop_closed_in_target():
    mgr = MediaManager()
    mgr._stop_event.set()

    mock_loop = MagicMock()
    mock_loop.run_until_complete.side_effect = lambda coro: coro.close()

    with patch("core.media_manager.asyncio.new_event_loop", return_value=mock_loop):
        with patch("core.media_manager.asyncio.set_event_loop"):
            mgr.start()
            if mgr._thread:
                mgr._thread.join(timeout=2.0)

            mock_loop.close.assert_called_once()


def test_agent_config_safe_mqtt_get():
    from agent import PCControlAgent

    agent = PCControlAgent.__new__(PCControlAgent)
    agent.config = {}

    with patch("paho.mqtt.client.Client") as mock_mqtt:
        with patch("certifi.where", return_value="dummy.pem"):
            agent.setup_mqtt_client()
            assert hasattr(agent, "mqtt")


def test_boot_agent_fail_fast_missing_config(tmp_path):
    from boot_agent import SystemAgent

    missing_config_path = tmp_path / "nonexistent_config.json"
    with patch("boot_agent.CONFIG_PATH", missing_config_path):
        with pytest.raises(SystemExit) as exc:
            SystemAgent()
        assert exc.value.code == 1


def test_bot_env_empty_string_conversion_safety():
    import subprocess
    import sys

    env = dict(os.environ)
    env.update({
        "TELEGRAM_BOT_TOKEN": "test_token_dummy",
        "TELEGRAM_ALLOWED_USER_ID": "",
        "MQTT_PORT": "",
    })
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'vds_bot'); import bot; assert bot.ALLOWED_USER_ID == 0; assert bot.MQTT_PORT == 1883",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_command_router_null_text_safety():
    mock_app = MagicMock()
    mock_app.allowed_user_id = 12345
    message = {
        "chat": {"id": 12345, "type": "private"},
        "from": {"id": 12345},
        "text": None,
    }

    # Should not raise AttributeError when message['text'] is None
    res = route_message(mock_app, message)
    assert res is None
    mock_app.ui.send_message.assert_not_called()


def test_power_scheduler_stale_status_shutdown_detection():
    from vds_bot.power_scheduler import PowerSchedulerMixin

    class DummyScheduler(PowerSchedulerMixin):
        def __init__(self):
            self.pending_shutdown_cmd_id = "cmd-123"
            self.shutdown_acknowledged = True
            self.shutdown_offline_detected_at = None
            self.pending_shutdown_started_at = 100.0
            self.mqtt = MagicMock()
            self.mqtt.mqtt_online = True
            self.mqtt.system_status = "stale"
            self.mqtt.desktop_status = "stale"
            self.monitoring = MagicMock()
            self.monitoring.telemetry_age.return_value = 30.0
            self.ui = MagicMock()

    sched = DummyScheduler()
    sched._check_shutdown_confirmation(now=105.0)

    # Criteria should be met and sustain window started
    assert sched.shutdown_offline_detected_at == 105.0


def test_mqtt_gateway_progress_ack_extends_ttl():
    from vds_bot.mqtt_gateway import MqttGateway

    gw = MqttGateway("user", "pass")
    gw.pending_commands["cmd-prog"] = {"action": "launch_app", "expires": 1000, "timeout": 60, "target": "desktop"}

    gw._handle_ack({"id": "cmd-prog", "result": "progress"})
    assert "cmd-prog" in gw.pending_commands
    assert gw.pending_commands["cmd-prog"]["expires"] > 1000

    gw._handle_ack({"id": "cmd-prog", "result": "executed"})
    assert "cmd-prog" not in gw.pending_commands


def test_physical_button_thread_safe_gesture_buffer():
    from vds_bot.physical_button import PhysicalButtonManager

    ui = MagicMock()
    mqtt = MagicMock()
    mgr = PhysicalButtonManager(ui, mqtt)

    assert hasattr(mgr, "_lock")
    mock_lock = MagicMock()
    mgr._lock = mock_lock

    mgr.revoke_morse_eligibility()
    assert mock_lock.__enter__.called


def test_power_flow_thread_safe_transitions(tmp_path):
    from vds_bot.power_flow import PowerFlowManager, PowerFlowStage

    with patch("vds_bot.power_flow.FLOW_STATE_PATH", tmp_path / "flow_state.json"):
        ui = MagicMock()
        mqtt = MagicMock()
        mqtt.system_session = "locked"
        mqtt.system_status = "online"
        mgr = PowerFlowManager(ui, mqtt)

        mgr.start_reboot_flow(chat_id=123, message_id=456, unlock=True)
        assert mgr.stage == PowerFlowStage.REBOOTING

        mgr.reset_flow()
        assert mgr.stage == PowerFlowStage.IDLE


def test_launcher_has_visible_window_closes_desktop_handle():
    from desktop.launcher import LauncherMixin

    mock_user32 = MagicMock()
    mock_user32.OpenDesktopW.return_value = 999
    mock_user32.GetThreadDesktop.return_value = 111

    with patch("desktop.launcher.ctypes.windll") as mock_windll:
        mock_windll.user32 = mock_user32
        mock_windll.kernel32.GetCurrentThreadId.return_value = 42
        with patch("psutil.process_iter", side_effect=RuntimeError("Process scan failed")):
            res = LauncherMixin._has_visible_window(["notepad.exe"])
            assert res is False
            mock_user32.CloseDesktop.assert_called_once_with(999)


def test_history_render_chart_closes_figure_on_plot_error(tmp_path):
    from vds_bot.history import TelemetryHistory

    store = TelemetryHistory(tmp_path / "telemetry.db")
    now = int(time.time())
    fake_rows = [
        (now - 120, 10.0, 40.0, 20.0, 50.0, 30.0, 35.0, 45.0, 5.0, 2.0, 1.0),
        (now - 60, 12.0, 41.0, 22.0, 51.0, 31.0, 36.0, 45.0, 6.0, 3.0, 1.0),
    ]

    fake_fig = MagicMock()
    fake_ax = MagicMock()
    fake_ax.plot.side_effect = RuntimeError("Matplotlib plotting error")
    fake_axes = [fake_ax, MagicMock(), MagicMock(), MagicMock()]

    mock_plt = MagicMock()
    mock_plt.subplots.return_value = (fake_fig, fake_axes)

    with patch.object(store, "rows", return_value=fake_rows):
        with patch("vds_bot.history.HAS_MATPLOTLIB", True):
            with patch("vds_bot.history.plt", mock_plt, create=True):
                with patch("vds_bot.history.mdates", MagicMock(), create=True):
                    with pytest.raises(RuntimeError, match="Matplotlib plotting error"):
                        store.render_chart(3600, "1 час")
                    mock_plt.close.assert_called_once_with(fake_fig)


def test_deploy_vds_preflight_includes_routers():
    from scripts import deploy_vds

    captured_cmds = []

    def fake_run_ssh(cmd, **kwargs):
        captured_cmds.append(cmd)
        res = MagicMock()
        res.stdout = "ALL_MODULES_IMPORTED_OK\n"
        res.stderr = ""
        return res

    with patch.object(deploy_vds, "run_ssh", side_effect=fake_run_ssh):
        deploy_vds.run_preflight("/opt/pc-control-bot/releases/test")

    assert len(captured_cmds) == 2
    import_cmd = captured_cmds[1]
    for router_name in (
        "antigravity_router",
        "command_router",
        "console_router",
        "monitoring_router",
        "physical_router",
        "player_router",
        "power_router",
        "workspace_router",
    ):
        assert router_name in import_cmd
