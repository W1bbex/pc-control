import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent import PCControlAgent
from vds_bot.bot import ALLOWED_USER_ID, PcControlBotApp, get_workspace_steps
from vds_bot.power_flow import PowerFlowManager, PowerFlowStage
from vds_bot.telegram_ui import TelegramBotApi


# ==============================================================================
# Desktop Agent VPN Tests
# ==============================================================================

def test_check_incy_vpn_active_false_when_no_process():
    with patch("psutil.process_iter", return_value=[]):
        assert PCControlAgent.check_incy_vpn_active() is False


def test_check_incy_vpn_active_true_when_process_and_probe():
    mock_proc = MagicMock()
    mock_proc.info = {"name": "incy.exe", "pid": 1234}
    mock_stat = MagicMock(isup=True)
    mock_addr = MagicMock(family=2, address="198.18.0.1")  # AF_INET = 2

    with patch("psutil.process_iter", return_value=[mock_proc]), \
         patch("psutil.net_if_addrs", return_value={"wwan99": [mock_addr]}), \
         patch("psutil.net_if_stats", return_value={"wwan99": mock_stat}), \
         patch.object(PCControlAgent, "_run_network_probe", return_value=True):
        assert PCControlAgent.check_incy_vpn_active() is True


def test_check_happ_vpn_active_false_when_no_process():
    with patch("psutil.process_iter", return_value=[]):
        assert PCControlAgent.check_happ_vpn_active() is False


def test_check_happ_vpn_active_true_when_process_and_probe():
    mock_proc = MagicMock()
    mock_proc.info = {"name": "Happ.exe", "pid": 5678}
    mock_stat = MagicMock(isup=True)
    mock_addr = MagicMock(family=2, address="10.0.0.2")

    with patch("psutil.process_iter", return_value=[mock_proc]), \
         patch("psutil.net_if_addrs", return_value={"happ-tun": [mock_addr]}), \
         patch("psutil.net_if_stats", return_value={"happ-tun": mock_stat}), \
         patch.object(PCControlAgent, "_run_network_probe", return_value=True):
        assert PCControlAgent.check_happ_vpn_active() is True


def test_connect_incy_vpn_when_already_active():
    agent = PCControlAgent.__new__(PCControlAgent)
    agent.config = {}
    with patch.object(agent, "check_incy_vpn_active", return_value=True):
        result = agent.connect_incy_vpn()
        assert result.get("verified") is True
        assert result.get("already_connected") is True


def test_connect_incy_vpn_auto_connect_and_launch(tmp_path):
    agent = PCControlAgent.__new__(PCControlAgent)
    agent.config = {"apps": {"INCY": "C:\\Program Files\\INCY\\incy.exe"}}

    fake_pref_dir = tmp_path / "incy"
    fake_pref_dir.mkdir(parents=True)
    fake_pref_file = fake_pref_dir / "preferences.json"
    fake_pref_file.write_text(json.dumps({"autoConnect": False}), encoding="utf-8")

    with patch("os.path.expandvars", return_value=str(fake_pref_file)), \
         patch.object(agent, "check_incy_vpn_active", side_effect=[False, True]), \
         patch("psutil.process_iter", return_value=[]), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("subprocess.Popen") as mock_popen:
        result = agent.connect_incy_vpn(timeout=2)
        assert result.get("verified") is True
        assert mock_popen.called
        saved_prefs = json.loads(fake_pref_file.read_text(encoding="utf-8"))
        assert saved_prefs.get("autoConnect") is True


def test_launch_app_dynamic_vpn_steps_incy():
    published_progress = []

    def fake_publish(info):
        published_progress.append(info)

    agent = PCControlAgent.__new__(PCControlAgent)
    agent.config = {
        "apps": {"Рабочий сетап": "workspace"},
        "scenarios": {
            "workspace": {
                "steps": [
                    {"step_id": "happ_vpn", "label": "VPN Step", "executable": "happ.exe", "verification": "happ_vpn"},
                ]
            }
        },
        "default_vpn": "incy",
    }

    with patch.object(agent, "connect_incy_vpn", return_value={"verified": True}), \
         patch.object(agent, "check_incy_vpn_active", return_value=False), \
         patch.object(agent, "minimize_all_windows", return_value=True), \
         patch("shutil.which", return_value="C:\\Program Files\\INCY\\incy.exe"), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("subprocess.Popen") as mock_popen:
        
        mock_popen.return_value.poll.return_value = None
        res = agent.launch_app(
            "Рабочий сетап",
            progress_callback=fake_publish,
            vpn="incy",
        )
        assert res.get("success") is True
        step_ids = [p["step_id"] for p in published_progress]
        assert "incy_vpn" in step_ids
        assert "happ_vpn" not in step_ids


def test_launch_app_dynamic_vpn_steps_happ():
    published_progress = []

    def fake_publish(info):
        published_progress.append(info)

    agent = PCControlAgent.__new__(PCControlAgent)
    agent.config = {
        "apps": {"Рабочий сетап": "workspace"},
        "scenarios": {
            "workspace": {
                "steps": [
                    {"step_id": "happ_vpn", "label": "VPN Step", "executable": "happ.exe", "verification": "happ_vpn"},
                ]
            }
        },
        "default_vpn": "incy",
    }

    with patch.object(agent, "connect_happ_vpn", return_value={"verified": True}), \
         patch.object(agent, "check_happ_vpn_active", return_value=False), \
         patch.object(agent, "minimize_all_windows", return_value=True), \
         patch("shutil.which", return_value="C:\\Program Files\\Happ\\happ.exe"), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("subprocess.Popen") as mock_popen:
        
        mock_popen.return_value.poll.return_value = None
        res = agent.launch_app(
            "Рабочий сетап",
            progress_callback=fake_publish,
            vpn="happ",
        )
        assert res.get("success") is True
        step_ids = [p["step_id"] for p in published_progress]
        assert "happ_vpn" in step_ids
        assert "incy_vpn" not in step_ids


# ==============================================================================
# VDS Bot Interactive VPN Selection Tests
# ==============================================================================

def test_get_workspace_steps():
    incy_steps = get_workspace_steps("incy")
    happ_steps = get_workspace_steps("happ")

    assert any(s["id"] == "incy_vpn" and "INCY" in s["label"] for s in incy_steps)
    assert not any(s["id"] == "happ_vpn" for s in incy_steps)

    assert any(s["id"] == "happ_vpn" and "Happ" in s["label"] for s in happ_steps)
    assert not any(s["id"] == "incy_vpn" for s in happ_steps)


def test_bot_workspace_start_direct_prompts_vpn_choice():
    real_ui = TelegramBotApi.__new__(TelegramBotApi)
    ui = MagicMock()
    ui.workspace_vpn_choice_markup.side_effect = real_ui.workspace_vpn_choice_markup
    mqtt = MagicMock()
    mqtt.health.evaluate_pc_state.return_value = MagicMock(session_status="unlocked", state="UNLOCKED")

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.monitoring = MagicMock()
    app.monitoring.telemetry_age.return_value = 5.0
    app.chosen_vpn = "incy"

    call = {
        "id": "cb-123",
        "data": "workspace_start_direct",
        "from": {"id": ALLOWED_USER_ID},
        "message": {"message_id": 999, "chat": {"id": ALLOWED_USER_ID}},
    }
    app.handle_callback(call)

    assert ui.send_message.called
    sent_text = ui.send_message.call_args[0][1]
    sent_markup = ui.send_message.call_args[1].get("reply_markup")
    assert "Выберите VPN" in sent_text
    all_callbacks = [
        btn["callback_data"]
        for row in sent_markup["inline_keyboard"]
        for btn in row
    ]
    assert "workspace_vpn:incy" in all_callbacks
    assert "workspace_vpn:happ" in all_callbacks


def test_bot_workspace_vpn_selection_starts_runner():
    ui = MagicMock()
    mqtt = MagicMock()
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.save_state = MagicMock()
    app.start_async_workspace_setup = MagicMock()

    call = {
        "id": "cb-456",
        "data": "workspace_vpn:incy",
        "from": {"id": ALLOWED_USER_ID},
        "message": {"message_id": 888, "chat": {"id": ALLOWED_USER_ID}},
    }
    app.handle_callback(call)

    assert getattr(app, "chosen_vpn", None) == "incy"
    assert app.power_flow.stage == PowerFlowStage.WORKSPACE_RUNNING
    assert app.start_async_workspace_setup.called
    kwargs = app.start_async_workspace_setup.call_args[1]
    assert kwargs.get("vpn") == "incy"


def test_bot_runner_waits_for_chosen_vpn():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.desktop_status = "online"
    mqtt.publish_desktop_action.side_effect = lambda action, *args, **kwargs: f"cmd-{action}"

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.physical_btn = MagicMock()
    app.chosen_vpn = "incy"
    app.workspace_cancel_event = threading.Event()
    app.vpn_ready_event = threading.Event()
    app.happ_ready_event = threading.Event()
    app.workspace_executed_event = threading.Event()
    app.minimize_ack_event = threading.Event()
    app.workspace_execution_result = None
    app.minimize_ack_result = None
    app.workspace_card_msg_id = None
    app.pending_minimize_cmd_id = None
    app.app_statuses = {}

    runner_thread = threading.Thread(
        target=app.run_workspace_setup_steps,
        args=(12345, None, "ws-vpn-1"),
        kwargs={"vpn": "incy"},
        daemon=True,
    )
    runner_thread.start()
    time.sleep(0.1)

    assert mqtt.publish_desktop_action.called
    action, payload = mqtt.publish_desktop_action.call_args[0][:2]
    assert action == "launch_app"
    assert payload.get("vpn") == "incy"

    cmd_id = app.active_workspace_cmd_id
    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "progress",
        "data": {"step_id": "incy_vpn", "app": "INCY", "state": "verified"}
    })

    assert app.vpn_ready_event.is_set()
    assert app.app_statuses["incy_vpn"] == "✅"

    for s in ["telegram", "yandex_music", "dolphin", "chrome", "antigravity"]:
        app.app_statuses[s] = "✅"

    # Full execution ACK
    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "executed",
        "data": {}
    })

    # Wait for pending_minimize_cmd_id to be populated by runner
    for _ in range(50):
        if app.pending_minimize_cmd_id:
            break
        time.sleep(0.02)

    assert app.pending_minimize_cmd_id is not None
    app._on_mqtt_event("ack", {
        "id": app.pending_minimize_cmd_id,
        "result": "executed",
        "data": {}
    })

    runner_thread.join(timeout=2.0)
    assert not runner_thread.is_alive()
