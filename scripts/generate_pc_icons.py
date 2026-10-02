"""
Generator for W1bbex Adaptive Custom Emojis using professional vector SVG icons.
Uses resvg-py for subpixel anti-aliased 100x100 WEBP rendering with needs_repainting=True.
"""

import io
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict
from PIL import Image
import requests
import resvg_py

LOG = logging.getLogger("pc-control-bot.icon_generator")

SVGS = {
    "power": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round">
  <path d="M18.36 6.64a9 9 0 1 1-12.73 0"/>
  <line x1="12" y1="2" x2="12" y2="12"/>
</svg>""",
        "⚡"
    ),

    "screenshot": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/>
  <circle cx="12" cy="13" r="3"/>
</svg>""",
        "📷"
    ),

    "sound": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
  <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" fill="white"/>
  <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
  <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
</svg>""",
        "🔊"
    ),

    "screen": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
  <rect width="20" height="14" x="2" y="3" rx="2"/>
  <line x1="8" x2="16" y1="21" y2="21"/>
  <line x1="12" x2="12" y1="17" y2="21"/>
</svg>""",
        "🖥"
    ),

    "apps": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
  <rect width="7.5" height="7.5" x="3" y="3" rx="2" fill="white"/>
  <rect width="7.5" height="7.5" x="13.5" y="3" rx="2" fill="white"/>
  <rect width="7.5" height="7.5" x="13.5" y="13.5" rx="2" fill="white"/>
  <rect width="7.5" height="7.5" x="3" y="13.5" rx="2" fill="white"/>
</svg>""",
        "📱"
    ),

    "gear": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/>
  <circle cx="12" cy="12" r="3"/>
</svg>""",
        "⚙️"
    ),

    "cpu": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
  <rect width="14" height="14" x="5" y="5" rx="2.5"/>
  <rect width="6" height="6" x="9" y="9" rx="1" fill="white"/>
  <path d="M9 1v4M15 1v4M9 19v4M15 19v4M1 9h4M1 15h4M19 9h4M19 15h4"/>
</svg>""",
        "💻"
    ),

    "ram": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="white">
  <path d="M4 6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h1.5v-2h2v2h2v-2h2v2h2v-2h2v2h2v-2h2v2H20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2H4zm2 3h3v4H6V9zm5 0h3v4h-3V9zm5 0h3v4h-3V9z"/>
</svg>""",
        "💾"
    ),

    "disk": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>
  <line x1="2" x2="22" y1="12" y2="12"/>
  <circle cx="6" cy="16" r="1" fill="white"/>
  <circle cx="10" cy="16" r="1" fill="white"/>
</svg>""",
        "💽"
    ),

    "network": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 20h.01"/>
  <path d="M2 8.82a15 15 0 0 1 20 0"/>
  <path d="M5 12.86a10 10 0 0 1 14 0"/>
  <path d="M8.5 16.43a5 5 0 0 1 7 0"/>
</svg>""",
        "📡"
    ),

    "lock": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
  <rect width="18" height="11" x="3" y="11" rx="2.5" ry="2.5"/>
  <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
  <circle cx="12" cy="16.5" r="1.5" fill="white"/>
</svg>""",
        "🔒"
    ),

    "light": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/>
  <path d="M9 18h6"/>
  <path d="M10 22h4"/>
</svg>""",
        "💡"
    ),

    "music": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
  <path d="M9 18V5l12-2v13"/>
  <circle cx="6" cy="18" r="3" fill="white"/>
  <circle cx="18" cy="16" r="3" fill="white"/>
</svg>""",
        "🎵"
    ),

    "game": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
  <path d="M17.3 5H6.7a4 4 0 0 0-4 3.7c-.1.7-.7 5.8-.7 7.3a3 3 0 0 0 3 3c1 0 1.5-.5 2-1l1.4-1.4a2 2 0 0 1 1.4-.6h4.4a2 2 0 0 1 1.4.6L17 18c.5.5 1 1 2 1a3 3 0 0 0 3-3c0-1.5-.6-6.6-.7-7.3A4 4 0 0 0 17.3 5z"/>
  <line x1="6" x2="10" y1="11" y2="11"/>
  <line x1="8" x2="8" y1="9" y2="13"/>
  <circle cx="15" cy="11" r="1" fill="white"/>
  <circle cx="17.5" cy="9.5" r="1" fill="white"/>
</svg>""",
        "🎮"
    ),

    "telegram": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="white">
  <path d="M9.78 18.65l.28-4.23 7.68-6.92c.34-.31-.07-.46-.52-.19L7.74 13.3 3.64 12c-.88-.25-.89-.86.2-1.3l15.97-6.16c.73-.33 1.43.18 1.15 1.3l-2.72 12.81c-.19.91-.74 1.13-1.5.71L12.6 16.3l-1.99 1.93c-.23.23-.42.42-.83.42z"/>
</svg>""",
        "✈️"
    ),

    "chrome": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="10"/>
  <path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/>
  <path d="M2 12h20"/>
</svg>""",
        "🌐"
    ),

    "terminal": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
  <rect width="20" height="16" x="2" y="4" rx="3"/>
  <polyline points="6 11 9 13.5 6 16"/>
  <line x1="12" x2="16" y1="16" y2="16"/>
</svg>""",
        "⌨️"
    )
}


def build_and_upload_pack(token: str, user_id: int, pack_name: str, pack_title: str) -> Dict[str, str]:
    base_url = f"https://api.telegram.org/bot{token}"
    stickers_payload = []
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=5)
    session.mount("https://", adapter)

    print(f"Generating and uploading {len(SVGS)} vector icons to Telegram...")
    for name, (svg_code, emoji_char) in SVGS.items():
        png_bytes = resvg_py.svg_to_bytes(svg_code)
        img = Image.open(io.BytesIO(png_bytes))
        buf = io.BytesIO()
        img.save(buf, format="WEBP")
        buf.seek(0)

        file_id = None
        for attempt in range(5):
            try:
                res = session.post(
                    f"{base_url}/uploadStickerFile",
                    data={"user_id": user_id, "sticker_format": "static"},
                    files={"sticker": (f"{name}.webp", buf, "image/webp")},
                    timeout=30,
                ).json()
                if res.get("ok"):
                    file_id = res["result"]["file_id"]
                    print(f"  [+] Uploaded {name:12} -> {file_id}")
                    break
                else:
                    print(f"  [-] Attempt {attempt+1} failed for {name}: {res}")
            except Exception as ex:
                print(f"  [!] Attempt {attempt+1} exception for {name}: {ex}")
            time.sleep(2)
            buf.seek(0)

        if not file_id:
            raise RuntimeError(f"Failed to upload sticker file for {name} after 5 attempts")
        time.sleep(0.8)

        stickers_payload.append({
            "sticker": file_id,
            "format": "static",
            "emoji_list": [emoji_char],
            "needs_repainting": True,
        })

    print(f"\nRegistering custom emoji sticker pack '{pack_name}' (Title: '{pack_title}')...")
    create_res = session.post(
        f"{base_url}/createNewStickerSet",
        json={
            "user_id": user_id,
            "name": pack_name,
            "title": pack_title,
            "sticker_type": "custom_emoji",
            "stickers": stickers_payload,
        },
        timeout=30,
    ).json()

    if not create_res.get("ok"):
        print(f"ERROR creating sticker set: {create_res}")
        raise RuntimeError(f"Failed to create sticker set: {create_res}")

    print("Sticker set successfully created! Fetching custom_emoji_id mapping...")
    set_info = session.get(
        f"{base_url}/getStickerSet",
        params={"name": pack_name},
        timeout=15,
    ).json()

    mapping = {}
    if set_info.get("ok"):
        for sticker, (name, _) in zip(set_info["result"]["stickers"], SVGS.items()):
            mapping[name] = sticker.get("custom_emoji_id")

    return mapping
