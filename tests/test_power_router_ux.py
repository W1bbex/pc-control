"""Regression tests for power menu confirmation steps and card cleanup."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from vds_bot.power_scheduler import PowerSchedulerMixin
from vds_bot.routers.power_router import route_power_callback
from vds_bot.ui_markups import UiMarkupsMixin


class DummyUi(UiMarkupsMixin):
    def __init__(self):
        self.sent = []
        self.callbacks = []

    def send_message(self, chat_id, text, reply_markup=None, message_id=None):
        self.sent.append((chat_id, text, reply_markup, message_id))
        return {"message_id": message_id or len(self.sent)}

    def answer_callback(self, callback_id, text=None, alert=False):
        self.callbacks.append((callback_id, text, alert))


class DummySchedulerApp(PowerSchedulerMixin):
    def __init__(self, ui):
        self.ui = ui
        self.pending_shutdown_cmd_id = None
        self.pending_shutdown_started_at = None
        self.shutdown_acknowledged = False
        self.shutdown_offline_detected_at = None
        self.monitoring = SimpleNamespace(telemetry_age=lambda: 1.0)
        self.mqtt = SimpleNamespace(
            mqtt_online=True,
            system_status="online",
            desktop_status="online",
            health=SimpleNamespace(
                evaluate_pc_state=lambda age, now=None: SimpleNamespace(state="READY")
            ),
            publish_system_action=MagicMock(return_value="cmd-shut-1"),
            publish_desktop_action=MagicMock(return_value="cmd-shut-d1"),
            publish_esp_action=MagicMock(),
        )
        self.save_state = MagicMock()

    def _power_menu_markup(self):
        return {"inline_keyboard": [self.ui.back_button("menu")]}


def test_shutdown_and_reboot_markups_require_confirmation_step():
    ui = DummyUi()
    shutdown_buttons = [
        btn["callback_data"]
        for row in ui.shutdown_menu_markup()["inline_keyboard"]
        for btn in row
    ]
    assert "shutdown_now" in shutdown_buttons
    assert "shutdown_now_confirm" not in shutdown_buttons

    reboot_buttons = [
        btn["callback_data"]
        for row in ui.reboot_menu_markup()["inline_keyboard"]
        for btn in row
    ]
    assert "reboot_ask:unlock" in reboot_buttons
    assert "reboot_ask:plain" in reboot_buttons
    assert "reboot_unlock" not in reboot_buttons
    assert "reboot_confirm" not in reboot_buttons


def test_shutdown_now_asks_confirmation_then_updates_card_on_confirm():
    ui = DummyUi()
    app = DummySchedulerApp(ui)

    handled = route_power_callback(app, "shutdown_now", chat_id=101, message_id=55, callback_id="cb-1")
    assert handled is True
    app.mqtt.publish_system_action.assert_not_called()
    assert len(ui.sent) == 1
    _, text, markup, msg_id = ui.sent[0]
    assert msg_id == 55
    assert "Вы уверены" in text
    callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
    assert callbacks == ["shutdown_now_confirm", "shutdown_pc"]

    handled = route_power_callback(app, "shutdown_now_confirm", chat_id=101, message_id=55, callback_id="cb-2")
    assert handled is True
    app.mqtt.publish_system_action.assert_called_once_with("shutdown")
    assert len(ui.sent) == 2
    _, sent_text, sent_markup, sent_msg_id = ui.sent[1]
    assert sent_msg_id == 55
    assert "Команда выключения отправлена" in sent_text
    assert sent_markup == {"inline_keyboard": [ui.back_button("power_menu")]}


def test_reboot_ask_shows_confirmation_and_guards_offline():
    ui = DummyUi()
    app = SimpleNamespace(
        ui=ui,
        mqtt=SimpleNamespace(system_status="online", desktop_status="offline", publish_system_action=MagicMock()),
        power_flow=SimpleNamespace(start_reboot_flow=MagicMock()),
    )

    assert route_power_callback(app, "reboot_ask:unlock", chat_id=101, message_id=77, callback_id="cb-r1")
    app.mqtt.publish_system_action.assert_not_called()
    _, _, markup_unlock, msg_id_unlock = ui.sent[-1]
    assert msg_id_unlock == 77
    assert [b["callback_data"] for r in markup_unlock["inline_keyboard"] for b in r] == ["reboot_unlock", "reboot_pc"]

    assert route_power_callback(app, "reboot_ask:plain", chat_id=101, message_id=78, callback_id="cb-r2")
    app.mqtt.publish_system_action.assert_not_called()
    _, _, markup_plain, msg_id_plain = ui.sent[-1]
    assert msg_id_plain == 78
    assert [b["callback_data"] for r in markup_plain["inline_keyboard"] for b in r] == ["reboot_confirm", "reboot_pc"]

    app.mqtt.system_status = "offline"
    sent_before = len(ui.sent)
    assert route_power_callback(app, "reboot_ask:plain", chat_id=101, message_id=79, callback_id="cb-r3")
    assert len(ui.sent) == sent_before
    assert ui.callbacks[-1][2] is True


def test_sleep_pc_and_sleep_confirm_update_card_and_check_online():
    ui = DummyUi()
    app = SimpleNamespace(
        ui=ui,
        mqtt=SimpleNamespace(
            system_status="online",
            desktop_status="offline",
            publish_system_action=MagicMock(return_value="cmd-sleep"),
            publish_desktop_action=MagicMock(),
            publish_esp_action=MagicMock(),
        ),
    )

    assert route_power_callback(app, "sleep_pc", chat_id=101, message_id=88, callback_id="cb-s1")
    _, _, confirm_markup, confirm_msg_id = ui.sent[-1]
    assert confirm_msg_id == 88
    cancel_btn = confirm_markup["inline_keyboard"][0][1]
    assert cancel_btn["callback_data"] == "power_menu"
    assert "✖" not in cancel_btn["text"]

    assert route_power_callback(app, "sleep_confirm", chat_id=101, message_id=88, callback_id="cb-s2")
    app.mqtt.publish_system_action.assert_called_once_with("sleep")
    app.mqtt.publish_esp_action.assert_called_once_with("LED_OFF")
    _, sent_text, sent_markup, sent_msg_id = ui.sent[-1]
    assert sent_msg_id == 88
    assert "Команда сна отправлена" in sent_text
    assert sent_markup == {"inline_keyboard": [ui.back_button("power_menu")]}

    app.mqtt.system_status = "offline"
    assert route_power_callback(app, "sleep_confirm", chat_id=101, message_id=89, callback_id="cb-s3")
    assert app.mqtt.publish_system_action.call_count == 1
    assert ui.callbacks[-1][2] is True
