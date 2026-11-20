"""Integration tests for workspace flow execution, cancellation, card recovery, and binary screenshot parsing."""

import json
import threading
import time

import vds_bot.bot as bot_mod
import vds_bot.power_flow as power_flow_mod
from agent import PCControlAgent
from vds_bot.bot import PcControlBotApp, unpack_screenshot_payload
from vds_bot.health_engine import HealthEngine
from vds_bot.history import TelemetryHistory
from vds_bot.monitoring import MonitoringManager
from vds_bot.physical_button import PhysicalButtonManager
from vds_bot.power_flow import PowerFlowManager, PowerFlowStage
from vds_bot.telegram_ui import TelegramBotApi


class MockTelegramUi:
    def __init__(self):
        self.sent_messages = []
        self.edited_messages = []
        self.photos = []
        self.callback_answers = []

    def send_message(self, chat_id, text, reply_markup=None, message_id=None):
        msg = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
            "message_id": message_id or len(self.sent_messages) + 100,
        }
        if message_id:
            self.edited_messages.append(msg)
        else:
            self.sent_messages.append(msg)
        return {"message_id": msg["message_id"]}

    def send_photo(self, chat_id, image_bytes, caption=None, filename="screenshot.jpg"):
        self.photos.append({"chat_id": chat_id, "bytes": image_bytes, "caption": caption, "filename": filename})
        return {"message_id": 999}

    def answer_callback(self, callback_id, text=None, alert=False):
        self.callback_answers.append({"id": callback_id, "text": text, "alert": alert})

    def back_button(self, target="menu"):
        return [{"text": "↩ Назад", "callback_data": target}]

    def main_menu_markup(self, *args, **kwargs):
        return {"inline_keyboard": []}

    def power_menu_markup(self, *args, **kwargs):
        return {"inline_keyboard": []}

    def monitoring_menu_markup(self, *args, **kwargs):
        return {"inline_keyboard": []}

    def apps_menu_markup(self, *args, **kwargs):
        return {"inline_keyboard": []}

    def history_menu_markup(self, *args, **kwargs):
        return {"inline_keyboard": []}

    def settings_menu_markup(self, *args, **kwargs):
        return {"inline_keyboard": []}

    def power_menu_text(self, *args, **kwargs):
        return "<b>⚡ ПК и экран</b>\n\nСостояние: Готов к работе"

    def apps_menu_text(self, *args, **kwargs):
        return "<b>Программы</b>"

    def sound_menu_text(self, *args, **kwargs):
        return "<b>Звук</b>"

    def history_menu_text(self, *args, **kwargs):
        return "<b>Графики</b>"

    def workspace_vpn_choice_text(self, *args, **kwargs):
        return "<b>Запуск рабочего сетапа</b>\n\nВыберите VPN для подключения:"


class MockMqttGateway:
    def __init__(self):
        self.published_messages = []
        self.system_status = "online"
        self.system_session = "unlocked"
        self.esp_status = "online"
        self.desktop_status = "online"
        self.mqtt_online = True
        self.health = HealthEngine()
        self.health.update("mqtt_broker", "online")
        self.health.update("windows_system", "online", ts=time.time())
        self.health.update("windows_desktop", "online", ts=time.time())
        self.health.update("windows_session", "unlocked", ts=time.time())
        self.health.update("telemetry", "fresh", ts=time.time())

    def publish_esp_action(self, action, value=None):
        cmd_id = f"esp-mock-{len(self.published_messages)}"
        self.published_messages.append(("esp", action, value, cmd_id))
        return cmd_id

    def publish_system_action(self, action, args=None, **kwargs):
        cmd_id = f"sys-mock-{len(self.published_messages)}"
        self.published_messages.append(("system", action, args, cmd_id))
        return cmd_id

    def publish_desktop_action(self, action, args=None, **kwargs):
        cmd_id = f"desk-mock-{len(self.published_messages)}"
        self.published_messages.append(("desktop", action, args, cmd_id))
        return cmd_id


def test_workspace_setup_driven_by_progress_acks_and_minimize_all(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "ALLOWED_USER_ID", 12345)
    monkeypatch.setattr(power_flow_mod, "FLOW_STATE_PATH", tmp_path / "flow_state.json")

    ui = MockTelegramUi()
    mqtt = MockMqttGateway()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.physical_btn = PhysicalButtonManager(ui, mqtt)
    app.monitoring = MonitoringManager(ui, mqtt, history_db_path=tmp_path / "telemetry_history.db")
    app.workspace_cancel_event = threading.Event()
    app.happ_ready_event = threading.Event()
    app.workspace_executed_event = threading.Event()
    app.minimize_ack_event = threading.Event()
    app.workspace_execution_result = None
    app.minimize_ack_result = None
    app.pending_screenshot_cmd_id = None
    app.active_workspace_cmd_id = None
    app.pending_minimize_cmd_id = None
    app.workspace_card_msg_id = None
    app.app_statuses = {}

    ws_id = "desk-ws-test-1"
    runner_thread = threading.Thread(
        target=app.run_workspace_setup_steps,
        args=(12345, None, ws_id),
        daemon=True,
    )
    runner_thread.start()
    time.sleep(0.2)

    assert len(mqtt.published_messages) == 1
    _, action, args, cmd_id = mqtt.published_messages[0]
    assert action == "launch_app"
    assert args["app"] == "Рабочий сетап"
    assert args["workspace_id"] == ws_id
    assert args.get("vpn") in {"happ", "incy"}

    for step_id, app_name in [
        ("telegram", "Telegram"),
        ("yandex_music", "YandexMusic"),
        ("dolphin", "Dolphin"),
        ("chrome", "Chrome"),
        ("chatgpt", "ChatGPT"),
    ]:
        app._on_mqtt_event("ack", {
            "id": cmd_id,
            "result": "progress",
            "data": {"step_id": step_id, "app": app_name, "state": "verified"},
        })
        time.sleep(0.05)

    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "progress",
        "data": {"step_id": "happ_vpn", "app": "Happ", "state": "connecting"},
    })
    time.sleep(0.05)

    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "progress",
        "data": {"step_id": "happ_vpn", "app": "Happ", "state": "verified"},
    })
    assert app.happ_ready_event.is_set(), "Happ ready event not set"

    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "progress",
        "data": {"step_id": "antigravity", "app": "Antigravity", "state": "verified"},
    })
    time.sleep(0.05)

    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "executed",
        "data": {"desktop_minimized": False},
    })

    time.sleep(0.2)
    assert len(mqtt.published_messages) == 2
    assert mqtt.published_messages[1][1] == "minimize_all"
    min_cmd_id = mqtt.published_messages[1][3]

    app._on_mqtt_event("ack", {
        "id": min_cmd_id,
        "result": "executed",
        "data": {},
    })

    runner_thread.join(timeout=2.0)
    assert not runner_thread.is_alive(), "Runner thread did not terminate"

    last_edited = ui.edited_messages[-1]
    assert "✅ Рабочий сетап успешно запущен!" in last_edited["text"]


def test_workspace_setup_cancellation_aborts_before_minimize_all(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "ALLOWED_USER_ID", 12345)
    monkeypatch.setattr(power_flow_mod, "FLOW_STATE_PATH", tmp_path / "flow_state.json")

    ui = MockTelegramUi()
    mqtt = MockMqttGateway()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.workspace_cancel_event = threading.Event()
    app.happ_ready_event = threading.Event()
    app.workspace_executed_event = threading.Event()
    app.minimize_ack_event = threading.Event()
    app.workspace_execution_result = None
    app.minimize_ack_result = None
    app.workspace_card_msg_id = None
    app.app_statuses = {}

    ws_id = "desk-ws-cancel-test"
    app.power_flow.stage = PowerFlowStage.WORKSPACE_RUNNING
    app.power_flow.active_workspace_id = ws_id
    app.power_flow.workspace_cancelled = False

    runner_thread = threading.Thread(
        target=app.run_workspace_setup_steps,
        args=(12345, None, ws_id),
        daemon=True,
    )
    runner_thread.start()
    time.sleep(0.2)

    assert len(mqtt.published_messages) == 1
    assert mqtt.published_messages[0][1] == "launch_app"

    app.power_flow.cancel_workspace(12345)
    app.workspace_cancel_event.set()

    assert len(mqtt.published_messages) == 2
    assert mqtt.published_messages[1][1] == "cancel_workspace"
    assert mqtt.published_messages[1][2] == {"workspace_id": ws_id}

    runner_thread.join(timeout=2.0)
    assert not runner_thread.is_alive(), "Runner thread did not terminate after cancellation"

    published_actions = [msg[1] for msg in mqtt.published_messages]
    assert "minimize_all" not in published_actions, "minimize_all must not run after cancellation"

    all_texts = [msg["text"] for msg in ui.sent_messages + ui.edited_messages]
    assert not any("Рабочий сетап успешно запущен" in t for t in all_texts)

    assert app.power_flow.stage == PowerFlowStage.IDLE
    assert app.power_flow.workspace_cancelled is False


def test_bot_restart_during_unlock_updates_card_with_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(power_flow_mod, "FLOW_STATE_PATH", tmp_path / "flow_state.json")

    ui = MockTelegramUi()
    mqtt = MockMqttGateway()

    flow_init = PowerFlowManager(ui, mqtt)
    flow_init.stage = PowerFlowStage.UNLOCKING
    flow_init.chat_id = 12345
    flow_init.message_id = 777
    flow_init.created_at = time.time()
    flow_init.expires_at = time.time() + 900
    flow_init.save_state()

    flow_reloaded = PowerFlowManager(ui, mqtt)
    assert flow_reloaded.stage == PowerFlowStage.UNLOCK_FAILED

    assert len(ui.edited_messages) == 1
    edited = ui.edited_messages[0]
    assert edited["message_id"] == 777
    assert "Разблокировка была прервана перезапуском бота" in edited["text"]
    assert edited["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "flow_unlock_retry"


def test_unpack_and_dispatch_binary_screenshot_payload():
    ui = MockTelegramUi()
    mqtt = MockMqttGateway()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.pending_screenshot_cmd_id = "desk-sc-123"

    header_dict = {"id": "desk-sc-123", "format": "jpeg", "width": 1600, "height": 1000}
    header_bytes = json.dumps(header_dict).encode("utf-8")
    header_len = len(header_bytes)
    jpeg_dummy_data = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x48\x00\x48\x00\x00\xff\xd9"
    full_payload = header_len.to_bytes(4, "big") + header_bytes + jpeg_dummy_data

    parsed_header, parsed_jpeg = unpack_screenshot_payload(full_payload)
    assert parsed_header["id"] == "desk-sc-123"
    assert parsed_header["width"] == 1600
    assert parsed_jpeg == jpeg_dummy_data

    app._on_mqtt_event("screenshot_image", full_payload)
    assert len(ui.photos) == 1
    assert ui.photos[0]["bytes"] == jpeg_dummy_data
    assert "Скриншот экрана ПК" in ui.photos[0]["caption"]


def test_workspace_minimize_all_failure_renders_retry_button(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "ALLOWED_USER_ID", 12345)
    monkeypatch.setattr(power_flow_mod, "FLOW_STATE_PATH", tmp_path / "flow_state.json")

    ui = MockTelegramUi()
    mqtt = MockMqttGateway()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.physical_btn = PhysicalButtonManager(ui, mqtt)
    app.monitoring = MonitoringManager(ui, mqtt, history_db_path=tmp_path / "telemetry_history.db")
    app.workspace_cancel_event = threading.Event()
    app.happ_ready_event = threading.Event()
    app.workspace_executed_event = threading.Event()
    app.minimize_ack_event = threading.Event()
    app.workspace_execution_result = None
    app.minimize_ack_result = None
    app.pending_screenshot_cmd_id = None
    app.active_workspace_cmd_id = None
    app.pending_minimize_cmd_id = None
    app.workspace_card_msg_id = None
    app.app_statuses = {}

    ws_id = "desk-ws-test-5"
    runner_thread = threading.Thread(
        target=app.run_workspace_setup_steps,
        args=(12345, None, ws_id),
        daemon=True,
    )
    runner_thread.start()
    time.sleep(0.1)

    cmd_id = mqtt.published_messages[0][3]
    for step_id in ["telegram", "yandex_music", "dolphin", "chrome", "happ_vpn", "antigravity", "chatgpt"]:
        app._on_mqtt_event("ack", {"id": cmd_id, "result": "progress", "data": {"step_id": step_id, "state": "verified"}})
    app._on_mqtt_event("ack", {"id": cmd_id, "result": "executed", "data": {}})

    time.sleep(0.2)
    min_cmd_id = mqtt.published_messages[1][3]

    app._on_mqtt_event("ack", {"id": min_cmd_id, "result": "failed", "data": {}})
    runner_thread.join(timeout=2.0)

    last_edited = ui.edited_messages[-1]
    assert "не удалось свернуть окна" in last_edited["text"]
    assert last_edited["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "minimize_windows"


def test_agent_get_idle_seconds_returns_int_or_none():
    idle = PCControlAgent.get_idle_seconds()
    assert idle is None or isinstance(idle, int), "get_idle_seconds must return int or None"


def test_menu_navigation_edits_existing_message_and_ignores_not_modified(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "ALLOWED_USER_ID", 12345)

    ui = MockTelegramUi()
    mqtt = MockMqttGateway()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.monitoring = MonitoringManager(ui, mqtt, history_db_path=tmp_path / "telemetry_history.db")
    app.physical_btn = PhysicalButtonManager(ui, mqtt)
    app.pending_telemetry_cmd_ids = {}

    callback = {
        "id": "cb1",
        "message": {"chat": {"id": 12345}, "message_id": 555},
        "data": "power_menu",
    }
    app.handle_callback(callback)

    assert len(ui.edited_messages) == 1
    assert ui.edited_messages[0]["message_id"] == 555
    assert len(ui.sent_messages) == 0

    real_api = TelegramBotApi("dummy_token")

    class MockSession:
        def post(self, url, json=None, timeout=None):
            class Response:
                status_code = 400

                def json(self):
                    return {
                        "ok": False,
                        "description": "Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message",
                    }

            return Response()

    real_api.session = MockSession()
    res = real_api.send_message(12345, "same text", message_id=555)
    assert res is not None
    assert res.get("not_modified") is True


def test_telemetry_refresh_ack_edits_original_card(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "ALLOWED_USER_ID", 12345)
    monkeypatch.setattr(power_flow_mod, "FLOW_STATE_PATH", tmp_path / "flow_state.json")

    ui = MockTelegramUi()
    mqtt = MockMqttGateway()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.physical_btn = PhysicalButtonManager(ui, mqtt)
    app.monitoring = MonitoringManager(ui, mqtt, history_db_path=tmp_path / "telemetry_history.db")
    app.active_workspace_cmd_id = None
    app.pending_minimize_cmd_id = None
    app.pending_telemetry_cmd_ids = {}

    callback = {
        "id": "cb2",
        "message": {"chat": {"id": 12345}, "message_id": 888},
        "data": "telemetry_now",
    }
    app.handle_callback(callback)

    assert len(mqtt.published_messages) == 1
    cmd_id = mqtt.published_messages[0][3]
    assert app.pending_telemetry_cmd_ids[cmd_id] == (12345, 888)

    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "executed",
        "data": {"cpu": {"load_percent": 12.5}},
    })

    assert len(ui.edited_messages) == 1
    assert ui.edited_messages[0]["message_id"] == 888
    assert len(ui.sent_messages) == 0


def test_telemetry_history_operations_close_sqlite(tmp_path):
    db_path = tmp_path / "test.db"
    history = TelemetryHistory(db_path)
    history.insert({"timestamp": int(time.time()), "cpu": {"load_percent": 15}})
    rows = history.rows(3600)
    stats = history.stats()
    history.cleanup()
    assert len(rows) >= 1
    assert stats["count"] >= 1


def test_manual_unlock_alerts_when_pc_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "ALLOWED_USER_ID", 12345)
    monkeypatch.setattr(power_flow_mod, "FLOW_STATE_PATH", tmp_path / "flow_state.json")

    ui = MockTelegramUi()
    mqtt = MockMqttGateway()
    mqtt.system_status = "offline"
    mqtt.health.update("windows_system", "offline")
    mqtt.health.update("windows_desktop", "offline")

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.power_flow.reset_flow()
    app.monitoring = MonitoringManager(ui, mqtt, history_db_path=tmp_path / "telemetry_history.db")

    callback = {
        "id": "cb3",
        "message": {"chat": {"id": 12345}, "message_id": 999},
        "data": "unlock_windows_manual",
    }
    app.handle_callback(callback)

    assert len(ui.callback_answers) == 1
    assert "ПК недоступен. Сначала включи его." in ui.callback_answers[0]["text"]
    assert ui.callback_answers[0]["alert"] is True
    assert app.power_flow.stage == PowerFlowStage.IDLE


def test_manual_unlock_online_locked_pc_updates_card_and_dispatches_unlock(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "ALLOWED_USER_ID", 12345)
    monkeypatch.setattr(power_flow_mod, "FLOW_STATE_PATH", tmp_path / "flow_state.json")

    ui = MockTelegramUi()
    mqtt = MockMqttGateway()
    mqtt.system_status = "online"
    mqtt.system_session = "locked"
    mqtt.health.update("windows_system", "online", ts=time.time())
    mqtt.health.update("windows_session", "locked", ts=time.time())

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.physical_btn = PhysicalButtonManager(ui, mqtt)
    app.monitoring = MonitoringManager(ui, mqtt, history_db_path=tmp_path / "telemetry_history.db")
    app.active_workspace_cmd_id = None
    app.pending_minimize_cmd_id = None
    app.pending_telemetry_cmd_ids = {}

    callback = {
        "id": "cb4",
        "message": {"chat": {"id": 12345}, "message_id": 777},
        "data": "unlock_start:plain",
    }
    app.handle_callback(callback)

    assert app.power_flow.chat_id == 12345
    assert app.power_flow.message_id == 777
    assert app.power_flow.flow_id is not None
    assert app.power_flow.expires_at > time.time()
    assert app.power_flow.stage == PowerFlowStage.UNLOCKING

    assert len(mqtt.published_messages) == 1
    _, action, _, cmd_id = mqtt.published_messages[0]
    assert action == "unlock"

    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "executed",
        "data": {"session": "unlocked"},
    })

    assert app.power_flow.stage == PowerFlowStage.UNLOCKING
    mqtt.system_session = "unlocked"
    app.power_flow.on_system_session_update("unlocked", 12345)

    assert app.power_flow.stage == PowerFlowStage.WAITING_WORKSPACE_DECISION
    assert len(ui.edited_messages) >= 1
    last_edited = ui.edited_messages[-1]
    assert last_edited["message_id"] == 777
    assert last_edited["chat_id"] == 12345
    assert "✅ Windows разблокирована!" in last_edited["text"]
    assert not any(msg["chat_id"] == 0 for msg in ui.sent_messages + ui.edited_messages)


def test_vpn_failed_ack_wakes_waiter_and_halts_workspace_immediately():
    ui = MockTelegramUi()
    mqtt = MockMqttGateway()
    mqtt.desktop_status = "online"

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.allowed_user_id = 12345
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.physical_btn = PhysicalButtonManager(ui, mqtt)
    app.save_state = lambda: None
    app.monitoring = type("M", (), {"telemetry_age": lambda s: 1.0})()
    app.state_lock = threading.RLock()
    app.workspace_cancel_event = threading.Event()
    app.happ_ready_event = threading.Event()
    app.vpn_ready_event = app.happ_ready_event
    app.workspace_executed_event = threading.Event()
    app.minimize_ack_event = threading.Event()
    app.app_statuses = {}
    app.workspace_execution_result = None
    app.workspace_execution_data = {}
    app.minimize_ack_result = None
    app.current_workspace_steps = bot_mod.WORKSPACE_STEPS
    app.active_workspace_cmd_id = None
    app.workspace_card_msg_id = 888
    app.pending_console_cmds = {}

    orig_publish = mqtt.publish_desktop_action

    def _emit_vpn_fail(action, args=None, **kwargs):
        cid = orig_publish(action, args, **kwargs)
        if action == "launch_app":
            app.active_workspace_cmd_id = cid
            app._on_mqtt_event("ack", {
                "id": cid,
                "result": "progress",
                "data": {"step_id": "incy_vpn", "state": "failed"},
            })
        return cid

    mqtt.publish_desktop_action = _emit_vpn_fail
    t0 = time.monotonic()
    app.run_workspace_setup_steps(12345, card_message_id=888, workspace_id="ws-vpn-fail", vpn="incy")
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0
    assert app.vpn_ready_event.is_set()
    assert app.app_statuses["incy_vpn"] == "❌ Ошибка VPN"
    assert any("Не удалось подтвердить подключение INCY" in msg["text"] for msg in ui.edited_messages)


def test_workspace_cancelled_and_unknown_action_acks_unblock_runner():
    ui = MockTelegramUi()
    mqtt = MockMqttGateway()
    mqtt.desktop_status = "online"

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.allowed_user_id = 12345
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.physical_btn = PhysicalButtonManager(ui, mqtt)
    app.save_state = lambda: None
    app.monitoring = type("M", (), {"telemetry_age": lambda s: 1.0})()
    app.state_lock = threading.RLock()
    app.workspace_cancel_event = threading.Event()
    app.happ_ready_event = threading.Event()
    app.vpn_ready_event = app.happ_ready_event
    app.workspace_executed_event = threading.Event()
    app.minimize_ack_event = threading.Event()
    app.app_statuses = {}
    app.workspace_execution_result = None
    app.workspace_execution_data = {}
    app.minimize_ack_result = None
    app.current_workspace_steps = bot_mod.WORKSPACE_STEPS
    app.active_workspace_cmd_id = None
    app.workspace_card_msg_id = 889
    app.pending_console_cmds = {}

    def _emit_acks(result_value):
        orig = mqtt.publish_desktop_action

        def _pub(action, args=None, **kwargs):
            cid = orig(action, args, **kwargs)
            if action == "launch_app":
                app.active_workspace_cmd_id = cid
                app._on_mqtt_event("ack", {
                    "id": cid,
                    "result": "progress",
                    "data": {"step_id": "incy_vpn", "state": "vpn_connected"},
                })
                app._on_mqtt_event("ack", {"id": cid, "result": result_value, "data": {}})
            return cid

        return _pub

    mqtt.publish_desktop_action = _emit_acks("cancelled")
    t0 = time.monotonic()
    app.run_workspace_setup_steps(12345, card_message_id=889, workspace_id="ws-cancel", vpn="incy")
    assert time.monotonic() - t0 < 2.0
    assert "отменён" in ui.edited_messages[-1]["text"]

    ui.edited_messages.clear()
    mqtt.publish_desktop_action = _emit_acks("unknown_action")
    t1 = time.monotonic()
    app.run_workspace_setup_steps(12345, card_message_id=889, workspace_id="ws-unknown", vpn="incy")
    assert time.monotonic() - t1 < 2.0
    assert "Ошибка выполнения рабочего сетапа" in ui.edited_messages[-1]["text"]
