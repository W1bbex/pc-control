#!/usr/bin/env python3

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

LOG = logging.getLogger("pc-control-bot.power_flow")

FLOW_STATE_PATH = Path("/var/lib/pc-control-bot/flow_state.json")
FLOW_TTL_SECONDS = 300  # 5 minutes


class PowerFlowStage:
    IDLE = "IDLE"
    POWERING_ON = "POWERING_ON"
    UNLOCKING = "UNLOCKING"
    UNLOCK_FAILED = "UNLOCK_FAILED"
    WAITING_WORKSPACE_DECISION = "WAITING_WORKSPACE_DECISION"
    WORKSPACE_RUNNING = "WORKSPACE_RUNNING"


class PowerFlowManager:
    def __init__(self, telegram_ui, mqtt_gateway):
        self.ui = telegram_ui
        self.mqtt = mqtt_gateway
        self.flow_id = None
        self.chat_id = None
        self.stage = PowerFlowStage.IDLE
        self.message_id = None
        self.created_at = 0.0
        self.expires_at = 0.0
        self.unlock_attempts = 0
        self.pending_unlock_command_id = None
        self.pending_unlock_sent_at = 0.0
        self.active_workspace_id = None
        self.workspace_cancelled = False
        self.auto_launch_workspace = False
        self.on_workspace_start_cb = None
        self._lock = threading.Lock()

        self.load_state()

    def load_state(self):
        try:
            if not FLOW_STATE_PATH.exists():
                return
            data = json.loads(FLOW_STATE_PATH.read_text(encoding="utf-8"))
            expires_at = float(data.get("expires_at", 0))
            if time.time() > expires_at:
                LOG.info("Persisted flow_state expired. Resetting to IDLE.")
                self.reset_flow()
                return

            self.flow_id = data.get("flow_id")
            self.chat_id = data.get("chat_id")
            self.stage = data.get("stage", PowerFlowStage.IDLE)
            self.message_id = data.get("message_id")
            self.created_at = float(data.get("created_at", 0))
            self.expires_at = expires_at
            self.unlock_attempts = int(data.get("unlock_attempts", 0))
            self.active_workspace_id = data.get("active_workspace_id")
            self.auto_launch_workspace = bool(data.get("auto_launch_workspace", False))

            # Fix 5: If bot restarted during UNLOCKING, switch to UNLOCK_FAILED and update Telegram UI card!
            if self.stage == PowerFlowStage.UNLOCKING:
                LOG.info("Bot restarted during UNLOCKING. Pausing auto-retry, requiring user interaction.")
                self.stage = PowerFlowStage.UNLOCK_FAILED
                self.pending_unlock_command_id = None
                if self.chat_id and self.message_id:
                    self.on_unlock_failed(self.chat_id, "Разблокировка была прервана перезапуском бота.")

            LOG.info("Loaded active flow_state: stage=%s, message_id=%s", self.stage, self.message_id)
        except Exception:
            LOG.exception("Could not load flow_state.json")
            self.reset_flow()

    def save_state(self):
        with self._lock:
            try:
                FLOW_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                temp = FLOW_STATE_PATH.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
                data = {
                    "flow_id": self.flow_id,
                    "chat_id": self.chat_id,
                    "stage": self.stage,
                    "message_id": self.message_id,
                    "created_at": self.created_at,
                    "expires_at": self.expires_at,
                    "unlock_attempts": self.unlock_attempts,
                    "active_workspace_id": self.active_workspace_id,
                    "auto_launch_workspace": self.auto_launch_workspace,
                }
                temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                try:
                    os.chmod(temp, 0o600)
                except Exception:
                    pass
                os.replace(temp, FLOW_STATE_PATH)
            except Exception:
                LOG.exception("Could not save flow_state.json")

    def reset_flow(self):
        self.flow_id = None
        self.chat_id = None
        self.stage = PowerFlowStage.IDLE
        self.message_id = None
        self.created_at = 0.0
        self.expires_at = 0.0
        self.unlock_attempts = 0
        self.pending_unlock_command_id = None
        self.active_workspace_id = None
        self.workspace_cancelled = False
        self.auto_launch_workspace = False
        if FLOW_STATE_PATH.exists():
            try:
                FLOW_STATE_PATH.unlink()
            except Exception:
                pass

    def start_power_on(self, chat_id):
        # Hardware protection: NEVER pulse power button if PC is already on/booting
        pc_state = self.mqtt.health.evaluate_pc_state()
        state_str = getattr(getattr(pc_state, "state", None), "value", str(getattr(pc_state, "state", "")))
        if (
            state_str in ("READY", "DEGRADED", "LOCKED", "BOOTING")
            or self.mqtt.system_status == "online"
            or self.mqtt.desktop_status == "online"
        ):
            LOG.warning("start_power_on ignored: PC is already running (state=%s, system=%s)", state_str, self.mqtt.system_status)
            self.ui.send_message(
                chat_id,
                f"ℹ️ <b>Компьютер уже включен!</b>\n\nТекущее состояние: {pc_state.label}",
                reply_markup=self.ui.power_menu_markup(pc_state=pc_state, session=self.mqtt.system_session, system_status=self.mqtt.system_status)
            )
            return

        # 2. Check ESP connectivity: ESP MUST be online to pulse power button
        if getattr(self.mqtt, "esp_status", None) != "online":
            LOG.warning("start_power_on rejected: ESP8266 is not online (status=%s)", getattr(self.mqtt, "esp_status", None))
            text = (
                "<b>❌ Модуль питания (ESP8266) не в сети!</b>\n\n"
                f"Статус платы: <code>{getattr(self.mqtt, 'esp_status', 'offline')}</code>.\n\n"
                "Плата ESP8266 отключена от питания или потеряла связь с сервером.\n"
                "Сигнал включения (импульс реле) не может быть доставлен.\n\n"
                "👉 <i>Подключите плату к питанию или включите ПК вручную кнопкой на корпусе.</i>"
            )
            self.ui.send_message(
                chat_id,
                text,
                reply_markup=self.ui.power_menu_markup(pc_state=pc_state, session=self.mqtt.system_session, system_status=self.mqtt.system_status),
                message_id=self.message_id
            )
            return

        press_cmd_id = self.mqtt.publish_esp_action("PRESS")
        if not press_cmd_id:
            LOG.error("Failed to publish PRESS command to ESP")
            text = (
                "<b>❌ Ошибка отправки команды на ESP8266</b>\n\n"
                "Не удалось отправить команду включения в MQTT."
            )
            self.ui.send_message(
                chat_id,
                text,
                reply_markup=self.ui.power_menu_markup(pc_state=pc_state, session=self.mqtt.system_session, system_status=self.mqtt.system_status),
                message_id=self.message_id
            )
            return

        self.flow_id = str(uuid.uuid4())
        self.chat_id = chat_id
        self.stage = PowerFlowStage.POWERING_ON
        self.created_at = time.time()
        self.expires_at = self.created_at + FLOW_TTL_SECONDS
        self.unlock_attempts = 0
        self.pending_unlock_command_id = None
        self.workspace_cancelled = False

        # Send LED command via ESP
        self.mqtt.publish_esp_action("LED_ON")

        text = (
            "<b>⚡ ПК запускается...</b>\n\n"
            "Сигнал включения отправлен.\n"
            "Разблокировать Windows при появлении экрана входа?"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "🚀 Разблок + Сетап", "style": "success", "callback_data": "flow_unlock_and_workspace"},
                    {"text": "🔐 Только разблок", "callback_data": "flow_unlock_yes"},
                ],
                [
                    {"text": "✖ Не сейчас", "callback_data": "flow_unlock_no"},
                ]
            ]
        }

        res = self.ui.send_message(chat_id, text, reply_markup=markup)
        if res and res.get("message_id"):
            self.message_id = res["message_id"]
        self.save_state()

    def handle_unlock_decision(self, choice, chat_id):
        if choice == "flow_unlock_no":
            text = (
                "<b>⚡ ПК запускается</b>\n\n"
                "Разблокировка Windows пропущена."
            )
            self.ui.send_message(chat_id, text, reply_markup=None, message_id=self.message_id)
            self.reset_flow()
            return

        if choice in {"flow_unlock_yes", "flow_unlock_and_workspace"}:
            self.stage = PowerFlowStage.UNLOCKING
            self.pending_unlock_command_id = None
            if choice == "flow_unlock_and_workspace":
                self.auto_launch_workspace = True
            self.save_state()
            if getattr(self, "auto_launch_workspace", False):
                text = (
                    "<b>🔐 Ожидаю экран входа Windows...</b>\n\n"
                    "🚀 <i>Жест 'Разблок + Сетап': сетап запустится сразу после разблокировки.</i>"
                )
            else:
                text = (
                    "<b>🔐 Ожидаю экран входа Windows...</b>\n\n"
                    "Как только система сообщит о готовности к вводу, будет передан пароль."
                )
            markup = self._get_unlocking_markup()
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=self.message_id)
            
            # Check if system session is ready to unlock
            self.check_and_trigger_unlock(chat_id)

    def _get_unlocking_markup(self):
        if getattr(self, "auto_launch_workspace", False):
            return {
                "inline_keyboard": [
                    [{"text": "✅ Сетап запланирован", "callback_data": "noop"}],
                    [{"text": "✖ Отмена", "callback_data": "flow_unlock_no"}],
                ]
            }
        return {
            "inline_keyboard": [
                [{"text": "🚀 + Запустить рабочий сетап", "callback_data": "flow_unlock_and_workspace"}],
                [{"text": "✖ Отмена", "callback_data": "flow_unlock_no"}],
            ]
        }

    def set_auto_launch_workspace(self, enable=True):
        self.auto_launch_workspace = bool(enable)
        self.save_state()

    def start_manual_unlock(self, chat_id, message_id=None, auto_launch_workspace=False):
        if self.stage == PowerFlowStage.UNLOCKING and self.pending_unlock_command_id:
            LOG.info("Manual unlock requested while an unlock command is already in-flight. Ignoring duplicate trigger.")
            if auto_launch_workspace:
                self.auto_launch_workspace = True
                self.save_state()
            text = (
                "<b>🔐 Разблокировка уже выполняется...</b>\n\n"
                "Ожидаю подтверждения от Windows."
            )
            self.ui.send_message(chat_id, text, reply_markup=None, message_id=message_id or self.message_id)
            return

        self.flow_id = str(uuid.uuid4())
        self.chat_id = chat_id
        self.message_id = message_id
        self.stage = PowerFlowStage.UNLOCKING
        self.created_at = time.time()
        self.expires_at = self.created_at + FLOW_TTL_SECONDS
        self.unlock_attempts = 0
        self.pending_unlock_command_id = None
        self.workspace_cancelled = False
        self.auto_launch_workspace = bool(auto_launch_workspace)

        self.save_state()

        text = (
            "<b>🔐 Ожидаю экран входа Windows...</b>\n\n"
            "Как только система сообщит о готовности к вводу, будет передан пароль."
        )
        markup = self._get_unlocking_markup()
        res = self.ui.send_message(chat_id, text, reply_markup=markup, message_id=self.message_id)
        if res and res.get("message_id"):
            self.message_id = res["message_id"]
            self.save_state()

        self.check_and_trigger_unlock(chat_id)

    def check_and_trigger_unlock(self, chat_id):
        if self.stage != PowerFlowStage.UNLOCKING:
            return

        session = self.mqtt.system_session
        system_online = self.mqtt.system_status == "online"
        LOG.info(
            "check_and_trigger_unlock: system_online=%s, session=%s, pending_cmd=%s, attempts=%d",
            system_online, session, self.pending_unlock_command_id, self.unlock_attempts
        )

        if session == "unlocked":
            # Session is already unlocked!
            self.on_unlock_success(chat_id)
            return

        # Note: no_console is NOT a ready login screen (ready states are login_screen or locked)
        if system_online and session in {"login_screen", "locked"}:
            now = time.time()
            if self.pending_unlock_command_id:
                sent_at = getattr(self, "pending_unlock_sent_at", 0.0)
                if sent_at > 0.0 and (now - sent_at > 45.0):
                    LOG.warning("Unlock command %s timed out waiting for ACK after 45s", self.pending_unlock_command_id)
                    self.pending_unlock_command_id = None
                    if self.unlock_attempts >= 2:
                        self.on_unlock_failed(chat_id, "Таймаут ответа от агента при разблокировке.")
                        return
                else:
                    LOG.info("Already waiting for an in-flight unlock command ACK (%s)", self.pending_unlock_command_id)
                    return

            if self.unlock_attempts >= 2:
                self.on_unlock_failed(chat_id, "Не удалось разблокировать Windows за 2 попытки.")
                return

            self.unlock_attempts += 1
            self.save_state()
            text = (
                f"<b>🔐 Ввожу пароль Windows... (попытка {self.unlock_attempts}/2)</b>\n\n"
                "Ожидаю подтверждения разблокировки от системы."
            )
            self.ui.send_message(chat_id, text, reply_markup=self._get_unlocking_markup(), message_id=self.message_id)

            # Send unlock command to boot_agent.py
            cmd_id = self.mqtt.publish_system_action("unlock")
            LOG.info("Sent unlock action to boot_agent.py (cmd_id=%s)", cmd_id)
            self.pending_unlock_command_id = cmd_id
            self.pending_unlock_sent_at = now

    def handle_ack(self, ack_data):
        cmd_id = ack_data.get("id")
        result = ack_data.get("result")
        data = ack_data.get("data") or {}

        if self.stage == PowerFlowStage.UNLOCKING and cmd_id == self.pending_unlock_command_id:
            self.pending_unlock_command_id = None

            sess = data.get("session") or self.mqtt.system_session
            if result == "executed" and sess == "unlocked":
                self.on_unlock_success(self.chat_id or 0)
            elif result == "busy":
                LOG.info("System agent reported unlock busy (already running)")
            else:
                LOG.warning("Unlock attempt %d ACK result=%s session=%s", self.unlock_attempts, result, sess)
                if self.unlock_attempts < 2:
                    text = (
                        "<b>⚠️ Первая попытка ввода пароля не подтверждена.</b>\n\n"
                        "Ожидаю стабилизации экрана входа перед второй попыткой..."
                    )
                    self.ui.send_message(self.chat_id or 0, text, reply_markup=None, message_id=self.message_id)
                else:
                    self.on_unlock_failed(self.chat_id or 0, f"Разблокировка не подтверждена ({result}).")

    def on_system_session_update(self, session, chat_id):
        """
        CRITICAL: Sole proof of unlocked session is system_session == 'unlocked'.
        desktop-agent online status is NEVER used as proof of session unlock.
        """
        LOG.info("on_system_session_update: session=%s, stage=%s, pending_cmd=%s, attempts=%d", session, self.stage, self.pending_unlock_command_id, self.unlock_attempts)
        if self.stage == PowerFlowStage.UNLOCKING:
            if session == "unlocked":
                self.on_unlock_success(chat_id)
            elif session in {"login_screen", "locked"}:
                if not self.pending_unlock_command_id and self.unlock_attempts < 2:
                    self.check_and_trigger_unlock(chat_id)
            elif session in {"no_console", "unknown"}:
                LOG.info("PC boot session '%s' reported, waiting for login_screen/locked...", session)
        elif self.stage == PowerFlowStage.UNLOCK_FAILED:
            if session == "unlocked":
                LOG.info("Session became unlocked manually after unlock failure, auto-recovering power flow")
                self.on_unlock_success(chat_id, manual=True)

    def on_unlock_success(self, chat_id, manual=False):
        status_title = "<b>ℹ️ Windows разблокирована вручную.</b>" if manual else "<b>✅ Windows разблокирована!</b>"
        if getattr(self, "auto_launch_workspace", False):
            LOG.info("Auto-launching workspace setup after %s unlock.", "manual" if manual else "successful")
            self.auto_launch_workspace = False
            self.stage = PowerFlowStage.WORKSPACE_RUNNING
            self.workspace_cancelled = False
            self.active_workspace_id = f"ws-{int(time.time())}"
            self.save_state()

            text = (
                f"{status_title}\n\n"
                "🚀 <i>Жест 'Разблок + Сетап': запускаю рабочий сетап...</i>"
            )
            self.ui.send_message(chat_id, text, reply_markup=None, message_id=self.message_id)

            if getattr(self, "on_workspace_start_cb", None):
                self.on_workspace_start_cb(chat_id, card_message_id=self.message_id, workspace_id=self.active_workspace_id)
            return

        self.stage = PowerFlowStage.WAITING_WORKSPACE_DECISION
        self.save_state()

        text = (
            f"{status_title}\n\n"
            "Запустить рабочий сетап? Выберите VPN:"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "🛡️ Сетап с INCY", "style": "success", "callback_data": "workspace_vpn:incy"},
                    {"text": "⚡ Сетап с Happ", "style": "primary", "callback_data": "workspace_vpn:happ"},
                ],
                [
                    {"text": "✖ Не сейчас", "callback_data": "flow_workspace_no"},
                ]
            ]
        }
        self.ui.send_message(chat_id, text, reply_markup=markup, message_id=self.message_id)

    def on_unlock_failed(self, chat_id, reason):
        self.stage = PowerFlowStage.UNLOCK_FAILED
        self.save_state()

        text = (
            f"<b>❌ Ошибка разблокировки Windows</b>\n\n"
            f"{reason}\n\n"
            "Пароль отклонён или экран входа недоступен."
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "🔁 Попробовать снова", "style": "success", "callback_data": "flow_unlock_retry"},
                    {"text": "✖ Отмена", "callback_data": "flow_unlock_cancel"},
                ]
            ]
        }
        self.ui.send_message(chat_id, text, reply_markup=markup, message_id=self.message_id)

    def handle_unlock_retry(self, chat_id):
        self.unlock_attempts = 0
        self.pending_unlock_command_id = None
        self.handle_unlock_decision("flow_unlock_yes", chat_id)

    def handle_unlock_cancel(self, chat_id):
        text = "<b>🔐 Разблокировка отменена.</b>"
        self.ui.send_message(chat_id, text, reply_markup=None, message_id=self.message_id)
        self.reset_flow()

    def handle_workspace_decision(self, choice, chat_id, runner_func):
        if choice == "flow_workspace_no":
            text = (
                "<b>✅ Windows готова к работе</b>\n\n"
                "Запуск рабочего сетапа пропущен."
            )
            self.ui.send_message(chat_id, text, reply_markup=None, message_id=self.message_id)
            self.reset_flow()
            return

        if choice == "flow_workspace_yes":
            self.stage = PowerFlowStage.WORKSPACE_RUNNING
            self.workspace_cancelled = False
            self.active_workspace_id = f"ws-{int(time.time())}"
            self.save_state()
            runner_func(chat_id, card_message_id=self.message_id, workspace_id=self.active_workspace_id)

    def cancel_workspace(self, chat_id):
        self.workspace_cancelled = True
        ws_id = self.active_workspace_id
        if ws_id:
            # Publish real cancel_workspace command with workspace_id to desktop agent
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})

        text = "<b>✖ Запуск рабочего сетапа отменён.</b>"
        self.ui.send_message(chat_id, text, reply_markup=None, message_id=self.message_id)
        # Note: reset_flow() is called by the workspace runner thread upon clean exit so workspace_cancelled remains True while runner halts.

    def check_timeout(self, now=None):
        """
        Runtime watchdog called periodically (e.g. from scheduler_tick).
        If an active power/unlock flow has exceeded FLOW_TTL_SECONDS,
        it is cleanly expired, resetting to IDLE and notifying the user.
        """
        now = time.time() if now is None else float(now)
        if self.stage in (PowerFlowStage.IDLE, PowerFlowStage.WORKSPACE_RUNNING):
            return False

        if self.expires_at > 0 and now > self.expires_at:
            LOG.warning(
                "PowerFlowManager: flow %s expired at stage %s (created_at=%.1f, expires_at=%.1f, now=%.1f)",
                self.flow_id, self.stage, self.created_at, self.expires_at, now
            )
            chat_id = self.chat_id
            msg_id = self.message_id
            self.reset_flow()

            if chat_id and msg_id:
                try:
                    text = (
                        "⏱ <b>Время ожидания запуска ПК истекло.</b>\n\n"
                        "Процесс был автоматически отменён по таймауту безопасности (5 минут)."
                    )
                    self.ui.send_message(chat_id, text, reply_markup=None, message_id=msg_id)
                except Exception:
                    LOG.exception("Failed to send flow timeout notification to chat %s", chat_id)
            return True
        return False
