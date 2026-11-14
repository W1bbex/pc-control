"""Tests for screenshot payload round-trip timeout and scheduled shutdown persistence."""

import json
import threading
import time

import vds_bot.bot as bot_mod
from vds_bot.bot import PcControlBotApp
from vds_bot.health_engine import HealthEngine
from vds_bot.monitoring import MonitoringManager


class FakeUi:
    def __init__(self):
        self.messages = []
        self.photos = []
        self.callbacks = []

    def send_message(self, chat_id, text, reply_markup=None, message_id=None):
        self.messages.append((chat_id, text, reply_markup, message_id))
        return {"message_id": message_id or len(self.messages)}

    def send_photo(self, chat_id, image_bytes, caption=None, filename="image.png"):
        self.photos.append((chat_id, image_bytes, caption, filename))
        return {"message_id": 99}

    def answer_callback(self, callback_id, text=None, alert=False):
        self.callbacks.append((callback_id, text, alert))

    def main_menu_markup(self, *args, **kwargs):
        return {"inline_keyboard": [[{"text": "menu"}]]}

    def power_menu_markup(self, *args, **kwargs):
        return {"inline_keyboard": [[{"text": "power"}]]}

    def shutdown_menu_markup(self, scheduled_ts=None, *args, **kwargs):
        return {"inline_keyboard": [[{"text": "shutdown"}]]}

    @staticmethod
    def back_button(target="menu"):
        return [{"text": "back", "callback_data": target}]


class FakeMqtt:
    system_status = "online"
    system_session = "unlocked"
    desktop_status = "online"
    esp_status = "online"
    mqtt_online = True

    def __init__(self):
        self.actions = []
        self.health = HealthEngine()
        self.health.update("mqtt_broker", "online")
        self.health.update("windows_system", "online", ts=time.time())
        self.health.update("windows_desktop", "online", ts=time.time())
        self.health.update("windows_session", "unlocked", ts=time.time())

    def publish_system_action(self, action, args=None):
        cmd_id = f"sys-{len(self.actions) + 1}"
        self.actions.append(("system", action, args, cmd_id))
        return cmd_id

    def publish_desktop_action(self, action, args=None):
        cmd_id = f"desk-{len(self.actions) + 1}"
        self.actions.append(("desktop", action, args, cmd_id))
        return cmd_id


class FakeHistory:
    def insert(self, payload):
        pass


class NoopFlow:
    def on_system_session_update(self, session, chat_id):
        pass

    def handle_ack(self, data):
        pass


class NoopPhysical:
    saved_password = None

    def handle_ack(self, data, chat_id, save_callback):
        pass


def make_app(monkeypatch, state_path):
    monkeypatch.setattr(bot_mod, "ALLOWED_USER_ID", 12345)
    monkeypatch.setattr(bot_mod, "STATE_PATH", state_path)
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = FakeUi()
    app.mqtt = FakeMqtt()
    app.power_flow = NoopFlow()
    app.physical_btn = NoopPhysical()
    app.monitoring = MonitoringManager.__new__(MonitoringManager)
    app.monitoring.ui = app.ui
    app.monitoring.mqtt = app.mqtt
    app.monitoring.history = FakeHistory()
    app.monitoring.latest_telemetry = None
    app.monitoring.latest_received_at = 0.0
    app.monitoring.alerts = {}
    app.state_lock = threading.RLock()
    app.offset = 0
    app.pending_screenshots = {}
    app.pending_telemetry_cmd_ids = {}
    app.awaiting_shutdown_time_chat_id = None
    app.shutdown_candidate_ts = None
    app.scheduled_shutdown_ts = None
    app.pending_shutdown_cmd_id = None
    app.pending_shutdown_started_at = None
    app.shutdown_acknowledged = False
    app.shutdown_offline_detected_at = None
    app.last_schedule_attempt_at = 0.0
    app.active_workspace_cmd_id = None
    app.pending_minimize_cmd_id = None
    return app


def callback(data, message_id=50):
    return {
        "id": "cb-1",
        "data": data,
        "message": {"chat": {"id": 12345}, "message_id": message_id},
    }


def test_screenshot_round_trip_and_timeout(tmp_path, monkeypatch):
    app = make_app(monkeypatch, tmp_path / "state.json")
    app.handle_callback(callback("screenshot"))
    cmd_id = app.mqtt.actions[-1][3]
    assert cmd_id in app.pending_screenshots

    header = json.dumps({"id": cmd_id, "format": "jpeg", "width": 100, "height": 50}).encode()
    jpeg = b"\xff\xd8test-image\xff\xd9"
    app._on_mqtt_event("screenshot_image", len(header).to_bytes(4, "big") + header + jpeg)
    assert app.ui.photos[0][1] == jpeg
    assert app.ui.photos[0][3] == "pc-screenshot.jpg"
    assert cmd_id not in app.pending_screenshots

    app.pending_screenshots["late"] = {"chat_id": 12345, "requested_at": 10.0}
    app.scheduler_tick(now=46.0)
    assert "late" not in app.pending_screenshots
    assert "35 секунд" in app.ui.messages[-1][1]


def test_schedule_persists_dispatches_and_confirms_offline(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    app = make_app(monkeypatch, state_path)
    target = time.time() + 300
    app._set_shutdown_schedule(12345, target)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["scheduled_shutdown_ts"] == target

    restarted = make_app(monkeypatch, state_path)
    restarted.load_state()
    assert restarted.scheduled_shutdown_ts == target

    app.scheduler_tick(now=target + 1)
    assert app.mqtt.actions[-1][1] == "shutdown"
    assert app.scheduled_shutdown_ts is None
    cmd_id = app.pending_shutdown_cmd_id
    assert cmd_id

    app._on_mqtt_event("ack", {"id": cmd_id, "result": "executed", "data": {}})
    assert app.shutdown_acknowledged is True
    assert "Windows приняла" in app.ui.messages[-1][1]

    app.mqtt.system_status = "offline"
    app.mqtt.desktop_status = "offline"
    app.mqtt.health.update("windows_system", "offline")
    app.mqtt.health.update("windows_desktop", "offline")

    app.scheduler_tick(now=target + 2)
    assert app.pending_shutdown_cmd_id is not None

    app.scheduler_tick(now=target + 12)
    assert app.pending_shutdown_cmd_id is not None

    app.scheduler_tick(now=target + 23)
    assert app.pending_shutdown_cmd_id is None
    assert "ПК перестал отвечать" in app.ui.messages[-1][1]
    assert app.mqtt.health.evaluate_pc_state().state == "SHUTDOWN_OBSERVED"


def test_exact_moscow_time_requires_confirmation(tmp_path, monkeypatch):
    app = make_app(monkeypatch, tmp_path / "state.json")
    app.awaiting_shutdown_time_chat_id = 12345
    app.handle_message({"chat": {"id": 12345}, "text": "23:45"})
    assert app.shutdown_candidate_ts is not None
    markup = app.ui.messages[-1][2]
    confirm_data = markup["inline_keyboard"][0][0]["callback_data"]
    assert confirm_data.startswith("shutdown_schedule_confirm:")
    assert "МСК" in app.ui.messages[-1][1]
