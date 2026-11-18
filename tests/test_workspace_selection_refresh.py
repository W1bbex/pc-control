from unittest.mock import MagicMock
from unittest.mock import patch
import json
from pathlib import Path

from desktop.launcher import LauncherMixin
from vds_bot.power_flow import PowerFlowManager, PowerFlowStage


def test_system_status_does_not_replace_vpn_selection():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.system_status = "online"
    flow = PowerFlowManager(ui, mqtt)
    flow.stage = PowerFlowStage.POWERING_ON
    flow.chat_id = 1
    flow.message_id = 2
    flow.selecting_mode = True

    flow.on_system_session_update("login_screen", 1)

    ui.send_message.assert_not_called()
    assert flow.logon_ready_at is not None

    flow.selecting_mode = False
    flow.on_system_session_update("login_screen", 1)
    ui.send_message.assert_called_once()


def test_chatgpt_alias_waits_for_delayed_real_process():
    config = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8"))
    step = next(s for s in config["apps"]["Рабочий сетап"]["steps"] if s["id"] == "chatgpt")
    launcher = LauncherMixin()
    launcher.config = {"apps": {"test": {"steps": [step]}}}
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    with patch("desktop.codex_launch.os.path.isfile", return_value=True), \
         patch("desktop.codex_launch._codex_processes", side_effect=lambda: [123] if clock[0] >= 3 else []), \
         patch("desktop.codex_launch.subprocess.Popen") as start, \
         patch("desktop.codex_launch.time.monotonic", side_effect=lambda: clock[0]), \
         patch("desktop.codex_launch.time.sleep", side_effect=sleep), \
         patch.object(launcher, "_find_visible_window", side_effect=lambda _: 101 if clock[0] < 5 else 102):
        result = launcher.launch_app("test")

    assert result["success"] is True
    assert result["verified_steps"] == ["chatgpt"]
    assert clock[0] >= 7.0
    assert start.call_args.args[0][0].lower().endswith("explorer.exe")
    assert "codex.exe" not in step["verification"]["targets"]


def test_codex_launch_does_not_report_success_without_window():
    launcher = LauncherMixin()
    launcher.config = {"apps": {"ChatGPT": "chatgpt-desktop.exe"}}
    with patch("desktop.launcher.diagnose_codex_launch", return_value={"state": "not_started"}) as run:
        result = launcher.launch_app("ChatGPT")
    assert result["success"] is False
    assert result["verified_steps"] == []
    assert run.call_args.args[0] == "2"


def test_codex_launch_uses_existing_visible_window_without_starting_again():
    from desktop import codex_launch
    with patch.object(codex_launch, "_codex_processes", return_value=[123]), \
         patch.object(codex_launch.subprocess, "Popen") as start:
        result = codex_launch.diagnose_codex_launch("2", "chatgpt-desktop.exe", lambda _: True, allow_existing=True)
    assert result["state"] == "window_visible"
    start.assert_not_called()
