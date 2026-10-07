"""
Unify and modernize W1bbex Icons sticker set (pc_icons_pack).
Replaces the first 17 thick/filled icons with pixel-perfect, thin outline Lucide-style vector icons (stroke-width: 1.8px).
Deletes unused duplicate lightning stickers.
Updates resources/icons.json and vds_bot/resources/icons.json.
"""

import io
import json
import logging
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv
import requests
from PIL import Image
import resvg_py

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
USER_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID") or os.getenv("TELEGRAM_USER_ID") or 123456789)
PACK_NAME = "pc_icons_pack"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

# 17 Modernized thin-stroke SVGs (stroke-width: 1.8px, fill: none, round corners)
REPLACEMENT_SVGS = {
    "power": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <path d="M18.36 6.64a9 9 0 1 1-12.73 0"/>
  <line x1="12" y1="2" x2="12" y2="12"/>
</svg>""",
        "⚡"
    ),
    "screenshot": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/>
  <circle cx="12" cy="13" r="3"/>
</svg>""",
        "📷"
    ),
    "sound": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
  <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
  <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
</svg>""",
        "🔊"
    ),
    "screen": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <rect width="20" height="14" x="2" y="3" rx="2"/>
  <line x1="8" x2="16" y1="21" y2="21"/>
  <line x1="12" x2="12" y1="17" y2="21"/>
</svg>""",
        "🖥"
    ),
    "apps": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <rect width="7" height="7" x="3" y="3" rx="1.5"/>
  <rect width="7" height="7" x="14" y="3" rx="1.5"/>
  <rect width="7" height="7" x="14" y="14" rx="1.5"/>
  <rect width="7" height="7" x="3" y="14" rx="1.5"/>
</svg>""",
        "📱"
    ),
    "gear": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/>
  <circle cx="12" cy="12" r="3"/>
</svg>""",
        "⚙️"
    ),
    "cpu": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <rect width="14" height="14" x="5" y="5" rx="2"/>
  <rect width="6" height="6" x="9" y="9" rx="1"/>
  <path d="M9 1v4M15 1v4M9 19v4M15 19v4M1 9h4M1 15h4M19 9h4M19 15h4"/>
</svg>""",
        "💻"
    ),
    "ram": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <path d="M4 6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h1.5v-2h2v2h2v-2h2v2h2v-2h2v2h2v-2h2v2H20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2H4z"/>
  <rect width="3" height="4" x="6" y="9" rx="0.5"/>
  <rect width="3" height="4" x="11" y="9" rx="0.5"/>
  <rect width="3" height="4" x="16" y="9" rx="0.5"/>
</svg>""",
        "💾"
    ),
    "disk": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <line x1="22" x2="2" y1="12" y2="12"/>
  <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>
  <line x1="6" x2="6.01" y1="16" y2="16"/>
  <line x1="10" x2="10.01" y1="16" y2="16"/>
</svg>""",
        "💽"
    ),
    "network": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <path d="M2 8.82a15 15 0 0 1 20 0"/>
  <path d="M5 12.86a10 10 0 0 1 14 0"/>
  <path d="M8.5 16.43a5 5 0 0 1 7 0"/>
  <line x1="12" x2="12.01" y1="20" y2="20"/>
</svg>""",
        "📡"
    ),
    "lock": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <rect width="18" height="11" x="3" y="11" rx="2" ry="2"/>
  <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
</svg>""",
        "🔒"
    ),
    "light": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/>
  <path d="M9 18h6M10 22h4"/>
</svg>""",
        "💡"
    ),
    "music": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <path d="M9 18V5l12-2v13"/>
  <circle cx="6" cy="18" r="3"/>
  <circle cx="18" cy="16" r="3"/>
</svg>""",
        "🎵"
    ),
    "game": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <line x1="6" x2="10" y1="12" y2="12"/>
  <line x1="8" x2="8" y1="10" y2="14"/>
  <line x1="15" x2="15.01" y1="13" y2="13"/>
  <line x1="18" x2="18.01" y1="11" y2="11"/>
  <rect width="20" height="12" x="2" y="6" rx="2"/>
</svg>""",
        "🎮"
    ),
    "telegram": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <path d="m22 2-7 20-4-9-9-4Z"/>
  <path d="M22 2 11 13"/>
</svg>""",
        "✈️"
    ),
    "chrome": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="10"/>
  <path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20M2 12h20"/>
</svg>""",
        "🌐"
    ),
    "terminal": (
        """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <polyline points="4 17 10 11 4 5"/>
  <line x1="12" x2="20" y1="19" y2="19"/>
</svg>""",
        "⌨️"
    ),
}

ORDER_INDEX = [
    "power", "screenshot", "sound", "screen", "apps", "gear",
    "cpu", "ram", "disk", "network", "lock", "light",
    "music", "game", "telegram", "chrome", "terminal"
]


def render_svg_to_webp_bytes(svg_code: str) -> io.BytesIO:
    png_bytes = resvg_py.svg_to_bytes(svg_code)
    img = Image.open(io.BytesIO(png_bytes))
    buf = io.BytesIO()
    img.save(buf, format="WEBP")
    buf.seek(0)
    return buf


def main():
    print(f"Fetching sticker set '{PACK_NAME}' from Telegram...")
    res = requests.get(f"{BASE_URL}/getStickerSet", params={"name": PACK_NAME}, timeout=15).json()
    if not res.get("ok"):
        print("Failed to get sticker set:", res)
        sys.exit(1)

    stickers = res["result"]["stickers"]
    print(f"Total current stickers in pack: {len(stickers)}")

    # 1. Replace first 17 stickers with our thin-line icons
    new_emoji_ids = {}
    for idx, name in enumerate(ORDER_INDEX):
        svg_code, emoji_char = REPLACEMENT_SVGS[name]
        old_sticker = stickers[idx]
        old_file_id = old_sticker["file_id"]
        old_emoji_id = old_sticker.get("custom_emoji_id")

        print(f"[{idx:02d}] Modernizing '{name}' (old custom_emoji_id: {old_emoji_id})...")
        webp_buf = render_svg_to_webp_bytes(svg_code)

        # Upload new sticker file
        upload_resp = requests.post(
            f"{BASE_URL}/uploadStickerFile",
            data={"user_id": USER_ID, "sticker_format": "static"},
            files={"sticker": (f"{name}.webp", webp_buf, "image/webp")},
            timeout=30,
        ).json()

        if not upload_resp.get("ok"):
            print(f"  [-] Failed to upload file for {name}: {upload_resp}")
            continue

        new_file_id = upload_resp["result"]["file_id"]

        # Call replaceStickerInSet
        replace_resp = requests.post(
            f"{BASE_URL}/replaceStickerInSet",
            json={
                "user_id": USER_ID,
                "name": PACK_NAME,
                "old_sticker": old_file_id,
                "sticker": {
                    "sticker": new_file_id,
                    "format": "static",
                    "emoji_list": [emoji_char],
                    "needs_repainting": True,
                },
            },
            timeout=30,
        ).json()

        if not replace_resp.get("ok"):
            print(f"  [-] Failed to replace sticker for {name}: {replace_resp}")
        else:
            print(f"  [+] Successfully replaced '{name}'")
        time.sleep(0.5)

    # 2. Delete duplicate lightning at index 18 if present
    # Re-fetch set to find duplicate lightning
    res_after = requests.get(f"{BASE_URL}/getStickerSet", params={"name": PACK_NAME}, timeout=15).json()
    if res_after.get("ok"):
        cur_stickers = res_after["result"]["stickers"]
        # In cur_stickers, check if index 18 is a duplicate lightning
        if len(cur_stickers) > 18:
            s18 = cur_stickers[18]
            if s18.get("emoji") == "⚡" and s18.get("custom_emoji_id") == "5330278899391705066":
                print(f"Deleting unused duplicate lightning (custom_emoji_id: {s18.get('custom_emoji_id')})...")
                del_res = requests.post(
                    f"{BASE_URL}/deleteStickerFromSet",
                    json={"sticker": s18["file_id"]},
                    timeout=15,
                ).json()
                print("Delete result:", del_res)

    # 3. Final fetch to get updated custom_emoji_id mapping
    final_res = requests.get(f"{BASE_URL}/getStickerSet", params={"name": PACK_NAME}, timeout=15).json()
    if not final_res.get("ok"):
        print("Failed to fetch final sticker set:", final_res)
        sys.exit(1)

    final_stickers = final_res["result"]["stickers"]
    print(f"\nFinal stickers count in pack: {len(final_stickers)}")

    # Load existing icons.json
    icons_path = BASE_DIR / "resources" / "icons.json"
    with open(icons_path, "r", encoding="utf-8") as f:
        icons_data = json.load(f)

    # Update mapping for our modernized 17 icons
    for idx, name in enumerate(ORDER_INDEX):
        st = final_stickers[idx]
        eid = st.get("custom_emoji_id")
        print(f"  {name:12} -> {eid}")
        icons_data[name] = str(eid)
        if name == "game":
            icons_data["apps"] = str(eid) # ensure apps / game both link to thin modern icons

    # Also make sure app_dolphin is preserved
    icons_data["app_dolphin"] = "5278321743709124204"

    # Save to both paths
    vds_icons_path = BASE_DIR / "vds_bot" / "resources" / "icons.json"
    for p in (icons_path, vds_icons_path):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(icons_data, f, indent=2, ensure_ascii=False)
        print(f"Updated {p}")

    print("\n[SUCCESS] Sticker pack successfully modernized and icons.json synchronized!")


if __name__ == "__main__":
    main()
