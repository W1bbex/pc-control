#!/usr/bin/env python3
"""Command Router: parses and executes slash-commands and text messages."""

from datetime import datetime, timedelta
import logging
import time

try:
    from zoneinfo import ZoneInfo
    MOSCOW = ZoneInfo("Europe/Moscow")
except Exception:
    from datetime import timezone
    MOSCOW = timezone(timedelta(hours=3))

LOG = logging.getLogger("pc-control-bot.routers.command")

try:
    from vds_bot.action_helpers import apply_led_power, apply_led_preset, apply_stealth_state
except ImportError:
    from action_helpers import apply_led_power, apply_led_preset, apply_stealth_state


def _handle_player_command(app, chat_id: int, incoming_msg_id=None) -> None:
    if incoming_msg_id:
        try:
            app.ui.delete_message(chat_id, incoming_msg_id)
        except Exception:
            pass
    media_state = getattr(app, "latest_media_state", None) or getattr(app.mqtt, "last_media_state", {})
    app._send_player_card(chat_id, media_state, business_connection_id=None)
    app.mqtt.send_media_command("refresh")


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
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return
    chat_id = chat.get("id")
    if not chat_id:
        return
    chat_type = chat.get("type", "private")
    from_id = message.get("from", {}).get("id") if isinstance(message.get("from"), dict) else None
    text = (message.get("text") or "").strip()
    allowed_id = _get_allowed_user_id(app)

    # Group chats support: only allow authorized user to trigger player
    if chat_type != "private":
        if from_id != allowed_id:
            return
        if text.lower().startswith(("/player", "плеер")):
            _handle_player_command(app, chat_id, message.get("message_id"))
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
        app.awaiting_shutdown_time_chat_id = None
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
        app.awaiting_shutdown_time_chat_id = None
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
            res = app.ui.send_message(chat_id, "🚪 <b>Консоль закрыта.</b>", reply_markup=app._main_menu_markup())
            if res and isinstance(res, dict) and "message_id" in res:
                app.active_main_menu_message_id = res["message_id"]
            app.save_state()
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
    cmd_parts = lower_text.split()
    if cmd_parts and cmd_parts[0] in ("/esc", "esc"):
        count = 1
        if len(cmd_parts) > 1 and cmd_parts[1].isdigit():
            count = max(1, min(int(cmd_parts[1]), 20))
        app.mqtt.publish_desktop_action("press_esc", {"count": count})
        app.ui.send_message(chat_id, f"⚡ Отправлено нажатие <b>ESC</b> ({count}x) на ПК.")
        return

    # 5. Stealth Mode (Blackout)
    if lower_text.startswith(("/stealth", "скрытность")):
        parts = lower_text.split()
        subcmd = parts[1] if len(parts) > 1 else ""
        if subcmd in ("on", "1", "вкл", "включить"):
            apply_stealth_state(app, chat_id, "on", is_command=True)
        elif subcmd in ("off", "0", "выкл", "отключить"):
            apply_stealth_state(app, chat_id, "off", is_command=True)
        else:
            apply_stealth_state(app, chat_id, "status", is_command=True)
        return

    # 6. Scheduled Shutdown
    time_text = text.strip()
    if getattr(app, "awaiting_shutdown_time_chat_id", None) == chat_id:
        lower_txt = time_text.lower()
        if lower_txt in ("/cancel", "отмена", "назад", "cancel"):
            app.awaiting_shutdown_time_chat_id = None
            app.ui.send_message(chat_id, "Ввод времени отменён.", reply_markup=app._main_menu_markup())
            return
        if lower_txt.startswith("/") and not lower_txt.startswith("/shutdown"):
            # User sent another slash command; abort awaiting time so the command can run
            app.awaiting_shutdown_time_chat_id = None

    if lower_text == "/shutdown":
        app.awaiting_shutdown_time_chat_id = None
        app.ui.send_message(
            chat_id,
            "<b>Выключение ПК</b>\n\nВыключить сейчас или запланировать по московскому времени?",
            reply_markup=app.ui.shutdown_menu_markup(app.scheduled_shutdown_ts),
        )
        return

    if text.lower().startswith("/shutdown "):
        time_text = text.split(None, 1)[1].strip()

    if getattr(app, "awaiting_shutdown_time_chat_id", None) == chat_id or text.lower().startswith("/shutdown "):
        try:
            hour_text, minute_text = time_text.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except (ValueError, TypeError):
            app.ui.send_message(chat_id, "❌ Введи время как <code>HH:MM</code>, например <code>23:45</code> (или напиши «отмена»).")
            return

        now = datetime.now(MOSCOW)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        app.awaiting_shutdown_time_chat_id = None
        app._show_shutdown_schedule_confirmation(chat_id, target)
        return

    # 7. Media player
    if lower_text.startswith("/player") or lower_text == "плеер":
        _handle_player_command(app, chat_id, message.get("message_id"))
        return

    # 8. Activity summary
    if lower_text.startswith("/activity") or lower_text == "активность":
        if getattr(app.mqtt, "desktop_status", None) != "online":
            app.ui.send_message(
                chat_id,
                "⚠️ <b>Desktop-агент не в сети</b>\nСтатистика активности недоступна при выключенном ПК.",
            )
            return
        today_str = datetime.now(MOSCOW).strftime("%Y-%m-%d")
        app.pending_activity_chat_id = chat_id
        app.pending_activity_requested_at = time.time()
        sent = app.mqtt.query_activity("summary", today_str)
        if not sent:
            app.pending_activity_chat_id = None
            app.pending_activity_requested_at = None
            app.ui.send_message(chat_id, "❌ <b>Не удалось запросить активность:</b> MQTT недоступен.")
            return
        res = app.ui.send_message(chat_id, f"📊 Запрашиваю статистику за {today_str}...")
        if res and isinstance(res, dict) and "message_id" in res:
            app.pending_activity_msg_id = res["message_id"]
        else:
            app.pending_activity_msg_id = None
        return

    # 9. Legacy LED commands (backward compatibility)
    if lower_text.startswith(("/led ", "подсветка ")):
        if getattr(app.mqtt, "esp_status", None) != "online":
            app.ui.send_message(chat_id, "❌ Модуль управления кулером (ESP8266) не в сети!")
            return
        parts = lower_text.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        applied = apply_led_preset(app, arg)
        if applied:
            rgb, name, _ = applied
            app.ui.send_message(chat_id, f"💡 Выбран оттенок #{arg}: {name} (<code>{rgb}</code>)")
            return

    if lower_text in {"/led_on", "включить подсветку"}:
        if getattr(app.mqtt, "esp_status", None) != "online":
            app.ui.send_message(chat_id, "❌ Модуль управления кулером (ESP8266) не в сети!")
            return
        cmd_id = apply_led_power(app, True)
        if cmd_id:
            app.ui.send_message(chat_id, "💡 Подсветка кулера включена")
        else:
            app.ui.send_message(chat_id, "❌ Не удалось отправить команду: MQTT недоступен.")
        return

    if lower_text in {"/led_off", "выключить подсветку"}:
        if getattr(app.mqtt, "esp_status", None) != "online":
            app.ui.send_message(chat_id, "❌ Модуль управления кулером (ESP8266) не в сети!")
            return
        cmd_id = apply_led_power(app, False)
        if cmd_id:
            app.ui.send_message(chat_id, "🌑 Подсветка кулера выключена")
        else:
            app.ui.send_message(chat_id, "❌ Не удалось отправить команду: MQTT недоступен.")
        return

    # 10. Interactive terminal line input
    if getattr(app, "console_active_chat_id", None) == chat_id and not text.startswith("/"):
        app.execute_console_command(chat_id, text, target=app.console_target, shell=app.console_shell)
        return
