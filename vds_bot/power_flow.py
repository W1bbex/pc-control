#!/usr/bin/env python3

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

LOG = logging.getLogger("pc-control-bot.power_flow")

try:
    from .power_state import (
        FLOW_STATE_PATH,
        FLOW_TTL_SECONDS,
        PowerFlowStage,
        clear_persisted_flow_state,
        load_persisted_flow_state,
        save_persisted_flow_state,
    )
    from .power_ui import (
        build_flow_btn,
        get_unlock_failed_markup,
        get_unlock_success_markup,
        get_unlocking_markup,
        resolve_target_mode_info,
    )
    from .telegram_ui import ICONS
except ImportError:
    from power_state import (
        FLOW_STATE_PATH,
        FLOW_TTL_SECONDS,
        PowerFlowStage,
        clear_persisted_flow_state,
        load_persisted_flow_state,
        save_persisted_flow_state,
    )
    from power_ui import (
        build_flow_btn,
        get_unlock_failed_markup,
        get_unlock_success_markup,
        get_unlocking_markup,
        resolve_target_mode_info,
    )
    from telegram_ui import ICONS


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
        self.auto_launch_game = False
        self.target_game = None
        self.target_game_vpn = None
        self.target_workspace_vpn = None
        self.reboot_with_unlock = False
        self.reboot_started_at = 0.0
        self.reboot_offline_detected = False
        self.on_workspace_start_cb = None
        self.on_game_start_cb = None
        self.last_card_update_at = 0.0
        self.win_online_at = None
        self.logon_ready_at = None
        self.unlocked_at = None
        self.selecting_mode = False
        self._starting_power_on = False
        self._unlock_dispatch_in_flight = False
        self._lock = threading.Lock()

        self.load_state()

    def load_state(self):
        try:
            data = load_persisted_flow_state(FLOW_STATE_PATH)
            if not data:
                return
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
            self.auto_launch_game = bool(data.get("auto_launch_game", False))
            self.target_game = data.get("target_game")
            self.target_game_vpn = data.get("target_game_vpn")
            self.target_workspace_vpn = data.get("target_workspace_vpn")
            self.reboot_with_unlock = bool(data.get("reboot_with_unlock", False))
            self.reboot_started_at = float(data.get("reboot_started_at", 0.0))
            self.reboot_offline_detected = bool(data.get("reboot_offline_detected", False))

            if self.stage == PowerFlowStage.UNLOCKING:
                LOG.info("Bot restarted during UNLOCKING. Pausing auto-retry, requiring user interaction.")
                self.stage = PowerFlowStage.UNLOCK_FAILED
                self.pending_unlock_command_id = None
                if self.chat_id and self.message_id:
                    try:
                        self.on_unlock_failed(self.chat_id, "Разблокировка была прервана перезапуском бота.")
                    except Exception as e:
                        LOG.warning("Could not send unlock failed notification during startup: %s", e)

            LOG.info("Loaded active flow_state: stage=%s, message_id=%s", self.stage, self.message_id)
        except Exception:
            LOG.exception("Could not load flow_state.json")
            self.reset_flow()

    def save_state(self):
        with self._lock:
            msg_id = None
            if isinstance(self.message_id, int):
                msg_id = self.message_id
            elif isinstance(self.message_id, str) and self.message_id.isdigit():
                msg_id = int(self.message_id)

            chat_id = None
            if isinstance(self.chat_id, int):
                chat_id = self.chat_id
            elif isinstance(self.chat_id, str) and self.chat_id.isdigit():
                chat_id = int(self.chat_id)

            data = {
                "flow_id": str(self.flow_id) if self.flow_id else None,
                "chat_id": chat_id,
                "stage": str(self.stage),
                "message_id": msg_id,
                "created_at": float(self.created_at) if isinstance(self.created_at, (int, float)) else 0.0,
                "expires_at": float(self.expires_at) if isinstance(self.expires_at, (int, float)) else 0.0,
                "unlock_attempts": int(self.unlock_attempts) if isinstance(self.unlock_attempts, (int, float)) else 0,
                "active_workspace_id": str(self.active_workspace_id) if self.active_workspace_id else None,
                "auto_launch_workspace": bool(self.auto_launch_workspace),
                "auto_launch_game": bool(self.auto_launch_game),
                "target_game": str(self.target_game) if self.target_game else None,
                "target_game_vpn": str(self.target_game_vpn) if self.target_game_vpn else None,
                "target_workspace_vpn": str(self.target_workspace_vpn) if getattr(self, "target_workspace_vpn", None) else None,
                "reboot_with_unlock": bool(self.reboot_with_unlock),
                "reboot_started_at": float(self.reboot_started_at) if isinstance(self.reboot_started_at, (int, float)) else 0.0,
                "reboot_offline_detected": bool(self.reboot_offline_detected),
            }
            return save_persisted_flow_state(data, FLOW_STATE_PATH)

    def reset_flow(self):
        with self._lock:
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
            self.auto_launch_game = False
            self.target_game = None
            self.target_game_vpn = None
            self.target_workspace_vpn = None
            self.reboot_with_unlock = False
            self.reboot_started_at = 0.0
            self.reboot_offline_detected = False
            self.last_card_update_at = 0.0
            self.win_online_at = None
            self.logon_ready_at = None
            self.unlocked_at = None
            self.selecting_mode = False
        clear_persisted_flow_state(FLOW_STATE_PATH)

    def start_power_on(self, chat_id, message_id=None):
        with self._lock:
            if self.stage == PowerFlowStage.POWERING_ON or getattr(self, "_starting_power_on", False):
                LOG.warning("start_power_on ignored: already in POWERING_ON stage")
                return
            self._starting_power_on = True
            if message_id:
                self.message_id = message_id
            mid = self.message_id

        try:
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
                    message_id=mid
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
                    message_id=mid
                )
                return

            with self._lock:
                self.flow_id = str(uuid.uuid4())
                self.chat_id = chat_id
                self.stage = PowerFlowStage.POWERING_ON
                self.created_at = time.time()
                self.expires_at = self.created_at + FLOW_TTL_SECONDS
                self.unlock_attempts = 0
                self.pending_unlock_command_id = None
                self.workspace_cancelled = False
                self.last_card_update_at = self.created_at
                self.win_online_at = None
                self.logon_ready_at = None
                self.unlocked_at = None

            # Send LED command via ESP if enabled by user
            led_state = getattr(getattr(self, "app", None), "user_led_state", "on")
            if led_state != "off":
                user_color = getattr(getattr(self, "app", None), "user_led_color", None)
                if user_color:
                    self.mqtt.publish_esp_action("LED_COLOR", value=user_color)
                else:
                    self.mqtt.publish_esp_action("LED_ON")
        finally:
            with self._lock:
                self._starting_power_on = False

        self._update_launch_card(force=True, is_initial=True)
        self.save_state()

    def _get_target_mode_info(self):
        return resolve_target_mode_info(
            auto_launch_workspace=getattr(self, "auto_launch_workspace", False),
            auto_launch_game=getattr(self, "auto_launch_game", False),
            target_workspace_vpn=getattr(self, "target_workspace_vpn", None),
            target_game=getattr(self, "target_game", None),
            target_game_vpn=getattr(self, "target_game_vpn", None),
            stage=self.stage,
        )

    def _update_launch_card(self, force=False, is_initial=False, chat_id=None):
        now = time.time()
        with self._lock:
            if self.stage not in (PowerFlowStage.POWERING_ON, PowerFlowStage.UNLOCKING):
                return
            if getattr(self, "selecting_mode", False):
                return
            cid = chat_id or self.chat_id
            if not cid:
                return
            self.chat_id = cid
            mid = self.message_id

            if not force and not is_initial and (now - getattr(self, "last_card_update_at", 0.0) < 2.5):
                return

            stage = self.stage
            created = getattr(self, "created_at", now)
            elapsed = max(0, int(now - created))
            is_delayed = elapsed >= 60

            target_mode, target_detail, target_label = self._get_target_mode_info()

            win_elapsed = (int(self.win_online_at - created) if getattr(self, "win_online_at", None) else None)
            logon_elapsed = (int(self.logon_ready_at - created) if getattr(self, "logon_ready_at", None) else None)
            unlocked_elapsed = (int(self.unlocked_at - created) if getattr(self, "unlocked_at", None) else None)
            unlock_attempts = self.unlock_attempts
            self.last_card_update_at = now

        sys_status = getattr(self.mqtt, "system_status", "offline")
        sys_session = getattr(self.mqtt, "system_session", "unknown")

        if hasattr(self.ui, "launch_card_text") and hasattr(self.ui, "launch_card_markup"):
            text = self.ui.launch_card_text(
                stage=stage,
                elapsed_seconds=elapsed,
                session_status=sys_session,
                system_status=sys_status,
                target_mode=target_mode,
                target_detail=target_detail,
                unlock_attempts=unlock_attempts,
                win_online_elapsed=win_elapsed,
                logon_ready_elapsed=logon_elapsed,
                unlocked_elapsed=unlocked_elapsed,
                is_delayed=is_delayed,
            )
            markup = self.ui.launch_card_markup(
                stage=stage,
                target_mode=target_mode,
                target_label=target_label,
                show_emergency=is_delayed,
            )
        else:
            text = "<b>ПК запускается...</b>"
            markup = self._get_unlocking_markup()

        res = self.ui.send_message(cid, text, reply_markup=markup, message_id=mid)
        if res and res.get("message_id") and not mid:
            updated_msg_id = False
            with self._lock:
                if self.stage != PowerFlowStage.IDLE and not self.message_id:
                    self.message_id = res["message_id"]
                    updated_msg_id = True
            if updated_msg_id:
                self.save_state()

    def tick(self, now=None):
        now = time.time() if now is None else float(now)
        if self.check_timeout(now):
            return True
        with self._lock:
            stage = self.stage
        if stage in (PowerFlowStage.POWERING_ON, PowerFlowStage.UNLOCKING):
            self._update_launch_card(force=False)
        return False

    def handle_unlock_decision(self, choice, chat_id, message_id=None):
        if choice == "flow_unlock_no":
            with self._lock:
                mid = self.message_id or message_id
            text = (
                "<b>ПК запускается</b>\n\n"
                "Разблокировка Windows пропущена."
            )
            markup = {"inline_keyboard": [self.ui.back_button("menu")]}
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=mid)
            self.reset_flow()
            return

        if choice in {"flow_unlock_yes", "flow_unlock_and_workspace", "flow_unlock_and_game"}:
            now = time.time()
            with self._lock:
                if not self.flow_id:
                    self.flow_id = str(uuid.uuid4())
                if not self.created_at:
                    self.created_at = now
                self.expires_at = now + FLOW_TTL_SECONDS
                self.unlock_attempts = 0
                self.stage = PowerFlowStage.UNLOCKING
                self.pending_unlock_command_id = None
                if choice == "flow_unlock_and_workspace":
                    self.auto_launch_workspace = True
                    self.auto_launch_game = False
                    if not getattr(self, "target_workspace_vpn", None):
                        self.target_workspace_vpn = "incy"
                elif choice == "flow_unlock_and_game":
                    self.auto_launch_game = True
                    self.auto_launch_workspace = False
                elif choice == "flow_unlock_yes":
                    self.auto_launch_workspace = False
                    self.auto_launch_game = False
                if chat_id:
                    self.chat_id = chat_id
            self.save_state()
            self._update_launch_card(force=True, chat_id=chat_id)
            self.check_and_trigger_unlock(chat_id)

    @staticmethod
    def _btn(text, style, callback_data, icon=None):
        return build_flow_btn(text, style, callback_data, icon=icon)

    @staticmethod
    def _invoke_cb_filtered(cb, cb_name, chat_id, card_message_id, optional_kwargs, **fixed_kwargs):
        if not cb:
            return
        try:
            import inspect
            sig = inspect.signature(cb)
            accepts_var_kw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
            kwargs = dict(fixed_kwargs)
            for k, v in optional_kwargs.items():
                if accepts_var_kw or k in sig.parameters:
                    kwargs[k] = v
            cb(chat_id, card_message_id=card_message_id, **kwargs)
        except Exception:
            LOG.exception("Failed to invoke %s", cb_name)

    def _get_unlocking_markup(self):
        return get_unlocking_markup(
            auto_game=getattr(self, "auto_launch_game", False),
            auto_workspace=getattr(self, "auto_launch_workspace", False),
        )

    def set_auto_launch_workspace(self, enable=True, vpn="incy", chat_id=None, message_id=None):
        cid = None
        now = time.time()
        with self._lock:
            self.auto_launch_workspace = bool(enable)
            if enable:
                self.flow_id = self.flow_id or str(uuid.uuid4())
                if not self.created_at:
                    self.created_at = now
                self.expires_at = now + FLOW_TTL_SECONDS
                self.unlock_attempts = 0
                self.workspace_cancelled = False
                self.auto_launch_game = False
                self.target_workspace_vpn = str(vpn)
                self.stage = PowerFlowStage.UNLOCKING
                self.pending_unlock_command_id = None
                if chat_id:
                    self.chat_id = chat_id
                if message_id is not None:
                    self.message_id = message_id
                cid = chat_id or self.chat_id
        self.save_state()
        if enable and cid:
            self._update_launch_card(force=True, chat_id=cid)
            self.check_and_trigger_unlock(cid)

    def set_auto_launch_game(self, game="gta5", vpn="incy", chat_id=None, message_id=None):
        now = time.time()
        with self._lock:
            self.flow_id = self.flow_id or str(uuid.uuid4())
            if not self.created_at:
                self.created_at = now
            self.expires_at = now + FLOW_TTL_SECONDS
            self.unlock_attempts = 0
            self.workspace_cancelled = False
            self.auto_launch_game = True
            self.auto_launch_workspace = False
            self.target_game = str(game)
            self.target_game_vpn = str(vpn)
            self.stage = PowerFlowStage.UNLOCKING
            self.pending_unlock_command_id = None
            if chat_id:
                self.chat_id = chat_id
            if message_id is not None:
                self.message_id = message_id
            cid = chat_id or self.chat_id
        self.save_state()

        if cid:
            self._update_launch_card(force=True, chat_id=cid)
            self.check_and_trigger_unlock(cid)

    def prepare_unlock_waiting(self, chat_id, auto_launch_workspace=False):
        now = time.time()
        with self._lock:
            self.flow_id = self.flow_id or str(uuid.uuid4())
            self.created_at = self.created_at or now
            self.expires_at = now + FLOW_TTL_SECONDS
            self.stage = PowerFlowStage.UNLOCKING
            self.unlock_attempts = 0
            self.pending_unlock_command_id = None
            self.workspace_cancelled = False
            if auto_launch_workspace:
                self.auto_launch_workspace = True
            self.chat_id = chat_id
        self.save_state()

    def start_direct_setup(self, chat_id, message_id=None, prefix="ws"):
        now = time.time()
        ws_id = f"{prefix}-{int(now)}"
        with self._lock:
            self.flow_id = self.flow_id or str(uuid.uuid4())
            self.created_at = self.created_at or now
            self.expires_at = now + FLOW_TTL_SECONDS
            self.chat_id = chat_id
            if message_id is not None:
                self.message_id = message_id
            self.stage = PowerFlowStage.WORKSPACE_RUNNING
            self.workspace_cancelled = False
            self.active_workspace_id = ws_id
        self.save_state()
        return ws_id

    def start_reboot_flow(self, chat_id, message_id=None, unlock=True):
        with self._lock:
            self.flow_id = str(uuid.uuid4())
            self.chat_id = chat_id
            self.message_id = message_id
            self.stage = PowerFlowStage.REBOOTING
            self.reboot_with_unlock = bool(unlock)
            self.reboot_started_at = time.time()
            self.reboot_offline_detected = False
            self.created_at = time.time()
            self.expires_at = self.created_at + FLOW_TTL_SECONDS
            self.unlock_attempts = 0
            self.pending_unlock_command_id = None
            self.workspace_cancelled = False
            self.auto_launch_workspace = False

        self.save_state()

        detail_text = (
            "⏳ Ожидаю выключения и повторной загрузки Windows для авторазблокировки..."
            if unlock
            else "⏳ Ожидаю завершения перезагрузки..."
        )
        text = (
            "<b>🔄 Перезагрузка ПК...</b>\n\n"
            "Команда на перезагрузку отправлена.\n"
            f"{detail_text}"
        )
        cancel_btn = self._btn("✖ Отмена ожидания", "danger", "flow_reboot_cancel", ICONS.get("cross"))
        markup = {"inline_keyboard": [[cancel_btn]]}
        res = self.ui.send_message(chat_id, text, reply_markup=markup, message_id=self.message_id)
        if res and res.get("message_id"):
            with self._lock:
                self.message_id = res["message_id"]
            self.save_state()

    def start_manual_unlock(self, chat_id, message_id=None, auto_launch_workspace=False):
        already_in_flight = False
        with self._lock:
            if self.stage == PowerFlowStage.UNLOCKING and self.pending_unlock_command_id:
                already_in_flight = True
                if auto_launch_workspace:
                    self.auto_launch_workspace = True
            else:
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

        if already_in_flight:
            LOG.info("Manual unlock requested while an unlock command is already in-flight. Ignoring duplicate trigger.")
            if auto_launch_workspace:
                self.save_state()
            text = (
                "<b>🔐 Разблокировка уже выполняется...</b>\n\n"
                "Ожидаю подтверждения от Windows."
            )
            self.ui.send_message(chat_id, text, reply_markup=None, message_id=message_id or self.message_id)
            return

        self.save_state()

        text = (
            "<b>🔐 Ожидаю экран входа Windows...</b>\n\n"
            "Как только система сообщит о готовности к вводу, будет передан пароль."
        )
        markup = self._get_unlocking_markup()
        res = self.ui.send_message(chat_id, text, reply_markup=markup, message_id=self.message_id)
        if res and res.get("message_id"):
            with self._lock:
                self.message_id = res["message_id"]
            self.save_state()

        self.check_and_trigger_unlock(chat_id)

    def check_and_trigger_unlock(self, chat_id):
        session = self.mqtt.system_session
        system_online = self.mqtt.system_status == "online"

        with self._lock:
            stage = self.stage
            pending_cmd = self.pending_unlock_command_id
            attempts = self.unlock_attempts

        LOG.info(
            "check_and_trigger_unlock: system_online=%s, session=%s, pending_cmd=%s, attempts=%d",
            system_online, session, pending_cmd, attempts
        )

        if stage != PowerFlowStage.UNLOCKING:
            return

        if session == "unlocked":
            # Session is already unlocked!
            self.on_unlock_success(chat_id)
            return

        # Note: no_console is NOT a ready login screen (ready states are login_screen or locked)
        if not (system_online and session in {"login_screen", "locked"}):
            return

        now = time.time()
        action = None
        fail_reason = None
        with self._lock:
            if self.stage != PowerFlowStage.UNLOCKING:
                return
            if self.pending_unlock_command_id:
                sent_at = getattr(self, "pending_unlock_sent_at", 0.0)
                if sent_at > 0.0 and (now - sent_at > 45.0):
                    LOG.warning("Unlock command %s timed out waiting for ACK after 45s", self.pending_unlock_command_id)
                    self.pending_unlock_command_id = None
                    if self.unlock_attempts >= 2:
                        action = "fail"
                        fail_reason = "Таймаут ответа от агента при разблокировке."
                else:
                    LOG.info("Already waiting for an in-flight unlock command ACK (%s)", self.pending_unlock_command_id)
                    return

            if action is None:
                if getattr(self, "_unlock_dispatch_in_flight", False):
                    LOG.info("Unlock dispatch already in progress; skipping duplicate trigger")
                    return
                if self.unlock_attempts >= 2:
                    action = "fail"
                    fail_reason = "Не удалось разблокировать Windows за 2 попытки."
                else:
                    self.unlock_attempts += 1
                    self._unlock_dispatch_in_flight = True
                    action = "dispatch"

        if action == "fail":
            self.on_unlock_failed(chat_id, fail_reason)
            return

        try:
            self.save_state()
            self._update_launch_card(force=True)
            with self._lock:
                if self.stage != PowerFlowStage.UNLOCKING:
                    return
            # Send unlock command to boot_agent.py
            cmd_id = self.mqtt.publish_system_action("unlock")
            LOG.info("Sent unlock action to boot_agent.py (cmd_id=%s)", cmd_id)
            with self._lock:
                self.pending_unlock_command_id = cmd_id
                self.pending_unlock_sent_at = now
        finally:
            with self._lock:
                self._unlock_dispatch_in_flight = False

    def handle_ack(self, ack_data):
        cmd_id = ack_data.get("id")
        result = ack_data.get("result")
        data = ack_data.get("data") or {}

        matched = False
        attempts = 0
        cid = 0
        mid = None
        with self._lock:
            if self.stage == PowerFlowStage.UNLOCKING and cmd_id == self.pending_unlock_command_id:
                self.pending_unlock_command_id = None
                matched = True
                attempts = self.unlock_attempts
                cid = self.chat_id or 0
                mid = self.message_id

        if matched:
            sess = data.get("session") or self.mqtt.system_session
            if result == "executed":
                if sess == "unlocked":
                    self.on_unlock_success(cid)
                else:
                    LOG.info(
                        "Unlock attempt %d keystrokes injected successfully. Awaiting session update (current session=%s)...",
                        attempts,
                        sess,
                    )
            elif result == "busy":
                LOG.info("System agent reported unlock busy (already running)")
            else:
                LOG.warning("Unlock attempt %d ACK failed: result=%s session=%s", attempts, result, sess)
                if attempts < 2:
                    text = (
                        "<b>⚠️ Первая попытка ввода пароля не подтверждена.</b>\n\n"
                        "Ожидаю стабилизации экрана входа перед второй попыткой..."
                    )
                    self.ui.send_message(cid, text, reply_markup=None, message_id=mid)
                else:
                    self.on_unlock_failed(cid, f"Разблокировка не подтверждена ({result}).")

    def on_system_session_update(self, session, chat_id):
        """
        CRITICAL: Sole proof of unlocked session is system_session == 'unlocked'.
        desktop-agent online status is NEVER used as proof of session unlock.
        """
        now = time.time()
        sys_status = getattr(self.mqtt, "system_status", "unknown")
        desk_status = getattr(self.mqtt, "desktop_status", None)

        action = None
        save_needed = False
        update_power_card = False
        mid = None

        with self._lock:
            LOG.info(
                "on_system_session_update: session=%s, stage=%s, pending_cmd=%s, attempts=%d",
                session, self.stage, self.pending_unlock_command_id, self.unlock_attempts
            )
            if sys_status == "online" and not getattr(self, "win_online_at", None):
                self.win_online_at = now
            if session in {"login_screen", "locked", "unlocked"} and not getattr(self, "logon_ready_at", None):
                self.logon_ready_at = now
            if session == "unlocked" and not getattr(self, "unlocked_at", None):
                self.unlocked_at = now

            if self.stage == PowerFlowStage.POWERING_ON:
                if session == "unlocked":
                    if self.auto_launch_workspace or self.auto_launch_game:
                        action = "unlock_success"
                    elif not getattr(self, "selecting_mode", False):
                        action = "unlock_success_manual"
                    else:
                        update_power_card = True
                else:
                    update_power_card = True

            if self.stage == PowerFlowStage.REBOOTING:
                is_reboot_elapsed = (now - getattr(self, "reboot_started_at", 0.0) >= 25.0)

                if sys_status == "offline" or (sys_status != "online" and session in ("unknown", "no_console")):
                    if not getattr(self, "reboot_offline_detected", False):
                        self.reboot_offline_detected = True
                        LOG.info("Reboot flow: offline state confirmed.")
                        save_needed = True
                    if sys_status == "offline":
                        action = "return"

                if action is None:
                    is_back_online = (
                        sys_status == "online"
                        and (
                            getattr(self, "reboot_offline_detected", False)
                            or (is_reboot_elapsed and session in {"login_screen", "locked"})
                        )
                        and session in {"login_screen", "locked", "unlocked"}
                    )
                    if is_back_online:
                        LOG.info("PC returned online after reboot (session=%s, unlock=%s)", session, getattr(self, "reboot_with_unlock", False))
                        mid = self.message_id
                        if getattr(self, "reboot_with_unlock", False):
                            self.stage = PowerFlowStage.UNLOCKING
                            self.unlock_attempts = 0
                            self.pending_unlock_command_id = None
                            save_needed = True
                            action = "unlock_success" if session == "unlocked" else "reboot_unlock_wait"
                        else:
                            self.stage = PowerFlowStage.IDLE
                            action = "reboot_done"
                    else:
                        action = "return"

            elif self.stage == PowerFlowStage.UNLOCKING:
                if session == "unlocked":
                    action = "unlock_success"
                elif session in {"login_screen", "locked"}:
                    if not self.pending_unlock_command_id and self.unlock_attempts < 2:
                        action = "check_unlock"
                elif session in {"no_console", "unknown"}:
                    LOG.info("PC boot session '%s' reported, waiting for login_screen/locked...", session)
            elif self.stage == PowerFlowStage.UNLOCK_FAILED:
                if session == "unlocked":
                    LOG.info("Session became unlocked manually after unlock failure, auto-recovering power flow")
                    action = "unlock_success_manual"

        if update_power_card:
            self._update_launch_card(force=True)
        if save_needed:
            self.save_state()

        if action == "unlock_success":
            self.on_unlock_success(chat_id)
        elif action == "unlock_success_manual":
            self.on_unlock_success(chat_id, manual=True)
        elif action == "check_unlock":
            self.check_and_trigger_unlock(chat_id)
        elif action == "reboot_unlock_wait":
            text = (
                "<b>ПК перезагрузился!</b>\n\n"
                "Ожидаю готовность экрана входа Windows для ввода пароля..."
            )
            self.ui.send_message(chat_id, text, reply_markup=self._get_unlocking_markup(), message_id=mid)
            self.check_and_trigger_unlock(chat_id)
        elif action == "reboot_done":
            text = f"<b>ПК успешно перезагружен!</b>\n\nСостояние: {session}"
            lock_btn = self._btn("Разблокировать Windows", "success", "unlock_windows_manual", ICONS.get("lock"))
            markup = {"inline_keyboard": [[lock_btn], self.ui.back_button("menu")]}
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=mid)
            self.reset_flow()

    def on_unlock_success(self, chat_id, manual=False):
        mode = None
        game_key = None
        vpn_key = None
        ws_id = None
        mid = None

        with self._lock:
            if self.stage in (PowerFlowStage.WORKSPACE_RUNNING, PowerFlowStage.WAITING_WORKSPACE_DECISION):
                return
            mid = self.message_id
            if getattr(self, "auto_launch_game", False) and getattr(self, "target_game", None):
                mode = "game"
                game_key = self.target_game
                vpn_key = getattr(self, "target_game_vpn", None) or "incy"
                self.auto_launch_game = False
                self.stage = PowerFlowStage.WORKSPACE_RUNNING
                self.workspace_cancelled = False
                ws_id = f"game-{int(time.time())}"
                self.active_workspace_id = ws_id
            elif getattr(self, "auto_launch_workspace", False):
                mode = "workspace"
                vpn_key = getattr(self, "target_workspace_vpn", None) or "incy"
                self.auto_launch_workspace = False
                self.stage = PowerFlowStage.WORKSPACE_RUNNING
                self.workspace_cancelled = False
                ws_id = f"ws-{int(time.time())}"
                self.active_workspace_id = ws_id
            else:
                mode = "decision"
                self.stage = PowerFlowStage.WAITING_WORKSPACE_DECISION
                self.auto_launch_workspace = False

        self.save_state()

        if mode == "game":
            self._invoke_cb_filtered(
                self.on_game_start_cb, "on_game_start_cb", chat_id, mid,
                {"workspace_id": ws_id}, game=game_key, vpn=vpn_key,
            )
            return

        if mode == "workspace":
            self._invoke_cb_filtered(
                self.on_workspace_start_cb, "on_workspace_start_cb", chat_id, mid,
                {"vpn": vpn_key}, workspace_id=ws_id,
            )
            return

        text, markup = get_unlock_success_markup(manual=manual)
        self.ui.send_message(chat_id, text, reply_markup=markup, message_id=mid)

    def on_unlock_failed(self, chat_id, reason):
        with self._lock:
            self.stage = PowerFlowStage.UNLOCK_FAILED
            mid = self.message_id
        self.save_state()

        text, markup = get_unlock_failed_markup(reason)
        self.ui.send_message(chat_id, text, reply_markup=markup, message_id=mid)

    def handle_unlock_retry(self, chat_id):
        with self._lock:
            self.unlock_attempts = 0
            self.pending_unlock_command_id = None
        self.handle_unlock_decision("flow_unlock_yes", chat_id)

    def handle_unlock_cancel(self, chat_id):
        with self._lock:
            mid = self.message_id
        text = "<b>Разблокировка отменена.</b>"
        markup = {"inline_keyboard": [self.ui.back_button("menu")]}
        self.ui.send_message(chat_id, text, reply_markup=markup, message_id=mid)
        self.reset_flow()

    def handle_workspace_decision(self, choice, chat_id, runner_func, message_id=None):
        if choice == "flow_workspace_no":
            with self._lock:
                mid = self.message_id or message_id
            text = (
                "<b>Windows готова к работе</b>\n\n"
                "Запуск рабочего сетапа пропущен."
            )
            markup = {"inline_keyboard": [self.ui.back_button("menu")]}
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=mid)
            self.reset_flow()
            return

        if choice == "flow_workspace_yes":
            with self._lock:
                self.stage = PowerFlowStage.WORKSPACE_RUNNING
                self.workspace_cancelled = False
                ws_id = f"ws-{int(time.time())}"
                self.active_workspace_id = ws_id
                mid = self.message_id
            self.save_state()
            runner_func(chat_id, card_message_id=mid, workspace_id=ws_id)

    def cancel_workspace(self, chat_id):
        with self._lock:
            self.workspace_cancelled = True
            ws_id = self.active_workspace_id
            mid = self.message_id
        if ws_id:
            # Publish real cancel_workspace command with workspace_id to desktop agent
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})

        text = "<b>Запуск рабочего сетапа отменён.</b>" if not (ws_id and str(ws_id).startswith("game")) else "<b>Запуск игры отменён.</b>"
        markup = {"inline_keyboard": [self.ui.back_button("menu")]}
        self.ui.send_message(chat_id, text, reply_markup=markup, message_id=mid)
        # Note: reset_flow() is called by the workspace runner thread upon clean exit so workspace_cancelled remains True while runner halts.

    def check_timeout(self, now=None):
        """
        Runtime watchdog called periodically (e.g. from scheduler_tick).
        If an active power/unlock flow has exceeded FLOW_TTL_SECONDS,
        it is cleanly expired, resetting to IDLE and notifying the user.
        """
        now = time.time() if now is None else float(now)
        with self._lock:
            if self.stage in (
                PowerFlowStage.IDLE,
                PowerFlowStage.WAITING_WORKSPACE_DECISION,
                PowerFlowStage.WORKSPACE_RUNNING,
            ):
                return False

            if not (self.expires_at > 0 and now > self.expires_at):
                return False

            flow_id = self.flow_id
            stage_at_timeout = self.stage
            created_at = self.created_at
            expires_at = self.expires_at
            chat_id = self.chat_id
            msg_id = self.message_id

        LOG.warning(
            "PowerFlowManager: flow %s expired at stage %s (created_at=%.1f, expires_at=%.1f, now=%.1f)",
            flow_id, stage_at_timeout, created_at, expires_at, now
        )
        self.reset_flow()

        if chat_id and msg_id:
            try:
                if stage_at_timeout == PowerFlowStage.REBOOTING:
                    text = (
                        "<b>Время ожидания перезагрузки истекло.</b>\n\n"
                        "ПК не вернулся в сеть в течение 5 минут."
                    )
                else:
                    text = (
                        "⏱ <b>Время ожидания запуска ПК истекло.</b>\n\n"
                        "Процесс был автоматически отменён по таймауту безопасности (5 минут)."
                    )
                markup = {"inline_keyboard": [self.ui.back_button("menu")]}
                self.ui.send_message(chat_id, text, reply_markup=markup, message_id=msg_id)
            except Exception:
                LOG.exception("Failed to send flow timeout notification to chat %s", chat_id)
        return True
