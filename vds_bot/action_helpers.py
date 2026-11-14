#!/usr/bin/env python3
"""Shared LED presets and action helpers for VDS bot routers."""

import html
import logging

LOG = logging.getLogger("pc-control-bot.action_helpers")

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


def led_brightness_percent(level: int) -> int:
    """Convert raw 0..255 PWM level to UI percentage 0..100."""
    clamped = max(0, min(255, int(level)))
    return int(round(clamped * 100.0 / 255.0))


def apply_led_power(app, turn_on: bool):
    """Apply LED on/off state and publish corresponding ESP MQTT command."""
    state = "on" if turn_on else "off"
    app.user_led_state = state
    app.last_auto_led_state = state
    if hasattr(app, "save_state"):
        app.save_state()
    if turn_on:
        color = getattr(app, "user_led_color", None)
        if color:
            return app.mqtt.publish_esp_action("LED_COLOR", value=color)
        return app.mqtt.publish_esp_action("LED_ON")
    return app.mqtt.publish_esp_action("LED_OFF")


def apply_led_preset(app, preset_id: str):
    """Apply named white preset to app state and publish LED_COLOR."""
    entry = LED_WHITE_PRESETS.get(preset_id)
    if not entry:
        return None
    rgb, name = entry
    app.user_led_state = "on"
    app.last_auto_led_state = "on"
    app.user_led_color = rgb
    if hasattr(app, "save_state"):
        app.save_state()
    cmd_id = app.mqtt.publish_esp_action("LED_COLOR", value=rgb)
    return rgb, name, cmd_id


def apply_stealth_state(app, chat_id: int, state: str, message_id=None, is_command: bool = False):
    """Dispatch stealth_mode change and update/send the stealth control card."""
    if state == "on":
        app.mqtt.publish_desktop_action("stealth_mode", {"state": "on"})
        hint = (
            "<i>Снятие режима: /stealth off или кнопка ниже.\n"
            "Локальный выход: <code>Win+W+1</code></i>"
            if is_command
            else "<i>Для отключения отправь /stealth off или нажми кнопку ниже.\n"
            "Локальный выход: <code>Win+W+1</code></i>"
        )
        text = (
            "<b>🕶️ Режим скрытности активирован</b>\n\n"
            "• Мониторы переведены в сон\n"
            "• Поглощение 100% кликов и клавиш («Черная дыра»)\n"
            "• Системный звук заглушен\n\n"
            f"{hint}"
        )
        msg = app.ui.send_message(chat_id, text, reply_markup=app.ui.stealth_menu_markup(active=True))
        if isinstance(msg, dict) and msg.get("message_id"):
            app.active_stealth_card = (chat_id, msg["message_id"])
        elif message_id:
            app.active_stealth_card = (chat_id, message_id)
        return msg

    if state == "off":
        app.mqtt.publish_desktop_action("stealth_mode", {"state": "off"})
        app.active_stealth_card = None
        text = (
            "<b>🔓 Режим скрытности отключен</b>\n\n"
            "• Дисплеи разбужены\n"
            "• Поглощение ввода снято\n"
            "• Звук восстановлен"
        )
        kwargs = {"reply_markup": app.ui.stealth_menu_markup(active=False)}
        if message_id is not None:
            kwargs["message_id"] = message_id
        return app.ui.send_message(chat_id, text, **kwargs)

    is_active = bool(app.monitoring.latest_telemetry.get("stealth_mode")) if getattr(app.monitoring, "latest_telemetry", None) else False
    status_label = "🟢 АКТИВЕН" if is_active else "⚪ ВЫКЛЮЧЕН"
    text = (
        f"<b>🕶️ Режим скрытности (Blackout Mode)</b>\n\n"
        f"Текущий статус: <b>{status_label}</b>\n\n"
        "Режим гасит мониторы, заглушает звук и поглощает все клики мыши и нажатия клавиш («Черная дыра»).\n"
        "Локальный выход доступен по комбинации <code>Win+W+1</code>."
    )
    msg = app.ui.send_message(chat_id, text, reply_markup=app.ui.stealth_menu_markup(active=is_active))
    if is_active and isinstance(msg, dict) and msg.get("message_id"):
        app.active_stealth_card = (chat_id, msg["message_id"])
    return msg


def safe_html(val) -> str:
    return html.escape(str(val or ""))
