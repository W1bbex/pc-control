"""
Script to send the adaptive custom emoji preview test bench to the user's Telegram PM.
"""

import json
import os
import sys
from pathlib import Path
import requests

DESCRIPTIONS = {
    "power": "Питание (Power / Вкл / Выкл)",
    "screenshot": "Скриншот (Screenshot / Камера)",
    "sound": "Звук (Sound / Громкость)",
    "screen": "Экран (Screen / Монитор)",
    "apps": "Приложения (Apps / Процессы)",
    "gear": "Настройки (Settings / Шестеренка)",
    "cpu": "Процессор (CPU / Чип)",
    "ram": "Память (RAM / ОЗУ)",
    "disk": "Диск (Storage / SSD / HDD)",
    "network": "Сеть (Network / Wi-Fi / Пинг)",
    "lock": "Блокировка (Lock / Lock Windows)",
    "light": "Свет (LED Light / Лампочка)",
    "music": "Музыка (Music / Плеер)",
    "game": "Игры (Games / Геймпад)",
    "telegram": "Telegram (Мессенджер)",
    "chrome": "Браузер (Chrome / Web)",
    "terminal": "Терминал (CLI / Консоль >_)",
}

FALLBACK_CHARS = {
    "power": "⚡", "screenshot": "📷", "sound": "🔊", "screen": "🖥",
    "apps": "📱", "gear": "⚙️", "cpu": "💻", "ram": "💾",
    "disk": "💽", "network": "📡", "lock": "🔒", "light": "💡",
    "music": "🎵", "game": "🎮", "telegram": "✈️", "chrome": "🌐", "terminal": "⌨️"
}


def send_preview():
    env_file = Path(__file__).resolve().parent.parent / ".env"
    env = dict(os.environ)
    if env_file.exists():
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            k, v = line.split("=", 1)
            val = v.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            env[k.strip()] = val

    token = env.get("TELEGRAM_BOT_TOKEN")
    user_id = env.get("TELEGRAM_ALLOWED_USER_ID") or env.get("TELEGRAM_USER_ID")

    if not token or not user_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_ALLOWED_USER_ID in .env")
        sys.exit(1)

    icons_file = Path(__file__).resolve().parent.parent / "resources" / "icons.json"
    with open(icons_file, "r", encoding="utf-8") as f:
        icons = json.load(f)

    DESCRIPTIONS["pc"] = "PC Control (Ноутбук в шапку)"
    DESCRIPTIONS["user"] = "Сессия (Силуэт пользователя)"
    DESCRIPTIONS["folder"] = "Накопители (Папка)"
    DESCRIPTIONS["sound_mute"] = "Без звука (Динамик с крестиком)"

    FALLBACK_CHARS["pc"] = "💻"
    FALLBACK_CHARS["user"] = "👤"
    FALLBACK_CHARS["folder"] = "📁"
    FALLBACK_CHARS["sound_mute"] = "🔇"

    pack_name = os.getenv("TELEGRAM_ICONS_EMOJI_PACK", "pc_icons_pack")
    lines = [
        f'<tg-emoji emoji-id="{icons.get("pc")}">💻</tg-emoji> <b>UI Icons — Обновленные векторные иконки</b>',
        f'<i>Стикерпак: {pack_name} (needs_repainting: True)</i>\n',
        f'<b><tg-emoji emoji-id="{icons.get("user")}">👤</tg-emoji> Секция: Сессия</b>',
        f'<b><tg-emoji emoji-id="{icons.get("power")}">⚡</tg-emoji> Секция: Железо</b>',
        f'<b><tg-emoji emoji-id="{icons.get("folder")}">📁</tg-emoji> Секция: Накопители</b>\n',
        "<b>Кнопки главного меню (проверь изящность контуров):</b>",
    ]

    for key in ["power", "screenshot", "game", "sound", "gear", "lock", "pc", "user", "folder", "sound_mute"]:
        emoji_id = icons.get(key)
        desc = DESCRIPTIONS.get(key, key)
        fb = FALLBACK_CHARS.get(key, "▫️")
        if emoji_id:
            lines.append(f'<tg-emoji emoji-id="{emoji_id}">{fb}</tg-emoji> — <b>{desc}</b>')
        else:
            lines.append(f"{fb} — <b>{desc}</b> <i>(не найден)</i>")

    text = "\n".join(lines)

    # Inline keyboard matching real dashboard layout exactly
    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "Питание",
                    "style": "success",
                    "icon_custom_emoji_id": icons.get("power"),
                    "callback_data": "preview_power"
                }
            ],
            [
                {
                    "text": "Скриншот",
                    "style": "primary",
                    "icon_custom_emoji_id": icons.get("screenshot"),
                    "callback_data": "preview_screenshot"
                },
                {
                    "text": "Программы",
                    "style": "primary",
                    "icon_custom_emoji_id": icons.get("game"),
                    "callback_data": "preview_apps"
                }
            ],
            [
                {
                    "text": "Звук",
                    "style": "primary",
                    "icon_custom_emoji_id": icons.get("sound"),
                    "callback_data": "preview_sound"
                },
                {
                    "text": "Настройки",
                    "style": "primary",
                    "icon_custom_emoji_id": icons.get("gear"),
                    "callback_data": "preview_gear"
                }
            ],
            [
                {
                    "text": "Заблокировать",
                    "style": "danger",
                    "icon_custom_emoji_id": icons.get("lock"),
                    "callback_data": "preview_lock"
                }
            ]
        ]
    }

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": int(user_id),
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup
    }

    res = requests.post(url, json=payload, timeout=20).json()
    if res.get("ok"):
        print(f"SUCCESS: Preview message sent to user {user_id}! (Message ID: {res['result']['message_id']})")
    else:
        print(f"FAILED to send preview message: {res}")


if __name__ == "__main__":
    send_preview()
