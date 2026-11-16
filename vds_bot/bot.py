#!/usr/bin/env python3
"""
Main Application Controller for PC Control Bot (VDS).
Integrates modular services: UI Templates, Workspace Runner, Player Manager, Console Service,
Power Scheduler, MQTT Dispatcher, and Telegram routers.
"""

import concurrent.futures
import html
import json
import logging
import os
import requests
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil

try:
    from zoneinfo import ZoneInfo
    MOSCOW = ZoneInfo("Europe/Moscow")
except Exception:
    MOSCOW = timezone(timedelta(hours=3))

try:
    from .telegram_ui import TelegramBotApi, ICONS
    from .ui_templates import UiTemplatesMixin
    from .workspace_runner import (
        WorkspaceRunnerMixin,
        get_workspace_steps,
        get_game_steps,
        format_workspace_step_line,
        GAME_INFO,
        WORKSPACE_STEPS,
    )
    from .player_manager import PlayerManagerMixin
    from .console_service import ConsoleServiceMixin
    from .power_scheduler import (
        PowerSchedulerMixin,
        SCREENSHOT_TIMEOUT_SECONDS,
        SHUTDOWN_CONFIRM_TIMEOUT_SECONDS,
    )
    from .mqtt_dispatcher import (
        MqttDispatcherMixin,
        unpack_screenshot_payload,
    )
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
    from telegram_ui import TelegramBotApi, ICONS
    from ui_templates import UiTemplatesMixin
    from workspace_runner import (
        WorkspaceRunnerMixin,
        get_workspace_steps,
        get_game_steps,
        format_workspace_step_line,
        GAME_INFO,
        WORKSPACE_STEPS,
    )
    from player_manager import PlayerManagerMixin
    from console_service import ConsoleServiceMixin
    from power_scheduler import (
        PowerSchedulerMixin,
        SCREENSHOT_TIMEOUT_SECONDS,
        SHUTDOWN_CONFIRM_TIMEOUT_SECONDS,
    )
    from mqtt_dispatcher import (
        MqttDispatcherMixin,
        unpack_screenshot_payload,
    )
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

try:
    from .mqtt_gateway import MqttGateway
    from .power_flow import PowerFlowManager, PowerFlowStage
    from .physical_button import PhysicalButtonManager
    from .monitoring import MonitoringManager
    from .health_engine import PCState
except ImportError:
    from mqtt_gateway import MqttGateway
    from power_flow import PowerFlowManager, PowerFlowStage
    from physical_button import PhysicalButtonManager
    from monitoring import MonitoringManager
    from health_engine import PCState

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
LOG = logging.getLogger("pc-control-bot")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID") or 0)
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "telegram_bot")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT") or 1883)

from typing import Optional

STATE_PATH = Path(os.environ.get("STATE_PATH", "/var/lib/pc-control-bot/state.json"))
LOCK_PATH = Path(os.environ.get("BOT_LOCK_PATH", str(STATE_PATH.parent / "bot.lock")))


def resolve_menu_view(data: str) -> Optional[str]:
    """Resolves whether a callback query data corresponds to navigating to a specific menu view.
    Returns the view name if it is a menu navigation, or None if it is an action callback.
    """
    if not isinstance(data, str):
        return None
    if data in ("menu", "main", "flow_unlock_no"):
        return "main"
    if data == "apps_menu":
        return "apps_menu"
    if data == "power_menu":
        return "power_menu"
    if data in ("shutdown_menu", "shutdown_pc") or data.startswith(("shutdown_after:", "shutdown_schedule")):
        return "shutdown_menu"
    if data == "reboot_menu":
        return "reboot_menu"
    if data == "stealth_menu":
        return "stealth_menu"
    if data == "sound_menu" or data.startswith("volume:"):
        return "sound_menu"
    if data == "settings_menu":
        return "settings_menu"
    if data == "monitoring_menu":
        return "monitoring_menu"
    if data == "history_menu" or data.startswith(("history_period:", "history:")):
        return "history_menu"
    if data in ("led_menu", "led_palette") or data.startswith("led_"):
        return "led_menu"
    if data == "console_close":
        return "main"
    if data in ("console_open", "console") or data.startswith("console_"):
        return "console"
    if data in ("antigravity_menu", "ag_refresh") or data.startswith("ag_"):
        return "antigravity_menu"
    if data in ("game_start_direct", "flow_unlock_and_game") or data.startswith(("flow_game", "direct_game")):
        return "game_menu"
    if data in ("workspace_start_direct", "flow_unlock_and_workspace") or data.startswith("workspace_vpn:"):
        return "workspace_menu"
    if data in ("physical_button", "physical_test", "physical_record_begin") or data.startswith("physical_"):
        return "physical_menu"
    if data in ("player", "player_refresh") or data.startswith("player_"):
        return "player_menu"
    return None


class PcControlBotApp(
    UiTemplatesMixin,
    WorkspaceRunnerMixin,
    PlayerManagerMixin,
    ConsoleServiceMixin,
    PowerSchedulerMixin,
    MqttDispatcherMixin,
):
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
        polling_adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=3)
        self.polling_session.mount("https://", polling_adapter)
        self.polling_session.mount("http://", polling_adapter)

        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tg-worker")
        self.async_dispatch = True

        self.mqtt = MqttGateway(
            username=MQTT_USERNAME,
            password=MQTT_PASSWORD,
            host=MQTT_HOST,
            port=MQTT_PORT,
            on_event_callback=self._on_mqtt_event,
        )
        self.power_flow = PowerFlowManager(self.ui, self.mqtt)
        self.power_flow.app = self
        self.physical_btn = PhysicalButtonManager(self.ui, self.mqtt)
        self.monitoring = MonitoringManager(self.ui, self.mqtt)
        try:
            from core.premium_checker import PremiumChecker
            self.premium_checker = PremiumChecker()
        except Exception:
            self.premium_checker = None

        def _is_shutting_down():
            if not getattr(self, "pending_shutdown_cmd_id", None):
                return False
            started_at = getattr(self, "pending_shutdown_started_at", None)
            if not started_at:
                return False
            try:
                return (time.time() - float(started_at) < 180)
            except (ValueError, TypeError):
                return False

        self.monitoring.is_shutting_down = _is_shutting_down

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
        self.workspace_execution_data = {}
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
        self.user_led_state = "on"
        self.user_led_color = "255,150,125"
        self.user_led_brightness = 160

        self.console_active_chat_id = None
        self.console_target = "desktop"
        self.console_shell = "powershell"
        self.pending_console_cmds = {}
        self.early_console_acks = {}

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
            res = self.ui.send_message(chat_id, "ℹ️ <b>Windows уже разблокирована.</b>")
            if res and isinstance(res, dict) and "message_id" in res:
                self.last_unlock_gesture_msg_id = res["message_id"]
                self.last_unlock_gesture_time = time.time()
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
            self.power_flow.prepare_unlock_waiting(chat_id, auto_launch_workspace=False)
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
            ws_id = self.power_flow.start_direct_setup(chat_id, prefix="ws")
            msg_text = (
                "🚀 <b>Жест 'Разблок + Сетап' принят!</b>\n"
                "Windows уже разблокирована — запускаю рабочий сетап..."
            )
            prev_msg_id = getattr(self, "last_unlock_gesture_msg_id", None)
            can_edit = prev_msg_id and (time.time() - getattr(self, "last_unlock_gesture_time", 0.0) < 5.0)
            if can_edit:
                self.ui.send_message(chat_id, msg_text, message_id=prev_msg_id)
            else:
                self.ui.send_message(chat_id, msg_text)
            self.start_async_workspace_setup(chat_id, workspace_id=ws_id)
            return

        prev_msg_id = getattr(self, "last_unlock_gesture_msg_id", None)
        can_edit = prev_msg_id and (time.time() - getattr(self, "last_unlock_gesture_time", 0.0) < 5.0)

        if system_online and session in {"login_screen", "locked"}:
            msg_text = "🚀 <b>Жест 'Разблок + Сетап' принят!</b>\nРазблокирую Windows и сразу запускаю рабочий сетап..."
            if can_edit:
                res = self.ui.send_message(chat_id, msg_text, message_id=prev_msg_id)
            else:
                res = self.ui.send_message(chat_id, msg_text)
            if res and isinstance(res, dict) and "message_id" in res:
                self.last_unlock_gesture_msg_id = res["message_id"]
                self.last_unlock_gesture_time = time.time()
            if self.power_flow.stage != PowerFlowStage.UNLOCKING:
                self.power_flow.start_manual_unlock(chat_id, auto_launch_workspace=True)
            else:
                self.power_flow.set_auto_launch_workspace(True, chat_id=chat_id)
        else:
            self.power_flow.prepare_unlock_waiting(chat_id, auto_launch_workspace=True)
            msg_text = "🚀 <b>Жест 'Разблок + Сетап' принят!</b>\n⏳ ПК загружается — после разблокировки автоматически запустится рабочий сетап."
            if can_edit:
                res = self.ui.send_message(chat_id, msg_text, message_id=prev_msg_id)
            else:
                res = self.ui.send_message(chat_id, msg_text)
            if res and isinstance(res, dict) and "message_id" in res:
                self.last_unlock_gesture_msg_id = res["message_id"]
                self.last_unlock_gesture_time = time.time()

    def load_state(self):
        with getattr(self, "state_lock", threading.RLock()):
            try:
                if not STATE_PATH.exists():
                    return
                data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return
            except Exception:
                LOG.exception("Could not load state.json")
                return

            def _restore(field, fn):
                try:
                    fn()
                except Exception as exc:
                    LOG.warning("Ignoring corrupt state.json field %r: %s", field, exc)

            _restore("telegram_offset", lambda: setattr(self, "offset", int(data.get("telegram_offset", 0) or 0)))

            def _restore_physical():
                pwd = data.get("physical_password")
                if pwd:
                    self.physical_btn.set_saved_password(pwd)
                unlock_pat = data.get("physical_unlock_pattern")
                if unlock_pat:
                    self.physical_btn.saved_unlock_pattern = unlock_pat
                self.physical_btn.last_physical_morse_success_at = data.get("last_physical_morse_success_at")
                self.physical_btn.physical_morse_unlock_eligible = bool(data.get("physical_morse_unlock_eligible", False))
                self.physical_btn.esp_locked = bool(data.get("esp_locked", False))

            _restore("physical_btn", _restore_physical)

            def _restore_shutdown():
                scheduled = data.get("scheduled_shutdown_ts")
                if scheduled:
                    self.scheduled_shutdown_ts = float(scheduled)
                pending_shutdown = data.get("pending_shutdown")
                if isinstance(pending_shutdown, dict):
                    self.pending_shutdown_cmd_id = pending_shutdown.get("id")
                    self.pending_shutdown_started_at = pending_shutdown.get("started_at")
                    self.shutdown_acknowledged = bool(pending_shutdown.get("acknowledged"))

            _restore("shutdown", _restore_shutdown)

            def _restore_player():
                active_p = data.get("active_player")
                if isinstance(active_p, dict) and active_p.get("message_id"):
                    self.active_player_message_id = active_p.get("message_id")
                    self.active_player_chat_id = active_p.get("chat_id")
                    self.active_player_business_connection_id = active_p.get("business_connection_id")
                    self.active_player_is_photo = bool(active_p.get("is_photo", False))
                    self.active_player_cover_url = active_p.get("cover_url")

            _restore("active_player", _restore_player)

            _restore("active_main_menu_message_id", lambda: setattr(self, "active_main_menu_message_id", data.get("active_main_menu_message_id")))
            _restore("current_menu_view", lambda: setattr(self, "current_menu_view", str(data.get("current_menu_view") or "main")))
            _restore("chosen_vpn", lambda: setattr(self, "chosen_vpn", data.get("chosen_vpn") if data.get("chosen_vpn") in ("incy", "happ") else getattr(self, "chosen_vpn", "incy")))
            _restore("console_target", lambda: setattr(self, "console_target", str(data.get("console_target") or "desktop")))
            _restore("console_shell", lambda: setattr(self, "console_shell", str(data.get("console_shell") or "powershell")))
            _restore("user_led_state", lambda: setattr(self, "user_led_state", str(data.get("user_led_state") or "on")))
            _restore("user_led_color", lambda: setattr(self, "user_led_color", str(data.get("user_led_color") or "255,150,125")))
            _restore("user_led_brightness", lambda: setattr(self, "user_led_brightness", int(data.get("user_led_brightness", 160))))

    def save_state(self):
        with getattr(self, "state_lock", threading.RLock()):
            temp = STATE_PATH.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
            try:
                STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
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
                    "chosen_vpn": getattr(self, "chosen_vpn", "incy"),
                    "console_target": getattr(self, "console_target", "desktop"),
                    "console_shell": getattr(self, "console_shell", "powershell"),
                    "user_led_state": getattr(self, "user_led_state", "on"),
                    "user_led_color": getattr(self, "user_led_color", "255,150,125"),
                    "user_led_brightness": getattr(self, "user_led_brightness", 160),
                }
                temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                try:
                    os.chmod(temp, 0o600)
                except Exception:
                    pass
                os.replace(temp, STATE_PATH)
                return True
            except Exception:
                LOG.exception("Could not save state.json")
                try:
                    temp.unlink(missing_ok=True)
                except Exception:
                    pass
                return False

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

    def handle_callback(self, callback):
        if getattr(self, "premium_checker", None) and callback.get("from"):
            try:
                self.premium_checker.record_user(callback["from"])
            except Exception:
                pass
        callback_id = callback["id"]
        msg = callback.get("message") or {}
        chat_id = msg.get("chat", {}).get("id") if isinstance(msg, dict) else None
        from_user_id = (callback.get("from") or {}).get("id") or chat_id
        if chat_id is None:
            chat_id = from_user_id
        message_id = msg.get("message_id") if isinstance(msg, dict) else None
        business_connection_id = callback.get("business_connection_id") or (msg.get("business_connection_id") if isinstance(msg, dict) else None)
        data = callback.get("data", "")

        target_user = self.allowed_user_id

        # Strict security: Only the owner (ALLOWED_USER_ID) can interact with any callbacks!
        if from_user_id != target_user:
            self.ui.answer_callback(callback_id, "Отказано", alert=True)
            return

        if chat_id != target_user and not data.startswith("player_") and not business_connection_id:
            self.ui.answer_callback(callback_id, "Доступ запрещён", alert=True)
            return

        LOG.info("Callback received: %s", data)

        new_view = resolve_menu_view(data)
        if new_view is not None:
            self.current_menu_view = new_view

        phys = getattr(self, "physical_btn", None)
        if phys is not None:
            phys_lock = getattr(phys, "_lock", None)
            if phys_lock is not None:
                with phys_lock:
                    was_test = getattr(phys, "mode", None) == "test" and data != "physical_test"
                    if was_test:
                        phys.mode = None
                        phys.input_message_id = None
            else:
                was_test = getattr(phys, "mode", None) == "test" and data != "physical_test"
                if was_test:
                    phys.mode = None
                    phys.input_message_id = None
            if was_test and hasattr(self.mqtt, "publish_esp_action"):
                self.mqtt.publish_esp_action("STOP_PHYSICAL_TEST")

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

    def handle_message(self, message):
        if getattr(self, "premium_checker", None) and isinstance(message, dict) and message.get("from"):
            try:
                self.premium_checker.record_user(message["from"])
            except Exception:
                pass
        route_message(self, message)

    def handle_business_message(self, message):
        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id") if isinstance(message, dict) else None
        conn_id = message.get("business_connection_id")
        from_id = (message.get("from") or {}).get("id") if isinstance(message, dict) else None

        LOG.info("Business message received: chat_id=%s conn_id=%s from_id=%s text=%r", chat_id, conn_id, from_id, text)

        if not text or not chat_id or not conn_id:
            return

        cmd = text.lower().split()[0]
        if cmd in {"/player", "!player", "плеер", "музыка"}:
            if from_id != self.allowed_user_id:
                LOG.warning("Ignoring /player from unauthorized user %s in business chat %s", from_id, chat_id)
                return

            actual_conn_id = None if chat_id == self.allowed_user_id else conn_id
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
        if not can_reply and user_id == self.allowed_user_id and is_enabled:
            self.ui.send_message(
                self.allowed_user_id,
                "⚠️ <b>Внимание: Telegram Business</b>\n\n"
                "Бот подключен, но у него <b>выключено разрешение «Отвечать на сообщения»</b> (<code>can_reply=false</code>).\n\n"
                "Чтобы бот мог отправлять плеер в чат:\n"
                "1. Открой в Telegram: <b>Настройки</b> ➔ <b>Telegram для бизнеса</b> ➔ <b>Чат-боты</b>\n"
                "2. Нажми на бота и <b>ВКЛЮЧИ</b>: <b>«Отвечать на сообщения»</b>\n"
                "3. В пункте <b>«Чаты»</b> выбери <b>«Все личные чаты»</b> (а не только новые)",
            )

    def _dispatch_update(self, update):
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

    def poll_updates(self):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        payload = {
            "offset": self.offset,
            "timeout": 20,
            "allowed_updates": ["message", "callback_query", "business_connection", "business_message", "edited_business_message"],
        }
        try:
            res = self.polling_session.post(url, json=payload, timeout=25)
            if res.status_code == 429:
                retry_after = 5.0
                try:
                    retry_val = (res.json().get("parameters") or {}).get("retry_after")
                    if retry_val is not None:
                        retry_after = min(max(float(retry_val), 1.0), 60.0)
                except Exception:
                    pass
                LOG.warning("Telegram getUpdates rate-limited (429); sleeping %.1fs", retry_after)
                time.sleep(retry_after)
                return
            if res.status_code == 409:
                LOG.error("Telegram getUpdates 409 Conflict (another poller active); backing off 5s")
                time.sleep(5)
                return
            if res.status_code != 200:
                time.sleep(2)
                return
            data = res.json()
            if not data.get("ok"):
                if data.get("error_code") == 429:
                    retry_val = (data.get("parameters") or {}).get("retry_after", 5)
                    retry_after = min(max(float(retry_val), 1.0), 60.0)
                    LOG.warning("Telegram API 429; sleeping %.1fs", retry_after)
                    time.sleep(retry_after)
                else:
                    time.sleep(2)
                return
            updates = data.get("result", [])
            if updates:
                for update in updates:
                    self.offset = int(update["update_id"]) + 1
                    if getattr(self, "async_dispatch", True) and hasattr(self, "_executor"):
                        try:
                            self._executor.submit(self._dispatch_update, update)
                        except RuntimeError:
                            # Executor was shut down during service termination
                            break
                    else:
                        self._dispatch_update(update)
                self.save_state()
        except Exception as err:
            LOG.error("Telegram polling error: %s", self.ui.sanitize_error(err))
            time.sleep(3)

    def _acquire_lock(self):
        try:
            LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            for _attempt in range(3):
                try:
                    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    try:
                        os.write(fd, str(os.getpid()).encode("utf-8"))
                    finally:
                        os.close(fd)
                    LOG.info("Acquired single-instance lock (PID %d) at %s", os.getpid(), LOCK_PATH)
                    return
                except FileExistsError:
                    try:
                        content = LOCK_PATH.read_text(encoding="utf-8").strip()
                    except FileNotFoundError:
                        continue
                    old_pid = int(content) if content.isdigit() else None
                    if old_pid == os.getpid():
                        LOG.warning("Lockfile holds our own PID %d (PID reuse after restart). Reclaiming lock.", old_pid)
                    elif old_pid and psutil.pid_exists(old_pid):
                        is_bot = False
                        proc = None
                        try:
                            proc = psutil.Process(old_pid)
                            cmdline_str = " ".join(proc.cmdline()).lower()
                            proc_name = proc.name().lower()
                            if "python" in proc_name or "pc-control" in cmdline_str or "bot.py" in cmdline_str:
                                is_bot = True
                        except psutil.NoSuchProcess:
                            is_bot = False
                        except psutil.AccessDenied:
                            LOG.warning("AccessDenied inspecting PID %d from lockfile; treating lock as active.", old_pid)
                            is_bot = True

                        if is_bot:
                            LOG.error("Another bot instance is already running (PID %d). Aborting to prevent 409 conflict.", old_pid)
                            sys.exit(0)
                            return
                        proc_name_str = "unknown"
                        if proc is not None:
                            try:
                                proc_name_str = proc.name()
                            except Exception:
                                pass
                        LOG.warning("Lockfile PID %d belongs to unrelated process (%s). Overwriting stale lock.", old_pid, proc_name_str)
                    else:
                        LOG.warning("Found stale lockfile from PID %s. Overwriting.", content)

                    LOCK_PATH.unlink(missing_ok=True)

            LOG.error("Failed to exclusively acquire single-instance lock after 3 attempts at %s; aborting.", LOCK_PATH)
            sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            LOG.error("Fatal: Could not acquire single-instance lock at %s: %s", LOCK_PATH, e)
            sys.exit(1)

    def _release_lock(self):
        try:
            if LOCK_PATH.exists():
                content = LOCK_PATH.read_text(encoding="utf-8").strip()
                if content == str(os.getpid()):
                    LOCK_PATH.unlink(missing_ok=True)
                    LOG.info("Released single-instance lock (PID %d)", os.getpid())
        except Exception as e:
            LOG.warning("Could not release single-instance lock: %s", e)

    def stop(self):
        if getattr(self, "_stopped", False):
            return
        self._stopped = True
        LOG.info("Stopping PC Control Bot...")
        self.running = False
        try:
            if hasattr(self, "mqtt") and self.mqtt is not None:
                if hasattr(self.mqtt, "stop") and callable(self.mqtt.stop):
                    self.mqtt.stop()
                elif hasattr(self.mqtt, "client"):
                    self.mqtt.client.loop_stop()
                    self.mqtt.client.disconnect()
        except Exception as e:
            LOG.warning("Error stopping MQTT client: %s", e)
        if hasattr(self, "_executor"):
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
                threads = list(getattr(self._executor, "_threads", []))
                for t in threads:
                    t.join(timeout=2.5)
            except TypeError:
                try:
                    self._executor.shutdown(wait=False)
                except Exception:
                    pass
            except Exception as e:
                LOG.warning("Error shutting down executor: %s", e)
        timer = getattr(self, "_player_timer", None)
        if timer is not None and hasattr(timer, "cancel"):
            try:
                timer.cancel()
            except Exception:
                pass
        self._player_timer = None
        self._player_timer_running = False
        polling_session = getattr(self, "polling_session", None)
        if polling_session is not None and hasattr(polling_session, "close"):
            try:
                polling_session.close()
            except Exception as e:
                LOG.warning("Error closing Telegram polling session: %s", e)
        try:
            if hasattr(self, "ui") and hasattr(self.ui, "close_sessions"):
                self.ui.close_sessions()
        except Exception as e:
            LOG.warning("Error closing Telegram HTTP sessions: %s", e)
        self._release_lock()

    def register_commands(self):
        try:
            self.ui.call(
                "setMyCommands",
                {
                    "commands": [
                        {"command": "menu", "description": "🖥 Главное меню"},
                        {"command": "player", "description": "🎵 Музыкальный плеер"},
                        {"command": "fix_ag", "description": "🌐 Сменить VPN и перезапустить Antigravity"},
                        {"command": "stealth", "description": "🕶️ Режим скрытности (Blackout)"},
                        {"command": "activity", "description": "📊 Активность за день"},
                        {"command": "antigravity", "description": "🔮 Профили Antigravity"},
                        {"command": "esc", "description": "⚡ Нажатие Escape (1..20x)"},
                        {"command": "console", "description": "💻 Консоль Windows"},
                        {"command": "cmd", "description": "💻 Командная строка CMD"},
                        {"command": "ps", "description": "⚡ Скрипт PowerShell"},
                        {"command": "shutdown", "description": "🛑 Выключение ПК"},
                        {"command": "start", "description": "🔄 Запуск и меню бота"},
                    ]
                },
            )
            LOG.info("Telegram setMyCommands successfully registered")
        except Exception as e:
            LOG.warning("Failed to register Telegram commands: %s", e)

    def run(self):
        self._acquire_lock()
        def _sig_handler(signum, frame):
            LOG.info("Received termination signal %s, initiating graceful shutdown...", signum)
            self.running = False

        try:
            signal.signal(signal.SIGINT, _sig_handler)
            signal.signal(signal.SIGTERM, _sig_handler)
        except Exception as e:
            LOG.warning("Could not register signal handlers: %s", e)

        LOG.info("Starting PC Control Bot application...")
        self.register_commands()
        self.mqtt.start()
        threading.Thread(target=self.scheduler_loop, name="shutdown-scheduler", daemon=True).start()
        target_user = self.allowed_user_id
        res = self.ui.send_message(target_user, self.monitoring.dashboard_text(), reply_markup=self._main_menu_markup())
        if res and isinstance(res, dict) and "message_id" in res:
            self.active_main_menu_message_id = res["message_id"]
            self.current_menu_view = "main"
            self.save_state()

        try:
            while self.running:
                self.poll_updates()
        finally:
            self.stop()


if __name__ == "__main__":
    app = PcControlBotApp()
    app.run()
