#!/usr/bin/env python3

import html
import json
import logging
import os
import requests
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

try:
    from .telegram_ui import TelegramBotApi, ICONS
except ImportError:
    from telegram_ui import TelegramBotApi, ICONS
from mqtt_gateway import MqttGateway
from power_flow import PowerFlowManager, PowerFlowStage
from physical_button import PhysicalButtonManager
from monitoring import MonitoringManager
from health_engine import PCState
try:
    from .routers import (
        route_message,
        route_power_callback,
        route_workspace_callback,
        route_player_callback,
        route_console_callback,
        route_monitoring_callback,
        route_physical_callback,
        route_antigravity_callback,
    )
except ImportError:
    from routers import (
        route_message,
        route_power_callback,
        route_workspace_callback,
        route_player_callback,
        route_console_callback,
        route_monitoring_callback,
        route_physical_callback,
        route_antigravity_callback,
    )


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


def get_workspace_steps(vpn="incy"):
    vpn_is_happ = str(vpn).lower() == "happ"
    vpn_item = (
        {
            "id": "happ_vpn",
            "label": "Happ (VPN)",
            "emoji_id": ICONS.get("app_happ"),
            "fallback_emoji": "🌐",
            "icon_key": "app_happ",
        }
        if vpn_is_happ
        else {
            "id": "incy_vpn",
            "label": "INCY (VPN)",
            "emoji_id": ICONS.get("app_incy"),
            "fallback_emoji": "🌐",
            "icon_key": "app_incy",
        }
    )
    return [
        {
            "id": "telegram",
            "label": "Telegram",
            "emoji_id": ICONS.get("app_telegram") or ICONS.get("telegram"),
            "fallback_emoji": "💬",
            "icon_key": "telegram",
        },
        {
            "id": "yandex_music",
            "label": "Yandex Music",
            "emoji_id": ICONS.get("app_yandex_music") or ICONS.get("music"),
            "fallback_emoji": "🎵",
            "icon_key": "music",
        },
        vpn_item,
        {
            "id": "chrome",
            "label": "Google Chrome",
            "emoji_id": ICONS.get("app_chrome") or ICONS.get("chrome"),
            "fallback_emoji": "🌐",
            "icon_key": "chrome",
        },
        {
            "id": "antigravity",
            "label": "Antigravity",
            "emoji_id": ICONS.get("app_antigravity") or ICONS.get("terminal"),
            "fallback_emoji": "🚀",
            "icon_key": "terminal",
        },
    ]


GAME_INFO = {
    "gta5": {
        "id": "game_gta5",
        "name": "GTA 5 Legacy",
        "scenario": "Игровой сетап: GTA 5 Legacy",
        "emoji_id": ICONS.get("game"),
        "fallback_emoji": "🚗",
    },
    "roblox": {
        "id": "game_roblox",
        "name": "Roblox",
        "scenario": "Игровой сетап: Roblox",
        "emoji_id": ICONS.get("game"),
        "fallback_emoji": "🟥",
    },
    "gtasa": {
        "id": "game_gtasa",
        "name": "GTA SA",
        "scenario": "Игровой сетап: GTA SA",
        "emoji_id": ICONS.get("game"),
        "fallback_emoji": "🚗",
    },
}


def get_game_steps(game_key="gta5", vpn="incy"):
    vpn_is_happ = str(vpn).lower() == "happ"
    vpn_item = (
        {
            "id": "happ_vpn",
            "label": "Happ (VPN)",
            "emoji_id": ICONS.get("app_happ"),
            "fallback_emoji": "🌐",
            "icon_key": "app_happ",
        }
        if vpn_is_happ
        else {
            "id": "incy_vpn",
            "label": "INCY (VPN)",
            "emoji_id": ICONS.get("app_incy"),
            "fallback_emoji": "🌐",
            "icon_key": "app_incy",
        }
    )
    g_info = GAME_INFO.get(game_key, GAME_INFO["gta5"])
    return [
        vpn_item,
        {
            "id": "antigravity",
            "label": "Antigravity",
            "emoji_id": ICONS.get("app_antigravity") or ICONS.get("terminal"),
            "fallback_emoji": "🚀",
            "icon_key": "terminal",
        },
        {
            "id": g_info["id"],
            "label": g_info["name"],
            "emoji_id": g_info["emoji_id"],
            "fallback_emoji": g_info["fallback_emoji"],
            "icon_key": "game",
        },
    ]


def format_workspace_step_line(step, status_str):
    app_emoji_id = step.get("emoji_id")
    fallback = step.get("fallback_emoji", "📱")
    if app_emoji_id:
        app_icon_tag = f'<tg-emoji emoji-id="{app_emoji_id}">{fallback}</tg-emoji> '
    else:
        app_icon_tag = f"{fallback} "

    clock_id = ICONS.get("clock")
    check_id = ICONS.get("check")
    cross_id = ICONS.get("cross")
    refresh_id = ICONS.get("refresh")

    raw_status = str(status_str or "⏳ В очереди")
    if raw_status in ("⏳ В очереди", "queued", "None"):
        status_display = f'<tg-emoji emoji-id="{clock_id}">⏳</tg-emoji> В очереди' if clock_id else "⏳ В очереди"
    elif raw_status in ("🔄 Запуск...", "starting", "running"):
        status_display = f'<tg-emoji emoji-id="{refresh_id}">🔄</tg-emoji> Запуск...' if refresh_id else "🔄 Запуск..."
    elif raw_status in ("✅", "✅ Запущен", "verified", "already_running_verified"):
        status_display = f'<tg-emoji emoji-id="{check_id}">✅</tg-emoji>' if check_id else "✅"
    elif "❌" in raw_status or "Ошибка" in raw_status or raw_status == "failed":
        status_display = f'<tg-emoji emoji-id="{cross_id}">❌</tg-emoji> {raw_status.replace("❌", "").strip()}' if cross_id else raw_status
    else:
        status_display = raw_status

    return f"{app_icon_tag}{step['label']} — {status_display}"


WORKSPACE_STEPS = get_workspace_steps("happ")

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
    "10": ("255,150,125", "Идеальный белый"),
    "11": ("255,135,100", "Естественный дневной"),
    "12": ("255,120,75", "Тёплый ламповый"),
    "eye": ("255,150,125", "Для глаз (Идеал)"),
    "photo": ("228,245,255", "Для фото и видео"),
    "глаз": ("255,150,125", "Для глаз (Идеал)"),
    "фото": ("228,245,255", "Для фото и видео"),
    "камера": ("228,245,255", "Для фото и видео"),
}


class PcControlBotApp:
    @property
    def allowed_user_id(self):
        return getattr(self, "_allowed_user_id", None) or ALLOWED_USER_ID

    @allowed_user_id.setter
    def allowed_user_id(self, val):
        self._allowed_user_id = val

    def __init__(self):
        if not TELEGRAM_TOKEN or not ALLOWED_USER_ID:
            LOG.error("TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID must be set.")
            sys.exit(1)

        self.ui = TelegramBotApi(TELEGRAM_TOKEN)
        self.polling_session = requests.Session()
        polling_adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=5, max_retries=3)
        self.polling_session.mount("https://", polling_adapter)
        self.polling_session.mount("http://", polling_adapter)
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
        self.vpn_ready_event = self.happ_ready_event
        self.chosen_vpn = "incy"
        self.current_workspace_steps = WORKSPACE_STEPS
        self.workspace_executed_event = threading.Event()
        self.minimize_ack_event = threading.Event()
        self.workspace_execution_result = None
        self.minimize_ack_result = None
        self.pending_screenshots = {}
        self.pending_telemetry_cmd_ids = {}
        self.pending_antigravity_cmd_ids = {}
        self.latest_antigravity_data = None
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
        self.active_stealth_card = None

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
        self.power_flow.on_game_start_cb = self.start_async_game_setup

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
                    self.physical_btn.esp_locked = bool(data.get("esp_locked", False))
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
                temp = STATE_PATH.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
                phys_btn = getattr(self, "physical_btn", None)
                data = {
                    "telegram_offset": getattr(self, "offset", 0),
                    "physical_password": getattr(phys_btn, "saved_password", None),
                    "physical_unlock_pattern": getattr(phys_btn, "saved_unlock_pattern", "CC"),
                    "last_physical_morse_success_at": getattr(phys_btn, "last_physical_morse_success_at", 0.0),
                    "physical_morse_unlock_eligible": getattr(phys_btn, "physical_morse_unlock_eligible", False),
                    "esp_locked": getattr(phys_btn, "esp_locked", False),
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
        stealth_active = bool(self.monitoring.latest_telemetry.get("stealth_mode")) if self.monitoring.latest_telemetry else False
        return self.ui.power_menu_markup(
            pc_state=pc_state,
            session=self.mqtt.system_session,
            system_status=self.mqtt.system_status,
            stealth_active=stealth_active,
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

        steps = getattr(self, "current_workspace_steps", WORKSPACE_STEPS)
        lines = [
            format_workspace_step_line(step, self.app_statuses.get(step['id'], '⏳ В очереди'))
            for step in steps
        ]
        card_title = getattr(self, "workspace_card_title", "<b>🚀 Запуск рабочего сетапа...</b>")
        text = f"{card_title}\n\n" + "\n".join(lines)
        cancel_btn = {"text": "Отменить запуск", "style": "danger", "callback_data": "flow_workspace_cancel"}
        if ICONS.get("cross"):
            cancel_btn["icon_custom_emoji_id"] = ICONS.get("cross")
        markup = {"inline_keyboard": [[cancel_btn]]}
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

        elif event_type == "graceful_shutdown":
            LOG.info("Graceful shutdown event received from Windows: %s", data)
            self._clear_pending_shutdown()
            if hasattr(self.power_flow, "reset_flow"):
                self.power_flow.reset_flow()
            if hasattr(self.mqtt, "publish_esp_action"):
                if self.mqtt.publish_esp_action("LED_OFF"):
                    self.last_auto_led_state = "off"
                    LOG.info("Auto-LED: Graceful shutdown -> LED_OFF sent to cooler")

        elif event_type == "physical_input":
            self.physical_btn.handle_event(data, ALLOWED_USER_ID)

        elif event_type == "telemetry":
            prev_stealth = bool(self.monitoring.latest_telemetry.get("stealth_mode")) if self.monitoring.latest_telemetry else False
            new_stealth = bool(data.get("stealth_mode")) if isinstance(data, dict) else False
            alerts = self.monitoring.process_telemetry(data)
            for alert_text in alerts:
                self.ui.send_message(self.allowed_user_id, alert_text)
            if prev_stealth and not new_stealth and getattr(self, "active_stealth_card", None):
                self.on_stealth_exit({"reason": "telemetry_diff"})
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
            getattr(self, "pending_telemetry_cmd_ids", {}).pop(cmd_id, None)
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
                        elif "incy" in an:
                            step_id = "incy_vpn"
                        elif "antigravity" in an:
                            step_id = "antigravity"
                        elif "gta5" in an or "gta 5" in an:
                            step_id = "game_gta5"
                        elif "roblox" in an:
                            step_id = "game_roblox"
                        elif "gtasa" in an or "gta sa" in an or "san andreas" in an:
                            step_id = "game_gtasa"

                    if step_id:
                        if state_val in {"verified", "already_running_verified", "ready", "vpn_connected"}:
                            self.app_statuses[step_id] = "✅"
                            if step_id in {"happ_vpn", "incy_vpn"}:
                                vpn_ev = getattr(self, "vpn_ready_event", None)
                                if vpn_ev:
                                    vpn_ev.set()
                                happ_ev = getattr(self, "happ_ready_event", None)
                                if happ_ev:
                                    happ_ev.set()
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

            pending_ag = getattr(self, "pending_antigravity_cmd_ids", {})
            if cmd_id in pending_ag:
                target = pending_ag.pop(cmd_id)
                req_chat_id, req_msg_id, action_kind = target

                if action_kind == "list":
                    if result == "executed" and isinstance(ack_data, dict):
                        self.latest_antigravity_data = ack_data
                        self.ui.send_message(
                            req_chat_id,
                            self.ui.antigravity_card_text(ack_data),
                            reply_markup=self.ui.antigravity_card_markup(ack_data),
                            message_id=req_msg_id,
                        )
                    else:
                        err = (ack_data or {}).get("error", "Не удалось загрузить аккаунты")
                        self.ui.send_message(req_chat_id, f"❌ Ошибка получения профилей Antigravity: {err}")

                elif action_kind == "switch":
                    if result == "executed" and isinstance(ack_data, dict):
                        switched_email = ack_data.get("email", "—")
                        restarted = ack_data.get("restarted_ide", False)
                        msg = f"✅ Профиль Antigravity переключен на <b>{switched_email}</b>!"
                        if restarted:
                            msg += "\n🖥️ <i>IDE Antigravity успешно перезапущена с новым токеном.</i>"
                        self.ui.send_message(req_chat_id, msg)
                        try:
                            from vds_bot.routers.antigravity_router import open_antigravity_menu
                            open_antigravity_menu(self, req_chat_id, message_id=req_msg_id)
                        except Exception:
                            pass
                    else:
                        err = (ack_data or {}).get("error", "Сбой при переключении")
                        self.ui.send_message(req_chat_id, f"❌ Ошибка смены профиля Antigravity: {err}")

                elif action_kind == "restart":
                    if result == "executed":
                        self.ui.send_message(req_chat_id, "🔄 <b>Antigravity IDE перезапущена</b>")
                    else:
                        self.ui.send_message(req_chat_id, "❌ Не удалось перезапустить IDE.")

                elif action_kind == "export":
                    if result == "executed" and isinstance(ack_data, dict):
                        cnt = ack_data.get("accounts_count", 0)
                        self.ui.send_message(req_chat_id, f"📦 <b>Экспорт выполнен успешно:</b> {cnt} аккаунтов сохранены в портативный формат.")
                    else:
                        self.ui.send_message(req_chat_id, "❌ Ошибка экспорта профилей.")

        elif event_type == "media_status":
            self.latest_media_state = data
            self._handle_media_status_event(data)
            self._update_dashboard_on_media(data)

        elif event_type == "activity_response":
            self._handle_activity_response_event(data)

        elif event_type == "stealth_exit":
            self.on_stealth_exit(data)

    def on_stealth_exit(self, data=None):
        """
        Handles local stealth mode exit event (triggered via hotkey Win+W+1 or watchdog).
        Updates telemetry cache, updates the stale card's button from 'Выйти' to 'Включить',
        and sends a push notification to Telegram.
        """
        LOG.info("Handling stealth_exit event: %s", data)
        if self.monitoring.latest_telemetry:
            self.monitoring.latest_telemetry["stealth_mode"] = False

        card = getattr(self, "active_stealth_card", None)
        self.active_stealth_card = None

        if card and isinstance(card, (tuple, list)) and len(card) == 2:
            chat_id, msg_id = card
            # Edit the stale message so the callback button changes from "Выйти" to "Включить"
            try:
                self.ui.send_message(
                    chat_id,
                    "<b>🔓 Режим скрытности отключен локально</b>\n\n"
                    "• Дисплеи разбужены\n"
                    "• Поглощение ввода снято\n"
                    "• Звук восстановлен\n\n"
                    "<i>Локальный выход: <code>Win+W+1</code></i>",
                    reply_markup=self.ui.stealth_menu_markup(active=False),
                    message_id=msg_id,
                )
            except Exception as err:
                LOG.warning("Failed editing active stealth card %s: %s", msg_id, err)

            # Send a push notification so Telegram rings/vibrates
            try:
                self.ui.send_message(
                    self.allowed_user_id,
                    "🔓 <b>Режим скрытности отключен локально</b> (хоткей <code>Win+W+1</code>)",
                )
            except Exception as err:
                LOG.warning("Failed sending stealth exit alert: %s", err)
        else:
            # If no previous card was tracked, send a new notification with the 'Включить' button
            try:
                self.ui.send_message(
                    self.allowed_user_id,
                    "🔓 <b>Режим скрытности отключен локально</b> (хоткей <code>Win+W+1</code>)\n\n"
                    "• Дисплеи разбужены\n"
                    "• Ввод мыши и клавиатуры восстановлен\n"
                    "• Звук восстановлен",
                    reply_markup=self.ui.stealth_menu_markup(active=False),
                )
            except Exception as err:
                LOG.warning("Failed sending stealth exit notification: %s", err)

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

    def _update_dashboard_on_media(self, data):
        menu_msg_id = getattr(self, "active_main_menu_message_id", None)
        if not menu_msg_id or getattr(self, "current_menu_view", "main") != "main":
            return
        try:
            text = self.monitoring.dashboard_text(data)
            markup = self._main_menu_markup()
            self.ui.send_message(
                ALLOWED_USER_ID,
                text,
                reply_markup=markup,
                message_id=menu_msg_id,
            )
        except Exception:
            LOG.exception("Failed to update dashboard on media event")

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

        # Modular Router Delegation
        if route_power_callback(self, data, chat_id, message_id, callback_id):
            return
        if route_workspace_callback(self, data, chat_id, message_id, callback_id):
            return
        if route_player_callback(self, data, chat_id, message_id, callback_id, business_connection_id=business_connection_id):
            return
        if route_console_callback(self, data, chat_id, message_id, callback_id):
            return
        if route_monitoring_callback(self, data, chat_id, message_id, callback_id):
            return
        if route_physical_callback(self, data, chat_id, message_id, callback_id):
            return
        if route_antigravity_callback(self, data, chat_id, message_id, callback_id):
            return

        self.ui.answer_callback(callback_id)

    def start_async_workspace_setup(self, chat_id, card_message_id=None, workspace_id=None, vpn=None):
        if not hasattr(self, "_workspace_start_lock"):
            self._workspace_start_lock = threading.Lock()
        with self._workspace_start_lock:
            if getattr(self, "_workspace_running", False):
                LOG.warning("Workspace setup runner is already active, ignoring duplicate start request")
                return
            self._workspace_running = True

        selected_vpn = vpn or getattr(self, "chosen_vpn", None) or "incy"

        def _runner():
            try:
                import inspect
                sig = inspect.signature(self.run_workspace_setup_steps)
                if "vpn" in sig.parameters or any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                    self.run_workspace_setup_steps(chat_id, card_message_id, workspace_id, vpn=selected_vpn)
                else:
                    self.run_workspace_setup_steps(chat_id, card_message_id, workspace_id)
            finally:
                self._workspace_running = False

        threading.Thread(
            target=_runner,
            name="workspace-runner-thread",
            daemon=True,
        ).start()

    def run_workspace_setup_steps(self, chat_id, card_message_id=None, workspace_id=None, vpn=None):
        """
        Asynchronous workspace setup steps driven entirely by real progress ACKs from agent.py:
        - Issue launch_app command with workspace_id and selected vpn.
        - Checklist card updates dynamically as progress ACKs arrive with step_id.
        - Strict VPN check: verifies VPN ready ACK. Halts & renders error if VPN fails/times out.
        - Strict full execution ACK check: verifies launch_app executed ACK.
        - Strict verification check: every step must be verified (no queued/unverified checkmarks).
        - Strict minimize_all ACK check: verifies minimize_all executed ACK before rendering final success!
        """
        active_vpn = vpn or getattr(self, "chosen_vpn", None)
        if not active_vpn:
            if hasattr(self, "happ_ready_event") and not hasattr(self, "vpn_ready_event"):
                active_vpn = "happ"
            else:
                active_vpn = "incy"
        self.chosen_vpn = active_vpn
        steps = get_workspace_steps(active_vpn)
        self.current_workspace_steps = steps
        self.app_statuses = {step["id"]: "⏳ В очереди" for step in steps}
        self.workspace_cancel_event.clear()

        vpn_ev = getattr(self, "vpn_ready_event", None)
        if vpn_ev:
            vpn_ev.clear()
        happ_ev = getattr(self, "happ_ready_event", None)
        if happ_ev:
            happ_ev.clear()

        self.workspace_executed_event.clear()
        self.minimize_ack_event.clear()
        self.workspace_execution_result = None
        self.minimize_ack_result = None

        def render_checklist():
            lines = [
                format_workspace_step_line(step, self.app_statuses.get(step['id'], '⏳ В очереди'))
                for step in self.current_workspace_steps
            ]
            return "<b>Запуск рабочего сетапа...</b>\n\n" + "\n".join(lines)

        cancel_btn = {"text": "Отменить запуск", "style": "danger", "callback_data": "flow_workspace_cancel"}
        if ICONS.get("cross"):
            cancel_btn["icon_custom_emoji_id"] = ICONS.get("cross")
        markup = {"inline_keyboard": [[cancel_btn]]}

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
            error_text = render_checklist() + "\n\n<b>Desktop-агент не в сети.</b>\nWindows загружается или desktop-агент ещё не подключился к брокеру."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить сетап", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": "workspace_start_direct"}],
                    [{"text": "Отмена", "style": "danger", "icon_custom_emoji_id": ICONS.get("cross"), "callback_data": "menu"}],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # Issue launch_app command to agent.py
        ws_id = workspace_id or f"desk-ws-{int(time.time())}"
        cmd_id = self.mqtt.publish_desktop_action(
            "launch_app",
            {"app": "Рабочий сетап", "workspace_id": ws_id, "vpn": active_vpn},
            timeout=120,
        )
        self.active_workspace_cmd_id = cmd_id

        # 1. Wait for Happ / INCY VPN readiness (up to 55 seconds)
        vpn_event = getattr(self, "vpn_ready_event", None) or getattr(self, "happ_ready_event", None)
        vpn_ready = self._wait_event_or_cancel(vpn_event, 55.0) if vpn_event else True

        if self.workspace_cancel_event.is_set():
            LOG.info("Runner thread detected cancellation during VPN wait. Halting.")
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.power_flow.reset_flow()
            return

        vpn_step_id = "incy_vpn" if active_vpn == "incy" else "happ_vpn"
        vpn_name = "INCY" if active_vpn == "incy" else "Happ"

        if not vpn_ready and (vpn_event and not vpn_event.is_set()):
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.app_statuses[vpn_step_id] = "❌ Ошибка VPN"
            error_text = render_checklist() + f"\n\n<b>Не удалось подтвердить подключение {vpn_name} (VPN).</b>\nЗапуск зависимых приложений отменён в целях безопасности."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить сетап", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": "workspace_start_direct"}],
                    [
                        {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                        {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                    ],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 2. Wait for full workspace scenario execution ACK from agent.py (up to 65 seconds)
        ws_executed = self._wait_event_or_cancel(self.workspace_executed_event, 65.0)

        if self.workspace_cancel_event.is_set():
            LOG.info("Runner thread detected cancellation during workspace execution wait. Halting.")
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.power_flow.reset_flow()
            return

        if not ws_executed or self.workspace_execution_result == "failed":
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            error_text = render_checklist() + "\n\n<b>Ошибка выполнения рабочего сетапа.</b>\nDesktop-агент сообщил о сбое или запуск превысил таймаут."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить сетап", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": "workspace_start_direct"}],
                    [
                        {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                        {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                    ],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 3. Strict verification check: every step must be strictly verified
        unverified = [s["label"] for s in self.current_workspace_steps if self.app_statuses.get(s["id"]) != "✅"]
        if unverified:
            notice_text = render_checklist() + f"\n\n<b>Рабочий сетап завершён с неполным подтверждением.</b>\nНе подтверждены: {', '.join(unverified)}"
            notice_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить сетап", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": "workspace_start_direct"}],
                    [
                        {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                        {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                    ],
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
                notice_text = render_checklist() + "\n\n<b>Рабочий сетап запущен, но не удалось свернуть окна.</b>"
                notice_markup = {
                    "inline_keyboard": [
                        [{"text": "Свернуть окна", "style": "primary", "icon_custom_emoji_id": ICONS.get("screen"), "callback_data": "minimize_windows"}],
                        [
                            {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                            {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                        ],
                    ]
                }
                self.ui.send_message(chat_id, notice_text, reply_markup=notice_markup, message_id=msg_id)
                self.power_flow.reset_flow()
                return

        # 5. Final Success (Only when minimize_all ACK executed!)
        final_text = render_checklist() + "\n\n<b>✅ Рабочий сетап успешно запущен!</b>\nВсе приложения открыты, VPN активен, окна свернуты."
        final_markup = {
            "inline_keyboard": [
                [
                    {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                    {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                ],
            ]
        }
        self.ui.send_message(chat_id, final_text, reply_markup=final_markup, message_id=msg_id)
        self.power_flow.reset_flow()

    def start_async_game_setup(self, chat_id, card_message_id=None, game="gta5", vpn=None, workspace_id=None):
        if not hasattr(self, "_workspace_start_lock"):
            self._workspace_start_lock = threading.Lock()
        with self._workspace_start_lock:
            if getattr(self, "_workspace_running", False):
                LOG.warning("Setup runner is already active, ignoring duplicate start request")
                return
            self._workspace_running = True

        selected_vpn = vpn or getattr(self, "chosen_vpn", None) or "incy"

        def _runner():
            try:
                self.run_game_setup_steps(chat_id, card_message_id, game=game, vpn=selected_vpn, workspace_id=workspace_id)
            finally:
                self._workspace_running = False

        threading.Thread(
            target=_runner,
            name="game-runner-thread",
            daemon=True,
        ).start()

    def run_game_setup_steps(self, chat_id, card_message_id=None, game="gta5", vpn=None, workspace_id=None):
        active_vpn = vpn or getattr(self, "chosen_vpn", None) or "incy"
        self.chosen_vpn = active_vpn
        g_info = GAME_INFO.get(game, GAME_INFO["gta5"])
        game_name = g_info["name"]
        scenario_name = g_info["scenario"]

        steps = get_game_steps(game, active_vpn)
        self.current_workspace_steps = steps
        self.workspace_card_title = f"<b>🎮 Запуск игрового сетапа ({game_name})...</b>"
        self.app_statuses = {step["id"]: "⏳ В очереди" for step in steps}
        self.workspace_cancel_event.clear()

        vpn_ev = getattr(self, "vpn_ready_event", None)
        if vpn_ev:
            vpn_ev.clear()
        happ_ev = getattr(self, "happ_ready_event", None)
        if happ_ev:
            happ_ev.clear()

        self.workspace_executed_event.clear()
        self.workspace_execution_result = None

        def render_checklist():
            lines = [
                format_workspace_step_line(step, self.app_statuses.get(step['id'], '⏳ В очереди'))
                for step in self.current_workspace_steps
            ]
            return f"<b>🎮 Запуск игрового сетапа ({game_name})...</b>\n\n" + "\n".join(lines)

        cancel_btn = {"text": "Отменить запуск", "style": "danger", "callback_data": "flow_workspace_cancel"}
        if ICONS.get("cross"):
            cancel_btn["icon_custom_emoji_id"] = ICONS.get("cross")
        markup = {"inline_keyboard": [[cancel_btn]]}

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
            error_text = render_checklist() + "\n\n<b>Desktop-агент не в сети.</b>\nWindows загружается или desktop-агент ещё не подключился к брокеру."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить запуск", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": f"direct_game_vpn:{game}:{active_vpn}"}],
                    [{"text": "Отмена", "style": "danger", "icon_custom_emoji_id": ICONS.get("cross"), "callback_data": "menu"}],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 1. Issue launch_app command to agent.py
        ws_id = workspace_id or f"game-ws-{int(time.time())}"
        cmd_id = self.mqtt.publish_desktop_action(
            "launch_app",
            {"app": scenario_name, "workspace_id": ws_id, "vpn": active_vpn},
            timeout=120,
        )
        self.active_workspace_cmd_id = cmd_id

        # 2. Wait for Happ / INCY VPN readiness
        vpn_event = getattr(self, "vpn_ready_event", None) or getattr(self, "happ_ready_event", None)
        vpn_ready = self._wait_event_or_cancel(vpn_event, 55.0) if vpn_event else True

        if self.workspace_cancel_event.is_set():
            LOG.info("Runner thread detected cancellation during VPN wait. Halting.")
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.power_flow.reset_flow()
            return

        vpn_step_id = "incy_vpn" if active_vpn == "incy" else "happ_vpn"
        vpn_name = "INCY" if active_vpn == "incy" else "Happ"

        if not vpn_ready and (vpn_event and not vpn_event.is_set()):
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.app_statuses[vpn_step_id] = "❌ Ошибка VPN"
            error_text = render_checklist() + f"\n\n<b>Не удалось подтвердить подключение {vpn_name} (VPN).</b>\nЗапуск игры отменён в целях безопасности."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить запуск", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": f"direct_game_vpn:{game}:{active_vpn}"}],
                    [
                        {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                        {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                    ],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 3. Wait for full execution ACK from agent.py (up to 65 seconds)
        ws_executed = self._wait_event_or_cancel(self.workspace_executed_event, 65.0)

        if self.workspace_cancel_event.is_set():
            LOG.info("Runner thread detected cancellation during game execution wait. Halting.")
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.power_flow.reset_flow()
            return

        if not ws_executed or self.workspace_execution_result == "failed":
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            error_text = render_checklist() + f"\n\n<b>Ошибка запуска {game_name}.</b>\nDesktop-агент сообщил о сбое или запуск превысил таймаут."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить запуск", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": f"direct_game_vpn:{game}:{active_vpn}"}],
                    [
                        {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                        {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                    ],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 4. Strict verification check: every step must be strictly verified
        unverified = [s["label"] for s in self.current_workspace_steps if self.app_statuses.get(s["id"]) != "✅"]
        if unverified:
            notice_text = render_checklist() + f"\n\n<b>Игровой сетап завершён с неполным подтверждением.</b>\nНе подтверждены: {', '.join(unverified)}"
            notice_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить запуск", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": f"direct_game_vpn:{game}:{active_vpn}"}],
                    [
                        {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                        {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                    ],
                ]
            }
            self.ui.send_message(chat_id, notice_text, reply_markup=notice_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 5. Final Success!
        final_text = (
            render_checklist()
            + f"\n\n<b>✅ Игровой сетап успешно запущен!</b>\n"
            f"{vpn_name} VPN подключен, Antigravity активен, {game_name} запущена."
        )
        final_markup = {
            "inline_keyboard": [
                [
                    {"text": "⚡ Нажать ESC", "style": "primary", "icon_custom_emoji_id": ICONS.get("terminal"), "callback_data": "press_esc"},
                ],
                [
                    {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                    {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                ],
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
        route_message(self, message)

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
        if self.mqtt.system_status == "online":
            cmd_id = self.mqtt.publish_system_action("shutdown")
        elif getattr(self.mqtt, "desktop_status", None) == "online":
            cmd_id = self.mqtt.publish_desktop_action("shutdown")
        else:
            self.ui.send_message(
                chat_id,
                f"❌ Агенты Windows сейчас недоступны (system: {self.mqtt.system_status}, desktop: {getattr(self.mqtt, 'desktop_status', 'offline')}). Выключение не отправлено.",
                reply_markup=self._power_menu_markup(),
            )
            return False
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
        session = getattr(self.mqtt, "system_session", "")
        if session in ("locked", "login_screen"):
            return False

        desk_status = getattr(self.mqtt, "desktop_status", None)
        if session != "unlocked" and desk_status != "online":
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

        # Power flow tick (live-log countdown/spinner and runtime timeout watchdog)
        if hasattr(self, "power_flow"):
            try:
                if hasattr(self.power_flow, "tick"):
                    self.power_flow.tick(now)
                elif hasattr(self.power_flow, "check_timeout"):
                    self.power_flow.check_timeout(now)
            except Exception:
                LOG.exception("power_flow.tick failed in scheduler_tick")

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
                emerg_btn = {"text": "⚡ Аварийное выключение (ESP)", "style": "danger", "callback_data": "emergency_power_off"}
                if ICONS.get("power"):
                    emerg_btn["icon_custom_emoji_id"] = ICONS.get("power")
                timeout_markup = {
                    "inline_keyboard": [
                        [emerg_btn],
                        self.ui.back_button("power_menu"),
                    ]
                }
                if self.shutdown_acknowledged:
                    self.ui.send_message(
                        ALLOWED_USER_ID,
                        "<b>⚠️ Связь потеряна, состояние ПК неизвестно.</b>\n\nКоманда выключения была принята Windows, но подтверждение завершения работы не получено за 2 минуты.",
                        reply_markup=timeout_markup,
                    )
                else:
                    self.ui.send_message(
                        ALLOWED_USER_ID,
                        "<b>❌ Выключение не подтверждено.</b>\n\nWindows не ответила на команду выключения за 2 минуты.",
                        reply_markup=timeout_markup,
                    )
                self._clear_pending_shutdown()

        if not self.scheduled_shutdown_ts or now < self.scheduled_shutdown_ts:
            return
        if now - self.scheduled_shutdown_ts > 600.0:
            LOG.info("Discarding stale scheduled shutdown (scheduled_at=%s, now=%s)", self.scheduled_shutdown_ts, now)
            self.scheduled_shutdown_ts = None
            self.save_state()
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
            res = self.polling_session.post(url, json=payload, timeout=25)
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
                    try:
                        if "message" in update:
                            self.handle_message(update["message"])
                        elif "business_message" in update:
                            self.handle_business_message(update["business_message"])
                        elif "business_connection" in update:
                            self.handle_business_connection(update["business_connection"])
                        elif "callback_query" in update:
                            self.handle_callback(update["callback_query"])
                    except Exception:
                        LOG.exception("Error processing Telegram update %s", update.get("update_id"))
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
                        {"command": "stealth", "description": "🕶️ Режим скрытности (Blackout)"},
                        {"command": "activity", "description": "📊 Активность за день"},
                        {"command": "antigravity", "description": "🔮 Профили Antigravity"},
                        {"command": "esc", "description": "⚡ Нажатие Escape (1..20x)"},
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
