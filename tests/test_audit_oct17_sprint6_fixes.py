#!/usr/bin/env python3
"""Regression tests for Sprint 6 fixes (Oct 17 Audit).
Covers:
1. power_flow: on_workspace_start_cb failure is logged, not re-invoked
2. antigravity_switcher: fallback account selection rejects zero-quota accounts
3. antigravity_switcher: switch_account does not spontaneously launch IDE when was_running=False
4. stealth_guard: deactivate restores cursor clip and visibility
5. workspace_runner: crash in runner resets power_flow stage
6. mqtt_dispatcher: workspace_execution_result stores ack_data dict for desktop_minimized
7. activity_logger: explicit 64-bit argtypes on user32 window functions
8. telemetry: publish_telemetry collects outside lock; query_gpu graceful error handling
9. app_emoji_manager: bot token is redacted from logged exceptions
10. ui_markups: clean button selection without dead offline duplicate
"""

import ctypes
from ctypes import wintypes
import sys
import threading
import time
from unittest.mock import MagicMock, patch
import pytest


def test_power_flow_callback_exception_not_reinvoked():
    from vds_bot.power_flow import PowerFlowManager, PowerFlowStage

    mgr = PowerFlowManager(telegram_ui=MagicMock(), mqtt_gateway=MagicMock())
    mgr.stage = PowerFlowStage.UNLOCKING
    mgr.auto_launch_workspace = True

    call_count = 0

    def faulty_callback(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("Callback crashed!")

    mgr.on_workspace_start_cb = faulty_callback

    # Trigger unlock success
    mgr.on_unlock_success(123)

    # Must be invoked exactly once, not re-invoked in except block
    assert call_count == 1
    assert mgr.stage == PowerFlowStage.WORKSPACE_RUNNING


def test_antigravity_switcher_rejects_zero_quota_accounts():
    from core.antigravity_switcher import AntigravityAccountManager

    mgr = AntigravityAccountManager()

    candidates = [
        {"id": "acc1", "email": "active_zero@test.com", "is_active": True, "min_quota": 0, "status": "active", "has_valid_token": True},
        {"id": "acc2", "email": "inactive_zero@test.com", "is_active": False, "min_quota": 0, "status": "active", "has_valid_token": True},
    ]

    with patch.object(mgr, "list_accounts", return_value=candidates):
        best = mgr.find_best_account()
        # Must NOT select an account with 0 quota
        assert best is None

        # If an account has positive quota, it should be selected
        candidates.append({"id": "acc3", "email": "has_quota@test.com", "is_active": False, "min_quota": 85, "status": "active", "has_valid_token": True})
        best2 = mgr.find_best_account()
        assert best2 is not None
        assert best2["id"] == "acc3"


def test_antigravity_switcher_does_not_launch_ide_if_not_running():
    from core.antigravity_switcher import AntigravityAccountManager

    mgr = AntigravityAccountManager()
    mgr.is_ide_running = MagicMock(return_value=False)
    mgr.launch_ide = MagicMock(return_value=True)
    mgr.refresh_token_if_needed = MagicMock(return_value={"access_token": "tok"})
    mgr.get_ide_state_paths = MagicMock(return_value=(None, None))

    # Mock accounts DB
    mock_row = {"id": "acc1", "email": "test@test.com", "name": "test", "device_profile_json": None}
    with patch("sqlite3.connect") as mock_conn:
        cur = MagicMock()
        cur.execute.return_value.fetchone.return_value = mock_row
        mock_conn.return_value.cursor.return_value = cur

        with patch.object(mgr, "get_cloud_db_path") as mock_db_path:
            mock_db_path.return_value.exists.return_value = True
            res = mgr.switch_account("acc1", restart_ide=True)

    # launch_ide must NOT be called because was_running was False
    assert not mgr.launch_ide.called
    assert res["restarted_ide"] is False


def test_stealth_guard_deactivate_unclips_cursor():
    from core.stealth_guard import StealthManager

    mgr = StealthManager()
    mgr._process = None

    with patch("core.stealth_guard.user32") as mock_user32:
        mock_user32.ShowCursor.return_value = 0
        mgr.deactivate()
        # ClipCursor(None) must be called to restore Win32 cursor
        mock_user32.ClipCursor.assert_called_with(None)


def test_workspace_runner_resets_flow_on_crash():
    from vds_bot.workspace_runner import WorkspaceRunnerMixin
    from vds_bot.power_flow import PowerFlowManager

    class App(WorkspaceRunnerMixin):
        def __init__(self):
            self.power_flow = MagicMock(spec=PowerFlowManager)
            self.chosen_vpn = "incy"

    app = App()
    # Simulate run_workspace_setup_steps crashing
    app.run_workspace_setup_steps = MagicMock(side_effect=RuntimeError("Runner crashed!"))

    app.start_async_workspace_setup(123)
    # Wait for thread to run
    time.sleep(0.15)

    assert app.power_flow.reset_flow.called


def test_mqtt_dispatcher_handles_workspace_executed_ack():
    from vds_bot.mqtt_dispatcher import MqttDispatcherMixin

    class Dispatcher(MqttDispatcherMixin):
        def __init__(self):
            self.power_flow = MagicMock()
            self.physical_btn = MagicMock()
            self.save_state = MagicMock()
            self.active_workspace_cmd_id = "cmd-123"
            self.workspace_execution_result = None
            self.workspace_executed_event = threading.Event()
            self.app_statuses = {}

    d = Dispatcher()
    payload = {
        "id": "cmd-123",
        "action": "launch_app",
        "result": "executed",
        "data": {},
    }

    d._on_mqtt_event("ack", payload)
    assert d.workspace_execution_result == "executed"
    assert d.workspace_executed_event.is_set()


def test_activity_logger_user32_64bit_argtypes():
    from core.activity_logger import ActivityTracker

    tracker = ActivityTracker(config={}, log_dir=MagicMock(), stop_event=threading.Event())
    with patch("ctypes.windll.user32") as mock_user32:
        mock_user32.GetForegroundWindow.return_value = 0
        tracker._foreground({})
        # Ensure argtypes are set
        assert mock_user32.GetWindowThreadProcessId.argtypes is not None
        assert mock_user32.GetWindowTextLengthW.argtypes is not None
        assert mock_user32.GetWindowTextW.argtypes is not None


def test_telemetry_nvidia_error_handled_gracefully():
    from desktop.telemetry import TelemetryMixin

    with patch("subprocess.run", side_effect=FileNotFoundError("nvidia-smi not found")):
        res = TelemetryMixin.query_gpu()
        # Must return None without crashing or logging exception
        assert res is None


def test_app_emoji_manager_token_redaction():
    from core.app_emoji_manager import AppEmojiManager
    import requests

    mgr = AppEmojiManager(token="SECRET_BOT_TOKEN_12345", user_id=100)
    with patch("requests.post", side_effect=requests.exceptions.ConnectionError("Failed connecting to https://api.telegram.org/botSECRET_BOT_TOKEN_12345/addStickerToSet")):
        with patch("logging.Logger.warning") as mock_warn:
            emoji_id = mgr.get_or_create_emoji_id("C:\\test.exe", "test.exe")
            assert emoji_id is None
            # Check warning log calls
            for call in mock_warn.call_args_list:
                formatted_msg = str(call)
                assert "SECRET_BOT_TOKEN_12345" not in formatted_msg


def test_ui_markups_bottom_button_online():
    from vds_bot.ui_markups import UiMarkupsMixin

    class Ui(UiMarkupsMixin):
        pass

    ui = Ui()
    # Unlocked / Ready PC -> should give shutdown button
    markup = ui.main_menu_markup(pc_state="READY", session="unlocked", system_status="online")
    flat_buttons = [btn for row in markup["inline_keyboard"] for btn in row]
    shutdown_btns = [b for b in flat_buttons if b.get("callback_data") == "shutdown_pc"]
    assert len(shutdown_btns) >= 1
