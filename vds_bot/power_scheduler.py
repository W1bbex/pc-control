#!/usr/bin/env python3
"""
Power Scheduling and Lifecycle Watchdog Service for PC Control Bot.
Handles delayed power-off countdowns, SHUTDOWN_OBSERVED confirmation loops, auto-LED cooling watchdog, and dashboard auto-refresh.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    MOSCOW = ZoneInfo("Europe/Moscow")
except Exception:
    MOSCOW = timezone(timedelta(hours=3))

try:
    from .telegram_ui import ICONS
except ImportError:
    from telegram_ui import ICONS

LOG = logging.getLogger("pc-control-bot.scheduler")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID") or 0)
SCREENSHOT_TIMEOUT_SECONDS = 35
SHUTDOWN_CONFIRM_TIMEOUT_SECONDS = 120


class PowerSchedulerMixin:
    """
    Mixin for PcControlBotApp providing shutdown scheduling, timeouts, auto-refresh, and periodic watchdog ticks.
    """

    def _check_shutdown_confirmation(self, now=None):
        now = time.time() if now is None else float(now)
        if not getattr(self, "pending_shutdown_cmd_id", None):
            return False

        # Criteria for SHUTDOWN_OBSERVED:
        # 1. shutdown command ACK received from Windows
        # 2. MQTT broker is online
        # 3. Both system and desktop agents are strictly offline
        # 4. Telemetry is absent or >= 20s old
        # 5. Sustained for >= 20s continuous
        telem_age = self.monitoring.telemetry_age()
        telem_absent = (
            telem_age is None
            or telem_age >= 20.0
            or (self.pending_shutdown_started_at and self.monitoring.latest_received_at < self.pending_shutdown_started_at)
        )
        both_offline = (
            self.mqtt.system_status in ("offline", "stale")
            and self.mqtt.desktop_status in ("offline", "stale")
        )

        if (
            getattr(self, "shutdown_acknowledged", False)
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
                target_user = getattr(self, "allowed_user_id", ALLOWED_USER_ID)
                self.ui.send_message(
                    target_user,
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
        if getattr(self, "pending_shutdown_cmd_id", None):
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
        session = getattr(self.mqtt, "system_session", "")
        if session in ("locked", "login_screen"):
            return False

        desk_status = getattr(self.mqtt, "desktop_status", None)
        if session != "unlocked" and desk_status != "online":
            return False

        age = self.monitoring.telemetry_age()
        if age is None or age > 30.0:
            return False

        telemetry = self.monitoring.latest_telemetry or {}
        active_app = (telemetry.get("active_application") or "").lower()

        telegram_clients = ("telegram", "ayugram", "kotatogram", "64gram", "unigram")
        if not any(client in active_app for client in telegram_clients):
            return False

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
            target_user = getattr(self, "allowed_user_id", ALLOWED_USER_ID)
            text = self.monitoring.dashboard_text(getattr(self, "latest_media_state", None) or getattr(self.mqtt, "last_media_state", {}))
            markup = self._main_menu_markup()
            res = self.ui.send_message(
                target_user,
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
        target_user = getattr(self, "allowed_user_id", ALLOWED_USER_ID)

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

        user_led_pref = getattr(self, "user_led_state", "on")
        with getattr(self, "state_lock", threading.RLock()):
            if user_led_pref == "off":
                if getattr(self, "last_auto_led_state", None) != "off":
                    if hasattr(self.mqtt, "publish_esp_action"):
                        if self.mqtt.publish_esp_action("LED_OFF"):
                            self.last_auto_led_state = "off"
                            LOG.info("Auto-LED Watchdog: user_led_state=off -> LED_OFF enforced")
            else:
                if (not watchdog_pc_online) and getattr(self, "last_auto_led_state", None) != "off":
                    if hasattr(self.mqtt, "publish_esp_action"):
                        if self.mqtt.publish_esp_action("LED_OFF"):
                            self.last_auto_led_state = "off"
                            LOG.info("Auto-LED Watchdog: PC offline/sleeping (telem_age=%s) -> LED_OFF sent to cooler", telem_age)
                elif watchdog_pc_online and getattr(self, "last_auto_led_state", None) != "on":
                    if hasattr(self.mqtt, "publish_esp_action"):
                        color = getattr(self, "user_led_color", None)
                        if color:
                            self.mqtt.publish_esp_action("LED_COLOR", value=color)
                        else:
                            self.mqtt.publish_esp_action("LED_ON")
                        self.last_auto_led_state = "on"
                        LOG.info("Auto-LED Watchdog: PC online/awake -> LED sent to cooler")

        for cmd_id, pending in list(getattr(self, "pending_screenshots", {}).items()):
            if now - pending["requested_at"] >= SCREENSHOT_TIMEOUT_SECONDS:
                self.pending_screenshots.pop(cmd_id, None)
                self.ui.send_message(pending["chat_id"], "❌ Скриншот не получен за 35 секунд. Попробуй ещё раз.")

        # Evict stale pending_telemetry_cmd_ids and pending_antigravity_cmd_ids (timeout 60s)
        pending_telem = getattr(self, "pending_telemetry_cmd_ids", {})
        pending_ag = getattr(self, "pending_antigravity_cmd_ids", {})
        cmd_timestamps = getattr(self, "pending_cmd_timestamps", {})

        if None in pending_telem:
            pending_telem.pop(None, None)
        if None in pending_ag:
            pending_ag.pop(None, None)

        for cmd_id, ts in list(cmd_timestamps.items()):
            if now - float(ts) >= 60.0:
                cmd_timestamps.pop(cmd_id, None)
                pending_telem.pop(cmd_id, None)
                pending_ag.pop(cmd_id, None)

        if getattr(self, "pending_shutdown_cmd_id", None) and getattr(self, "pending_shutdown_started_at", None):
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
                if getattr(self, "shutdown_acknowledged", False):
                    self.ui.send_message(
                        target_user,
                        "<b>⚠️ Связь потеряна, состояние ПК неизвестно.</b>\n\nКоманда выключения была принята Windows, но подтверждение завершения работы не получено за 2 минуты.",
                        reply_markup=timeout_markup,
                    )
                else:
                    self.ui.send_message(
                        target_user,
                        "<b>❌ Выключение не подтверждено.</b>\n\nWindows не ответила на команду выключения за 2 минуты.",
                        reply_markup=timeout_markup,
                    )
                self._clear_pending_shutdown()

        if not getattr(self, "scheduled_shutdown_ts", None) or now < self.scheduled_shutdown_ts:
            return
        if now - self.scheduled_shutdown_ts > 600.0:
            LOG.info("Discarding stale scheduled shutdown (scheduled_at=%s, now=%s)", self.scheduled_shutdown_ts, now)
            self.scheduled_shutdown_ts = None
            self.save_state()
            return
        if now - getattr(self, "last_schedule_attempt_at", 0.0) < 15:
            return
        self.last_schedule_attempt_at = now

        pc_state = self.mqtt.health.evaluate_pc_state(self.monitoring.telemetry_age(), now=now)
        if pc_state.state == "SHUTDOWN_OBSERVED":
            self.scheduled_shutdown_ts = None
            self.save_state()
            self.ui.send_message(target_user, "ℹ️ Таймер сработал, но ПК уже был выключен (наблюдается).")
            return
        if self.mqtt.system_status != "online":
            return
        if self._request_shutdown(target_user, source="schedule", now=now):
            self.scheduled_shutdown_ts = None
            self.save_state()

    def scheduler_loop(self):
        while getattr(self, "running", True):
            try:
                self.scheduler_tick()
            except Exception:
                LOG.exception("Shutdown scheduler tick failed")
            time.sleep(1)
