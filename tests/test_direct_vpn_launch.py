from unittest.mock import MagicMock, patch

from desktop.launcher import LauncherMixin
from desktop.action_dispatcher import ActionDispatcherMixin
from vds_bot.routers.workspace_router import route_workspace_callback


def test_direct_happ_button_does_not_use_default_incy():
    launcher = LauncherMixin()
    launcher.config = {"default_vpn": "incy", "apps": {"Happ": "happ.exe", "INCY": "incy.exe"}}
    with patch.object(launcher, "check_happ_vpn_active", return_value=True, create=True) as happ, \
         patch.object(launcher, "check_incy_vpn_active", return_value=True, create=True) as incy, \
         patch("desktop.launcher.shutil.which", side_effect=lambda value: value):
        result = launcher.launch_app("Happ")
    assert result["success"] is True
    happ.assert_called_once()
    incy.assert_not_called()


def test_direct_incy_button_does_not_use_default_happ():
    launcher = LauncherMixin()
    launcher.config = {"default_vpn": "happ", "apps": {"Happ": "happ.exe", "INCY": "incy.exe"}}
    with patch.object(launcher, "check_happ_vpn_active", return_value=True, create=True) as happ, \
         patch.object(launcher, "check_incy_vpn_active", return_value=True, create=True) as incy, \
         patch("desktop.launcher.shutil.which", side_effect=lambda value: value):
        result = launcher.launch_app("INCY")
    assert result["success"] is True
    incy.assert_called_once()
    happ.assert_not_called()


def test_happ_button_asks_before_vpn_connection():
    app = MagicMock()
    app.mqtt.desktop_status = "online"
    assert route_workspace_callback(app, "app:Happ", 1, 2, "cb")
    app.mqtt.publish_desktop_action.assert_not_called()
    markup = app.ui.send_message.call_args.kwargs["reply_markup"]
    callbacks = {button.get("callback_data") for row in markup["inline_keyboard"] for button in row}
    assert {"happ_launch:open", "happ_launch:connect"} <= callbacks


def test_happ_choices_pass_explicit_connection_intent():
    for selection, expected in (("open", False), ("connect", True)):
        app = MagicMock()
        app.mqtt.desktop_status = "online"
        assert route_workspace_callback(app, f"happ_launch:{selection}", 1, 2, "cb")
        app.mqtt.publish_desktop_action.assert_called_once_with("launch_app", {"app": "Happ", "connect_vpn": expected})


def test_happ_open_only_does_not_connect_vpn():
    launcher = LauncherMixin()
    launcher.config = {"default_vpn": "incy", "apps": {"Happ": "happ.exe"}}
    process = MagicMock()
    process.info = {"name": "happ.exe"}
    with patch.object(launcher, "check_happ_vpn_active", create=True) as check, \
         patch.object(launcher, "connect_happ_vpn", create=True) as connect, \
         patch("desktop.launcher.psutil.process_iter", return_value=[process]), \
         patch("desktop.launcher.shutil.which", side_effect=lambda value: value):
        result = launcher.launch_app("Happ", connect_vpn=False)
    assert result["success"] is True
    check.assert_not_called()
    connect.assert_not_called()


def test_happ_open_only_starts_app_without_vpn_automation():
    launcher = LauncherMixin()
    launcher.config = {"default_vpn": "incy", "apps": {"Happ": "happ.exe"}}
    with patch.object(launcher, "check_happ_vpn_active", create=True) as check, \
         patch.object(launcher, "connect_happ_vpn", create=True) as connect, \
         patch("desktop.launcher.psutil.process_iter", return_value=[]), \
         patch("desktop.launcher.shutil.which", side_effect=lambda value: value), \
         patch("desktop.launcher.subprocess.Popen") as start, \
         patch("desktop.launcher.time.sleep"):
        start.return_value.poll.return_value = None
        result = launcher.launch_app("Happ", connect_vpn=False)
    assert result["success"] is True
    assert start.call_args.args[0] == ["happ.exe"]
    check.assert_not_called()
    connect.assert_not_called()


def test_desktop_dispatch_preserves_open_only_choice():
    dispatcher = ActionDispatcherMixin()
    dispatcher.launch_app = MagicMock(return_value={"success": True})
    dispatcher.acknowledge = MagicMock()
    dispatcher.execute_action("command", "launch_app", {"app": "Happ", "connect_vpn": False})
    assert dispatcher.launch_app.call_args.kwargs["connect_vpn"] is False
