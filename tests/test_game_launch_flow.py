import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent import PCControlAgent
from vds_bot.bot import ALLOWED_USER_ID, PcControlBotApp, get_game_steps, GAME_INFO
from vds_bot.power_flow import PowerFlowManager, PowerFlowStage
from vds_bot.telegram_ui import TelegramBotApi


# ==============================================================================
# Game Steps Tests
# ==============================================================================

def test_get_game_steps_gta5_incy():
    steps = get_game_steps("gta5", "incy")
    step_ids = [s["id"] for s in steps]
    assert step_ids == ["incy_vpn", "antigravity", "game_gta5"]
    assert steps[0]["label"] == "INCY (VPN)"
    assert steps[1]["label"] == "Antigravity"
    assert steps[2]["label"] == "GTA 5 Legacy"


def test_get_game_steps_roblox_happ():
    steps = get_game_steps("roblox", "happ")
    step_ids = [s["id"] for s in steps]
    assert step_ids == ["happ_vpn", "antigravity", "game_roblox"]
    assert steps[0]["label"] == "Happ (VPN)"
    assert steps[1]["label"] == "Antigravity"
    assert steps[2]["label"] == "Roblox"


def test_get_game_steps_gtasa_incy():
    steps = get_game_steps("gtasa", "incy")
    step_ids = [s["id"] for s in steps]
    assert step_ids == ["incy_vpn", "antigravity", "game_gtasa"]
    assert steps[2]["label"] == "GTA SA"


def test_get_game_steps_minecraft_incy():
    steps = get_game_steps("minecraft", "incy")
    step_ids = [s["id"] for s in steps]
    assert step_ids == ["incy_vpn", "antigravity", "game_minecraft"]
    assert steps[0]["label"] == "INCY (VPN)"
    assert steps[1]["label"] == "Antigravity"
    assert steps[2]["label"] == "Minecraft"


def test_get_game_steps_warcraft3_happ():
    steps = get_game_steps("warcraft3", "happ")
    step_ids = [s["id"] for s in steps]
    assert step_ids == ["happ_vpn", "antigravity", "game_warcraft3"]
    assert steps[0]["label"] == "Happ (VPN)"
    assert steps[1]["label"] == "Antigravity"
    assert steps[2]["label"] == "Warcraft 3"


# ==============================================================================
# PowerFlowManager Game Mode Tests
# ==============================================================================

def test_power_flow_start_power_on_includes_game_button():
    ui = TelegramBotApi("token")
    ui.send_message = MagicMock(return_value={"message_id": 101})
    mqtt = MagicMock()
    mqtt.health.evaluate_pc_state.return_value = MagicMock(state="OFF")
    mqtt.system_status = "offline"
    mqtt.desktop_status = "offline"
    mqtt.esp_status = "online"

    with patch.object(PowerFlowManager, "load_state"), patch.object(PowerFlowManager, "save_state"):
        flow = PowerFlowManager(ui, mqtt)
        flow.start_power_on(ALLOWED_USER_ID)

        assert ui.send_message.called
        sent_markup = ui.send_message.call_args[1].get("reply_markup")
        callbacks = [btn["callback_data"] for row in sent_markup["inline_keyboard"] for btn in row]
        assert "flow_unlock_and_game" in callbacks
        assert "flow_unlock_and_workspace" in callbacks
        assert "flow_unlock_yes" in callbacks
        assert "flow_unlock_no" in callbacks


def test_power_flow_set_auto_launch_game():
    ui = TelegramBotApi("token")
    ui.send_message = MagicMock(return_value={"message_id": 102})
    mqtt = MagicMock()
    mqtt.system_session = "locked"
    mqtt.system_status = "online"

    with patch.object(PowerFlowManager, "load_state"), patch.object(PowerFlowManager, "save_state"):
        flow = PowerFlowManager(ui, mqtt)
        flow.message_id = 999
        flow.set_auto_launch_game(game="gta5", vpn="incy", chat_id=ALLOWED_USER_ID)

        assert flow.auto_launch_game is True
        assert flow.target_game == "gta5"
        assert flow.target_game_vpn == "incy"
        assert flow.stage == PowerFlowStage.UNLOCKING
        assert ui.send_message.called
        first_call_text = ui.send_message.call_args_list[0][0][1]
        assert "GTA 5 Legacy" in first_call_text
        assert "INCY VPN" in first_call_text


def test_power_flow_on_unlock_success_calls_game_cb():
    ui = TelegramBotApi("token")
    ui.send_message = MagicMock()
    mqtt = MagicMock()

    with patch.object(PowerFlowManager, "load_state"), patch.object(PowerFlowManager, "save_state"):
        flow = PowerFlowManager(ui, mqtt)
        game_started = []
        flow.on_game_start_cb = lambda cid, card_message_id=None, game="gta5", vpn="incy": game_started.append((cid, game, vpn))
        flow.auto_launch_game = True
        flow.target_game = "roblox"
        flow.target_game_vpn = "happ"
        flow.stage = PowerFlowStage.UNLOCKING

        flow.on_unlock_success(ALLOWED_USER_ID)

        assert len(game_started) == 1
        assert game_started[0] == (ALLOWED_USER_ID, "roblox", "happ")
        assert flow.auto_launch_game is False


# ==============================================================================
# Bot Callback and Routing Tests
# ==============================================================================

def test_bot_callback_flow_unlock_and_game():
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
        "id": "cb-game-1",
        "data": "flow_unlock_and_game",
        "from": {"id": ALLOWED_USER_ID},
        "message": {"message_id": 999, "chat": {"id": ALLOWED_USER_ID}},
    }
    app.handle_callback(call)

    assert ui.send_message.called
    sent_text = ui.send_message.call_args[0][1]
    assert "Выберите игру" in sent_text
    sent_markup = ui.send_message.call_args[1].get("reply_markup")
    all_callbacks = [btn["callback_data"] for row in sent_markup["inline_keyboard"] for btn in row]
    assert "flow_game:minecraft" in all_callbacks
    assert "flow_game:warcraft3" in all_callbacks
    assert "flow_game:gta5" in all_callbacks
    assert "flow_game:roblox" in all_callbacks
    assert "flow_game:gtasa" in all_callbacks

    # Verify custom emoji icons are attached to buttons
    btn_by_cb = {btn["callback_data"]: btn for row in sent_markup["inline_keyboard"] for btn in row if "callback_data" in btn}
    assert btn_by_cb["flow_game:minecraft"].get("icon_custom_emoji_id")
    assert btn_by_cb["flow_game:warcraft3"].get("icon_custom_emoji_id")
    assert btn_by_cb["flow_game:gta5"].get("icon_custom_emoji_id")
    assert btn_by_cb["flow_game:roblox"].get("icon_custom_emoji_id")
    assert btn_by_cb["flow_game:gtasa"].get("icon_custom_emoji_id")


def test_bot_callback_flow_game_choice():
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
        "id": "cb-game-2",
        "data": "flow_game:gta5",
        "from": {"id": ALLOWED_USER_ID},
        "message": {"message_id": 999, "chat": {"id": ALLOWED_USER_ID}},
    }
    app.handle_callback(call)

    assert ui.send_message.called
    sent_text = ui.send_message.call_args[0][1]
    assert "Выберите VPN" in sent_text
    sent_markup = ui.send_message.call_args[1].get("reply_markup")
    all_callbacks = [btn["callback_data"] for row in sent_markup["inline_keyboard"] for btn in row]
    assert "flow_game_vpn:gta5:incy" in all_callbacks
    assert "flow_game_vpn:gta5:happ" in all_callbacks


def test_bot_callback_flow_game_vpn_sets_power_flow():
    ui = TelegramBotApi("token")
    ui.answer_callback = MagicMock()
    mqtt = MagicMock()
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.save_state = MagicMock()

    with patch.object(app.power_flow, "set_auto_launch_game") as mock_set:
        call = {
            "id": "cb-game-3",
            "data": "flow_game_vpn:gtasa:happ",
            "from": {"id": ALLOWED_USER_ID},
            "message": {"message_id": 999, "chat": {"id": ALLOWED_USER_ID}},
        }
        app.handle_callback(call)

        mock_set.assert_called_once_with(game="gtasa", vpn="happ", chat_id=ALLOWED_USER_ID)


def test_bot_callback_press_esc():
    ui = TelegramBotApi("token")
    ui.answer_callback = MagicMock()
    mqtt = MagicMock()
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.save_state = MagicMock()

    call = {
        "id": "cb-esc",
        "data": "press_esc",
        "from": {"id": ALLOWED_USER_ID},
        "message": {"message_id": 999, "chat": {"id": ALLOWED_USER_ID}},
    }
    app.handle_callback(call)

    mqtt.publish_desktop_action.assert_called_once_with("press_esc", {"count": 1})


def test_bot_message_esc_command():
    ui = TelegramBotApi("token")
    ui.send_message = MagicMock()
    mqtt = MagicMock()
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.monitoring = MagicMock()
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.save_state = MagicMock()

    msg = {
        "chat": {"id": ALLOWED_USER_ID, "type": "private"},
        "from": {"id": ALLOWED_USER_ID},
        "text": "/esc 3",
    }
    app.handle_message(msg)

    mqtt.publish_desktop_action.assert_called_once_with("press_esc", {"count": 3})
    assert ui.send_message.called
    assert "3x" in ui.send_message.call_args[0][1]


# ==============================================================================
# Desktop Agent ESC Simulation Tests
# ==============================================================================

def test_agent_send_key_esc():
    with patch("ctypes.windll.user32.keybd_event") as mock_keybd:
        res = PCControlAgent.send_key_esc(count=2, delay=0.01)
        assert res.get("success") is True
        assert res.get("count") == 2
        # Each press triggers keydown (1) and keyup (1), total 4 calls for count=2
        assert mock_keybd.call_count == 4
