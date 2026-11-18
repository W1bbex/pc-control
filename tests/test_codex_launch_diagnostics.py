from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest

from desktop import codex_launch
from desktop.action_dispatcher import ActionDispatcherMixin
from desktop.launcher import LauncherMixin
from vds_bot.routers.workspace_router import route_workspace_callback
from vds_bot.mqtt_dispatcher import MqttDispatcherMixin


@pytest.mark.parametrize("variant", ["1", "2", "3"])
def test_variant_passes_distinct_launch_command_and_reports_visible_window(variant):
    process = MagicMock()
    clock = [0.0]
    def sleep(seconds):
        clock[0] += seconds
    with patch.object(codex_launch.os.path, "isfile", return_value=True), \
         patch.object(codex_launch, "_codex_processes", side_effect=[[], *([[123]] * 6)]), \
         patch.object(codex_launch.subprocess, "Popen", return_value=process) as launch, \
         patch.object(codex_launch.time, "monotonic", side_effect=lambda: clock[0]), \
         patch.object(codex_launch.time, "sleep", side_effect=sleep):
        result = codex_launch.diagnose_codex_launch(variant, "chatgpt-desktop.exe", lambda _: True)
    command = launch.call_args.args[0]
    assert result["state"] == "window_visible"
    if variant == "1":
        assert command == ["chatgpt-desktop.exe"]
    elif variant == "2":
        assert command[-1] == "chatgpt-desktop.exe"
        assert command[0].lower().endswith("explorer.exe")
    else:
        assert command[-1].startswith("shell:AppsFolder\\")


def test_no_launch_when_chatgpt_process_is_still_running():
    with patch.object(codex_launch, "_codex_processes", return_value=[123]), \
         patch.object(codex_launch.subprocess, "Popen") as launch:
        result = codex_launch.diagnose_codex_launch("2", "chatgpt-desktop.exe", lambda _: True)
    assert result["state"] == "already_running"
    launch.assert_not_called()


def test_direct_failed_launch_reports_exit_code_without_false_success():
    process = MagicMock()
    process.poll.return_value = 15
    with patch.object(codex_launch.os.path, "isfile", return_value=True), \
         patch.object(codex_launch, "_codex_processes", return_value=[]), \
         patch.object(codex_launch.subprocess, "Popen", return_value=process):
        result = codex_launch.diagnose_codex_launch("1", "chatgpt-desktop.exe", lambda _: False, timeout=0)
    assert result["state"] == "not_started"
    assert result["exit_code"] == 15


def test_router_exposes_three_options_and_sends_selected_variant():
    app = MagicMock()
    app.mqtt.desktop_status = "online"
    assert route_workspace_callback(app, "codex_diagnostics", 1, 2, "cb")
    markup = app.ui.send_message.call_args.kwargs["reply_markup"]
    callbacks = {button.get("callback_data") for row in markup["inline_keyboard"] for button in row}
    assert {"codex_test:1", "codex_test:2", "codex_test:3"} <= callbacks
    assert route_workspace_callback(app, "codex_test:2", 1, 2, "cb2")
    app.mqtt.publish_desktop_action.assert_called_once_with("diagnose_codex_launch", {"variant": "2"}, timeout=40)


@pytest.mark.parametrize("remaining_windows, expected", [([101, None], True), ([101] * 5, False)])
def test_workspace_verifies_codex_minimization_before_reporting_result(remaining_windows, expected):
    launcher = LauncherMixin()
    launcher.config = {"apps": {"workspace": {"steps": [{"id": "chatgpt", "command": ["chatgpt-desktop.exe"]}],
                                              "minimize_after": True}}}
    windows = MagicMock()
    launcher.minimize_all_windows = windows.minimize_all
    launcher._find_visible_window = windows.find_window
    windows.find_window.side_effect = remaining_windows
    cancel_event = MagicMock()
    cancel_event.wait.return_value = False
    cancel_event.is_set.return_value = False
    with patch("desktop.launcher.diagnose_codex_launch", return_value={"state": "window_visible"}), \
         patch("desktop.launcher.threading.Event", return_value=cancel_event), \
         patch("desktop.launcher.ctypes.windll.user32", windows.user32):
        result = launcher.launch_app("workspace", workspace_id="ws-1")

    assert result["success"] is True
    assert result["desktop_minimized"] is expected
    assert [call.args for call in cancel_event.wait.call_args_list[:2]] == [(2.0,), (2.0,)]
    assert [call[0] for call in windows.mock_calls[:3]] == ["minimize_all", "find_window", "user32.ShowWindowAsync"]
    windows.find_window.assert_called_with(["chatgpt.exe"], exclude_minimized=True)
    windows.user32.ShowWindowAsync.assert_called_with(101, 6)


def test_desktop_minimize_fallback_reports_unminimized_codex():
    app = ActionDispatcherMixin()
    app.minimize_workspace_windows = MagicMock(return_value=False)
    app.acknowledge = MagicMock()

    app.execute_action("cmd", "minimize_all", {"verify_codex": True})

    app.acknowledge.assert_called_once_with("cmd", "failed", {"reason": "codex_not_minimized"})


def test_diagnostic_errors_reported_with_variant():
    app = ActionDispatcherMixin()
    app.config = {"apps": {"ChatGPT": "chatgpt-desktop.exe"}}
    app.acknowledge = MagicMock()
    app._has_visible_window = MagicMock()
    with patch("desktop.action_dispatcher.diagnose_codex_launch", side_effect=OSError("start failed")):
        app.execute_action("cmd", "diagnose_codex_launch", {"variant": "2"})
    response = app.acknowledge.call_args.args[2]
    assert response["state"] == "error"
    assert response["variant"] == "2"


def test_bot_reports_failed_variant_and_exit_code():
    app = SimpleNamespace(allowed_user_id=42, ui=MagicMock(), power_flow=MagicMock(),
                          physical_btn=MagicMock(), save_state=MagicMock())
    MqttDispatcherMixin._on_mqtt_event(app, "ack", {
        "id": "command", "result": "executed",
        "data": {"codex_diagnostic": True, "variant": "1", "state": "not_started", "exit_code": 15},
    })
    chat_id, text = app.ui.send_message.call_args.args[:2]
    assert chat_id == 42
    assert "вариант 1" in text
    assert "15" in text
