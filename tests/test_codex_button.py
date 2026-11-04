from unittest.mock import MagicMock

from vds_bot.routers.workspace_router import route_workspace_callback
from vds_bot.telegram_ui import TelegramBotApi


def test_codex_button_dispatches_existing_chatgpt_target():
    ui = TelegramBotApi("test")
    buttons = [button for row in ui.apps_menu_markup()["inline_keyboard"] for button in row]
    button = next(item for item in buttons if item["text"] == "Codex")
    assert button["callback_data"] == "app:ChatGPT"
    assert button.get("icon_custom_emoji_id")

    app = MagicMock()
    app.mqtt.desktop_status = "online"
    assert route_workspace_callback(app, button["callback_data"], 1, 2, "callback")
    app.mqtt.publish_desktop_action.assert_called_once_with("launch_app", {"app": "ChatGPT"})
