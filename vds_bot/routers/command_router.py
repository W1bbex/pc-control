#!/usr/bin/env python3
"""Command Router: parses and executes slash-commands and text messages."""

from datetime import datetime, timedelta
import logging

try:
    from zoneinfo import ZoneInfo
    MOSCOW = ZoneInfo("Europe/Moscow")
except Exception:
    from datetime import timezone
    MOSCOW = timezone(timedelta(hours=3))

LOG = logging.getLogger("pc-control-bot.routers.command")


def _get_allowed_user_id(app) -> int:
    val = getattr(app, "allowed_user_id", None)
    if val is not None:
        return val
    try:
        from vds_bot.bot import ALLOWED_USER_ID
        return ALLOWED_USER_ID
    except ImportError:
        try:
            import bot
            return getattr(bot, "ALLOWED_USER_ID", 0)
        except Exception:
            return 0


def route_message(app, message: dict) -> None:
    """Handle incoming Telegram text message or slash command."""
    chat_id = message["chat"]["id"]
    chat_type = message.get("chat", {}).get("type", "private")
    from_id = message.get("from", {}).get("id")
    text = message.get("text", "").strip()
    allowed_id = _get_allowed_user_id(app)

    # Group chats support: only allow authorized user to trigger player
    if chat_type != "private":
        if from_id != allowed_id:
            return
        if text.lower().startswith(("/player", "плеер")):
            incoming_msg_id = message.get("message_id")
            if incoming_msg_id:
                try:
                    app.ui.delete_message(chat_id, incoming_msg_id)
                except Exception:
                    pass
            media_state = getattr(app, "latest_media_state", None) or getattr(app.mqtt, "last_media_state", {})
            app._send_player_card(chat_id, media_state, business_connection_id=None)
            app.mqtt.send_media_command("refresh")
        return

    if chat_id != allowed_id:
        app.ui.send_message(chat_id, "Доступ запрещён.")
        return

    lower_text = text.lower().strip()

    try:
        from .antigravity_router import route_antigravity_command
    except ImportError:
        try:
            from routers.antigravity_router import route_antigravity_command
        except ImportError:
            from vds_bot.routers.antigravity_router import route_antigravity_command

    if route_antigravity_command(app, chat_id, text):
        return

    # 1. Main Menu / Dashboard
    if lower_text in {"/start", "/menu", "меню", "меню пк"}:
        app.console_active_chat_id = None
        res = app.ui.send_message(
            chat_id,
            app.monitoring.dashboard_text(app.latest_media_state or getattr(app.mqtt, "last_media_state", {})),
            reply_markup=app._main_menu_markup(),
        )
        if res and isinstance(res, dict) and "message_id" in res:
            app.active_main_menu_message_id = res["message_id"]
            app.current_menu_view = "main"
            app.save_state()
        return

    # 2. Console (Open/Exit)
    if lower_text in {"/console", "консоль"}:
        app.console_active_chat_id = chat_id
        app.current_menu_view = "console"
        text_card = app.ui.console_card_text(target=app.console_target, shell=app.console_shell)
        markup = app.ui.console_menu_markup(target=app.console_target, shell=app.console_shell)
        app.ui.send_message(chat_id, text_card, reply_markup=markup)
        return

    if lower_text in {"/exit", "выход"}:
        if app.console_active_chat_id == chat_id:
            app.console_active_chat_id = None
            app.current_menu_view = "main"
            app.ui.send_message(chat_id, "🚪 <b>Консоль закрыта.</b>", reply_markup=app._main_menu_markup())
            return

    # 3. Direct CMD & PowerShell execution
    if lower_text.startswith("/cmd"):
        parts = text.split(None, 1)
        if len(parts) > 1 and parts[1].strip():
            app.execute_console_command(chat_id, parts[1].strip(), target=app.console_target, shell="cmd")
        else:
            app.ui.send_message(chat_id, "ℹ️ Использование: <code>/cmd &lt;команда&gt;</code>\nНапример: <code>/cmd dir C:\\</code>")
        return

    if lower_text.startswith("/ps"):
        parts = text.split(None, 1)
        if len(parts) > 1 and parts[1].strip():
            app.execute_console_command(chat_id, parts[1].strip(), target=app.console_target, shell="powershell")
        else:
            app.ui.send_message(chat_id, "ℹ️ Использование: <code>/ps &lt;команда&gt;</code>\nНапример: <code>/ps Get-Process</code>")
        return

    # 4. Hardware ESC key simulation
    if lower_text.startswith(("/esc", "esc")):
        parts = lower_text.split()
        count = 1
        if len(parts) > 1 and parts[1].isdigit():
            count = max(1, min(int(parts[1]), 20))
        app.mqtt.publish_desktop_action("press_esc", {"count": count})
        app.ui.send_message(chat_id, f"⚡ Отправлено нажатие <b>ESC</b> ({count}x) на ПК.")
        return

    # 5. Stealth Mode (Blackout)
    if lower_text.startswith(("/stealth", "скрытность")):
        parts = lower_text.split()
        subcmd = parts[1] if len(parts) > 1 else ""
        if subcmd in ("on", "1", "вкл", "включить"):
            app.mqtt.publish_desktop_action("stealth_mode", {"state": "on"})
            msg = app.ui.send_message(
                chat_id,
                "<b>🕶️ Режим скрытности активирован</b>\n\n"
                "• Мониторы переведены в спящий режим\n"
                "• Физический ввод мыши и клавиатуры поглощается\n"
                "• Системный звук заглушен\n\n"
                "<i>Снятие режима: /stealth off или кнопка ниже.\n"
                "Локальный выход: <code>Win+W+1</code></i>",
                reply_markup=app.ui.stealth_menu_markup(active=True),
            )
            if isinstance(msg, dict) and msg.get("message_id"):
                app.active_stealth_card = (chat_id, msg["message_id"])
        elif subcmd in ("off", "0", "выкл", "отключить"):
            app.mqtt.publish_desktop_action("stealth_mode", {"state": "off"})
            app.active_stealth_card = None
            app.ui.send_message(
                chat_id,
                "<b>🔓 Режим скрытности отключен</b>\n\n"
                "• Дисплеи разбужены\n"
                "• Ввод мыши и клавиатуры восстановлен\n"
                "• Звук восстановлен",
                reply_markup=app.ui.stealth_menu_markup(active=False),
            )
        else:
            is_active = bool(app.monitoring.latest_telemetry.get("stealth_mode")) if app.monitoring.latest_telemetry else False
            status_label = "🟢 АКТИВЕН" if is_active else "⚪ ВЫКЛЮЧЕН"
            msg = app.ui.send_message(
                chat_id,
                f"<b>🕶️ Режим скрытности (Blackout Mode)</b>\n\n"
                f"Текущий статус: <b>{status_label}</b>\n\n"
                "Режим гасит мониторы, заглушает звук и поглощает все клики мыши и нажатия клавиш («Черная дыра»).\n"
                "Локальный выход доступен по комбинации <code>Win+W+1</code>.",
                reply_markup=app.ui.stealth_menu_markup(active=is_active),
            )
            if is_active and isinstance(msg, dict) and msg.get("message_id"):
                app.active_stealth_card = (chat_id, msg["message_id"])
        return

    # 6. Interactive terminal line input
    if getattr(app, "console_active_chat_id", None) == chat_id and not text.startswith("/"):
        app.execute_console_command(chat_id, text, target=app.console_target, shell=app.console_shell)
        return

    # 7. Media player
    if lower_text.startswith("/player") or lower_text == "плеер":
        incoming_msg_id = message.get("message_id")
        if incoming_msg_id:
            try:
                app.ui.delete_message(chat_id, incoming_msg_id)
            except Exception:
                pass
        media_state = getattr(app, "latest_media_state", None) or getattr(app.mqtt, "last_media_state", {})
        app._send_player_card(chat_id, media_state, business_connection_id=None)
        app.mqtt.send_media_command("refresh")
        return

    # 8. Activity summary
    if lower_text.startswith("/activity") or lower_text == "активность":
        today_str = datetime.now(MOSCOW).strftime("%Y-%m-%d")
        app.pending_activity_chat_id = chat_id
        app.mqtt.query_activity("summary", today_str)
        app.ui.send_message(chat_id, f"📊 Запрашиваю статистику за {today_str}...")
        return

    # 9. Legacy LED commands (backward compatibility)
    if lower_text.startswith(("/led ", "подсветка ")):
        if getattr(app.mqtt, "esp_status", None) != "online":
            app.ui.send_message(chat_id, "❌ Модуль управления кулером (ESP8266) не в сети!")
            return
        parts = lower_text.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        try:
            from .monitoring_router import LED_WHITE_PRESETS
        except ImportError:
            try:
                from routers.monitoring_router import LED_WHITE_PRESETS
            except ImportError:
                from vds_bot.routers.monitoring_router import LED_WHITE_PRESETS
        if arg in LED_WHITE_PRESETS:
            rgb, name = LED_WHITE_PRESETS[arg]
            app.mqtt.publish_esp_action("LED_COLOR", value=rgb)
            app.ui.send_message(chat_id, f"💡 Выбран оттенок #{arg}: {name} (<code>{rgb}</code>)")
            return

    if lower_text in {"/led_on", "включить подсветку"}:
        if getattr(app.mqtt, "esp_status", None) != "online":
            app.ui.send_message(chat_id, "❌ Модуль управления кулером (ESP8266) не в сети!")
            return
        app.mqtt.publish_esp_action("LED_ON")
        app.ui.send_message(chat_id, "💡 Подсветка кулера включена")
        return

    if lower_text in {"/led_off", "выключить подсветку"}:
        if getattr(app.mqtt, "esp_status", None) != "online":
            app.ui.send_message(chat_id, "❌ Модуль управления кулером (ESP8266) не в сети!")
            return
        app.mqtt.publish_esp_action("LED_OFF")
        app.ui.send_message(chat_id, "🌑 Подсветка кулера выключена")
        return

    # 10. Scheduled Shutdown
    time_text = text
    if text.lower().startswith("/shutdown "):
        time_text = text.split(None, 1)[1].strip()
    if getattr(app, "awaiting_shutdown_time_chat_id", None) == chat_id or text.lower().startswith("/shutdown "):
        try:
            hour_text, minute_text = time_text.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except (ValueError, TypeError):
            app.ui.send_message(chat_id, "❌ Введи время как <code>HH:MM</code>, например <code>23:45</code>.")
            return

        now = datetime.now(MOSCOW)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        app.awaiting_shutdown_time_chat_id = None
        app._show_shutdown_schedule_confirmation(chat_id, target)
        return
