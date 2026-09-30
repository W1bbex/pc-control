#!/usr/bin/env python3

import base64
import html
import json
import logging
import os
import queue
import secrets
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    MOSCOW = ZoneInfo("Europe/Moscow")
except Exception:
    MOSCOW = timezone(timedelta(hours=3))

from telegram_ui import TelegramBotApi
from mqtt_gateway import MqttGateway
from power_flow import PowerFlowManager, PowerFlowStage
from physical_button import PhysicalButtonManager
from monitoring import MonitoringManager

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
LOG = logging.getLogger("pc-control-bot")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "0"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "telegram_bot")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

STATE_PATH = Path("/var/lib/pc-control-bot/state.json")
SCREENSHOT_TIMEOUT_SECONDS = 35
SHUTDOWN_CONFIRM_TIMEOUT_SECONDS = 120


def unpack_screenshot_payload(payload):
    """
    Unpacks binary screenshot payload sent by agent.py:
    [4 bytes big-endian header length] + [JSON header string] + [JPEG image binary data]
    """
    if not isinstance(payload, bytes) or len(payload) < 4:
        raise ValueError("Payload too short for screenshot header length")
    header_len = int.from_bytes(payload[:4], "big")
    if len(payload) < 4 + header_len:
        raise ValueError("Payload truncated inside screenshot JSON header")
    header_json = payload[4 : 4 + header_len].decode("utf-8", errors="replace")
    header_data = json.loads(header_json)
    jpeg_bytes = payload[4 + header_len :]
    return header_data, jpeg_bytes


WORKSPACE_STEPS = [
    {"id": "telegram", "label": "💬 Telegram"},
    {"id": "yandex_music", "label": "🎵 Yandex Music"},
    {"id": "happ_vpn", "label": "⚡ Happ (VPN)"},
    {"id": "chrome", "label": "🌐 Google Chrome"},
    {"id": "antigravity", "label": "🚀 Antigravity"},
]

LED_WHITE_PRESETS = {
    "1": ("255,150,165", "Холодный снежный"),
    "2": ("255,140,160", "Арктический холодный"),
    "3": ("255,130,155", "Ледяной кристалл"),
    "4": ("255,120,150", "Глубокий холодный"),
    "5": ("255,160,160", "Симметричный чистый (G=B)"),
    "6": ("255,145,145", "Симметричный нейтральный"),
    "7": ("255,130,130", "Сбалансированный белый"),
    "8": ("255,115,115", "Мягкий белый"),
    "9": ("255,165,140", "Светлый нейтральный"),
    "10": ("255,150,125", "Идеальный белый ⭐️"),
    "11": ("255,135,100", "Естественный дневной"),
    "12": ("255,120,75", "Тёплый ламповый"),
    "eye": ("255,150,125", "Для глаз (Идеал) 👁"),
    "photo": ("228,245,255", "Для фото и видео 📸"),
    "глаз": ("255,150,125", "Для глаз (Идеал) 👁"),
    "фото": ("228,245,255", "Для фото и видео 📸"),
    "камера": ("228,245,255", "Для фото и видео 📸"),
}


class PcControlBotApp:
    def __init__(self):
        if not TELEGRAM_TOKEN or not ALLOWED_USER_ID:
            LOG.error("TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID must be set.")
            sys.exit(1)

        self.ui = TelegramBotApi(TELEGRAM_TOKEN)
        self.mqtt = MqttGateway(
            username=MQTT_USERNAME,
            password=MQTT_PASSWORD,
            host=MQTT_HOST,
            port=MQTT_PORT,
            on_event_callback=self._on_mqtt_event,
        )
        self.power_flow = PowerFlowManager(self.ui, self.mqtt)
        self.physical_btn = PhysicalButtonManager(self.ui, self.mqtt)
        self.monitoring = MonitoringManager(self.ui, self.mqtt)
        self.monitoring.is_shutting_down = lambda: bool(
            getattr(self, "pending_shutdown_cmd_id", None)
            and getattr(self, "pending_shutdown_started_at", None)
            and (time.time() - float(self.pending_shutdown_started_at) < 180)
        )

        self.state_lock = threading.RLock()
        self.offset = 0
        self.running = True
        self.workspace_cancel_event = threading.Event()
        self.happ_ready_event = threading.Event()
        self.workspace_executed_event = threading.Event()
        self.minimize_ack_event = threading.Event()
        self.workspace_execution_result = None
        self.minimize_ack_result = None
        self.pending_screenshots = {}
        self.pending_telemetry_cmd_ids = {}
        self.awaiting_shutdown_time_chat_id = None
        self.shutdown_candidate_ts = None
        self.scheduled_shutdown_ts = None
        self.pending_shutdown_cmd_id = None
        self.pending_shutdown_started_at = None
        self.shutdown_acknowledged = False
        self.shutdown_offline_detected_at = None
        self.last_schedule_attempt_at = 0.0
        self.active_workspace_cmd_id = None
        self.pending_minimize_cmd_id = None
        self.workspace_card_msg_id = None
        self.app_statuses = {}

        self.active_player_message_id = None
        self.active_player_chat_id = None
        self.last_player_edit_time = 0.0
        self.player_edit_lock = threading.Lock()
        self.latest_media_state = {}
        self.pending_activity_chat_id = None
        self.pending_activity_msg_id = None
        self.active_main_menu_message_id = None
        self.current_menu_view = "main"
        self.last_dashboard_auto_refresh = 0.0
        self.last_esp_status = None
        self.last_auto_led_state = None

        self.console_active_chat_id = None
        self.console_target = "desktop"
        self.console_shell = "powershell"
        self.pending_console_cmds = {}

        self.physical_btn.on_trigger_unlock_cb = self.handle_physical_unlock_trigger
        self.physical_btn.on_trigger_unlock_and_workspace_cb = self.handle_physical_unlock_and_workspace_trigger
        self.physical_btn.on_state_change_cb = self.save_state
        self.power_flow.on_workspace_start_cb = self.start_async_workspace_setup

        self.load_state()

    def handle_physical_unlock_trigger(self, chat_id):
        session = self.mqtt.system_session
        system_online = (self.mqtt.system_status == "online")

        if session == "unlocked":
            self.ui.send_message(chat_id, "ℹ️ <b>Windows уже разблокирована.</b>")
            return

        if system_online and session in {"login_screen", "locked"}:
            res = self.ui.send_message(
                chat_id,
                "🔓 <b>Жест разблокировки принят!</b>\nВвожу пароль Windows...",
            )
            if res and isinstance(res, dict) and "message_id" in res:
                self.last_unlock_gesture_msg_id = res["message_id"]
                self.last_unlock_gesture_time = time.time()
            self.power_flow.start_manual_unlock(chat_id)
        else:
            self.power_flow.stage = PowerFlowStage.UNLOCKING
            self.power_flow.unlock_attempts = 0
            self.power_flow.pending_unlock_command_id = None
            self.power_flow.chat_id = chat_id
            self.power_flow.save_state()
            res = self.ui.send_message(
                chat_id,
                "🔓 <b>Жест разблокировки принят!</b>\n⏳ ПК загружается — пароль будет введён автоматически при появлении экрана входа.",
            )
            if res and isinstance(res, dict) and "message_id" in res:
                self.last_unlock_gesture_msg_id = res["message_id"]
                self.last_unlock_gesture_time = time.time()

    def handle_physical_unlock_and_workspace_trigger(self, chat_id):
        session = self.mqtt.system_session
        system_online = (self.mqtt.system_status == "online")

        if session == "unlocked":
            if self.power_flow.stage == PowerFlowStage.WORKSPACE_RUNNING:
                self.ui.send_message(chat_id, "ℹ️ <b>Рабочий сетап уже запускается.</b>")
                return
            self.power_flow.stage = PowerFlowStage.WORKSPACE_RUNNING
            self.power_flow.workspace_cancelled = False
            self.power_flow.active_workspace_id = f"ws-{int(time.time())}"
            self.power_flow.save_state()
            self.ui.send_message(
                chat_id,
                "🚀 <b>Жест 'Разблок + Сетап' принят!</b>\nWindows уже разблокирована — запускаю рабочий сетап...",
            )
            self.start_async_workspace_setup(chat_id, workspace_id=self.power_flow.active_workspace_id)
            return

        self.power_flow.set_auto_launch_workspace(True)

        prev_msg_id = getattr(self, "last_unlock_gesture_msg_id", None)
        can_edit = prev_msg_id and (time.time() - getattr(self, "last_unlock_gesture_time", 0.0) < 5.0)

        if system_online and session in {"login_screen", "locked"}:
            msg_text = "🚀 <b>Жест 'Разблок + Сетап' принят!</b>\nРазблокирую Windows и сразу запускаю рабочий сетап..."
            if can_edit:
                self.ui.send_message(chat_id, msg_text, message_id=prev_msg_id)
            else:
                self.ui.send_message(chat_id, msg_text)
            if self.power_flow.stage != PowerFlowStage.UNLOCKING:
                self.power_flow.start_manual_unlock(chat_id, auto_launch_workspace=True)
        else:
            self.power_flow.stage = PowerFlowStage.UNLOCKING
            self.power_flow.unlock_attempts = 0
            self.power_flow.pending_unlock_command_id = None
            self.power_flow.chat_id = chat_id
            self.power_flow.save_state()
            msg_text = "🚀 <b>Жест 'Разблок + Сетап' принят!</b>\n⏳ ПК загружается — после разблокировки автоматически запустится рабочий сетап."
            if can_edit:
                self.ui.send_message(chat_id, msg_text, message_id=prev_msg_id)
            else:
                self.ui.send_message(chat_id, msg_text)

    def load_state(self):
        with getattr(self, "state_lock", threading.RLock()):
            try:
                if STATE_PATH.exists():
                    data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                    self.offset = int(data.get("telegram_offset", 0))
                    pwd = data.get("physical_password")
                    if pwd:
                        self.physical_btn.set_saved_password(pwd)
                    unlock_pat = data.get("physical_unlock_pattern")
                    if unlock_pat:
                        self.physical_btn.saved_unlock_pattern = unlock_pat
                    self.physical_btn.last_physical_morse_success_at = data.get("last_physical_morse_success_at")
                    self.physical_btn.physical_morse_unlock_eligible = bool(data.get("physical_morse_unlock_eligible", False))
                    scheduled = data.get("scheduled_shutdown_ts")
                    if scheduled:
                        self.scheduled_shutdown_ts = float(scheduled)
                    pending_shutdown = data.get("pending_shutdown") or {}
                    self.pending_shutdown_cmd_id = pending_shutdown.get("id")
                    self.pending_shutdown_started_at = pending_shutdown.get("started_at")
                    self.shutdown_acknowledged = bool(pending_shutdown.get("acknowledged"))
                    active_p = data.get("active_player") or {}
                    if active_p.get("message_id"):
                        self.active_player_message_id = active_p.get("message_id")
                        self.active_player_chat_id = active_p.get("chat_id")
                        self.active_player_business_connection_id = active_p.get("business_connection_id")
                        self.active_player_is_photo = bool(active_p.get("is_photo", False))
                        self.active_player_cover_url = active_p.get("cover_url")
                    self.active_main_menu_message_id = data.get("active_main_menu_message_id")
                    self.current_menu_view = data.get("current_menu_view", "main")
                    self.console_target = data.get("console_target", "desktop")
                    self.console_shell = data.get("console_shell", "powershell")
            except Exception:
                LOG.exception("Could not load state.json")

    def save_state(self):
        with getattr(self, "state_lock", threading.RLock()):
            try:
                STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                temp = STATE_PATH.with_suffix(".tmp")
                phys_btn = getattr(self, "physical_btn", None)
                data = {
                    "telegram_offset": getattr(self, "offset", 0),
                    "physical_password": getattr(phys_btn, "saved_password", None),
                    "physical_unlock_pattern": getattr(phys_btn, "saved_unlock_pattern", "CC"),
                    "last_physical_morse_success_at": getattr(phys_btn, "last_physical_morse_success_at", 0.0),
                    "physical_morse_unlock_eligible": getattr(phys_btn, "physical_morse_unlock_eligible", False),
                    "scheduled_shutdown_ts": getattr(self, "scheduled_shutdown_ts", None),
                    "pending_shutdown": {
                        "id": getattr(self, "pending_shutdown_cmd_id", None),
                        "started_at": getattr(self, "pending_shutdown_started_at", None),
                        "acknowledged": getattr(self, "shutdown_acknowledged", False),
                    } if getattr(self, "pending_shutdown_cmd_id", None) else None,
                    "active_player": {
                        "message_id": getattr(self, "active_player_message_id", None),
                        "chat_id": getattr(self, "active_player_chat_id", None),
                        "business_connection_id": getattr(self, "active_player_business_connection_id", None),
                        "is_photo": getattr(self, "active_player_is_photo", False),
                        "cover_url": getattr(self, "active_player_cover_url", None),
                    } if getattr(self, "active_player_message_id", None) else None,
                    "active_main_menu_message_id": getattr(self, "active_main_menu_message_id", None),
                    "current_menu_view": getattr(self, "current_menu_view", "main"),
                    "console_target": getattr(self, "console_target", "desktop"),
                    "console_shell": getattr(self, "console_shell", "powershell"),
                }
                temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                os.chmod(temp, 0o600)
                os.replace(temp, STATE_PATH)
            except Exception:
                LOG.exception("Could not save state.json")

    def _main_menu_markup(self):
        pc_state = self.mqtt.health.evaluate_pc_state(self.monitoring.telemetry_age())
        return self.ui.main_menu_markup(
            pc_state=pc_state,
            session=self.mqtt.system_session,
            system_status=self.mqtt.system_status,
        )

    def _power_menu_markup(self):
        pc_state = self.mqtt.health.evaluate_pc_state(self.monitoring.telemetry_age())
        return self.ui.power_menu_markup(
            pc_state=pc_state,
            session=self.mqtt.system_session,
            system_status=self.mqtt.system_status,
        )

    def _check_shutdown_confirmation(self, now=None):
        now = time.time() if now is None else float(now)
        if not self.pending_shutdown_cmd_id:
            return False

        # Criteria for SHUTDOWN_OBSERVED:
        # 1. shutdown command ACK received from Windows
        # 2. MQTT broker is online
        # 3. Both system and desktop agents are strictly offline
        # 4. Telemetry is absent or >= 20s old
        # 5. Sustained for >= 20s continuous
        telem_age = self.monitoring.telemetry_age()
        telem_absent = (telem_age is None or telem_age >= 20.0 or (self.pending_shutdown_started_at and self.monitoring.latest_received_at < self.pending_shutdown_started_at))
        both_offline = (self.mqtt.system_status == "offline" and self.mqtt.desktop_status == "offline")

        if (
            self.shutdown_acknowledged
            and getattr(self.mqtt, "mqtt_online", False)
            and both_offline
            and telem_absent
        ):
            if self.shutdown_offline_detected_at is None:
                self.shutdown_offline_detected_at = now
                LOG.info("Shutdown criteria met, starting sustain window (20s) at %s", now)
            elif now - self.shutdown_offline_detected_at >= 20.0:
                LOG.info("Shutdown criteria sustained for >= 20s. Confirming SHUTDOWN_OBSERVED.")
                self.mqtt.health.set_shutdown_observed(ttl=3600.0, now=now)
                self.ui.send_message(
                    ALLOWED_USER_ID,
                    "<b>✅ ПК перестал отвечать после принятой команды выключения.</b>",
                    reply_markup=self._main_menu_markup(),
                )
                self._clear_pending_shutdown()
                return True
        else:
            if self.shutdown_offline_detected_at is not None:
                LOG.info("Shutdown criteria broken, resetting sustain window")
                self.shutdown_offline_detected_at = None

        return False

    def _wait_event_or_cancel(self, event, timeout):
        start = time.time()
        while time.time() - start < timeout:
            if self.workspace_cancel_event.is_set():
                return False
            if event.wait(timeout=0.5):
                return True
        return False

    def _update_workspace_card_ui(self):
        if not self.workspace_card_msg_id:
            return
        if self.workspace_cancel_event.is_set():
            return

        lines = [
            f"{step['label']} — {self.app_statuses.get(step['id'], '⏳ В очереди')}"
            for step in WORKSPACE_STEPS
        ]
        text = "<b>🚀 Запуск рабочего сетапа...</b>\n\n" + "\n".join(lines)
        markup = {"inline_keyboard": [[{"text": "Отменить запуск", "style": "danger", "callback_data": "flow_workspace_cancel"}]]}
        self.ui.send_message(ALLOWED_USER_ID, text, reply_markup=markup, message_id=self.workspace_card_msg_id)

    def _on_mqtt_event(self, event_type, data):
        if event_type in {"system_status", "health_update"}:
            session = data.get("session") or data.get("system_session")
            LOG.info("_on_mqtt_event %s: system_session=%s (system_status=%s)", event_type, session, data.get("system_status"))
            self.power_flow.on_system_session_update(session, ALLOWED_USER_ID)
            self._check_shutdown_confirmation(time.time())

            sys_status = data.get("system_status") or self.mqtt.system_status
            desk_status = data.get("desktop_status") or self.mqtt.desktop_status
            esp_status = data.get("esp_status") or getattr(self.mqtt, "esp_status", "unknown")
            pc_online = (
                sys_status == "online"
                or desk_status == "online"
                or session in ("unlocked", "locked", "login_screen")
            )
            pc_offline = not pc_online

            prev_esp_status = getattr(self, "last_esp_status", None)
            self.last_esp_status = esp_status
            esp_just_reconnected = (prev_esp_status is not None and prev_esp_status != "online" and esp_status == "online")

            if esp_just_reconnected:
                LOG.info("Auto-LED: ESP reconnected (status=%s, prev=%s, pc_online=%s)", esp_status, prev_esp_status, pc_online)
                if pc_online:
                    if hasattr(self.mqtt, "publish_esp_action"):
                        if self.mqtt.publish_esp_action("LED_ON"):
                            self.last_auto_led_state = "on"
                            LOG.info("Auto-LED: ESP reconnected while PC is online -> resynced LED_ON to cooler")
                else:
                    if hasattr(self.mqtt, "publish_esp_action"):
                        if self.mqtt.publish_esp_action("LED_OFF"):
                            self.last_auto_led_state = "off"
                            LOG.info("Auto-LED: ESP reconnected while PC is offline -> resynced LED_OFF to cooler")
            else:
                if pc_offline and getattr(self, "last_auto_led_state", None) != "off":
                    if hasattr(self.mqtt, "publish_esp_action"):
                        if self.mqtt.publish_esp_action("LED_OFF"):
                            self.last_auto_led_state = "off"
                            LOG.info("Auto-LED: PC went offline/asleep (sys=%s, desk=%s, sess=%s) -> LED_OFF sent to cooler", sys_status, desk_status, session)
                elif pc_online and getattr(self, "last_auto_led_state", None) != "on":
                    if hasattr(self.mqtt, "publish_esp_action"):
                        if self.mqtt.publish_esp_action("LED_ON"):
                            self.last_auto_led_state = "on"
                            LOG.info("Auto-LED: PC came online/awake (sys=%s, desk=%s, sess=%s) -> LED_ON sent to cooler", sys_status, desk_status, session)

        elif event_type == "physical_input":
            self.physical_btn.handle_event(data, ALLOWED_USER_ID)

        elif event_type == "telemetry":
            alerts = self.monitoring.process_telemetry(data)
            for alert_text in alerts:
                self.ui.send_message(ALLOWED_USER_ID, alert_text)
            now = time.time()
            if self._is_user_in_telegram_on_pc() and (now - getattr(self, "last_dashboard_auto_refresh", 0.0) >= 10.0):
                self._check_dashboard_auto_refresh(now)

        elif event_type == "screenshot_image":
            try:
                header_data, jpeg_bytes = unpack_screenshot_payload(data)
                cmd_id = header_data.get("id")
                pending_screenshots = getattr(self, "pending_screenshots", {})
                pending = pending_screenshots.pop(cmd_id, None)
                if pending is None and cmd_id == getattr(self, "pending_screenshot_cmd_id", None):
                    pending = {"chat_id": ALLOWED_USER_ID, "requested_at": time.time()}
                if pending is None:
                    LOG.info("Ignored unsolicited or expired screenshot with id %s", cmd_id)
                    return
                if header_data.get("format") != "jpeg" or not jpeg_bytes.startswith(b"\xff\xd8"):
                    raise ValueError("Screenshot payload is not a JPEG image")
                if not jpeg_bytes.endswith(b"\xff\xd9"):
                    raise ValueError("Screenshot JPEG is truncated")
                LOG.info("Parsed screenshot JPEG (%d bytes, %dx%d)", len(jpeg_bytes), header_data.get("width", 0), header_data.get("height", 0))
                captured_at = datetime.now(MOSCOW).strftime("%d.%m.%Y %H:%M:%S")
                sent = self.ui.send_photo(
                    pending["chat_id"],
                    jpeg_bytes,
                    caption=f"<b>🖼️ Скриншот экрана ПК</b>\n{captured_at} МСК",
                    filename="pc-screenshot.jpg",
                )
                if not sent:
                    self.ui.send_message(pending["chat_id"], "❌ Telegram не принял изображение. Попробуй ещё раз.")
            except Exception:
                LOG.exception("Failed to unpack screenshot binary payload")
                self.ui.send_message(ALLOWED_USER_ID, "❌ Ошибка парсинга скриншота от desktop-агента.")

        elif event_type == "command_timeout":
            cmd_id = data.get("id")
            pending = getattr(self, "pending_screenshots", {}).pop(cmd_id, None)
            if pending:
                self.ui.send_message(pending["chat_id"], "❌ Desktop-агент не прислал скриншот за 30 секунд.")
            if cmd_id == getattr(self, "pending_shutdown_cmd_id", None):
                self.ui.send_message(
                    ALLOWED_USER_ID,
                    "<b>❌ Выключение не подтверждено.</b>\n\nСистемный агент не ответил. ПК мог остаться включённым.",
                    reply_markup=self._power_menu_markup(),
                )
                self._clear_pending_shutdown()
            if getattr(self.power_flow, "pending_unlock_command_id", None) and cmd_id == self.power_flow.pending_unlock_command_id:
                self.power_flow.pending_unlock_command_id = None
                self.power_flow.on_unlock_failed(self.power_flow.chat_id or ALLOWED_USER_ID, "Таймаут ожидания ответа от агента при разблокировке.")
            if getattr(self.physical_btn, "pending_command_id", None) and cmd_id == self.physical_btn.pending_command_id:
                self.physical_btn.pending_command_id = None
                self.ui.send_message(ALLOWED_USER_ID, "❌ Таймаут ожидания ответа от ESP.")
            if cmd_id in getattr(self, "pending_console_cmds", {}):
                pending_cmd = self.pending_console_cmds.pop(cmd_id)
                c_chat_id = pending_cmd["chat_id"]
                c_cmd = pending_cmd["command"]
                c_target = pending_cmd["target"]
                safe_c = html.escape(c_cmd)
                self.ui.send_message(
                    c_chat_id,
                    f"⏱ <b>Таймаут выполнения команды:</b> <code>{safe_c}</code>\n\n"
                    f"Агент <b>{c_target.upper()}</b> не ответил за отведённое время.",
                )

        elif event_type == "ack":
            LOG.debug("MQTT ACK received: %s", data)
            self.power_flow.handle_ack(data)
            self.physical_btn.handle_ack(data, ALLOWED_USER_ID, self.save_state)

            cmd_id = data.get("id")
            result = data.get("result")
            ack_data = data.get("data") or {}

            if cmd_id in getattr(self, "pending_console_cmds", {}):
                pending_cmd = self.pending_console_cmds.pop(cmd_id)
                self._handle_console_ack(pending_cmd, data)

            if cmd_id == getattr(self, "pending_shutdown_cmd_id", None):
                if result == "executed":
                    self.shutdown_acknowledged = True
                    self.save_state()
                    self.ui.send_message(
                        ALLOWED_USER_ID,
                        "<b>✅ Windows приняла команду выключения.</b>\n\nОжидаю подтверждение, что ПК действительно перешёл в offline…",
                    )
                elif result not in {"progress"}:
                    self.ui.send_message(
                        ALLOWED_USER_ID,
                        f"<b>❌ Выключение отклонено.</b>\n\nОтвет системного агента: <code>{result}</code>",
                        reply_markup=self._power_menu_markup(),
                    )
                    self._clear_pending_shutdown()

            # Progress ACK tracking for active workspace command
            active_ws_cmd = getattr(self, "active_workspace_cmd_id", None)
            if active_ws_cmd and cmd_id == active_ws_cmd:
                step_id = ack_data.get("step_id")
                app_name = ack_data.get("app")
                state_val = ack_data.get("state")

                if result == "progress":
                    if not step_id and app_name:
                        an = app_name.lower()
                        if "telegram" in an or "ayugram" in an:
                            step_id = "telegram"
                        elif "music" in an or "музыка" in an:
                            step_id = "yandex_music"
                        elif "dolphin" in an:
                            step_id = "dolphin"
                        elif "chrome" in an:
                            step_id = "chrome"
                        elif "happ" in an:
                            step_id = "happ_vpn"
                        elif "antigravity" in an:
                            step_id = "antigravity"

                    if step_id:
                        if state_val in {"verified", "already_running_verified", "ready", "vpn_connected"}:
                            self.app_statuses[step_id] = "✅"
                            if step_id == "happ_vpn":
                                self.happ_ready_event.set()
                        elif state_val == "connecting":
                            self.app_statuses[step_id] = "⏳ Подключение VPN..."
                        elif state_val == "starting":
                            self.app_statuses[step_id] = "⏳ Запуск..."
                        elif state_val == "failed":
                            self.app_statuses[step_id] = "❌"

                        self._update_workspace_card_ui()

                elif result == "executed":
                    LOG.info("Workspace setup launch_app executed ACK received.")
                    self.workspace_execution_result = "executed"
                    self.workspace_executed_event.set()

                elif result == "failed":
                    LOG.warning("Workspace setup failed ACK received: %s", ack_data)
                    self.workspace_execution_result = "failed"
                    self.workspace_executed_event.set()

            # Minimize ACK tracking
            pending_min = getattr(self, "pending_minimize_cmd_id", None)
            if pending_min and cmd_id == pending_min:
                LOG.info("minimize_all ACK received: result=%s", result)
                self.minimize_ack_result = result
                self.minimize_ack_event.set()

            pending_telemetry = getattr(self, "pending_telemetry_cmd_ids", {})
            if cmd_id in pending_telemetry:
                target = pending_telemetry.pop(cmd_id)
                if isinstance(target, tuple):
                    req_chat_id, req_msg_id = target
                else:
                    req_chat_id, req_msg_id = target, None

                if result == "executed" and isinstance(ack_data, dict):
                    alerts = self.monitoring.process_telemetry(ack_data)
                    for alert_text in alerts:
                        self.ui.send_message(ALLOWED_USER_ID, alert_text)
                    self.ui.send_message(
                        req_chat_id,
                        self.monitoring.dashboard_text(),
                        reply_markup=self.ui.monitoring_menu_markup(),
                        message_id=req_msg_id,
                    )

        elif event_type == "media_status":
            self.latest_media_state = data
            self._handle_media_status_event(data)

        elif event_type == "activity_response":
            self._handle_activity_response_event(data)

    def _handle_media_status_event(self, data):
        msg_id = getattr(self, "active_player_message_id", None)
        chat_id = getattr(self, "active_player_chat_id", None)
        if not msg_id or not chat_id:
            return

        now = time.time()
        last_rendered = getattr(self, "_last_rendered_media_data", {})
        is_track_change = (
            not last_rendered
            or last_rendered.get("title") != data.get("title")
            or last_rendered.get("artist") != data.get("artist")
            or last_rendered.get("is_playing") != data.get("is_playing")
            or last_rendered.get("is_liked") != data.get("is_liked")
            or last_rendered.get("cover_url") != data.get("cover_url")
            or last_rendered.get("emoji_ids") != data.get("emoji_ids")
            or last_rendered.get("artist_avatar_id") != data.get("artist_avatar_id")
        )
        min_interval = 0.5 if is_track_change else 2.8

        with self.player_edit_lock:
            time_since = now - self.last_player_edit_time
            if time_since < min_interval:
                self._pending_player_data = data
                if not getattr(self, "_player_timer_running", False):
                    self._player_timer_running = True
                    delay = max(0.1, min_interval - time_since)
                    timer = threading.Timer(delay, self._flush_pending_player_update)
                    self._player_timer = timer
                    timer.start()
                return
            timer = getattr(self, "_player_timer", None)
            if timer and timer.is_alive():
                timer.cancel()
            self._player_timer_running = False
            self.last_player_edit_time = now
            self._pending_player_data = None
            self._last_rendered_media_data = dict(data)

        self._render_and_send_player(chat_id, msg_id, data)

    def _flush_pending_player_update(self):
        with self.player_edit_lock:
            self._player_timer_running = False
            data = getattr(self, "_pending_player_data", None)
            self._pending_player_data = None
            if not data:
                return
            self.last_player_edit_time = time.time()
            self._last_rendered_media_data = dict(data)

        msg_id = getattr(self, "active_player_message_id", None)
        chat_id = getattr(self, "active_player_chat_id", None)
        if msg_id and chat_id:
            self._render_and_send_player(chat_id, msg_id, data)

    def _delete_old_player_card(self):
        old_msg_id = getattr(self, "active_player_message_id", None)
        old_chat_id = getattr(self, "active_player_chat_id", None)
        old_conn_id = getattr(self, "active_player_business_connection_id", None)
        if old_chat_id == ALLOWED_USER_ID:
            old_conn_id = None
        if old_msg_id and old_chat_id:
            try:
                self.ui.delete_message(old_chat_id, old_msg_id, business_connection_id=old_conn_id)
            except Exception as e:
                LOG.warning("Could not delete old player card %s in chat %s: %s", old_msg_id, old_chat_id, e)
        self.active_player_message_id = None
        self.active_player_chat_id = None
        self.active_player_business_connection_id = None
        self.active_player_is_photo = False
        self.active_player_cover_url = None

    def _send_player_card(self, chat_id, media_state, business_connection_id=None):
        if chat_id == ALLOWED_USER_ID:
            business_connection_id = None
        self._delete_old_player_card()
        cover_url = media_state.get("cover_url") if media_state else None
        markup = self.ui.player_card_markup(media_state)

        res = None
        is_photo = False
        if cover_url:
            caption = self.ui.player_card_caption(media_state)
            res = self.ui.send_photo(
                chat_id,
                cover_url,
                caption=caption,
                reply_markup=markup,
                business_connection_id=business_connection_id,
            )
            if res and isinstance(res, dict) and "message_id" in res:
                is_photo = True

        if not res or not isinstance(res, dict) or "message_id" not in res:
            text = self.ui.player_card_text(media_state)
            res = self.ui.send_message(
                chat_id,
                text,
                reply_markup=markup,
                business_connection_id=business_connection_id,
            )
            is_photo = False

        if res and isinstance(res, dict) and "message_id" in res:
            self.active_player_message_id = res["message_id"]
            self.active_player_chat_id = chat_id
            self.active_player_business_connection_id = business_connection_id
            self.active_player_is_photo = is_photo
            self.active_player_cover_url = cover_url if is_photo else None
            self.last_player_edit_time = time.time()
            self.save_state()
            LOG.info("Active player set to chat %s msg %s (is_photo=%s, conn=%s)", chat_id, res["message_id"], is_photo, business_connection_id)
            return res
        return None

    def _render_and_send_player(self, chat_id, msg_id, data):
        if getattr(self, "active_player_message_id", None) != msg_id:
            return
        conn_id = getattr(self, "active_player_business_connection_id", None)
        if chat_id == ALLOWED_USER_ID:
            conn_id = None
        markup = self.ui.player_card_markup(data)
        is_photo = getattr(self, "active_player_is_photo", False)
        cover_url = data.get("cover_url") if data else None

        if not is_photo and cover_url:
            self._send_player_card(chat_id, data, business_connection_id=conn_id)
            return

        try:
            if is_photo:
                last_cover = getattr(self, "active_player_cover_url", None)
                caption = self.ui.player_card_caption(data)
                if cover_url and cover_url != last_cover:
                    edit_res = self.ui.edit_message_media(
                        chat_id,
                        msg_id,
                        cover_url,
                        caption=caption,
                        reply_markup=markup,
                        business_connection_id=conn_id,
                    )
                    if edit_res and isinstance(edit_res, dict) and "message_id" in edit_res:
                        self.active_player_cover_url = cover_url
                        return
                self.ui.edit_message_caption(
                    chat_id,
                    msg_id,
                    caption,
                    reply_markup=markup,
                    business_connection_id=conn_id,
                )
            else:
                text = self.ui.player_card_text(data)
                self.ui.send_message(
                    chat_id,
                    text,
                    reply_markup=markup,
                    message_id=msg_id,
                    business_connection_id=conn_id,
                )
        except Exception as e:
            LOG.warning("Failed updating player card %s: %s", msg_id, e)

    def _handle_activity_response_event(self, data):
        chat_id = getattr(self, "pending_activity_chat_id", None)
        msg_id = getattr(self, "pending_activity_msg_id", None)
        if not chat_id:
            return
        self.pending_activity_chat_id = None
        self.pending_activity_msg_id = None

        req = data.get("req", {})
        result = data.get("result", [])
        date_str = req.get("val", datetime.now().strftime("%Y-%m-%d"))
        text = self.ui.activity_card_text(result, date_str)
        markup = {"inline_keyboard": [self.ui.back_button("menu")]}
        self.ui.send_message(chat_id, text, reply_markup=markup, message_id=msg_id)

    def handle_callback(self, callback):
        callback_id = callback["id"]
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        from_user_id = callback.get("from", {}).get("id") or chat_id
        message_id = callback.get("message", {}).get("message_id")
        business_connection_id = callback.get("message", {}).get("business_connection_id")
        data = callback.get("data", "")

        # Strict security: Only the owner (ALLOWED_USER_ID) can interact with any callbacks!
        if from_user_id != ALLOWED_USER_ID:
            self.ui.answer_callback(callback_id, "Отказано", alert=True)
            return

        if chat_id != ALLOWED_USER_ID and not data.startswith("player_") and not business_connection_id:
            self.ui.answer_callback(callback_id, "Доступ запрещён", alert=True)
            return

        LOG.info("Callback received: %s", data)

        if data != "menu" and not data.startswith("player_"):
            self.current_menu_view = data

        # 1. Quick Navigation Callbacks
        if data == "menu":
            self.ui.answer_callback(callback_id)
            self.current_menu_view = "main"
            self.active_main_menu_message_id = message_id
            self.save_state()
            self.ui.send_message(chat_id, self.monitoring.dashboard_text(), reply_markup=self._main_menu_markup(), message_id=message_id)
            return

        if data == "settings_menu":
            self.ui.answer_callback(callback_id)
            self.current_menu_view = "settings_menu"
            text = (
                "⚙️ <b>Настройки и сервисы</b>\n\n"
                "Выбери нужный раздел:"
            )
            self.ui.send_message(
                chat_id,
                text,
                reply_markup=self.ui.settings_menu_markup(),
                message_id=message_id,
            )
            return

        if data == "menu_player":
            self.ui.answer_callback(callback_id)
            if message_id:
                try:
                    self.ui.delete_message(chat_id, message_id)
                except Exception:
                    pass

            media_state = getattr(self, "latest_media_state", None) or getattr(self.mqtt, "last_media_state", {})
            self._send_player_card(chat_id, media_state, business_connection_id=None)
            self.mqtt.send_media_command("refresh")
            return

        if data.startswith("player_"):
            action = data.replace("player_", "")
            self.active_player_message_id = message_id
            self.active_player_chat_id = chat_id
            self.active_player_business_connection_id = None if chat_id == ALLOWED_USER_ID else business_connection_id

            if action != "toggle_like":
                self.ui.answer_callback(callback_id)

            # Optimistic state update for instant UI button feedback (<50ms)
            media_state = dict(getattr(self, "latest_media_state", None) or getattr(self.mqtt, "last_media_state", {}) or {})
            optimistic_changed = False

            if action == "toggle_play":
                self.mqtt.send_media_command("toggle_play")
                if media_state:
                    media_state["is_playing"] = not media_state.get("is_playing", False)
                    optimistic_changed = True
            elif action == "seek_fwd_10":
                self.mqtt.send_media_command("seek", delta=10)
                if media_state:
                    dur = float(media_state.get("duration", 0.0))
                    cur_pos = float(media_state.get("position", 0.0))
                    media_state["position"] = min(dur, cur_pos + 10.0) if dur > 0 else cur_pos + 10.0
                    optimistic_changed = True
            elif action == "seek_back_10":
                self.mqtt.send_media_command("seek", delta=-10)
                if media_state:
                    cur_pos = float(media_state.get("position", 0.0))
                    media_state["position"] = max(0.0, cur_pos - 10.0)
                    optimistic_changed = True
            elif action == "next":
                self.mqtt.send_media_command("next")
                with self.player_edit_lock:
                    self.last_player_edit_time = 0.0
            elif action == "prev":
                self.mqtt.send_media_command("prev")
                with self.player_edit_lock:
                    self.last_player_edit_time = 0.0
            elif action == "wave":
                self.mqtt.send_media_command("wave")
                with self.player_edit_lock:
                    self.last_player_edit_time = 0.0
            elif action == "toggle_like":
                self.mqtt.send_media_command("toggle_like")
                if media_state:
                    new_liked = not media_state.get("is_liked", False)
                    media_state["is_liked"] = new_liked
                    optimistic_changed = True
                    toast = "❤️ Добавлено в Мою волну / Любимые" if new_liked else "💔 Удалено из любимых"
                    self.ui.answer_callback(callback_id, text=toast)
                else:
                    self.ui.answer_callback(callback_id)

            if optimistic_changed and message_id:
                self.latest_media_state = media_state
                with self.player_edit_lock:
                    self.last_player_edit_time = time.time()
                self._render_and_send_player(chat_id, message_id, media_state)
            return

        if data == "menu_activity":
            self.ui.answer_callback(callback_id)
            today_str = datetime.now(MOSCOW).strftime("%Y-%m-%d")
            self.pending_activity_chat_id = chat_id
            self.pending_activity_msg_id = message_id
            self.mqtt.query_activity("summary", today_str)
            self.ui.send_message(chat_id, f"📊 Запрашиваю статистику за {today_str}...", message_id=message_id)
            return

        if data == "power_menu":
            self.ui.answer_callback(callback_id)
            pc_state = self.mqtt.health.evaluate_pc_state(self.monitoring.telemetry_age())
            text = (
                "<b>⚡ ПК и экран</b>\n\n"
                f"<b>Состояние:</b> {pc_state.label}\n"
                f"<i>{pc_state.reason}</i>\n\n"
                f"<b>Сессия:</b> {self.mqtt.system_session}\n"
                f"<b>ESP:</b> {self.mqtt.esp_status}"
            )
            self.ui.send_message(chat_id, text, reply_markup=self._power_menu_markup(), message_id=message_id)
            return

        if data == "monitoring_menu":
            self.ui.answer_callback(callback_id)
            text = self.monitoring.dashboard_text()
            self.ui.send_message(chat_id, text, reply_markup=self.ui.monitoring_menu_markup(), message_id=message_id)
            return

        if data == "apps_menu":
            self.ui.answer_callback(callback_id)
            self.ui.send_message(chat_id, "<b>🧩 Программы</b>\n\nВыбери программу для запуска:", reply_markup=self.ui.apps_menu_markup(), message_id=message_id)
            return

        if data == "console_open":
            self.ui.answer_callback(callback_id)
            self.console_active_chat_id = chat_id
            self.current_menu_view = "console"
            text = self.ui.console_card_text(target=self.console_target, shell=self.console_shell)
            markup = self.ui.console_menu_markup(target=self.console_target, shell=self.console_shell)
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
            return

        if data == "console_close":
            self.ui.answer_callback(callback_id)
            self.console_active_chat_id = None
            self.current_menu_view = "main"
            self.ui.send_message(chat_id, self.monitoring.dashboard_text(), reply_markup=self._main_menu_markup(), message_id=message_id)
            return

        if data == "console_toggle_shell":
            self.ui.answer_callback(callback_id)
            self.console_shell = "cmd" if self.console_shell == "powershell" else "powershell"
            self.save_state()
            text = self.ui.console_card_text(target=self.console_target, shell=self.console_shell)
            markup = self.ui.console_menu_markup(target=self.console_target, shell=self.console_shell)
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
            return

        if data == "console_toggle_target":
            self.ui.answer_callback(callback_id)
            self.console_target = "system" if self.console_target == "desktop" else "desktop"
            self.save_state()
            text = self.ui.console_card_text(target=self.console_target, shell=self.console_shell)
            markup = self.ui.console_menu_markup(target=self.console_target, shell=self.console_shell)
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
            return

        if data.startswith("console_quick:"):
            q_cmd = data.split(":", 1)[1]
            self.ui.answer_callback(callback_id, f"Выполняю {q_cmd}...")
            self.execute_console_command(chat_id, q_cmd, target=self.console_target, shell=self.console_shell)
            return

        if data == "sound_menu":
            self.ui.answer_callback(callback_id)
            self.ui.send_message(
                chat_id,
                "<b>🔊 Управление громкостью</b>",
                reply_markup=self.ui.sound_menu_markup(),
                message_id=message_id,
            )
            return

        if data == "physical_button":
            self.ui.answer_callback(callback_id)
            text = self.physical_btn.physical_control_text()
            markup = self.physical_btn.physical_control_markup()
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
            return

        if data == "led_menu":
            self.ui.answer_callback(callback_id)
            text = (
                "<b>💡 Калибровка белого цвета кулера</b>\n\n"
                "Кулер: <i>Ocypus Gamma A40 WH ARGB</i>\n"
                "Пин управления: <code>D2 (ESP8266)</code>\n\n"
                "<b>⚡ Быстрые режимы:</b>\n"
                "👁 <b>Для глаз:</b> <code>255, 150, 125</code> (Идеал вживую)\n"
                "📸 <b>Для камеры:</b> <code>228, 245, 255</code> (Под сенсор камеры)\n\n"
                "<b>🎨 Палитра оттенков (1..12):</b>\n"
                "1️⃣ <code>255, 150, 165</code> — Холодный снежный (зелень срезана)\n"
                "2️⃣ <code>255, 140, 160</code> — Арктический холодный (без циана)\n"
                "3️⃣ <code>255, 130, 155</code> — Ледяной кристалл\n"
                "4️⃣ <code>255, 120, 150</code> — Глубокий холодный\n"
                "5️⃣ <code>255, 160, 160</code> — Симметричный чистый (G=B)\n"
                "6️⃣ <code>255, 145, 145</code> — Симметричный нейтральный\n"
                "7️⃣ <code>255, 130, 130</code> — Сбалансированный белый\n"
                "8️⃣ <code>255, 115, 115</code> — Мягкий белый\n"
                "9️⃣ <code>255, 165, 140</code> — Светлый нейтральный\n"
                "🔟 <code>255, 150, 125</code> — ⭐️ <b>Идеальный белый (Калибровка)</b>\n"
                "1️⃣1️⃣ <code>255, 135, 100</code> — Естественный дневной\n"
                "1️⃣2️⃣ <code>255, 120, 75</code> — Тёплый ламповый\n\n"
                "<i>Нажимай кнопки 1..12 ниже — оттенок меняется мгновенно!</i>"
            )
            self.ui.send_message(chat_id, text, reply_markup=self.ui.led_menu_markup(), message_id=message_id)
            return

        if data.startswith("led_action:"):
            if getattr(self.mqtt, "esp_status", None) != "online":
                self.ui.answer_callback(callback_id, "❌ Модуль управления кулером (ESP8266) не в сети!", alert=True)
                return
            action = data.split(":", 1)[1]
            if action == "on":
                self.mqtt.publish_esp_action("LED_ON")
                self.ui.answer_callback(callback_id, "💡 Подсветка включена")
            elif action == "off":
                self.mqtt.publish_esp_action("LED_OFF")
                self.ui.answer_callback(callback_id, "🌑 Подсветка выключена")
            elif action.startswith("bright:"):
                level = action.split(":", 1)[1]
                self.mqtt.publish_esp_action("LED_BRIGHTNESS", value=level)
                self.ui.answer_callback(callback_id, f"🔆 Яркость: {level}")
            elif action.startswith("color:"):
                rgb = action.split(":", 1)[1]
                self.mqtt.publish_esp_action("LED_COLOR", value=rgb)
                self.ui.answer_callback(callback_id, "🎨 Оттенок изменён")
            elif action.startswith("preset:"):
                preset_id = action.split(":", 1)[1]
                if preset_id in LED_WHITE_PRESETS:
                    rgb, name = LED_WHITE_PRESETS[preset_id]
                    self.mqtt.publish_esp_action("LED_COLOR", value=rgb)
                    self.ui.answer_callback(callback_id, f"#{preset_id}: {name} ({rgb})")
            return

        # Screenshot & Graphs buttons
        if data == "screenshot":
            if self.mqtt.desktop_status != "online":
                self.ui.answer_callback(callback_id, "Desktop-агент недоступен — скриншот сделать нельзя.", alert=True)
                return
            self.ui.answer_callback(callback_id, "Делаю скриншот...")
            cmd_id = self.mqtt.publish_desktop_action("screenshot")
            if not cmd_id:
                self.ui.send_message(chat_id, "❌ Не удалось отправить запрос скриншота: MQTT недоступен.")
                return
            self.pending_screenshots[cmd_id] = {"chat_id": chat_id, "requested_at": time.time()}
            return

        if data == "history_menu":
            self.ui.answer_callback(callback_id)
            stats = self.monitoring.history.stats()
            text = (
                "<b>📈 Графики</b>\n\n"
                f"Точек телеметрии: {stats['count']}\n"
                "Выбери период для графика нагрузки, температур, RAM, диска и сети."
            )
            self.ui.send_message(chat_id, text, reply_markup=self.ui.history_menu_markup(), message_id=message_id)
            return

        if data.startswith("history:"):
            period = data.split(":", 1)[1]
            periods = {
                "1h": (3600, "1 час"),
                "6h": (6 * 3600, "6 часов"),
                "24h": (24 * 3600, "24 часа"),
                "7d": (7 * 86400, "7 дней"),
                "30d": (30 * 86400, "30 дней"),
            }
            if period not in periods:
                self.ui.answer_callback(callback_id, "Неизвестный период", alert=True)
                return
            seconds, title = periods[period]
            self.ui.answer_callback(callback_id, "Строю график...")
            try:
                chart_bytes, count = self.monitoring.history.render_chart(seconds, title)
            except ValueError:
                self.ui.send_message(
                    chat_id,
                    "<b>📈 Графики</b>\n\nНедостаточно данных: подожди, пока агент соберёт хотя бы две точки телеметрии.",
                    reply_markup=self.ui.history_menu_markup(),
                )
                return
            except Exception:
                LOG.exception("Could not render telemetry chart")
                self.ui.send_message(
                    chat_id,
                    "<b>📈 Графики</b>\n\nНе удалось построить график. Состояние ПК продолжает работать.",
                    reply_markup=self.ui.history_menu_markup(),
                )
                return
            self.ui.send_photo(chat_id, chart_bytes, caption=f"<b>📈 Телеметрия за {title}</b>\nТочек: {count}")
            return

        if data == "telemetry_now":
            if self.mqtt.desktop_status != "online":
                self.ui.answer_callback(callback_id, "Desktop-агент пока недоступен — свежую телеметрию получить нельзя.", alert=True)
                return
            self.ui.answer_callback(callback_id, "Обновляю состояние...")
            cmd_id = self.mqtt.publish_desktop_action("telemetry")
            self.pending_telemetry_cmd_ids[cmd_id] = (chat_id, message_id)
            return

        # 2. Boot & Unlock Flow Callbacks
        if data == "power_on":
            pc_state = self.mqtt.health.evaluate_pc_state(self.monitoring.telemetry_age())
            state_str = getattr(getattr(pc_state, "state", None), "value", str(getattr(pc_state, "state", "")))
            if (
                state_str in ("READY", "DEGRADED", "LOCKED", "BOOTING")
                or self.mqtt.system_status == "online"
                or self.mqtt.desktop_status == "online"
            ):
                self.ui.answer_callback(callback_id, "Компьютер уже включен!", alert=True)
                return

            if getattr(self.mqtt, "esp_status", None) != "online":
                self.ui.answer_callback(callback_id, "❌ Модуль питания (ESP8266) не в сети!", alert=True)
                self.power_flow.start_power_on(chat_id)
                return

            self.physical_btn.revoke_morse_eligibility()
            self.save_state()
            self.ui.answer_callback(callback_id, "Запускаю ПК...")
            self.power_flow.start_power_on(chat_id)
            return

        if data in {"flow_unlock_yes", "flow_unlock_no", "flow_unlock_and_workspace"}:
            if data == "flow_unlock_and_workspace":
                self.ui.answer_callback(callback_id, "🚀 Сетап запустится после разблокировки!")
            else:
                self.ui.answer_callback(callback_id)
            self.power_flow.handle_unlock_decision(data, chat_id)
            return

        if data == "noop":
            self.ui.answer_callback(callback_id)
            return

        if data == "flow_unlock_retry":
            self.ui.answer_callback(callback_id, "Повторяю разблокировку...")
            self.power_flow.handle_unlock_retry(chat_id)
            return

        if data == "flow_unlock_cancel":
            self.ui.answer_callback(callback_id)
            self.power_flow.handle_unlock_cancel(chat_id)
            return

        # 3. Workspace Flow Callbacks
        if data in {"flow_workspace_yes", "flow_workspace_no"}:
            self.ui.answer_callback(callback_id)
            self.power_flow.handle_workspace_decision(data, chat_id, self.start_async_workspace_setup)
            return

        if data == "workspace_start_direct":
            pc_state = self.mqtt.health.evaluate_pc_state(self.monitoring.telemetry_age())
            if pc_state.session_status == "unlocked":
                self.ui.answer_callback(callback_id, "Запускаю рабочий сетап...")
                self.power_flow.stage = PowerFlowStage.WORKSPACE_RUNNING
                self.power_flow.workspace_cancelled = False
                self.power_flow.active_workspace_id = f"ws-{int(time.time())}"
                self.power_flow.save_state()
                self.start_async_workspace_setup(chat_id, card_message_id=message_id, workspace_id=self.power_flow.active_workspace_id)
            elif pc_state.state == "LOCKED":
                self.ui.answer_callback(callback_id)
                text = "<b>🔒 Windows не разблокирована.</b>\n\nРазблокировать сейчас?"
                markup = {
                    "inline_keyboard": [
                        [
                            {"text": "Да, разблокировать", "style": "success", "callback_data": "flow_unlock_yes"},
                            {"text": "✖ Не сейчас", "callback_data": "flow_unlock_no"},
                        ]
                    ]
                }
                self.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
            else:
                self.ui.answer_callback(callback_id)
                text = f"<b>{pc_state.label}</b>\n\n{pc_state.reason}\n\nВключить ПК сейчас?"
                markup = {
                    "inline_keyboard": [
                        [
                            {"text": "Включить ПК", "style": "success", "callback_data": "power_on"},
                            {"text": "✖ Не сейчас", "callback_data": "menu"},
                        ]
                    ]
                }
                self.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
            return

        if data == "flow_workspace_cancel":
            self.ui.answer_callback(callback_id, "Отменяю...")
            self.power_flow.cancel_workspace(chat_id)
            self.workspace_cancel_event.set()
            return

        # 4. Manual Actions
        if data == "unlock_windows_manual":
            pc_state = self.mqtt.health.evaluate_pc_state(self.monitoring.telemetry_age())
            if pc_state.state in ("SHUTDOWN_OBSERVED", "UNREACHABLE") or self.mqtt.system_status != "online":
                self.ui.answer_callback(callback_id, "ПК недоступен. Сначала включи его.", alert=True)
                return
            self.ui.answer_callback(callback_id, "Разблокирую...")
            self.power_flow.start_manual_unlock(chat_id, message_id=message_id)
            return

        if data == "lock_windows":
            self.ui.answer_callback(callback_id, "Блокирую...")
            self.mqtt.publish_desktop_action("lock")
            self.mqtt.publish_system_action("lock")
            return

        if data == "toggle_screen":
            self.ui.answer_callback(callback_id, "Переключаю экран...")
            self.mqtt.publish_desktop_action("screen_off")
            return

        if data == "minimize_windows":
            self.ui.answer_callback(callback_id, "Сворачиваю окна...")
            self.mqtt.publish_desktop_action("minimize_all")
            return

        if data == "sleep_pc":
            self.ui.answer_callback(callback_id)
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "Да, перевести в сон", "style": "primary", "callback_data": "sleep_confirm"},
                        {"text": "✖ Отмена", "callback_data": "menu"},
                    ]
                ]
            }
            self.ui.send_message(
                chat_id,
                "<b>💤 Перевод ПК в спящий режим</b>\n\nВы уверены?",
                reply_markup=markup,
                message_id=message_id,
            )
            return

        if data == "sleep_confirm":
            self.ui.answer_callback(callback_id, "Отправляю в сон...")
            self.mqtt.publish_system_action("sleep")
            if hasattr(self.mqtt, "publish_esp_action"):
                self.mqtt.publish_esp_action("LED_OFF")
            return

        if data == "hibernate_pc":
            self.ui.answer_callback(callback_id, "Перевожу в гибернацию...")
            self.mqtt.publish_desktop_action("hibernate")
            if hasattr(self.mqtt, "publish_esp_action"):
                self.mqtt.publish_esp_action("LED_OFF")
            return

        if data.startswith("volume:"):
            value = data.split(":", 1)[1]
            if value == "toggle":
                args = {"toggle": True}
            elif value in {"25", "50", "75", "100"}:
                args = {"percent": int(value)}
            else:
                self.ui.answer_callback(callback_id, "Неизвестный уровень громкости", alert=True)
                return
            self.ui.answer_callback(callback_id, "Меняю громкость...")
            self.mqtt.publish_desktop_action("volume", args)
            return

        if data == "reboot_pc":
            self.ui.answer_callback(callback_id)
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "Да, перезагрузить", "style": "primary", "callback_data": "reboot_confirm"},
                        {"text": "✖ Отмена", "callback_data": "menu"},
                    ]
                ]
            }
            self.ui.send_message(
                chat_id,
                "<b>🔄 Перезагрузка ПК</b>\n\nВы уверены, что хотите перезагрузить компьютер?",
                reply_markup=markup,
                message_id=message_id,
            )
            return

        if data == "reboot_confirm":
            self.ui.answer_callback(callback_id, "Перезагружаю...")
            self.mqtt.publish_system_action("restart")
            if hasattr(self.mqtt, "publish_esp_action"):
                self.mqtt.publish_esp_action("LED_OFF")
            return

        if data == "shutdown_pc":
            self.ui.answer_callback(callback_id)
            self.ui.send_message(
                chat_id,
                "<b>🛑 Выключение ПК</b>\n\nВыключить сейчас или запланировать по московскому времени?",
                reply_markup=self.ui.shutdown_menu_markup(self.scheduled_shutdown_ts),
                message_id=message_id,
            )
            return

        if data == "shutdown_now_confirm":
            self.ui.answer_callback(callback_id)
            self._request_shutdown(chat_id, source="manual")
            return

        if data == "shutdown_at_time":
            self.ui.answer_callback(callback_id)
            self.awaiting_shutdown_time_chat_id = chat_id
            self.ui.send_message(
                chat_id,
                "<b>⏰ Выключение по времени</b>\n\nОтправь время в формате <code>HH:MM</code>. Часовой пояс — Москва.\nНапример: <code>23:45</code>",
                reply_markup={"inline_keyboard": [self.ui.back_button("shutdown_pc")]},
                message_id=message_id,
            )
            return

        if data.startswith("shutdown_after:"):
            try:
                minutes = int(data.split(":", 1)[1])
            except ValueError:
                self.ui.answer_callback(callback_id, "Некорректный таймер", alert=True)
                return
            self.ui.answer_callback(callback_id)
            target = datetime.now(MOSCOW) + timedelta(minutes=minutes)
            self._show_shutdown_schedule_confirmation(chat_id, target, message_id)
            return

        if data.startswith("shutdown_schedule_confirm:"):
            try:
                target_ts = float(data.rsplit(":", 1)[1])
            except ValueError:
                self.ui.answer_callback(callback_id, "Некорректное время", alert=True)
                return
            self.ui.answer_callback(callback_id, "Таймер установлен")
            self._set_shutdown_schedule(chat_id, target_ts, message_id)
            return

        if data == "shutdown_schedule_cancel":
            self.ui.answer_callback(callback_id, "Таймер отменён")
            self.scheduled_shutdown_ts = None
            self.shutdown_candidate_ts = None
            self.awaiting_shutdown_time_chat_id = None
            self.save_state()
            self.ui.send_message(
                chat_id,
                "<b>✅ Таймер выключения отменён.</b>",
                reply_markup=self._power_menu_markup(),
                message_id=message_id,
            )
            return

        if data.startswith("app:"):
            app_name = data.split(":", 1)[1]
            self.ui.answer_callback(callback_id, f"Запускаю {app_name}...")
            self.mqtt.publish_desktop_action("launch_app", {"app": app_name})
            return

        # 5. Physical Button Management Callbacks
        if data == "physical_test":
            self.ui.answer_callback(callback_id)
            self.physical_btn.input_entries = ["Ожидаю нажатия на кнопку..."]
            text = "<b>🕹 Физическая кнопка — диагностика</b>\n\nОжидаю нажатия..."
            res = self.ui.send_message(chat_id, text, reply_markup={"inline_keyboard": [self.ui.back_button("physical_button")]})
            if res and res.get("message_id"):
                self.physical_btn.input_message_id = res["message_id"]
            return

        if data == "physical_record_begin":
            self.ui.answer_callback(callback_id)
            self.mqtt.publish_esp_action("START_PHYSICAL_RECORDING")
            text = (
                "<b>🎙 Записываю пароль</b>\n\n"
                "Натискай от 4 до 12 шагов.\n"
                "CLICK — короче 500 мс, HOLD — от 500 мс.\n"
                "После 3 секунд тишины покажу комбинацию для сохранения."
            )
            self.ui.send_message(chat_id, text, reply_markup={"inline_keyboard": [[{"text": "✖ Отмена", "callback_data": "physical_button"}]]})
            return

        if data.startswith("physical_record_save:"):
            seq = data.split(":", 1)[1]
            self.ui.answer_callback(callback_id, "Сохраняю...")
            self.physical_btn.start_save_password(seq, chat_id, self.save_state)
            return

        if data == "physical_password_begin":
            self.ui.answer_callback(callback_id)
            self.physical_btn.password_draft = []
            text = self.physical_btn.physical_builder_text()
            markup = self.physical_btn.physical_builder_markup()
            self.ui.send_message(chat_id, text, reply_markup=markup)
            return

        if data.startswith("physical_add:"):
            self.ui.answer_callback(callback_id)
            symbol = data.split(":", 1)[1]
            if len(self.physical_btn.password_draft) < 12:
                self.physical_btn.password_draft.append(symbol)
            text = self.physical_btn.physical_builder_text()
            markup = self.physical_btn.physical_builder_markup()
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=callback["message"]["message_id"])
            return

        if data == "physical_back":
            self.ui.answer_callback(callback_id)
            if self.physical_btn.password_draft:
                self.physical_btn.password_draft.pop()
            text = self.physical_btn.physical_builder_text()
            markup = self.physical_btn.physical_builder_markup()
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=callback["message"]["message_id"])
            return

        if data == "physical_save":
            self.ui.answer_callback(callback_id, "Сохраняю...")
            seq = "".join(self.physical_btn.password_draft)
            self.physical_btn.start_save_password(seq, chat_id, self.save_state)
            return

        if data == "physical_password_delete":
            self.ui.answer_callback(callback_id, "Удаляю...")
            self.physical_btn.start_delete_password(chat_id)
            return

        if data == "physical_unlock":
            self.ui.answer_callback(callback_id, "Разблокирую...")
            self.physical_btn.start_unlock_input(chat_id)
            return

        if data == "physical_unlock_pattern_begin":
            self.ui.answer_callback(callback_id)
            self.physical_btn.unlock_draft = list(self.physical_btn.saved_unlock_pattern or "CC")
            text = self.physical_btn.unlock_builder_text()
            markup = self.physical_btn.unlock_builder_markup()
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
            return

        if data.startswith("unlock_pat_add:"):
            self.ui.answer_callback(callback_id)
            symbol = data.split(":", 1)[1]
            if len(self.physical_btn.unlock_draft) < 8:
                self.physical_btn.unlock_draft.append(symbol)
            text = self.physical_btn.unlock_builder_text()
            markup = self.physical_btn.unlock_builder_markup()
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
            return

        if data == "unlock_pat_back":
            self.ui.answer_callback(callback_id)
            if self.physical_btn.unlock_draft:
                self.physical_btn.unlock_draft.pop()
            text = self.physical_btn.unlock_builder_text()
            markup = self.physical_btn.unlock_builder_markup()
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
            return

        if data == "unlock_pat_reset":
            self.ui.answer_callback(callback_id)
            self.physical_btn.unlock_draft = ["C", "C"]
            text = self.physical_btn.unlock_builder_text()
            markup = self.physical_btn.unlock_builder_markup()
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
            return

        if data == "unlock_pat_save":
            self.ui.answer_callback(callback_id, "Сохраняю...")
            seq = "".join(self.physical_btn.unlock_draft)
            self.physical_btn.saved_unlock_pattern = seq
            self.save_state()
            pat_disp = self.physical_btn.format_password_display(seq)
            text = (
                f"<b>✅ Жест разблокировки сохранён!</b>\n\n<blockquote><b>{pat_disp}</b></blockquote>\n"
                "Теперь при вводе этой комбинации на кнопке (в течение 5 минут после морзе-включения) Windows автоматически разблокируется."
            )
            self.ui.send_message(chat_id, text, reply_markup=self.physical_btn.physical_control_markup(), message_id=message_id)
            return

        self.ui.answer_callback(callback_id)

    def start_async_workspace_setup(self, chat_id, card_message_id=None, workspace_id=None):
        if not hasattr(self, "_workspace_start_lock"):
            self._workspace_start_lock = threading.Lock()
        with self._workspace_start_lock:
            if getattr(self, "_workspace_running", False):
                LOG.warning("Workspace setup runner is already active, ignoring duplicate start request")
                return
            self._workspace_running = True

        def _runner():
            try:
                self.run_workspace_setup_steps(chat_id, card_message_id, workspace_id)
            finally:
                self._workspace_running = False

        threading.Thread(
            target=_runner,
            name="workspace-runner-thread",
            daemon=True,
        ).start()

    def run_workspace_setup_steps(self, chat_id, card_message_id=None, workspace_id=None):
        """
        Asynchronous workspace setup steps driven entirely by real progress ACKs from agent.py:
        - Issue launch_app command with workspace_id.
        - Checklist card updates dynamically as progress ACKs arrive with step_id.
        - Strict Happ/VPN check: verifies Happ ready ACK. Halts & renders error if VPN fails/times out.
        - Strict full execution ACK check: verifies launch_app executed ACK.
        - Strict verification check: every step must be verified (no queued/unverified checkmarks).
        - Strict minimize_all ACK check: verifies minimize_all executed ACK before rendering final success!
        """
        self.app_statuses = {step["id"]: "⏳ В очереди" for step in WORKSPACE_STEPS}
        self.workspace_cancel_event.clear()
        self.happ_ready_event.clear()
        self.workspace_executed_event.clear()
        self.minimize_ack_event.clear()
        self.workspace_execution_result = None
        self.minimize_ack_result = None

        def render_checklist():
            lines = [
                f"{step['label']} — {self.app_statuses.get(step['id'], '⏳ В очереди')}"
                for step in WORKSPACE_STEPS
            ]
            return "<b>🚀 Запуск рабочего сетапа...</b>\n\n" + "\n".join(lines)

        markup = {"inline_keyboard": [[{"text": "Отменить запуск", "style": "danger", "callback_data": "flow_workspace_cancel"}]]}

        res = self.ui.send_message(chat_id, render_checklist(), reply_markup=markup, message_id=card_message_id)
        msg_id = res["message_id"] if res and res.get("message_id") else card_message_id
        self.workspace_card_msg_id = msg_id

        # 0. Wait up to 15s for desktop agent to be online if needed
        if self.mqtt.desktop_status != "online":
            LOG.info("Desktop agent not online yet, waiting up to 15s...")
            for _ in range(30):
                if self.workspace_cancel_event.is_set():
                    self.power_flow.reset_flow()
                    return
                if self.mqtt.desktop_status == "online":
                    break
                time.sleep(0.5)

        if self.mqtt.desktop_status != "online":
            LOG.warning("Desktop agent still not online after 15s wait.")
            error_text = render_checklist() + "\n\n<b>❌ Desktop-агент не в сети.</b>\nWindows загружается или desktop-агент ещё не подключился к брокеру."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "🔁 Повторить сетап", "callback_data": "workspace_start_direct"}],
                    [{"text": "✖ Отмена", "callback_data": "menu"}],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # Issue launch_app command to agent.py
        ws_id = workspace_id or f"desk-ws-{int(time.time())}"
        cmd_id = self.mqtt.publish_desktop_action("launch_app", {"app": "Рабочий сетап", "workspace_id": ws_id}, timeout=60)
        self.active_workspace_cmd_id = cmd_id

        # 1. Wait for Happ / VPN readiness (up to 35 seconds)
        vpn_ready = self._wait_event_or_cancel(self.happ_ready_event, 35.0)

        if self.workspace_cancel_event.is_set():
            LOG.info("Runner thread detected cancellation during Happ wait. Halting.")
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.power_flow.reset_flow()
            return

        if not vpn_ready and not self.happ_ready_event.is_set():
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.app_statuses["happ_vpn"] = "❌ Ошибка VPN"
            error_text = render_checklist() + "\n\n<b>❌ Не удалось подтвердить подключение Happ (VPN).</b>\nЗапуск зависимых приложений отменён в целях безопасности."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "🔁 Повторить сетап", "callback_data": "workspace_start_direct"}],
                    [{"text": "🖼️ Скриншот", "callback_data": "screenshot"}, {"text": "📍 Главное меню", "callback_data": "menu"}],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 2. Wait for full workspace scenario execution ACK from agent.py (up to 45 seconds)
        ws_executed = self._wait_event_or_cancel(self.workspace_executed_event, 45.0)

        if self.workspace_cancel_event.is_set():
            LOG.info("Runner thread detected cancellation during workspace execution wait. Halting.")
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.power_flow.reset_flow()
            return

        if not ws_executed or self.workspace_execution_result == "failed":
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            error_text = render_checklist() + "\n\n<b>❌ Ошибка выполнения рабочего сетапа.</b>\nDesktop-агент сообщил о сбое или запуск превысил таймаут."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "🔁 Повторить сетап", "callback_data": "workspace_start_direct"}],
                    [{"text": "🖼️ Скриншот", "callback_data": "screenshot"}, {"text": "📍 Главное меню", "callback_data": "menu"}],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 3. Strict verification check: every step must be strictly verified
        unverified = [s["label"] for s in WORKSPACE_STEPS if self.app_statuses.get(s["id"]) != "✅"]
        if unverified:
            notice_text = render_checklist() + f"\n\n<b>⚠️ Рабочий сетап завершён с неполным подтверждением.</b>\nНе подтверждены: {', '.join(unverified)}"
            notice_markup = {
                "inline_keyboard": [
                    [{"text": "🔁 Повторить сетап", "callback_data": "workspace_start_direct"}],
                    [{"text": "🖼️ Скриншот", "callback_data": "screenshot"}, {"text": "📍 Главное меню", "callback_data": "menu"}],
                ]
            }
            self.ui.send_message(chat_id, notice_text, reply_markup=notice_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 4. Standalone minimize_all and wait for ACK (only if not already minimized by agent)
        already_minimized = (
            isinstance(self.workspace_execution_result, dict)
            and self.workspace_execution_result.get("desktop_minimized")
        )
        if not already_minimized:
            min_cmd_id = self.mqtt.publish_desktop_action("minimize_all")
            self.pending_minimize_cmd_id = min_cmd_id
            min_ack_received = self._wait_event_or_cancel(self.minimize_ack_event, 10.0)

            if self.workspace_cancel_event.is_set():
                LOG.info("Runner thread detected cancellation during minimize_all wait. Halting.")
                self.power_flow.reset_flow()
                return

            # Check min_ack_received and minimize_ack_result strictly
            if not min_ack_received or self.minimize_ack_result != "executed":
                notice_text = render_checklist() + "\n\n<b>⚠️ Рабочий сетап запущен, но не удалось свернуть окна.</b>"
                notice_markup = {
                    "inline_keyboard": [
                        [{"text": "🖥 Свернуть окна", "callback_data": "minimize_windows"}],
                        [{"text": "🖼️ Скриншот", "callback_data": "screenshot"}, {"text": "📍 Главное меню", "callback_data": "menu"}],
                    ]
                }
                self.ui.send_message(chat_id, notice_text, reply_markup=notice_markup, message_id=msg_id)
                self.power_flow.reset_flow()
                return

        # 5. Final Success (Only when minimize_all ACK executed!)
        final_text = render_checklist() + "\n\n<b>✅ Рабочий сетап успешно запущен!</b>\nВсе приложения открыты, VPN активен, окна свернуты."
        final_markup = {
            "inline_keyboard": [
                [{"text": "🖼️ Скриншот", "callback_data": "screenshot"}, {"text": "📍 Главное меню", "callback_data": "menu"}],
            ]
        }
        self.ui.send_message(chat_id, final_text, reply_markup=final_markup, message_id=msg_id)
        self.power_flow.reset_flow()

    def execute_console_command(self, chat_id, cmd_text, target=None, shell=None):
        if not cmd_text or not cmd_text.strip():
            self.ui.send_message(chat_id, "⚠️ Команда не может быть пустой.")
            return

        cmd_text = cmd_text.strip()
        target = (target or getattr(self, "console_target", "desktop") or "desktop").lower()
        shell = (shell or getattr(self, "console_shell", "powershell") or "powershell").lower()

        # Check agent status
        if target == "desktop" and self.mqtt.desktop_status != "online":
            self.ui.send_message(
                chat_id,
                "❌ <b>Desktop-агент offline.</b>\nПереключитесь на <b>SYSTEM</b> или дождитесь разблокировки ПК.",
            )
            return
        elif target == "system" and self.mqtt.system_status != "online":
            self.ui.send_message(
                chat_id,
                "❌ <b>System-агент offline.</b>\nВключите ПК или проверьте службу boot_agent.",
            )
            return

        timeout_s = 60
        action = "powershell" if shell == "powershell" else "cmd"
        args = {"command": cmd_text, "timeout": timeout_s, "shell": shell}

        safe_cmd = html.escape(cmd_text)
        shell_badge = "PowerShell" if shell == "powershell" else "CMD"
        target_badge = "Desktop" if target == "desktop" else "SYSTEM"
        status_msg = self.ui.send_message(
            chat_id,
            f"⏳ [<b>{target_badge}</b> | <b>{shell_badge}</b>] Выполняю:\n<code>{safe_cmd}</code>"
        )
        status_msg_id = status_msg.get("message_id") if isinstance(status_msg, dict) else None

        if target == "system":
            cmd_id = self.mqtt.publish_system_action(action, args=args, timeout=timeout_s)
        else:
            cmd_id = self.mqtt.publish_desktop_action(action, args=args, timeout=timeout_s)

        if not cmd_id:
            msg_fail = "❌ Не удалось отправить команду в MQTT (брокер недоступен)."
            if status_msg_id:
                self.ui.send_message(chat_id, msg_fail, message_id=status_msg_id)
            else:
                self.ui.send_message(chat_id, msg_fail)
            return

        self.pending_console_cmds[cmd_id] = {
            "chat_id": chat_id,
            "message_id": status_msg_id,
            "command": cmd_text,
            "target": target,
            "shell": shell,
            "started_at": time.time(),
        }

    def _handle_console_ack(self, pending_cmd, data):
        chat_id = pending_cmd["chat_id"]
        status_msg_id = pending_cmd.get("message_id")
        cmd_text = pending_cmd["command"]
        target = pending_cmd["target"]
        shell = pending_cmd["shell"]
        started_at = pending_cmd.get("started_at", time.time())
        elapsed = max(0.0, time.time() - started_at)

        ack_data = data.get("data") or {}
        exit_code = ack_data.get("exit_code")
        if exit_code is None:
            exit_code = 0 if data.get("result") == "executed" else 1
        stdout = (ack_data.get("stdout") or "").strip()
        stderr = (ack_data.get("stderr") or "").strip()

        status_badge = "✅" if exit_code == 0 else f"❌ ({exit_code})"
        shell_badge = "PowerShell" if shell == "powershell" else "CMD"
        target_badge = "Desktop" if target == "desktop" else "SYSTEM"
        header = (
            f"💻 <b>Консоль [{target_badge} | {shell_badge}]</b>\n"
            f"Команда: <code>{html.escape(cmd_text)}</code>\n"
            f"Статус: {status_badge} | Время: <code>{elapsed:.2f}s</code>\n"
        )

        full_output = ""
        if stdout:
            full_output += stdout
        if stderr:
            if full_output:
                full_output += "\n--- STDERR ---\n"
            full_output += stderr

        if not full_output:
            full_output = "(нет вывода)"

        combined_len = len(header) + len(full_output) + 30
        if combined_len <= 3500:
            formatted_text = f"{header}\n<pre><code>{html.escape(full_output)}</code></pre>"
            res = None
            if status_msg_id:
                res = self.ui.send_message(chat_id, formatted_text, message_id=status_msg_id)
            if not res:
                self.ui.send_message(chat_id, formatted_text)
        else:
            preview = full_output[:1000]
            summary_text = (
                f"{header}\n"
                f"⚠️ <i>Вывод слишком большой ({len(full_output)} симв.). Полный вывод прикреплён файлом.</i>\n\n"
                f"<pre><code>{html.escape(preview)}\n... [см. вложенный файл]</code></pre>"
            )
            res = None
            if status_msg_id:
                res = self.ui.send_message(chat_id, summary_text, message_id=status_msg_id)
            if not res:
                self.ui.send_message(chat_id, summary_text)
            self.ui.send_document(
                chat_id,
                full_output.encode("utf-8"),
                filename="console_output.txt",
                caption=f"📄 Вывод: {html.escape(cmd_text[:40])}",
            )

    def handle_message(self, message):
        chat_id = message["chat"]["id"]
        chat_type = message.get("chat", {}).get("type", "private")
        from_id = message.get("from", {}).get("id")
        text = message.get("text", "").strip()

        # Group chats support: only allow player command if user is authorized
        if chat_type != "private":
            if text.lower().startswith(("/player", "!player", "плеер", "музыка")):
                incoming_msg_id = message.get("message_id")
                if incoming_msg_id:
                    try:
                        self.ui.delete_message(chat_id, incoming_msg_id)
                    except Exception:
                        pass
                media_state = getattr(self, "latest_media_state", None) or getattr(self.mqtt, "last_media_state", {})
                self._send_player_card(chat_id, media_state, business_connection_id=None)
                self.mqtt.send_media_command("refresh")
            return

        if chat_id != ALLOWED_USER_ID:
            self.ui.send_message(chat_id, "Доступ запрещён.")
            return

        lower_text = text.lower().strip()
        if lower_text in {"/start", "/menu", "меню", "меню пк"}:
            self.console_active_chat_id = None
            res = self.ui.send_message(
                chat_id,
                self.monitoring.dashboard_text(self.latest_media_state or getattr(self.mqtt, "last_media_state", {})),
                reply_markup=self._main_menu_markup(),
            )
            if res and isinstance(res, dict) and "message_id" in res:
                self.active_main_menu_message_id = res["message_id"]
                self.current_menu_view = "main"
                self.save_state()
            return

        if lower_text in {"/console", "консоль", "терминал"}:
            self.console_active_chat_id = chat_id
            self.current_menu_view = "console"
            text_card = self.ui.console_card_text(target=self.console_target, shell=self.console_shell)
            markup = self.ui.console_menu_markup(target=self.console_target, shell=self.console_shell)
            self.ui.send_message(chat_id, text_card, reply_markup=markup)
            return

        if lower_text in {"/exit", "/quit", "выход"}:
            if self.console_active_chat_id == chat_id:
                self.console_active_chat_id = None
                self.current_menu_view = "main"
                self.ui.send_message(chat_id, "🚪 <b>Консоль закрыта.</b>", reply_markup=self._main_menu_markup())
                return

        if lower_text.startswith("/cmd"):
            parts = text.split(None, 1)
            if len(parts) > 1 and parts[1].strip():
                self.execute_console_command(chat_id, parts[1].strip(), target=self.console_target, shell="cmd")
            else:
                self.ui.send_message(chat_id, "ℹ️ Использование: <code>/cmd &lt;команда&gt;</code>\nНапример: <code>/cmd dir C:\\</code>")
            return

        if lower_text.startswith("/ps"):
            parts = text.split(None, 1)
            if len(parts) > 1 and parts[1].strip():
                self.execute_console_command(chat_id, parts[1].strip(), target=self.console_target, shell="powershell")
            else:
                self.ui.send_message(chat_id, "ℹ️ Использование: <code>/ps &lt;команда&gt;</code>\nНапример: <code>/ps Get-Process</code>")
            return

        if getattr(self, "console_active_chat_id", None) == chat_id and not text.startswith("/"):
            self.execute_console_command(chat_id, text, target=self.console_target, shell=self.console_shell)
            return

        if lower_text in {"/led", "/led_menu", "подсветка", "кулер"}:
            text = (
                "<b>💡 Калибровка белого цвета кулера</b>\n\n"
                "Кулер: <i>Ocypus Gamma A40 WH ARGB</i>\n"
                "Пин управления: <code>D2 (ESP8266)</code>\n\n"
                "<b>⚡ Быстрые режимы:</b>\n"
                "👁 <b>Для глаз:</b> <code>255, 150, 125</code> (Идеал вживую)\n"
                "📸 <b>Для камеры:</b> <code>228, 245, 255</code> (Под сенсор камеры)\n\n"
                "<b>🎨 Палитра оттенков (1..12):</b>\n"
                "1️⃣ <code>255, 150, 165</code> — Холодный снежный (зелень срезана)\n"
                "2️⃣ <code>255, 140, 160</code> — Арктический холодный (без циана)\n"
                "3️⃣ <code>255, 130, 155</code> — Ледяной кристалл\n"
                "4️⃣ <code>255, 120, 150</code> — Глубокий холодный\n"
                "5️⃣ <code>255, 160, 160</code> — Симметричный чистый (G=B)\n"
                "6️⃣ <code>255, 145, 145</code> — Симметричный нейтральный\n"
                "7️⃣ <code>255, 130, 130</code> — Сбалансированный белый\n"
                "8️⃣ <code>255, 115, 115</code> — Мягкий белый\n"
                "9️⃣ <code>255, 165, 140</code> — Светлый нейтральный\n"
                "🔟 <code>255, 150, 125</code> — ⭐️ <b>Идеальный белый (Калибровка)</b>\n"
                "1️⃣1️⃣ <code>255, 135, 100</code> — Естественный дневной\n"
                "1️⃣2️⃣ <code>255, 120, 75</code> — Тёплый ламповый\n\n"
                "<i>Нажимай кнопки 1..12 ниже — оттенок меняется мгновенно!</i>"
            )
            self.ui.send_message(chat_id, text, reply_markup=self.ui.led_menu_markup())
            return

        if lower_text.startswith(("/led ", "подсветка ")):
            if getattr(self.mqtt, "esp_status", None) != "online":
                self.ui.send_message(chat_id, "❌ Модуль управления кулером (ESP8266) не в сети!")
                return
            parts = lower_text.split(None, 1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            if arg in LED_WHITE_PRESETS:
                rgb, name = LED_WHITE_PRESETS[arg]
                self.mqtt.publish_esp_action("LED_COLOR", value=rgb)
                self.ui.send_message(chat_id, f"💡 Выбран оттенок #{arg}: {name} (<code>{rgb}</code>)")
                return

        if lower_text in {"/led_on", "включить подсветку"}:
            if getattr(self.mqtt, "esp_status", None) != "online":
                self.ui.send_message(chat_id, "❌ Модуль управления кулером (ESP8266) не в сети!")
                return
            self.mqtt.publish_esp_action("LED_ON")
            self.ui.send_message(chat_id, "💡 Подсветка кулера включена")
            return

        if lower_text in {"/led_off", "выключить подсветку"}:
            if getattr(self.mqtt, "esp_status", None) != "online":
                self.ui.send_message(chat_id, "❌ Модуль управления кулером (ESP8266) не в сети!")
                return
            self.mqtt.publish_esp_action("LED_OFF")
            self.ui.send_message(chat_id, "🌑 Подсветка кулера выключена")
            return

        if lower_text.startswith(("/player", "!player", "/music")) or lower_text in {"плеер", "музыка"}:
            incoming_msg_id = message.get("message_id")
            if incoming_msg_id:
                try:
                    self.ui.delete_message(chat_id, incoming_msg_id)
                except Exception:
                    pass
            media_state = getattr(self, "latest_media_state", None) or getattr(self.mqtt, "last_media_state", {})
            self._send_player_card(chat_id, media_state, business_connection_id=None)
            self.mqtt.send_media_command("refresh")
            return

        if text.lower().startswith("/activity") or text.lower() in {"активность"}:
            today_str = datetime.now(MOSCOW).strftime("%Y-%m-%d")
            self.pending_activity_chat_id = chat_id
            self.mqtt.query_activity("summary", today_str)
            self.ui.send_message(chat_id, f"📊 Запрашиваю статистику за {today_str}...")
            return

        time_text = text
        if text.lower().startswith("/shutdown "):
            time_text = text.split(None, 1)[1].strip()
        if self.awaiting_shutdown_time_chat_id == chat_id or text.lower().startswith("/shutdown "):
            try:
                hour_text, minute_text = time_text.split(":", 1)
                hour, minute = int(hour_text), int(minute_text)
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except (ValueError, TypeError):
                self.ui.send_message(chat_id, "❌ Введи время как <code>HH:MM</code>, например <code>23:45</code>.")
                return

            now = datetime.now(MOSCOW)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            self.awaiting_shutdown_time_chat_id = None
            self._show_shutdown_schedule_confirmation(chat_id, target)

    def _show_shutdown_schedule_confirmation(self, chat_id, target, message_id=None):
        self.shutdown_candidate_ts = target.timestamp()
        target_text = target.astimezone(MOSCOW).strftime("%d.%m.%Y в %H:%M МСК")
        markup = {
            "inline_keyboard": [
                [{"text": "Установить таймер", "style": "success", "callback_data": f"shutdown_schedule_confirm:{int(self.shutdown_candidate_ts)}"}],
                [{"text": "✖ Отмена", "callback_data": "shutdown_pc"}],
            ]
        }
        self.ui.send_message(
            chat_id,
            f"<b>Подтверди выключение</b>\n\nПК будет выключен <b>{target_text}</b>.",
            reply_markup=markup,
            message_id=message_id,
        )

    def _set_shutdown_schedule(self, chat_id, target_ts, message_id=None):
        if target_ts <= time.time():
            self.ui.send_message(chat_id, "❌ Это время уже прошло. Выбери новое.", reply_markup=self.ui.shutdown_menu_markup())
            return
        self.scheduled_shutdown_ts = target_ts
        self.shutdown_candidate_ts = None
        self.last_schedule_attempt_at = 0.0
        self.save_state()
        target_text = datetime.fromtimestamp(target_ts, MOSCOW).strftime("%d.%m.%Y в %H:%M МСК")
        self.ui.send_message(
            chat_id,
            f"<b>✅ Таймер установлен.</b>\n\nПК выключится <b>{target_text}</b>. Таймер сохранится после перезапуска бота.",
            reply_markup=self.ui.shutdown_menu_markup(self.scheduled_shutdown_ts),
            message_id=message_id,
        )

    def _request_shutdown(self, chat_id, source, now=None):
        now = time.time() if now is None else float(now)
        if self.pending_shutdown_cmd_id:
            self.ui.send_message(chat_id, "⏳ Команда выключения уже отправлена. Ожидаю подтверждение.")
            return False
        pc_state = self.mqtt.health.evaluate_pc_state(self.monitoring.telemetry_age(), now=now)
        if pc_state.state == "SHUTDOWN_OBSERVED":
            self.ui.send_message(chat_id, "ℹ️ ПК уже выключен (наблюдается).", reply_markup=self._power_menu_markup())
            return False
        if not getattr(self.mqtt, "mqtt_online", False):
            self.ui.send_message(chat_id, "❌ MQTT недоступен. Выключение не отправлено.")
            return False
        if self.mqtt.system_status != "online":
            self.ui.send_message(
                chat_id,
                f"❌ Системный агент сейчас недоступен ({self.mqtt.system_status}). Выключение не отправлено.",
                reply_markup=self._power_menu_markup(),
            )
            return False
        cmd_id = self.mqtt.publish_system_action("shutdown")
        if hasattr(self.mqtt, "publish_esp_action"):
            self.mqtt.publish_esp_action("LED_OFF")
        if not cmd_id:
            self.ui.send_message(chat_id, "❌ MQTT недоступен. Выключение не отправлено.")
            return False
        self.pending_shutdown_cmd_id = cmd_id
        self.pending_shutdown_started_at = now
        self.shutdown_acknowledged = False
        self.shutdown_offline_detected_at = None
        self.save_state()
        label = "по таймеру" if source == "schedule" else "вручную"
        self.ui.send_message(chat_id, f"<b>🛑 Команда выключения отправлена {label}.</b>\n\nЖду ответ Windows…")
        return True

    def _clear_pending_shutdown(self):
        self.pending_shutdown_cmd_id = None
        self.pending_shutdown_started_at = None
        self.shutdown_acknowledged = False
        self.shutdown_offline_detected_at = None
        self.save_state()

    def _is_user_in_telegram_on_pc(self):
        # PC session must be unlocked
        if getattr(self.mqtt, "system_session", "") != "unlocked":
            return False

        # Telemetry must be fresh (<= 30s)
        age = self.monitoring.telemetry_age()
        if age is None or age > 30.0:
            return False

        telemetry = self.monitoring.latest_telemetry or {}
        active_app = (telemetry.get("active_application") or "").lower()

        telegram_clients = ("telegram", "ayugram", "kotatogram", "64gram", "unigram")
        if not any(client in active_app for client in telegram_clients):
            return False

        # User idle time check (don't refresh if user stepped away from PC > 5 min)
        idle = telemetry.get("idle_seconds")
        if idle is not None and idle > 300:
            return False

        return True

    def _check_dashboard_auto_refresh(self, now):
        if not getattr(self, "active_main_menu_message_id", None):
            return
        if getattr(self, "current_menu_view", "main") != "main":
            return

        if not self._is_user_in_telegram_on_pc():
            return

        telemetry = self.monitoring.latest_telemetry
        if not isinstance(telemetry, dict):
            telemetry = {}
        in_pc_control = bool(telemetry.get("in_pc_control_chat") is True)
        if not in_pc_control:
            active_title = str(telemetry.get("active_window_title") or "").lower()
            if "pc control" in active_title or "your_pc_bot" in active_title:
                in_pc_control = True

        refresh_interval = 5.0 if in_pc_control else 10.0
        if now - getattr(self, "last_dashboard_auto_refresh", 0.0) < refresh_interval:
            return

        self.last_dashboard_auto_refresh = now
        try:
            text = self.monitoring.dashboard_text(self.latest_media_state or getattr(self.mqtt, "last_media_state", {}))
            markup = self._main_menu_markup()
            res = self.ui.send_message(
                ALLOWED_USER_ID,
                text,
                reply_markup=markup,
                message_id=self.active_main_menu_message_id,
            )
            if res and res.get("not_modified"):
                pass
            elif not res:
                LOG.debug("Auto-refresh of main menu message %s returned None", self.active_main_menu_message_id)
        except Exception:
            LOG.exception("Failed to auto-refresh main menu")

    def scheduler_tick(self, now=None):
        now = time.time() if now is None else float(now)

        self._check_dashboard_auto_refresh(now)

        # Power flow runtime timeout watchdog (cancels zombie flows after 5m)
        if hasattr(self, "power_flow") and hasattr(self.power_flow, "check_timeout"):
            try:
                self.power_flow.check_timeout(now)
            except Exception:
                LOG.exception("power_flow.check_timeout failed in scheduler_tick")

        # Auto-LED cooler power watchdog (cuts fan power if PC is turned off, asleep, or frozen)
        telem_age = self.monitoring.telemetry_age()
        sys_status = getattr(self.mqtt, "system_status", "unknown")
        desk_status = getattr(self.mqtt, "desktop_status", "unknown")
        sys_session = getattr(self.mqtt, "system_session", "unknown")
        watchdog_pc_online = (
            sys_status == "online"
            or desk_status == "online"
            or sys_session in ("unlocked", "locked", "login_screen")
        )
        if telem_age is not None and telem_age > 45.0:
            watchdog_pc_online = False

        if (not watchdog_pc_online) and getattr(self, "last_auto_led_state", None) != "off":
            if hasattr(self.mqtt, "publish_esp_action"):
                if self.mqtt.publish_esp_action("LED_OFF"):
                    self.last_auto_led_state = "off"
                    LOG.info("Auto-LED Watchdog: PC offline/sleeping (telem_age=%s) -> LED_OFF sent to cooler", telem_age)
        elif watchdog_pc_online and getattr(self, "last_auto_led_state", None) != "on":
            if hasattr(self.mqtt, "publish_esp_action"):
                if self.mqtt.publish_esp_action("LED_ON"):
                    self.last_auto_led_state = "on"
                    LOG.info("Auto-LED Watchdog: PC online/awake -> LED_ON sent to cooler")

        for cmd_id, pending in list(self.pending_screenshots.items()):
            if now - pending["requested_at"] >= SCREENSHOT_TIMEOUT_SECONDS:
                self.pending_screenshots.pop(cmd_id, None)
                self.ui.send_message(pending["chat_id"], "❌ Скриншот не получен за 35 секунд. Попробуй ещё раз.")

        if self.pending_shutdown_cmd_id and self.pending_shutdown_started_at:
            confirmed = self._check_shutdown_confirmation(now)
            if not confirmed and (now - float(self.pending_shutdown_started_at) >= SHUTDOWN_CONFIRM_TIMEOUT_SECONDS):
                if self.shutdown_acknowledged:
                    self.ui.send_message(
                        ALLOWED_USER_ID,
                        "<b>⚠️ Связь потеряна, состояние ПК неизвестно.</b>\n\nКоманда выключения была принята Windows, но подтверждение завершения работы не получено за 2 минуты.",
                        reply_markup=self._power_menu_markup(),
                    )
                else:
                    self.ui.send_message(
                        ALLOWED_USER_ID,
                        "<b>❌ Выключение не подтверждено.</b>\n\nWindows не ответила на команду выключения за 2 минуты.",
                        reply_markup=self._power_menu_markup(),
                    )
                self._clear_pending_shutdown()

        if not self.scheduled_shutdown_ts or now < self.scheduled_shutdown_ts:
            return
        if now - self.last_schedule_attempt_at < 15:
            return
        self.last_schedule_attempt_at = now

        pc_state = self.mqtt.health.evaluate_pc_state(self.monitoring.telemetry_age(), now=now)
        if pc_state.state == "SHUTDOWN_OBSERVED":
            self.scheduled_shutdown_ts = None
            self.save_state()
            self.ui.send_message(ALLOWED_USER_ID, "ℹ️ Таймер сработал, но ПК уже был выключен (наблюдается).")
            return
        if self.mqtt.system_status != "online":
            return
        if self._request_shutdown(ALLOWED_USER_ID, source="schedule", now=now):
            self.scheduled_shutdown_ts = None
            self.save_state()

    def scheduler_loop(self):
        while self.running:
            try:
                self.scheduler_tick()
            except Exception:
                LOG.exception("Shutdown scheduler tick failed")
            time.sleep(1)

    def handle_business_message(self, message):
        text = (message.get("text") or "").strip()
        chat_id = message.get("chat", {}).get("id")
        conn_id = message.get("business_connection_id")
        from_id = message.get("from", {}).get("id")

        LOG.info("Business message received: chat_id=%s conn_id=%s from_id=%s text=%r", chat_id, conn_id, from_id, text)

        if not text or not chat_id or not conn_id:
            return

        cmd = text.lower().split()[0]
        if cmd in {"/player", "!player", "плеер", "музыка"}:
            if from_id != ALLOWED_USER_ID:
                LOG.warning("Ignoring /player from unauthorized user %s in business chat %s", from_id, chat_id)
                return

            actual_conn_id = None if chat_id == ALLOWED_USER_ID else conn_id
            incoming_msg_id = message.get("message_id")
            if incoming_msg_id:
                try:
                    self.ui.delete_message(chat_id, incoming_msg_id, business_connection_id=actual_conn_id)
                except Exception:
                    pass

            media_state = getattr(self, "latest_media_state", None) or getattr(self.mqtt, "last_media_state", {})
            self._send_player_card(chat_id, media_state, business_connection_id=actual_conn_id)
            self.mqtt.send_media_command("refresh")
            return

    def handle_business_connection(self, connection):
        conn_id = connection.get("id")
        user_id = connection.get("user", {}).get("id")
        is_enabled = connection.get("is_enabled", False)
        can_reply = connection.get("can_reply", False)
        LOG.info("Telegram Business Connection update: id=%s user=%s enabled=%s can_reply=%s", conn_id, user_id, is_enabled, can_reply)
        if not can_reply and user_id == ALLOWED_USER_ID and is_enabled:
            self.ui.send_message(
                ALLOWED_USER_ID,
                "⚠️ <b>Внимание: Telegram Business</b>\n\n"
                "Бот подключен, но у него <b>выключено разрешение «Отвечать на сообщения»</b> (<code>can_reply=false</code>).\n\n"
                "Чтобы бот мог отправлять плеер в чат:\n"
                "1. Открой в Telegram: <b>Настройки</b> ➔ <b>Telegram для бизнеса</b> ➔ <b>Чат-боты</b>\n"
                "2. Нажми на бота и <b>ВКЛЮЧИ</b>: <b>«Отвечать на сообщения»</b>\n"
                "3. В пункте <b>«Чаты»</b> выбери <b>«Все личные чаты»</b> (а не только новые)",
            )

    def poll_updates(self):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        payload = {
            "offset": self.offset,
            "timeout": 20,
            "allowed_updates": ["message", "callback_query", "business_connection", "business_message", "edited_business_message"],
        }
        try:
            res = self.ui.session.post(url, json=payload, timeout=25)
            if res.status_code != 200:
                time.sleep(2)
                return
            data = res.json()
            if not data.get("ok"):
                time.sleep(2)
                return
            updates = data.get("result", [])
            if updates:
                for update in updates:
                    self.offset = int(update["update_id"]) + 1
                    if "message" in update:
                        self.handle_message(update["message"])
                    elif "business_message" in update:
                        self.handle_business_message(update["business_message"])
                    elif "business_connection" in update:
                        self.handle_business_connection(update["business_connection"])
                    elif "callback_query" in update:
                        self.handle_callback(update["callback_query"])
                self.save_state()
        except Exception:
            LOG.exception("Telegram polling exception")
            time.sleep(3)

    def register_commands(self):
        try:
            self.ui.call(
                "setMyCommands",
                {
                    "commands": [
                        {"command": "menu", "description": "🖥 Главное меню"},
                        {"command": "player", "description": "🎵 Музыкальный плеер"},
                        {"command": "console", "description": "💻 Консоль Windows"},
                    ]
                },
            )
            LOG.info("Telegram setMyCommands successfully registered")
        except Exception as e:
            LOG.warning("Failed to register Telegram commands: %s", e)

    def run(self):
        LOG.info("Starting PC Control Bot application...")
        self.register_commands()
        self.mqtt.start()
        threading.Thread(target=self.scheduler_loop, name="shutdown-scheduler", daemon=True).start()
        res = self.ui.send_message(ALLOWED_USER_ID, self.monitoring.dashboard_text(), reply_markup=self._main_menu_markup())
        if res and isinstance(res, dict) and "message_id" in res:
            self.active_main_menu_message_id = res["message_id"]
            self.current_menu_view = "main"
            self.save_state()

        while self.running:
            self.poll_updates()


if __name__ == "__main__":
    app = PcControlBotApp()
    app.run()
