"""Exercise synchronous transport replies against real gateway and UI handlers."""

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from vds_bot.bot import PcControlBotApp
from vds_bot.mqtt_gateway import MqttGateway
from vds_bot.routers.monitoring_router import route_monitoring_callback


@pytest.fixture
def app():
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.allowed_user_id = 123
    app.ui = MagicMock()
    app.ui.send_message.return_value = {"message_id": 555}
    app.ui.back_button.return_value = [{"text": "Назад", "callback_data": "menu"}]
    app.save_state = MagicMock()
    app.state_lock = threading.RLock()
    app.power_flow = MagicMock()
    app.physical_btn = MagicMock()
    app.monitoring = MagicMock()
    app.pending_console_cmds = {}
    app.early_console_acks = {}
    app.pending_screenshots = {}
    app.pending_shutdown_cmd_id = None
    app._main_menu_markup = MagicMock(return_value={})
    app._power_menu_markup = MagicMock(return_value={})
    app._executor = SimpleNamespace(submit=lambda fn, *args: fn(*args))
    app.mqtt = MqttGateway("offline", "offline")
    app.mqtt.mqtt_online = True
    now = time.time()
    app.mqtt.health.update("windows_system", "online", ts=now)
    app.mqtt.health.update("windows_desktop", "online", ts=now)
    app.mqtt.health.evaluate_pc_state = MagicMock(return_value=SimpleNamespace(state="ONLINE"))
    return app


def reply_from_publish(app, result="executed"):
    def publish(topic, payload, qos=1):
        if not payload.startswith("{"):
            return True
        request = json.loads(payload)
        ack = {"id": request["id"], "result": result, "host": "offline", "timestamp": 1000}
        if request["action"] in {"cmd", "powershell"}:
            ack["data"] = {"exit_code": 0, "stdout": "offline reply", "stderr": ""}
        app.mqtt._handle_ack(ack)
        app._on_mqtt_event("ack", ack)
        return True
    return publish


@pytest.mark.parametrize("target", ["desktop", "system"])
def test_console_fast_real_ack_is_delivered_once(app, target):
    app.mqtt.publish = reply_from_publish(app)
    app._handle_console_ack = MagicMock()
    app.execute_console_command(123, "echo offline", target=target, shell="cmd")
    app._handle_console_ack.assert_called_once()
    assert app.pending_console_cmds == {}
    assert app.mqtt.pending_commands == {}


def test_console_publish_failure_clears_registered_request(app):
    app.mqtt.publish = MagicMock(return_value=False)
    app.execute_console_command(123, "echo offline", target="desktop", shell="cmd")
    assert app.pending_console_cmds == {}
    assert app.mqtt.pending_commands == {}


def test_screenshot_fast_reply_is_not_discarded(app):
    def publish(topic, payload, qos=1):
        request = json.loads(payload)
        header = json.dumps({"id": request["id"], "format": "jpeg", "width": 1, "height": 1}).encode()
        image = b"\xff\xd8offline\xff\xd9"
        app._on_mqtt_event("screenshot_image", len(header).to_bytes(4, "big") + header + image)
        return True
    app.mqtt.publish = publish
    assert route_monitoring_callback(app, "screenshot", 123, 555, "callback")
    app.ui.send_photo.assert_called_once()
    assert app.pending_screenshots == {}


def test_screenshot_publish_failure_clears_registered_request(app):
    app.mqtt.publish = MagicMock(return_value=False)
    assert route_monitoring_callback(app, "screenshot", 123, 555, "callback")
    assert app.pending_screenshots == {}


def test_shutdown_fast_ack_remains_acknowledged(app):
    app.mqtt.publish = reply_from_publish(app)
    assert app._request_shutdown(123, "manual")
    assert app.shutdown_acknowledged is True


def test_shutdown_fast_rejection_does_not_restore_pending(app):
    app.mqtt.publish = reply_from_publish(app, result="failed")
    app._request_shutdown(123, "manual")
    assert app.pending_shutdown_cmd_id is None
    assert app.shutdown_acknowledged is False


def test_shutdown_publish_failure_clears_registered_request(app):
    app.mqtt.publish = MagicMock(return_value=False)
    assert not app._request_shutdown(123, "manual")
    assert app.pending_shutdown_cmd_id is None

