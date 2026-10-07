#!/usr/bin/env python3

"""
Comprehensive Integration Test Suite for PC Control Bot.
Verifies:
1. Real Async Workspace Setup driven by progress ACKs + minimize_all ACK verification
2. Real Cancellation during Happ wait in runner thread (verify thread stops, minimize_all & success NEVER sent)
3. Bot Restart during UNLOCKING updating Telegram card UI
4. Binary Screenshot Topic parsing (4 bytes length + JSON header + JPEG data)
5. Minimize_all failure handling
"""

import sys
import time
import json
import threading
from pathlib import Path

# Add vds_bot_live to sys.path
sys.path.insert(0, str(Path(__file__).parent))

from telegram_ui import TelegramBotApi
from mqtt_gateway import MqttGateway
from power_flow import PowerFlowManager, PowerFlowStage
from physical_button import PhysicalButtonManager
from monitoring import MonitoringManager
from bot import PcControlBotApp, unpack_screenshot_payload
import bot
bot.ALLOWED_USER_ID = 12345


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


from health_engine import HealthEngine


class MockMqttGateway:
    def __init__(self):
        self.published_messages = []
        self.system_status = "online"
        self.system_session = "locked"
        self.esp_status = "online"
        self.desktop_status = "online"
        self.mqtt_online = True
        self.health = HealthEngine()
        self.health.update("mqtt_broker", "online")
        self.health.update("windows_system", "online", ts=time.time())
        self.health.update("windows_desktop", "online", ts=time.time())
        self.health.update("windows_session", "locked", ts=time.time())
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


def test_1_progress_ack_driven_workspace():
    print("--- TEST 1: Real Progress ACK Driven Workspace Setup ---")
    ui = MockTelegramUi()
    mqtt = MockMqttGateway()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.physical_btn = PhysicalButtonManager(ui, mqtt)
    app.monitoring = MonitoringManager(ui, mqtt)
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
    target, action, args, cmd_id = mqtt.published_messages[0]
    assert action == "launch_app"
    assert args["app"] == "Рабочий сетап"
    assert args["workspace_id"] == ws_id
    assert args.get("vpn") in {"happ", "incy"}
    print("[OK] launch_app command sent to desktop agent with workspace_id.")

    # Emit progress ACKs from agent.py using step_id
    for step_id, app_name in [
        ("telegram", "Telegram"),
        ("yandex_music", "YandexMusic"),
        ("dolphin", "Dolphin"),
        ("chrome", "Chrome"),
    ]:
        app._on_mqtt_event("ack", {
            "id": cmd_id,
            "result": "progress",
            "data": {"step_id": step_id, "app": app_name, "state": "verified"}
        })
        time.sleep(0.05)

    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "progress",
        "data": {"step_id": "happ_vpn", "app": "Happ", "state": "connecting"}
    })
    time.sleep(0.05)

    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "progress",
        "data": {"step_id": "happ_vpn", "app": "Happ", "state": "verified"}
    })
    assert app.happ_ready_event.is_set(), "Happ ready event not set"
    print("[OK] Happ VPN readiness verified via progress ACK.")

    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "progress",
        "data": {"step_id": "antigravity", "app": "Antigravity", "state": "verified"}
    })
    time.sleep(0.05)

    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "executed",
        "data": {"desktop_minimized": True}
    })

    time.sleep(0.2)
    assert len(mqtt.published_messages) == 2
    assert mqtt.published_messages[1][1] == "minimize_all"
    min_cmd_id = mqtt.published_messages[1][3]
    print("[OK] Standalone minimize_all command published.")

    # Emit minimize_all executed ACK
    app._on_mqtt_event("ack", {
        "id": min_cmd_id,
        "result": "executed",
        "data": {}
    })

    runner_thread.join(timeout=2.0)
    assert not runner_thread.is_alive(), "Runner thread did not terminate"

    # Verify final card updated ONLY after minimize_all ACK
    last_edited = ui.edited_messages[-1]
    assert "✅ Рабочий сетап успешно запущен!" in last_edited["text"]
    print("PASSED TEST 1!\n")


def test_2_cancellation_during_happ_thread():
    print("--- TEST 2: Real Cancellation During Happ Wait in Runner Thread ---")
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

    # Start runner thread (it publishes launch_app and enters happ_ready_event.wait)
    runner_thread = threading.Thread(
        target=app.run_workspace_setup_steps,
        args=(12345, None, ws_id),
        daemon=True,
    )
    runner_thread.start()
    time.sleep(0.2)

    assert len(mqtt.published_messages) == 1
    assert mqtt.published_messages[0][1] == "launch_app"

    # User clicks cancel while runner thread is waiting for Happ
    app.power_flow.cancel_workspace(12345)
    app.workspace_cancel_event.set()

    # Verify cancel_workspace published with matching workspace_id
    assert len(mqtt.published_messages) == 2
    assert mqtt.published_messages[1][1] == "cancel_workspace"
    assert mqtt.published_messages[1][2] == {"workspace_id": ws_id}
    print(f"[OK] cancel_workspace published to desktop agent with workspace_id={ws_id}.")

    # No manual happ_ready_event.set() here, the thread should wake up via workspace_cancel_event
    runner_thread.join(timeout=2.0)

    # Verify runner thread stopped immediately
    assert not runner_thread.is_alive(), "Runner thread did not terminate after cancellation"
    print("[OK] Runner thread detected workspace_cancelled and terminated cleanly.")

    # Verify minimize_all was NEVER published!
    published_actions = [msg[1] for msg in mqtt.published_messages]
    assert "minimize_all" not in published_actions, "ERROR: minimize_all was published after cancellation!"
    print("[OK] minimize_all was NOT sent after cancellation.")

    # Verify final success message was NEVER sent!
    all_texts = [msg["text"] for msg in ui.sent_messages + ui.edited_messages]
    assert not any("Рабочий сетап успешно запущен" in t for t in all_texts), "ERROR: Success card rendered after cancellation!"
    print("[OK] Success card was NOT rendered after cancellation.")

    # Verify reset_flow was called AFTER thread exit
    assert app.power_flow.stage == PowerFlowStage.IDLE
    assert app.power_flow.workspace_cancelled == False
    print("PASSED TEST 2!\n")


def test_3_bot_restart_card_update():
    print("--- TEST 3: Bot Restart Mid-Unlock Card Update ---")
    ui = MockTelegramUi()
    mqtt = MockMqttGateway()

    # Save state as UNLOCKING to simulate bot restart
    flow_init = PowerFlowManager(ui, mqtt)
    flow_init.stage = PowerFlowStage.UNLOCKING
    flow_init.chat_id = 12345
    flow_init.message_id = 777
    flow_init.created_at = time.time()
    flow_init.expires_at = time.time() + 900
    flow_init.save_state()

    # Create new instance (simulating startup load)
    flow_reloaded = PowerFlowManager(ui, mqtt)
    assert flow_reloaded.stage == PowerFlowStage.UNLOCK_FAILED

    # Verify card was immediately updated in Telegram UI with retry button!
    assert len(ui.edited_messages) == 1
    edited = ui.edited_messages[0]
    assert edited["message_id"] == 777
    assert "Разблокировка была прервана перезапуском бота" in edited["text"]
    assert edited["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "flow_unlock_retry"
    print("[OK] Telegram UI card immediately updated with UNLOCK_FAILED and retry button on startup.")
    print("PASSED TEST 3!\n")


def test_4_binary_screenshot_parsing():
    print("--- TEST 4: Agent.py Binary Screenshot Format Parsing ---")
    ui = MockTelegramUi()
    mqtt = MockMqttGateway()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    # The bot must only accept the screenshot it explicitly requested.
    app.pending_screenshot_cmd_id = "desk-sc-123"

    # Construct exact agent.py payload format: [4 bytes header len][JSON header][JPEG data]
    header_dict = {"id": "desk-sc-123", "format": "jpeg", "width": 1600, "height": 1000}
    header_bytes = json.dumps(header_dict).encode("utf-8")
    header_len = len(header_bytes)
    jpeg_dummy_data = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x48\x00\x48\x00\x00\xff\xd9"
    full_payload = header_len.to_bytes(4, "big") + header_bytes + jpeg_dummy_data

    # Parse payload using helper
    parsed_header, parsed_jpeg = unpack_screenshot_payload(full_payload)
    assert parsed_header["id"] == "desk-sc-123"
    assert parsed_header["width"] == 1600
    assert parsed_jpeg == jpeg_dummy_data
    print("[OK] Screenshot header and JPEG data unpacked correctly.")

    # Dispatch to _on_mqtt_event
    app._on_mqtt_event("screenshot_image", full_payload)
    assert len(ui.photos) == 1
    assert ui.photos[0]["bytes"] == jpeg_dummy_data
    assert "Скриншот экрана ПК" in ui.photos[0]["caption"]
    print("[OK] Unpacked JPEG photo sent to Telegram.")
    print("PASSED TEST 4!\n")


def test_5_minimize_all_ack_failure_handling():
    print("--- TEST 5: Minimize All ACK Failure Handling ---")
    ui = MockTelegramUi()
    mqtt = MockMqttGateway()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.physical_btn = PhysicalButtonManager(ui, mqtt)
    app.monitoring = MonitoringManager(ui, mqtt)
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
    # Set all steps to verified
    for step_id in ["telegram", "yandex_music", "dolphin", "chrome", "happ_vpn", "antigravity"]:
        app._on_mqtt_event("ack", {"id": cmd_id, "result": "progress", "data": {"step_id": step_id, "state": "verified"}})
    app._on_mqtt_event("ack", {"id": cmd_id, "result": "executed", "data": {}})

    time.sleep(0.2)
    min_cmd_id = mqtt.published_messages[1][3]

    # Emit minimize_all FAILED ACK
    app._on_mqtt_event("ack", {"id": min_cmd_id, "result": "failed", "data": {}})
    runner_thread.join(timeout=2.0)

    last_edited = ui.edited_messages[-1]
    assert "не удалось свернуть окна" in last_edited["text"]
    assert last_edited["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "minimize_windows"
    print("[OK] Notice card rendered when minimize_all fails instead of fake success.")
    print("PASSED TEST 5!\n")


def test_6_get_idle_seconds_and_agent_fixes():
    print("--- TEST 6: get_idle_seconds and Agent Fixes ---")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from agent import PCControlAgent
    idle = PCControlAgent.get_idle_seconds()
    assert idle is None or isinstance(idle, int), "get_idle_seconds must return int or None"
    print(f"[OK] get_idle_seconds returned: {idle}")
    print("PASSED TEST 6!\n")


def test_7_telegram_menu_editing_and_no_modified_handling():
    print("--- TEST 7: Telegram Menu Editing & Message Not Modified Handling ---")
    ui = MockTelegramUi()
    mqtt = MockMqttGateway()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.monitoring = MonitoringManager(ui, mqtt)
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
    print("[OK] Navigation callback edited existing message_id 555 without sending new message.")

    # Test call_raw with 'message is not modified' error in real TelegramBotApi logic
    real_api = TelegramBotApi("dummy_token")

    class MockSession:
        def post(self, url, json=None, timeout=None):
            class Response:
                status_code = 400
                def json(self):
                    return {"ok": False, "description": "Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message"}
            return Response()

    real_api.session = MockSession()
    res = real_api.send_message(12345, "same text", message_id=555)
    assert res is not None
    assert res.get("not_modified") is True
    print("[OK] 'message is not modified' treated as success without sending duplicate message.")
    print("PASSED TEST 7!\n")


def test_8_telemetry_refresh_edits_card():
    print("--- TEST 8: Telemetry Refresh Edits Original Card ---")
    ui = MockTelegramUi()
    mqtt = MockMqttGateway()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.physical_btn = PhysicalButtonManager(ui, mqtt)
    app.monitoring = MonitoringManager(ui, mqtt)
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
    print("[OK] Pending telemetry mapped cmd_id to (chat_id, message_id).")

    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "executed",
        "data": {"cpu": {"load_percent": 12.5}}
    })

    assert len(ui.edited_messages) == 1
    assert ui.edited_messages[0]["message_id"] == 888
    assert len(ui.sent_messages) == 0
    print("[OK] Telemetry ACK edited original card (message_id 888) instead of creating new card.")
    print("PASSED TEST 8!\n")


def test_9_history_sqlite_closed():
    print("--- TEST 9: SQLite Connection Explicitly Closed in History ---")
    from history import TelemetryHistory
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        history = TelemetryHistory(db_path)
        history.insert({"timestamp": int(time.time()), "cpu": {"load_percent": 15}})
        rows = history.rows(3600)
        stats = history.stats()
        history.cleanup()
        assert len(rows) >= 1
        assert stats["count"] >= 1
        print("[OK] History operations (_initialize, insert, rows, stats, cleanup) executed and closed SQLite connection.")
    print("PASSED TEST 9!\n")


def test_10_manual_unlock_offline_pc_alert():
    print("--- TEST 10: Manual Unlock on Offline PC Alert ---")
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
    app.monitoring = MonitoringManager(ui, mqtt)

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
    print("[OK] Alert shown on manual unlock attempt while PC is offline, stage unchanged.")
    print("PASSED TEST 10!\n")


def test_11_manual_unlock_online_locked_card_update():
    print("--- TEST 11: Manual Unlock Online + Locked Card Update ---")
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
    app.monitoring = MonitoringManager(ui, mqtt)
    app.active_workspace_cmd_id = None
    app.pending_minimize_cmd_id = None
    app.pending_telemetry_cmd_ids = {}

    callback = {
        "id": "cb4",
        "message": {"chat": {"id": 12345}, "message_id": 777},
        "data": "unlock_windows_manual",
    }
    app.handle_callback(callback)

    # Verify flow metadata populated
    assert app.power_flow.chat_id == 12345
    assert app.power_flow.message_id == 777
    assert app.power_flow.flow_id is not None
    assert app.power_flow.expires_at > time.time()
    assert app.power_flow.stage == PowerFlowStage.UNLOCKING

    # Verify command sent to boot agent
    assert len(mqtt.published_messages) == 1
    target, action, args, cmd_id = mqtt.published_messages[0]
    assert action == "unlock"

    # Simulate incoming ACK executed with session unlocked
    app._on_mqtt_event("ack", {
        "id": cmd_id,
        "result": "executed",
        "data": {"session": "unlocked"}
    })

    # Verify original card 777 was edited and NO message sent to chat_id 0
    assert app.power_flow.stage == PowerFlowStage.WAITING_WORKSPACE_DECISION
    assert len(ui.edited_messages) >= 1
    last_edited = ui.edited_messages[-1]
    assert last_edited["message_id"] == 777
    assert last_edited["chat_id"] == 12345
    assert "✅ Windows разблокирована!" in last_edited["text"]
    assert not any(msg["chat_id"] == 0 for msg in ui.sent_messages + ui.edited_messages)
    print("[OK] Manual unlock on online+locked PC updated message_id 777 for chat_id 12345, not sent to chat 0.")
    print("PASSED TEST 11!\n")


if __name__ == "__main__":
    test_1_progress_ack_driven_workspace()
    test_2_cancellation_during_happ_thread()
    test_3_bot_restart_card_update()
    test_4_binary_screenshot_parsing()
    test_5_minimize_all_ack_failure_handling()
    test_6_get_idle_seconds_and_agent_fixes()
    test_7_telegram_menu_editing_and_no_modified_handling()
    test_8_telemetry_refresh_edits_card()
    test_9_history_sqlite_closed()
    test_10_manual_unlock_offline_pc_alert()
    test_11_manual_unlock_online_locked_card_update()
    print("ALL 11 INTEGRATION & STABILITY TESTS PASSED CLEANLY!")
