#!/usr/bin/env python3
"""
MQTT Event Dispatcher and Message Handler for PC Control Bot.
Processes incoming telemetry, screenshot frames, action ACKs, and hardware events.
"""

import html
import json
import logging
import os
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

LOG = logging.getLogger("pc-control-bot.mqtt_dispatcher")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "0"))


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


class MqttDispatcherMixin:
    """
    Mixin for PcControlBotApp providing MQTT event routing and handling.
    """

    def _on_mqtt_event(self, event_type, data):
        target_user = getattr(self, "allowed_user_id", ALLOWED_USER_ID)

        if event_type in {"system_status", "health_update"}:
            session = data.get("session") or data.get("system_session")
            LOG.info("_on_mqtt_event %s: system_session=%s (system_status=%s)", event_type, session, data.get("system_status"))
            self.power_flow.on_system_session_update(session, target_user)
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
            self.physical_btn.handle_event(data, target_user)

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
                    pending = {"chat_id": target_user, "requested_at": time.time()}
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
                self.ui.send_message(target_user, "❌ Ошибка парсинга скриншота от desktop-агента.")

        elif event_type == "command_timeout":
            cmd_id = data.get("id")
            pending = getattr(self, "pending_screenshots", {}).pop(cmd_id, None)
            if pending:
                self.ui.send_message(pending["chat_id"], "❌ Desktop-агент не прислал скриншот за 30 секунд.")
            getattr(self, "pending_telemetry_cmd_ids", {}).pop(cmd_id, None)
            if cmd_id == getattr(self, "pending_shutdown_cmd_id", None):
                self.ui.send_message(
                    target_user,
                    "<b>❌ Выключение не подтверждено.</b>\n\nСистемный агент не ответил. ПК мог остаться включённым.",
                    reply_markup=self._power_menu_markup(),
                )
                self._clear_pending_shutdown()
            if getattr(self.power_flow, "pending_unlock_command_id", None) and cmd_id == self.power_flow.pending_unlock_command_id:
                self.power_flow.pending_unlock_command_id = None
                self.power_flow.on_unlock_failed(self.power_flow.chat_id or target_user, "Таймаут ожидания ответа от агента при разблокировке.")
            if getattr(self.physical_btn, "pending_command_id", None) and cmd_id == self.physical_btn.pending_command_id:
                self.physical_btn.pending_command_id = None
                self.ui.send_message(target_user, "❌ Таймаут ожидания ответа от ESP.")
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
            self.physical_btn.handle_ack(data, target_user, self.save_state)

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
                        target_user,
                        "<b>✅ Windows приняла команду выключения.</b>\n\nОжидаю подтверждение, что ПК действительно перешёл в offline…",
                    )
                elif result not in {"progress"}:
                    self.ui.send_message(
                        target_user,
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
                        self.ui.send_message(target_user, alert_text)
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
