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
    with patch.object(agent, "check_incy_vpn_active", return_value=True), \
         patch.object(agent, "probe_google_https", return_value=True):
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
         patch.object(agent, "probe_google_https", return_value=True), \
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

    for s in ["telegram", "yandex_music", "dolphin", "chrome", "antigravity", "chatgpt"]:
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


def test_reboot_unlock_flow_lifecycle():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.system_status = "online"
    mqtt.publish_system_action.return_value = "cmd-rst-1"
    mqtt.publish_esp_action.return_value = "cmd-esp-1"

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.save_state = MagicMock()

    call = {
        "id": "cb-rst-1",
        "data": "reboot_unlock",
        "from": {"id": ALLOWED_USER_ID},
        "message": {"message_id": 777, "chat": {"id": ALLOWED_USER_ID}},
    }
    app.handle_callback(call)

    assert mqtt.publish_system_action.called
    assert mqtt.publish_system_action.call_args[0][0] == "restart"
    assert app.power_flow.stage == PowerFlowStage.REBOOTING
    assert app.power_flow.reboot_with_unlock is True

    # Simulate PC going offline
    mqtt.system_status = "offline"
    mqtt.desktop_status = "offline"
    app.power_flow.on_system_session_update("unknown", ALLOWED_USER_ID)
    assert app.power_flow.reboot_offline_detected is True

    # Simulate PC booting back up to login_screen
    mqtt.system_status = "online"
    mqtt.system_session = "login_screen"
    mqtt.publish_system_action.return_value = "cmd-unlock-1"
    app.power_flow.on_system_session_update("login_screen", ALLOWED_USER_ID)

    assert app.power_flow.stage == PowerFlowStage.UNLOCKING
    assert app.power_flow.pending_unlock_command_id == "cmd-unlock-1"

    # Simulate unlock success
    mqtt.system_session = "unlocked"
    app.power_flow.on_system_session_update("unlocked", ALLOWED_USER_ID)
    assert app.power_flow.stage == PowerFlowStage.WAITING_WORKSPACE_DECISION

    # Verify VPN choice prompt sent
    sent_text = ui.send_message.call_args[0][1]
    assert "Выберите VPN" in sent_text


@pytest.mark.parametrize("vpn", ["incy", "happ"])
def test_reboot_with_preselected_vpn_starts_workspace_after_new_login(vpn):
    ui = MagicMock(spec=TelegramBotApi)
    ui.send_message.return_value = {"message_id": 777}
    mqtt = MagicMock(system_status="online", system_session="unlocked", desktop_status="online")
    mqtt.publish_system_action.side_effect = ["cmd-restart", "cmd-unlock"]
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)

    app.handle_callback({
        "id": "cb-reboot-vpn",
        "data": f"reboot_unlock:{vpn}",
        "from": {"id": ALLOWED_USER_ID},
        "message": {"message_id": 777, "chat": {"id": ALLOWED_USER_ID}},
    })
    mqtt.publish_system_action.assert_called_once_with("restart")
    assert vpn in ui.send_message.call_args.args[1].lower()

    flow = PowerFlowManager(ui, mqtt)
    assert flow.stage == PowerFlowStage.REBOOTING
    assert flow.target_workspace_vpn == vpn
    flow.on_workspace_start_cb = MagicMock()
    flow.on_system_session_update("unlocked", ALLOWED_USER_ID)
    flow.on_workspace_start_cb.assert_not_called()

    mqtt.system_status = "offline"
    flow.on_system_session_update("unknown", ALLOWED_USER_ID)
    mqtt.system_status = "online"
    mqtt.system_session = "login_screen"
    flow.on_system_session_update("login_screen", ALLOWED_USER_ID)
    mqtt.publish_system_action.assert_called_with("unlock")
    flow.on_workspace_start_cb.assert_not_called()

    mqtt.system_session = "unlocked"
    flow.on_system_session_update("unlocked", ALLOWED_USER_ID)
    flow.on_system_session_update("unlocked", ALLOWED_USER_ID)
    assert flow.stage == PowerFlowStage.WORKSPACE_RUNNING
    flow.on_workspace_start_cb.assert_called_once_with(
        ALLOWED_USER_ID, card_message_id=777, workspace_id=flow.active_workspace_id, vpn=vpn,
    )


def test_reboot_plain_flow_lifecycle():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.system_status = "online"
    mqtt.publish_system_action.return_value = "cmd-rst-2"

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.save_state = MagicMock()

    call = {
        "id": "cb-rst-2",
        "data": "reboot_confirm",
        "from": {"id": ALLOWED_USER_ID},
        "message": {"message_id": 778, "chat": {"id": ALLOWED_USER_ID}},
    }
    app.handle_callback(call)

    assert app.power_flow.stage == PowerFlowStage.REBOOTING
    assert app.power_flow.reboot_with_unlock is False

    # Simulate reboot completed
    mqtt.system_status = "online"
    app.power_flow.reboot_offline_detected = True
    app.power_flow.on_system_session_update("login_screen", ALLOWED_USER_ID)

    assert app.power_flow.stage == PowerFlowStage.IDLE
    sent_text = ui.send_message.call_args[0][1]
    assert "ПК успешно перезагружен" in sent_text


def test_workspace_steps_app_emojis_and_no_generic_shield_lightning():
    from vds_bot.bot import format_workspace_step_line, get_workspace_steps
    from vds_bot.telegram_ui import ICONS, TelegramBotApi

    steps_happ = get_workspace_steps("happ")
    steps_incy = get_workspace_steps("incy")

    happ_step = next(s for s in steps_happ if s["id"] == "happ_vpn")
    incy_step = next(s for s in steps_incy if s["id"] == "incy_vpn")

    # Labels must be clean without generic ⚡ / 🛡️
    assert "⚡" not in happ_step["label"]
    assert "🛡" not in incy_step["label"]

    # Verify emoji_id matches app icons
    assert happ_step["emoji_id"] == ICONS.get("app_happ")
    assert incy_step["emoji_id"] == ICONS.get("app_incy")

    # Verify Telegram, Yandex Music, Chrome, Antigravity have app emojis
    tg_step = next(s for s in steps_happ if s["id"] == "telegram")
    ym_step = next(s for s in steps_happ if s["id"] == "yandex_music")
    chrome_step = next(s for s in steps_happ if s["id"] == "chrome")
    ag_step = next(s for s in steps_happ if s["id"] == "antigravity")
    chatgpt_step = next(s for s in steps_happ if s["id"] == "chatgpt")

    assert tg_step["emoji_id"] == ICONS.get("app_telegram")
    assert ym_step["emoji_id"] == ICONS.get("app_yandex_music")
    assert chrome_step["emoji_id"] == ICONS.get("app_chrome")
    assert ag_step["emoji_id"] == ICONS.get("app_antigravity")
    assert chatgpt_step["emoji_id"] == (ICONS.get("app_chatgpt") or ICONS.get("bot"))

    # Verify formatted checklist lines contain tg-emoji
    line_queued = format_workspace_step_line(happ_step, "⏳ В очереди")
    assert "tg-emoji" in line_queued
    assert "Happ (VPN)" in line_queued
    assert "В очереди" in line_queued

    line_done = format_workspace_step_line(tg_step, "✅")
    assert "tg-emoji" in line_done
    assert "Telegram" in line_done

    # Verify buttons in workspace_vpn_choice_markup don't have generic ⚡ / 🛡️ in text and have app icons
    ui = TelegramBotApi("dummy_token")
    markup = ui.workspace_vpn_choice_markup()
    buttons = [btn for row in markup["inline_keyboard"] for btn in row]
    incy_btn = next(b for b in buttons if b.get("callback_data") == "workspace_vpn:incy")
    happ_btn = next(b for b in buttons if b.get("callback_data") == "workspace_vpn:happ")

    assert "⚡" not in happ_btn["text"]
    assert "🛡" not in incy_btn["text"]
    assert incy_btn["icon_custom_emoji_id"] == ICONS.get("app_incy")
    assert happ_btn["icon_custom_emoji_id"] == ICONS.get("app_happ")

    # Verify fallbacks are clean without generic ⚡ / 🛡️
    assert "⚡" not in happ_step.get("fallback_emoji", "")
    assert "🛡" not in incy_step.get("fallback_emoji", "")

    # Verify Dolphin has app icon in apps_menu_markup
    apps_markup = ui.apps_menu_markup()
    apps_btns = [btn for row in apps_markup["inline_keyboard"] for btn in row]
    dolphin_btn = next(b for b in apps_btns if b.get("callback_data") == "app:Dolphin")
    assert dolphin_btn["icon_custom_emoji_id"] == ICONS.get("app_dolphin")
    assert ICONS.get("app_dolphin") == "5278321743709124204"


def test_reboot_offline_detection_race_condition():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.system_status = "online"
    mqtt.desktop_status = "online"
    mqtt.system_session = "unlocked"

    flow = PowerFlowManager(ui, mqtt)
    flow.start_reboot_flow(ALLOWED_USER_ID, message_id=101, unlock=True)
    assert flow.stage == PowerFlowStage.REBOOTING
    assert flow.reboot_offline_detected is False

    # Simulate t = 8.5s while PC is still shutting down (still online, unlocked)
    with patch("time.time", return_value=flow.reboot_started_at + 8.5):
        flow.on_system_session_update("unlocked", ALLOWED_USER_ID)

    # Must NOT have prematurely jumped to UNLOCKING or WAITING_WORKSPACE_DECISION
    assert flow.stage == PowerFlowStage.REBOOTING
    assert flow.reboot_offline_detected is False


def test_locked_workspace_direct_uses_unlock_and_workspace():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.health.evaluate_pc_state.return_value = MagicMock(
        state="LOCKED",
        session_status="locked",
        system_status="online",
        label="Заблокирован",
        reason="Экран входа",
    )

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.monitoring = MagicMock()
    app.monitoring.telemetry_age.return_value = 5.0
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.save_state = MagicMock()

    call = {
        "id": "cb-ws-locked",
        "data": "workspace_start_direct",
        "from": {"id": ALLOWED_USER_ID},
        "message": {"message_id": 888, "chat": {"id": ALLOWED_USER_ID}},
    }
    app.handle_callback(call)

    sent_markup = ui.send_message.call_args[1]["reply_markup"]
    buttons = [btn for row in sent_markup["inline_keyboard"] for btn in row]
    unlock_btn = next(b for b in buttons if b.get("callback_data") == "flow_unlock_and_workspace")
    assert unlock_btn["text"] == "Разблокировать и запустить"


def test_menu_callback_resets_waiting_workspace_decision():
    ui = MagicMock()
    mqtt = MagicMock()
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.monitoring = MagicMock()
    app.monitoring.dashboard_text.return_value = "Dashboard"
    app._main_menu_markup = MagicMock(return_value={"inline_keyboard": []})
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.power_flow.stage = PowerFlowStage.WAITING_WORKSPACE_DECISION
    app.save_state = MagicMock()

    call = {
        "id": "cb-menu-reset",
        "data": "menu",
        "from": {"id": ALLOWED_USER_ID},
        "message": {"message_id": 999, "chat": {"id": ALLOWED_USER_ID}},
    }
    app.handle_callback(call)

    assert app.power_flow.stage == PowerFlowStage.IDLE


def test_apps_menu_launch_offline_guard():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.desktop_status = "offline"

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.save_state = MagicMock()

    call = {
        "id": "cb-app-offline",
        "data": "app:Telegram",
        "from": {"id": ALLOWED_USER_ID},
        "message": {"message_id": 111, "chat": {"id": ALLOWED_USER_ID}},
    }
    app.handle_callback(call)

    # Must NOT publish action to MQTT
    assert not mqtt.publish_desktop_action.called
    # Must show alert toast explaining why
    ui.answer_callback.assert_called_with(
        "cb-app-offline",
        "ПК заблокирован или выключен. Разблокируйте Windows для запуска программ.",
        alert=True,
    )


def test_bot_callback_flow_unlock_and_workspace_prompts_vpn():
    ui = TelegramBotApi("token")
    ui.send_message = MagicMock()
    ui.answer_callback = MagicMock()
    mqtt = MagicMock()
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.save_state = MagicMock()

    call = {
        "id": "cb-ws-prompt",
        "data": "flow_unlock_and_workspace",
        "from": {"id": ALLOWED_USER_ID},
        "message": {"message_id": 222, "chat": {"id": ALLOWED_USER_ID}},
    }
    app.handle_callback(call)

    assert ui.send_message.called
    sent_text = ui.send_message.call_args[0][1]
    assert "Выберите VPN" in sent_text
    sent_markup = ui.send_message.call_args[1].get("reply_markup")
    callbacks = [btn["callback_data"] for row in sent_markup["inline_keyboard"] for btn in row]
    assert "flow_workspace_vpn:incy" in callbacks
    assert "flow_workspace_vpn:happ" in callbacks
    assert "flow_unlock_no" in callbacks


def test_bot_callback_flow_workspace_vpn_sets_power_flow():
    ui = TelegramBotApi("token")
    ui.send_message = MagicMock(return_value={"message_id": 333})
    ui.answer_callback = MagicMock()
    mqtt = MagicMock()
    mqtt.system_session = "locked"
    mqtt.system_status = "online"
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.power_flow.message_id = 333
    app.save_state = MagicMock()

    with patch.object(PowerFlowManager, "save_state"), patch.object(PowerFlowManager, "load_state"):
        call = {
            "id": "cb-ws-vpn-happ",
            "data": "flow_workspace_vpn:happ",
            "from": {"id": ALLOWED_USER_ID},
            "message": {"message_id": 333, "chat": {"id": ALLOWED_USER_ID}},
        }
        app.handle_callback(call)

        assert app.power_flow.auto_launch_workspace is True
        assert app.power_flow.target_workspace_vpn == "happ"
        assert app.power_flow.stage == PowerFlowStage.UNLOCKING
        assert ui.send_message.called
        first_call_text = ui.send_message.call_args_list[0][0][1]
        assert "Happ VPN" in first_call_text
        assert "Запуск начнётся сразу после разблокировки" in first_call_text


def test_power_flow_on_unlock_success_calls_workspace_cb_with_vpn():
    ui = MagicMock()
    mqtt = MagicMock()

    with patch.object(PowerFlowManager, "load_state"), patch.object(PowerFlowManager, "save_state"):
        flow = PowerFlowManager(ui, mqtt)
        ws_started = []
        flow.on_workspace_start_cb = lambda cid, card_message_id=None, workspace_id=None, vpn=None: ws_started.append((cid, workspace_id, vpn))
        flow.auto_launch_workspace = True
        flow.target_workspace_vpn = "happ"
        flow.stage = PowerFlowStage.UNLOCKING

        flow.on_unlock_success(ALLOWED_USER_ID)

        assert len(ws_started) == 1
        assert ws_started[0][0] == ALLOWED_USER_ID
        assert ws_started[0][2] == "happ"
        assert flow.auto_launch_workspace is False
        assert flow.stage == PowerFlowStage.WORKSPACE_RUNNING



