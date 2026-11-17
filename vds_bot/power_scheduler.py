#!/usr/bin/env python3
"""
Power Scheduling and Lifecycle Watchdog Service for PC Control Bot.
Handles delayed power-off countdowns, SHUTDOWN_OBSERVED confirmation loops, auto-LED cooling watchdog, and dashboard auto-refresh.
"""

import html
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


def get_app_state_lock(app):
    """Return the app's state_lock, creating a reusable fallback lock if omitted in mocks."""
    lock = getattr(app, "state_lock", None)
    if lock is None:
        lock = getattr(app, "_fallback_state_lock", None)
        if lock is None:
            lock = threading.RLock()
            app._fallback_state_lock = lock
    return lock


def is_pc_online_for_auto_led(app, sys_status, desk_status, session):
    """Single source of truth for whether the cooler Auto-LED should stay powered."""
    pc_online = (
        sys_status == "online"
        or desk_status == "online"
        or session in ("unlocked", "locked", "login_screen")
    )
    telem_age = None
    monitoring = getattr(app, "monitoring", None)
    if monitoring is not None and hasattr(monitoring, "telemetry_age"):
        telem_age = monitoring.telemetry_age()
        if isinstance(telem_age, (int, float)) and not isinstance(telem_age, bool) and telem_age > 45.0:
            if not (sys_status == "online" and session in ("login_screen", "locked")):
                pc_online = False
    return pc_online, telem_age


def sync_auto_led(app, pc_online, esp_just_reconnected=False, force_off=False, source="Auto-LED", detail=""):
    """Compute target Auto-LED state under lock, publish MQTT command outside lock, then commit state."""
    mqtt_gw = getattr(app, "mqtt", None)
    if not mqtt_gw or not hasattr(mqtt_gw, "publish_esp_action"):
        return

    with get_app_state_lock(app):
        user_led_pref = getattr(app, "user_led_state", "on")
        color = getattr(app, "user_led_color", None)
        current_state = getattr(app, "last_auto_led_state", None)
        desired_state = "off" if (force_off or user_led_pref == "off" or not pc_online) else "on"

        if not esp_just_reconnected and not force_off and current_state == desired_state:
            return

        epoch = getattr(app, "_auto_led_seq", 0) + 1
        app._auto_led_seq = epoch
        if desired_state == "off":
            action, value = "LED_OFF", None
        elif color:
            action, value = "LED_COLOR", color
        else:
            action, value = "LED_ON", None

    if action == "LED_COLOR":
        published = mqtt_gw.publish_esp_action("LED_COLOR", value=value)
    else:
        published = mqtt_gw.publish_esp_action(action)

    if not published:
        return

    with get_app_state_lock(app):
        if getattr(app, "_auto_led_seq", epoch) == epoch:
            app.last_auto_led_state = desired_state

    suffix = f" ({detail})" if detail else ""
    LOG.info("%s: -> %s%s", source, action, suffix)


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
        app_lock = get_app_state_lock(self)
        with app_lock:
            p_started = getattr(self, "pending_shutdown_started_at", None)
            s_offline = getattr(self, "shutdown_offline_detected_at", None)
            ack = getattr(self, "shutdown_acknowledged", False)

        telem_absent = (
            telem_age is None
            or telem_age >= 20.0
            or (p_started is not None and self.monitoring.latest_received_at < p_started)
        )
        both_offline = (
            self.mqtt.system_status in ("offline", "stale", "unknown")
            and self.mqtt.desktop_status in ("offline", "stale", "unknown")
        )

        if (
            ack
            and getattr(self.mqtt, "mqtt_online", False)
            and both_offline
            and telem_absent
        ):
            if s_offline is None:
                with app_lock:
                    self.shutdown_offline_detected_at = now
                LOG.info("Shutdown criteria met, starting sustain window (20s) at %s", now)
            elif (now - float(s_offline)) >= 20.0:
                LOG.info("Shutdown criteria sustained for >= 20s. Confirming SHUTDOWN_OBSERVED.")
                self.mqtt.health.set_shutdown_observed(ttl=3600.0, now=now)
                self._clear_pending_shutdown()
                return True
        else:
            if self.shutdown_offline_detected_at is not None:
                LOG.info("Shutdown criteria broken, resetting sustain window")
                self.shutdown_offline_detected_at = None

        return False

    def _notify_shutdown_confirmed(self, chat_id=None):
        target_user = chat_id or getattr(self, "allowed_user_id", ALLOWED_USER_ID)
        try:
            self.ui.send_message(
                target_user,
                "<b>✅ ПК перестал отвечать после принятой команды выключения.</b>",
                reply_markup=self._main_menu_markup(),
            )
        except Exception as e:
            LOG.warning("Failed to send shutdown confirmation message: %s", e)

    def _show_shutdown_schedule_confirmation(self, chat_id, target, message_id=None):
        self.shutdown_candidate_ts = target.timestamp()
        target_text = target.astimezone(MOSCOW).strftime("%d.%m.%Y в %H:%M МСК")
        markup = {
            "inline_keyboard": [
                [{"text": "Установить таймер", "style": "success", "callback_data": f"shutdown_schedule_confirm:{int(self.shutdown_candidate_ts)}"}],
                [{"text": "Отмена", "callback_data": "shutdown_pc"}],
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

    def _request_shutdown(self, chat_id, source, now=None, message_id=None):
        now = time.time() if now is None else float(now)
        back_markup = {"inline_keyboard": [self.ui.back_button("power_menu")]}
        if getattr(self, "pending_shutdown_cmd_id", None):
            self.ui.send_message(
                chat_id,
                "⏳ Команда выключения уже отправлена. Ожидаю подтверждение.",
                reply_markup=back_markup,
                message_id=message_id,
            )
            return False
        pc_state = self.mqtt.health.evaluate_pc_state(self.monitoring.telemetry_age(), now=now)
        if pc_state.state == "SHUTDOWN_OBSERVED":
            self.ui.send_message(
                chat_id,
                "ℹ️ ПК уже выключен (наблюдается).",
                reply_markup=self._power_menu_markup(),
                message_id=message_id,
            )
            return False
        if not getattr(self.mqtt, "mqtt_online", False):
            self.ui.send_message(
                chat_id,
                "❌ MQTT недоступен. Выключение не отправлено.",
                reply_markup=back_markup,
                message_id=message_id,
            )
            return False
        gen_id = getattr(type(self.mqtt), "generate_command_id", None)
        if self.mqtt.system_status == "online":
            publish_target = "system"
        elif getattr(self.mqtt, "desktop_status", None) == "online":
            publish_target = "desktop"
        else:
            self.ui.send_message(
                chat_id,
                f"❌ Агенты Windows сейчас недоступны (system: {self.mqtt.system_status}, desktop: {getattr(self.mqtt, 'desktop_status', 'offline')}). Выключение не отправлено.",
                reply_markup=self._power_menu_markup(),
                message_id=message_id,
            )
            return False

        if callable(gen_id):
            cmd_id = self.mqtt.generate_command_id("sys" if publish_target == "system" else "desk")
            self.pending_shutdown_cmd_id = cmd_id
            self.pending_shutdown_started_at = now
            self.shutdown_acknowledged = False
            self.shutdown_offline_detected_at = None
            if publish_target == "system":
                sent_id = self.mqtt.publish_system_action("shutdown", command_id=cmd_id)
            else:
                sent_id = self.mqtt.publish_desktop_action("shutdown", command_id=cmd_id)
            if hasattr(self.mqtt, "publish_esp_action"):
                self.mqtt.publish_esp_action("LED_OFF")
            if not sent_id:
                if self.pending_shutdown_cmd_id == cmd_id:
                    self._clear_pending_shutdown()
                self.ui.send_message(
                    chat_id,
                    "❌ MQTT недоступен. Выключение не отправлено.",
                    reply_markup=back_markup,
                    message_id=message_id,
                )
                return False
            if self.pending_shutdown_cmd_id == cmd_id:
                self.save_state()
        else:
            if publish_target == "system":
                cmd_id = self.mqtt.publish_system_action("shutdown")
            else:
                cmd_id = self.mqtt.publish_desktop_action("shutdown")
            if hasattr(self.mqtt, "publish_esp_action"):
                self.mqtt.publish_esp_action("LED_OFF")
            if not cmd_id:
                self.ui.send_message(
                    chat_id,
                    "❌ MQTT недоступен. Выключение не отправлено.",
                    reply_markup=back_markup,
                    message_id=message_id,
                )
                return False
            self.pending_shutdown_cmd_id = cmd_id
            self.pending_shutdown_started_at = now
            self.shutdown_acknowledged = False
            self.shutdown_offline_detected_at = None
            self.save_state()
        label = "по таймеру" if source == "schedule" else "вручную"
        self.ui.send_message(
            chat_id,
            f"<b>🛑 Команда выключения отправлена {label}.</b>\n\nЖду ответ Windows…",
            reply_markup=back_markup,
            message_id=message_id,
        )
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
            bot_username = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@").lower()
            if "pc control" in active_title or (bool(bot_username) and bot_username in active_title):
                in_pc_control = True

        refresh_interval = 5.0 if in_pc_control else 10.0
        if now - getattr(self, "last_dashboard_auto_refresh", 0.0) < refresh_interval:
            return

        self.last_dashboard_auto_refresh = now

        def _send_refresh():
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
            finally:
                self._dashboard_refresh_in_flight = False

        if getattr(self, "_dashboard_refresh_in_flight", False):
            return
        self._dashboard_refresh_in_flight = True
        executor = getattr(self, "_executor", None)
        if executor is not None and getattr(self, "async_dispatch", False):
            try:
                executor.submit(_send_refresh)
            except Exception:
                self._dashboard_refresh_in_flight = False
        else:
            _send_refresh()

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
        sys_status = getattr(self.mqtt, "system_status", "unknown")
        desk_status = getattr(self.mqtt, "desktop_status", "unknown")
        sys_session = getattr(self.mqtt, "system_session", "unknown")
        watchdog_pc_online, telem_age = is_pc_online_for_auto_led(self, sys_status, desk_status, sys_session)
        sync_auto_led(
            self,
            watchdog_pc_online,
            source="Auto-LED Watchdog",
            detail=f"telem_age={telem_age}",
        )

        timed_out_screenshots = []
        timed_out_console_cmds = []
        timed_out_ag = []
        timed_out_activity = None
        shutdown_timeout_acknowledged = None
        shutdown_confirmed_notify = False
        schedule_action = None

        app_lock = get_app_state_lock(self)
        with app_lock:
            for cmd_id, pending in list(getattr(self, "pending_screenshots", {}).items()):
                if now - pending.get("requested_at", 0) >= SCREENSHOT_TIMEOUT_SECONDS:
                    self.pending_screenshots.pop(cmd_id, None)
                    timed_out_screenshots.append(pending)

            for cmd_id, pending in list(getattr(self, "pending_console_cmds", {}).items()):
                started_at = pending.get("started_at", now) if isinstance(pending, dict) else now
                if now - started_at >= 60.0:
                    self.pending_console_cmds.pop(cmd_id, None)
                    timed_out_console_cmds.append(pending)

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
                    ag_pending = pending_ag.pop(cmd_id, None)
                    if ag_pending:
                        timed_out_ag.append(ag_pending)

            act_chat = getattr(self, "pending_activity_chat_id", None)
            act_ts = getattr(self, "pending_activity_requested_at", None)
            if act_chat and act_ts is not None and (now - float(act_ts) >= 15.0):
                act_msg = getattr(self, "pending_activity_msg_id", None)
                self.pending_activity_chat_id = None
                self.pending_activity_msg_id = None
                self.pending_activity_requested_at = None
                timed_out_activity = (act_chat, act_msg)

            p_cmd = getattr(self, "pending_shutdown_cmd_id", None)
            p_started = getattr(self, "pending_shutdown_started_at", None)
            if p_cmd and p_started:
                confirmed = self._check_shutdown_confirmation(now)
                if confirmed:
                    shutdown_confirmed_notify = True
                elif (now - float(p_started) >= SHUTDOWN_CONFIRM_TIMEOUT_SECONDS):
                    shutdown_timeout_acknowledged = getattr(self, "shutdown_acknowledged", False)
                    self._clear_pending_shutdown()

            sched_ts = getattr(self, "scheduled_shutdown_ts", None)
            last_attempt = getattr(self, "last_schedule_attempt_at", 0.0)
            if sched_ts and now >= sched_ts:
                if now - sched_ts > 600.0:
                    LOG.info("Discarding stale scheduled shutdown (scheduled_at=%s, now=%s)", sched_ts, now)
                    self.scheduled_shutdown_ts = None
                    schedule_action = "save_stale"
                elif now - last_attempt >= 15:
                    self.last_schedule_attempt_at = now
                    schedule_action = "evaluate"

        for pending in timed_out_screenshots:
            chat_id = pending.get("chat_id") if isinstance(pending, dict) else None
            if chat_id:
                try:
                    self.ui.send_message(chat_id, "❌ Скриншот не получен за 35 секунд. Попробуй ещё раз.")
                except Exception:
                    pass

        for pending in timed_out_console_cmds:
            if isinstance(pending, dict):
                msg_id = pending.get("message_id")
                chat_id = pending.get("chat_id")
                cmd_short = html.escape(str(pending.get("command", ""))[:40])
                fail_text = f"⏱ <b>Таймаут выполнения команды:</b>\n<code>{cmd_short}</code>\n\nАгент не прислал ответ за 60 секунд."
                if chat_id:
                    try:
                        self.ui.send_message(chat_id, fail_text, message_id=msg_id)
                    except Exception:
                        pass

        for ag_item in timed_out_ag:
            if isinstance(ag_item, tuple) and len(ag_item) >= 3:
                c_id, m_id, act = ag_item
                action_name = {
                    "list": "запрос списка профилей",
                    "switch": "переключение профиля",
                    "restart": "перезапуск IDE",
                    "heal": "лечение Antigravity & VPN",
                    "export": "экспорт аккаунтов",
                }.get(act, "операция с Antigravity")
                try:
                    back_target = "antigravity_menu" if m_id else "menu"
                    self.ui.send_message(
                        c_id,
                        f"⏱ <b>Таймаут: {action_name}</b>\n\nDesktop-агент не прислал ответ за 60 секунд.",
                        reply_markup={"inline_keyboard": [self.ui.back_button(back_target)]},
                        message_id=m_id,
                    )
                except Exception:
                    pass

        if timed_out_activity:
            act_chat, act_msg = timed_out_activity
            try:
                self.ui.send_message(
                    act_chat,
                    "⏱ <b>Desktop-агент не прислал статистику активности вовремя.</b>",
                    reply_markup={"inline_keyboard": [self.ui.back_button("menu")]},
                    message_id=act_msg,
                )
            except Exception:
                LOG.exception("Failed to send activity timeout notification")

        if shutdown_confirmed_notify:
            self._notify_shutdown_confirmed()

        if shutdown_timeout_acknowledged is not None:
            emerg_btn = {"text": "⚡ Аварийное выключение (ESP)", "style": "danger", "callback_data": "emergency_power_off"}
            if ICONS.get("power"):
                emerg_btn["icon_custom_emoji_id"] = ICONS.get("power")
            timeout_markup = {
                "inline_keyboard": [
                    [emerg_btn],
                    self.ui.back_button("power_menu"),
                ]
            }
            if shutdown_timeout_acknowledged:
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

        if schedule_action == "save_stale":
            self.save_state()
            return

        if schedule_action == "evaluate":
            pc_state = self.mqtt.health.evaluate_pc_state(self.monitoring.telemetry_age(), now=now)
            if pc_state.state == "SHUTDOWN_OBSERVED":
                with app_lock:
                    self.scheduled_shutdown_ts = None
                self.save_state()
                self.ui.send_message(target_user, "ℹ️ Таймер сработал, но ПК уже был выключен (наблюдается).")
                return
            if self.mqtt.system_status != "online" and getattr(self.mqtt, "desktop_status", None) != "online":
                return
            if self._request_shutdown(target_user, source="schedule", now=now):
                with app_lock:
                    self.scheduled_shutdown_ts = None
                self.save_state()

    def scheduler_loop(self):
        while getattr(self, "running", True):
            try:
                self.scheduler_tick()
            except Exception:
                LOG.exception("Shutdown scheduler tick failed")
            time.sleep(1)
