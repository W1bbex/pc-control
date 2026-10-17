#!/usr/bin/env python3
"""Power Router: handles callbacks for power, locks, displays, stealth, sleep, reboot, and shutdown."""

from datetime import datetime, timedelta
import logging

try:
    from zoneinfo import ZoneInfo
    MOSCOW = ZoneInfo("Europe/Moscow")
except Exception:
    from datetime import timezone
    MOSCOW = timezone(timedelta(hours=3))

LOG = logging.getLogger("pc-control-bot.routers.power")


def route_power_callback(app, data: str, chat_id: int, message_id: int, callback_id: str) -> bool:
    """Handle power-related callback queries. Returns True if handled, False otherwise."""
    # 1. Power Menu Navigation
    if data == "power_menu":
        app.current_menu_view = "power_menu"
        app.awaiting_shutdown_time_chat_id = None
        app.ui.answer_callback(callback_id)
        pc_state = app.mqtt.health.evaluate_pc_state(app.monitoring.telemetry_age())
        text = (
            app.ui.power_menu_text(
                pc_state=pc_state,
                session=app.mqtt.system_session,
                desktop_status=app.mqtt.desktop_status,
                esp_status=app.mqtt.esp_status,
            )
            if hasattr(app.ui, "power_menu_text")
            else ""
        )
        if not isinstance(text, str) or not text:
            text = f"<b>ПК и экран</b>\n\n<b>Состояние:</b> {getattr(pc_state, 'label', 'Готов к работе')}"
        app.ui.send_message(chat_id, text, reply_markup=app._power_menu_markup(), message_id=message_id)
        return True

    # 2. Power On
    if data == "power_on":
        stage = getattr(getattr(app, "power_flow", None), "stage", None)
        if stage == "POWERING_ON":
            app.ui.answer_callback(callback_id, "Компьютер уже запускается...", alert=True)
            return True

        pc_state = app.mqtt.health.evaluate_pc_state(app.monitoring.telemetry_age())
        state_str = getattr(getattr(pc_state, "state", None), "value", str(getattr(pc_state, "state", "")))
        if (
            state_str in ("READY", "DEGRADED", "LOCKED", "BOOTING")
            or app.mqtt.system_status == "online"
            or app.mqtt.desktop_status == "online"
        ):
            app.ui.answer_callback(callback_id, "Компьютер уже включен!", alert=True)
            return True

        if getattr(app.mqtt, "esp_status", None) != "online":
            app.ui.answer_callback(callback_id, "❌ Модуль питания (ESP8266) не в сети!", alert=True)
            return True

        app.physical_btn.revoke_morse_eligibility()
        app.save_state()
        app.ui.answer_callback(callback_id, "Запускаю ПК...")
        app.power_flow.start_power_on(chat_id)
        return True

    # 3. Manual Actions (Lock / Unlock / Screen / Minimize)
    if data == "unlock_windows_manual":
        pc_state = app.mqtt.health.evaluate_pc_state(app.monitoring.telemetry_age())
        if pc_state.state in ("SHUTDOWN_OBSERVED", "UNREACHABLE") or app.mqtt.system_status != "online":
            app.ui.answer_callback(callback_id, "ПК недоступен. Сначала включи его.", alert=True)
            return True
        app.ui.answer_callback(callback_id, "Разблокирую...")
        app.power_flow.start_manual_unlock(chat_id, message_id=message_id)
        return True

    if data == "lock_windows":
        app.ui.answer_callback(callback_id, "Блокирую...")
        app.mqtt.publish_desktop_action("lock")
        app.mqtt.publish_system_action("lock")
        return True

    if data == "toggle_screen":
        app.ui.answer_callback(callback_id, "Переключаю экран...")
        app.mqtt.publish_desktop_action("screen_off")
        return True

    if data == "minimize_windows":
        app.ui.answer_callback(callback_id, "Сворачиваю окна...")
        app.mqtt.publish_desktop_action("minimize_all")
        return True

    # 4. Stealth Mode (Blackout)
    if data in ("toggle_stealth", "stealth_toggle"):
        app.ui.answer_callback(callback_id, "Переключаю режим скрытности...")
        app.mqtt.publish_desktop_action("stealth_mode", {"state": "toggle"})
        return True

    if data == "stealth_on":
        app.ui.answer_callback(callback_id, "Включаю режим скрытности...")
        app.mqtt.publish_desktop_action("stealth_mode", {"state": "on"})
        msg = app.ui.send_message(
            chat_id,
            "<b>🕶️ Режим скрытности активирован</b>\n\n"
            "• Мониторы переведены в сон\n"
            "• Поглощение 100% кликов и клавиш («Черная дыра»)\n"
            "• Системный звук заглушен\n\n"
            "<i>Для отключения отправь /stealth off или нажми кнопку ниже.\n"
            "Локальный выход: <code>Win+W+1</code></i>",
            reply_markup=app.ui.stealth_menu_markup(active=True),
        )
        if isinstance(msg, dict) and msg.get("message_id"):
            app.active_stealth_card = (chat_id, msg["message_id"])
        elif message_id:
            app.active_stealth_card = (chat_id, message_id)
        return True

    if data == "stealth_off":
        app.ui.answer_callback(callback_id, "Отключаю режим скрытности...")
        app.mqtt.publish_desktop_action("stealth_mode", {"state": "off"})
        app.active_stealth_card = None
        app.ui.send_message(
            chat_id,
            "<b>🔓 Режим скрытности отключен</b>\n\n"
            "• Дисплеи разбужены\n"
            "• Поглощение ввода снято\n"
            "• Звук восстановлен",
            reply_markup=app.ui.stealth_menu_markup(active=False),
            message_id=message_id,
        )
        return True

    # 5. Sleep & Hibernate
    if data == "sleep_pc":
        app.ui.answer_callback(callback_id)
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Да, перевести в сон", "style": "primary", "callback_data": "sleep_confirm"},
                    {"text": "✖ Отмена", "callback_data": "menu"},
                ]
            ]
        }
        app.ui.send_message(
            chat_id,
            "<b>💤 Перевод ПК в спящий режим</b>\n\nВы уверены?",
            reply_markup=markup,
            message_id=message_id,
        )
        return True

    if data == "sleep_confirm":
        app.ui.answer_callback(callback_id, "Отправляю в сон...")
        if app.mqtt.system_status == "online":
            app.mqtt.publish_system_action("sleep")
        elif getattr(app.mqtt, "desktop_status", None) == "online":
            app.mqtt.publish_desktop_action("sleep")
        if hasattr(app.mqtt, "publish_esp_action"):
            app.mqtt.publish_esp_action("LED_OFF")
        return True

    # 6. Reboot
    if data == "reboot_pc":
        app.current_menu_view = "reboot_menu"
        app.ui.answer_callback(callback_id)
        app.ui.send_message(
            chat_id,
            "<b>🔄 Перезагрузка ПК</b>\n\nВыберите тип перезагрузки:\n\n"
            "• <b>Перезагрузка + Разблок</b> — ПК перезагрузится, дождётся экрана входа, введёт пароль и предложит рабочий сетап.\n"
            "• <b>Только перезагрузка</b> — обычный рестарт без авторазблокировки.",
            reply_markup=app.ui.reboot_menu_markup(),
            message_id=message_id,
        )
        return True

    if data == "reboot_unlock":
        if app.mqtt.system_status != "online" and getattr(app.mqtt, "desktop_status", None) != "online":
            app.ui.answer_callback(callback_id, "ПК не в сети. Перезагрузка невозможна.", alert=True)
            return True
        app.ui.answer_callback(callback_id, "Перезагружаю с авторазблоком...")
        if app.mqtt.system_status == "online":
            app.mqtt.publish_system_action("restart")
        elif getattr(app.mqtt, "desktop_status", None) == "online":
            app.mqtt.publish_desktop_action("restart")
        if hasattr(app.mqtt, "publish_esp_action"):
            app.mqtt.publish_esp_action("LED_OFF")
        app.power_flow.start_reboot_flow(chat_id, message_id=message_id, unlock=True)
        return True

    if data == "reboot_confirm":
        if app.mqtt.system_status != "online" and getattr(app.mqtt, "desktop_status", None) != "online":
            app.ui.answer_callback(callback_id, "ПК не в сети. Перезагрузка невозможна.", alert=True)
            return True
        app.ui.answer_callback(callback_id, "Перезагружаю...")
        if app.mqtt.system_status == "online":
            app.mqtt.publish_system_action("restart")
        elif getattr(app.mqtt, "desktop_status", None) == "online":
            app.mqtt.publish_desktop_action("restart")
        if hasattr(app.mqtt, "publish_esp_action"):
            app.mqtt.publish_esp_action("LED_OFF")
        app.power_flow.start_reboot_flow(chat_id, message_id=message_id, unlock=False)
        return True

    if data == "flow_reboot_cancel":
        app.ui.answer_callback(callback_id, "Ожидание отменено")
        app.power_flow.reset_flow()
        app.ui.send_message(
            chat_id,
            "<b>Ожидание перезагрузки отменено.</b>",
            reply_markup=app._power_menu_markup(),
            message_id=message_id,
        )
        return True

    # 7. Shutdown
    if data == "shutdown_pc":
        app.current_menu_view = "shutdown_menu"
        app.awaiting_shutdown_time_chat_id = None
        app.ui.answer_callback(callback_id)
        app.ui.send_message(
            chat_id,
            "<b>Выключение ПК</b>\n\nВыключить сейчас или запланировать по московскому времени?",
            reply_markup=app.ui.shutdown_menu_markup(app.scheduled_shutdown_ts),
            message_id=message_id,
        )
        return True

    if data == "shutdown_now_confirm":
        app.ui.answer_callback(callback_id)
        app._request_shutdown(chat_id, source="manual")
        return True

    if data == "shutdown_at_time":
        app.ui.answer_callback(callback_id)
        app.awaiting_shutdown_time_chat_id = chat_id
        app.ui.send_message(
            chat_id,
            "<b>Выключение по времени</b>\n\nОтправь время в формате <code>HH:MM</code>. Часовой пояс — Москва.\nНапример: <code>23:45</code>",
            reply_markup={"inline_keyboard": [app.ui.back_button("shutdown_pc")]},
            message_id=message_id,
        )
        return True

    if data.startswith("shutdown_after:"):
        try:
            minutes = int(data.split(":", 1)[1])
        except ValueError:
            app.ui.answer_callback(callback_id, "Некорректный таймер", alert=True)
            return True
        app.ui.answer_callback(callback_id)
        target = datetime.now(MOSCOW) + timedelta(minutes=minutes)
        app._show_shutdown_schedule_confirmation(chat_id, target, message_id)
        return True

    if data.startswith("shutdown_schedule_confirm:"):
        try:
            target_ts = float(data.rsplit(":", 1)[1])
        except ValueError:
            app.ui.answer_callback(callback_id, "Некорректное время", alert=True)
            return True
        app.ui.answer_callback(callback_id, "Таймер установлен")
        app._set_shutdown_schedule(chat_id, target_ts, message_id)
        return True

    if data == "shutdown_schedule_cancel":
        app.ui.answer_callback(callback_id, "Таймер отменён")
        app.scheduled_shutdown_ts = None
        app.shutdown_candidate_ts = None
        app.save_state()
        app.ui.send_message(
            chat_id,
            "<b>Таймер выключения отменён.</b>",
            reply_markup=app.ui.shutdown_menu_markup(),
            message_id=message_id,
        )
        return True

    # 8. Emergency Power Off (ESP8266 FORCE_OFF)
    if data == "emergency_power_off":
        if getattr(app.mqtt, "esp_status", None) != "online":
            app.ui.answer_callback(callback_id, "❌ Модуль питания (ESP8266) не в сети!", alert=True)
            return True

        app.ui.answer_callback(callback_id)
        text = (
            app.ui.emergency_power_off_confirm_text()
            if hasattr(app.ui, "emergency_power_off_confirm_text")
            else "<b>⚠️ Аварийное выключение ПК</b>\n\nЗажать кнопку питания на 5 секунд через ESP8266?"
        )
        markup = (
            app.ui.emergency_power_off_confirm_markup()
            if hasattr(app.ui, "emergency_power_off_confirm_markup")
            else {
                "inline_keyboard": [
                    [{"text": "⚡ Да, выключить принудительно", "style": "danger", "callback_data": "emergency_power_off_confirm"}],
                    [{"text": "✖ Отмена", "callback_data": "power_menu"}],
                ]
            }
        )
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    if data == "emergency_power_off_confirm":
        if getattr(app.mqtt, "esp_status", None) != "online":
            app.ui.answer_callback(callback_id, "❌ Модуль питания (ESP8266) не в сети!", alert=True)
            return True

        app.ui.answer_callback(callback_id, "Выполняю аварийное выключение...")
        LOG.warning("Emergency power off triggered by chat_id=%s via ESP8266 FORCE_OFF", chat_id)

        # Send FORCE_OFF command (holds relay pin for 5000ms)
        app.mqtt.publish_esp_action("FORCE_OFF")
        # Send LED_OFF to cut cooler power
        if hasattr(app.mqtt, "publish_esp_action"):
            app.mqtt.publish_esp_action("LED_OFF")

        # Reset any ongoing power flows
        if hasattr(app, "power_flow"):
            app.power_flow.reset_flow()

        text = (
            "<b>⚡ Аварийное выключение выполнено</b>\n\n"
            "Команда удержания кнопки питания (5 секунд) отправлена на ESP8266.\n"
            "Питание ПК принудительно отключено."
        )
        back_markup = {"inline_keyboard": [app.ui.back_button("menu")]}
        app.ui.send_message(chat_id, text, reply_markup=back_markup, message_id=message_id)
        return True

    return False
