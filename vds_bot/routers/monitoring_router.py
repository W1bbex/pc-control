#!/usr/bin/env python3
"""Monitoring Router: handles callbacks for dashboard navigation, settings, telemetry, screenshots, history charts, sound, and LED."""

from datetime import datetime, timedelta, timezone
import logging
import threading
import time

try:
    from zoneinfo import ZoneInfo
    MOSCOW = ZoneInfo("Europe/Moscow")
except Exception:
    MOSCOW = timezone(timedelta(hours=3))

try:
    from vds_bot.power_flow import PowerFlowStage
    from vds_bot.telegram_ui import ICONS
except ImportError:
    from power_flow import PowerFlowStage
    from telegram_ui import ICONS

LOG = logging.getLogger("pc-control-bot.routers.monitoring")

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


def route_monitoring_callback(app, data: str, chat_id: int, message_id: int, callback_id: str) -> bool:
    """Handle monitoring, dashboard, telemetry, screenshot, sound, and LED callbacks. Returns True if handled, False otherwise."""
    # 1. Main Dashboard Menu Navigation
    if data == "menu":
        app.ui.answer_callback(callback_id)
        if hasattr(app, "power_flow") and getattr(app.power_flow, "stage", None) in (
            PowerFlowStage.WAITING_WORKSPACE_DECISION,
            PowerFlowStage.UNLOCK_FAILED,
        ):
            app.power_flow.reset_flow()
        app.current_menu_view = "main"
        app.active_main_menu_message_id = message_id
        app.save_state()
        app.ui.send_message(
            chat_id,
            app.monitoring.dashboard_text(),
            reply_markup=app._main_menu_markup(),
            message_id=message_id,
        )
        return True

    # 2. Settings Menu
    if data == "settings_menu":
        app.ui.answer_callback(callback_id)
        app.current_menu_view = "settings_menu"
        gear_icon = ICONS.get("gear")
        prefix = f'<tg-emoji emoji-id="{gear_icon}">⚙️</tg-emoji> ' if gear_icon else ""
        text = (
            f"{prefix}<b>Настройки и сервисы</b>\n\n"
            "Выбери нужный раздел:"
        )
        app.ui.send_message(
            chat_id,
            text,
            reply_markup=app.ui.settings_menu_markup(),
            message_id=message_id,
        )
        return True

    # 3. Monitoring Menu
    if data == "monitoring_menu":
        app.ui.answer_callback(callback_id)
        text = app.monitoring.dashboard_text()
        app.ui.send_message(chat_id, text, reply_markup=app.ui.monitoring_menu_markup(), message_id=message_id)
        return True

    # 4. Activity Query
    if data == "menu_activity":
        app.ui.answer_callback(callback_id, "Запрашиваю активность...")
        today_str = datetime.now(MOSCOW).strftime("%Y-%m-%d")
        app.pending_activity_chat_id = chat_id
        app.pending_activity_msg_id = message_id
        app.mqtt.query_activity("summary", today_str)
        app.ui.send_message(chat_id, f"📊 Запрашиваю статистику за {today_str}...", message_id=message_id)
        return True

    # 5. Screenshot
    if data == "screenshot":
        if getattr(app.mqtt, "desktop_status", None) != "online":
            app.ui.answer_callback(callback_id, "Desktop-агент недоступен — скриншот сделать нельзя.", alert=True)
            return True
        app.ui.answer_callback(callback_id, "Делаю скриншот...")
        cmd_id = app.mqtt.publish_desktop_action("screenshot")
        if not cmd_id:
            app.ui.send_message(chat_id, "❌ Не удалось отправить запрос скриншота: MQTT недоступен.")
            return True
        app.pending_screenshots[cmd_id] = {"chat_id": chat_id, "requested_at": time.time()}
        return True

    # 6. Telemetry History Menu
    if data == "history_menu":
        app.ui.answer_callback(callback_id)
        stats = app.monitoring.history.stats()
        text = app.ui.history_menu_text(count=stats.get("count", 0))
        app.ui.send_message(chat_id, text, reply_markup=app.ui.history_menu_markup(), message_id=message_id)
        return True

    # 7. Render History Chart
    if data.startswith("history:"):
        period = data.split(":", 1)[1]
        periods = {
            "1h": (3600, "1 час"),
            "6h": (6 * 3600, "6 часов"),
            "24h": (24 * 3600, "24 часа"),
            "7d": (7 * 86400, "7 дней"),
            "30d": (30 * 86400, "30 дней"),
        }
        if period not in periods:
            app.ui.answer_callback(callback_id, "Неизвестный период", alert=True)
            return True
        seconds, title = periods[period]
        app.ui.answer_callback(callback_id, "Строю график...")

        def _build_and_send_chart():
            try:
                chart_bytes, count = app.monitoring.history.render_chart(seconds, title)
            except ValueError:
                app.ui.send_message(
                    chat_id,
                    "<b>📈 Графики</b>\n\nНедостаточно данных: подожди, пока агент соберёт хотя бы две точки телеметрии.",
                    reply_markup=app.ui.history_menu_markup(),
                )
                return
            except Exception:
                LOG.exception("Could not render telemetry chart")
                app.ui.send_message(
                    chat_id,
                    "<b>📈 Графики</b>\n\nНе удалось построить график. Состояние ПК продолжает работать.",
                    reply_markup=app.ui.history_menu_markup(),
                )
                return
            app.ui.send_photo(chat_id, chart_bytes, caption=f"<b>📈 Телеметрия за {title}</b>\nТочек: {count}")

        threading.Thread(target=_build_and_send_chart, daemon=True, name="chart-renderer").start()
        return True

    # 8. Force Telemetry Now
    if data == "telemetry_now":
        if getattr(app.mqtt, "desktop_status", None) != "online":
            app.ui.answer_callback(callback_id, "Desktop-агент пока недоступен — свежую телеметрию получить нельзя.", alert=True)
            return True
        app.ui.answer_callback(callback_id, "Обновляю состояние...")
        cmd_id = app.mqtt.publish_desktop_action("telemetry")
        app.pending_telemetry_cmd_ids[cmd_id] = (chat_id, message_id)
        return True

    # 9. LED Menu
    if data == "led_menu":
        app.ui.answer_callback(callback_id)
        text = (
            "<b>💡 Калибровка белого цвета кулера</b>\n\n"
            "Кулер: <i>Ocypus Gamma A40 WH ARGB</i>\n"
            "Пин управления: <code>D2 (ESP8266)</code>\n\n"
            "<b>⚡ Быстрые режимы:</b>\n"
            "👁 <b>Для глаз:</b> <code>255, 150, 125</code> (Идеал вживую)\n"
            "📸 <b>Для камеры:</b> <code>228, 245, 255</code> (Под сенсор камеры)\n\n"
            "<b>🎨 Палитра оттенков (1..12):</b>\n"
            "1️⃣ <code>255, 150, 165</code> — Холодный снежный (зелень срезана)\n"
            "2️⃣ <code>255, 140, 160</code> — Арктический холодный (без циана)\n"
            "3️⃣ <code>255, 130, 155</code> — Ледяной кристалл\n"
            "4️⃣ <code>255, 120, 150</code> — Глубокий холодный\n"
            "5️⃣ <code>255, 160, 160</code> — Симметричный чистый (G=B)\n"
            "6️⃣ <code>255, 145, 145</code> — Симметричный нейтральный\n"
            "7️⃣ <code>255, 130, 130</code> — Сбалансированный белый\n"
            "8️⃣ <code>255, 115, 115</code> — Мягкий белый\n"
            "9️⃣ <code>255, 165, 140</code> — Светлый нейтральный\n"
            "🔟 <code>255, 150, 125</code> — ⭐️ <b>Идеальный белый (Калибровка)</b>\n"
            "1️⃣1️⃣ <code>255, 135, 100</code> — Естественный дневной\n"
            "1️⃣2️⃣ <code>255, 120, 75</code> — Тёплый ламповый\n\n"
            "<i>Нажимай кнопки 1..12 ниже — оттенок меняется мгновенно!</i>"
        )
        app.ui.send_message(chat_id, text, reply_markup=app.ui.led_menu_markup(), message_id=message_id)
        return True

    # 11. LED Actions
    if data.startswith("led_action:"):
        if getattr(app.mqtt, "esp_status", None) != "online":
            app.ui.answer_callback(callback_id, "❌ Модуль управления кулером (ESP8266) не в сети!", alert=True)
            return True
        action = data.split(":", 1)[1]
        if action == "on":
            app.mqtt.publish_esp_action("LED_ON")
            app.ui.answer_callback(callback_id, "💡 Подсветка включена")
        elif action == "off":
            app.mqtt.publish_esp_action("LED_OFF")
            app.ui.answer_callback(callback_id, "🌑 Подсветка выключена")
        elif action.startswith("bright:"):
            level = action.split(":", 1)[1]
            app.mqtt.publish_esp_action("LED_BRIGHTNESS", value=level)
            app.ui.answer_callback(callback_id, f"🔆 Яркость: {level}")
        elif action.startswith("color:"):
            rgb = action.split(":", 1)[1]
            app.mqtt.publish_esp_action("LED_COLOR", value=rgb)
            app.ui.answer_callback(callback_id, "🎨 Оттенок изменён")
        elif action.startswith("preset:"):
            preset_id = action.split(":", 1)[1]
            if preset_id in LED_WHITE_PRESETS:
                rgb, name = LED_WHITE_PRESETS[preset_id]
                app.mqtt.publish_esp_action("LED_COLOR", value=rgb)
                app.ui.answer_callback(callback_id, f"#{preset_id}: {name} ({rgb})")
        return True

    return False
