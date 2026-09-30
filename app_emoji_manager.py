import io
import json
import logging
import os
import threading
import time
from pathlib import Path
import requests

from icon_extractor import extract_app_icon_png

LOG = logging.getLogger("app_emoji_manager")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
_data_cache = DATA_DIR / "app_icon_cache.json"
_root_cache = BASE_DIR / "app_icon_cache.json"
CACHE_FILE = _data_cache if _data_cache.exists() or not _root_cache.exists() else _root_cache

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

DEFAULT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_def_tg_uid = os.getenv("TELEGRAM_USER_ID", "0")
DEFAULT_USER_ID = int(_def_tg_uid) if _def_tg_uid else 0
DEFAULT_PACK_NAME = "pc_apps_pack"

IGNORED_PROCESSES = {
    "idle",
    "system",
    "dwm.exe",
    "lsass.exe",
    "svchost.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "searchhost.exe",
    "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
    "textinputhost.exe",
    "lockapp.exe",
    "applicationframehost.exe",
    "python.exe",
    "pythonw.exe",
    "psexec64.exe",
    "psexec.exe",
    "cmd.exe",
    "powershell.exe",
    "conhost.exe",
    "taskmgr.exe",
    "systemsettings.exe",
    "runtimebroker.exe",
}


class AppEmojiManager:
    def __init__(self, token=None, user_id=None, pack_name=None):
        self.token = token or DEFAULT_TOKEN
        self.user_id = user_id or DEFAULT_USER_ID
        self.pack_name = pack_name or DEFAULT_PACK_NAME
        self.cache = self._load_cache()
        self.last_notified_exe = None
        self.last_notified_time = 0.0
        self.upload_lock = threading.Lock()

    def _load_cache(self):
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                LOG.error("Failed to load icon cache: %s", e)
        return {}

    def _save_cache(self):
        try:
            temp = CACHE_FILE.with_suffix(".tmp")
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, indent=2, ensure_ascii=False)
            temp.replace(CACHE_FILE)
        except Exception as e:
            LOG.error("Failed to save icon cache: %s", e)

    def get_or_create_emoji_id(self, exe_path, exe_name):
        key = exe_name.lower()
        with self.upload_lock:
            if key in self.cache:
                entry = self.cache[key]
                if entry.get("emoji_id"):
                    return entry["emoji_id"]
                if entry.get("failed_at") and (time.time() - entry["failed_at"] < 60.0):
                    return None

            if not os.path.exists(exe_path):
                return None

            # Extract real 100x100 transparent PNG
            png_bytes = extract_app_icon_png(exe_path)
            if not png_bytes:
                LOG.warning("Could not extract icon for %s (%s)", exe_name, exe_path)
                self.cache[key] = {"emoji_id": None, "failed_at": time.time()}
                self._save_cache()
                return None

            try:
                # Upload to Telegram Sticker Pack
                files = {"sticker": (f"{key}.png", png_bytes, "image/png")}
                stickers_data = {
                    "sticker": "attach://sticker",
                    "emoji_list": ["🪟"],
                    "format": "static"
                }
                res = requests.post(
                    f"https://api.telegram.org/bot{self.token}/addStickerToSet",
                    data={
                        "user_id": self.user_id,
                        "name": self.pack_name,
                        "sticker": json.dumps(stickers_data)
                    },
                    files=files,
                    timeout=5
                )
                data = res.json()
                if not data.get("ok"):
                    desc = data.get("description", "")
                    if "STICKERSET_INVALID" in desc:
                        LOG.info("Sticker set invalid/missing, recreating set via createNewStickerSet...")
                        c_res = requests.post(
                            f"https://api.telegram.org/bot{self.token}/createNewStickerSet",
                            data={
                                "user_id": self.user_id,
                                "name": self.pack_name,
                                "title": "PC Control App Icons",
                                "sticker_type": "custom_emoji",
                                "stickers": json.dumps([stickers_data])
                            },
                            files={"sticker": (f"{key}.png", png_bytes, "image/png")},
                            timeout=5
                        )
                        data = c_res.json()

                    if not data.get("ok"):
                        LOG.error("Failed adding sticker for %s: %s", exe_name, data)
                        self.cache[key] = {"emoji_id": None, "failed_at": time.time()}
                        self._save_cache()
                        return None

                # Get sticker set to find newest custom_emoji_id
                set_res = requests.get(
                    f"https://api.telegram.org/bot{self.token}/getStickerSet",
                    params={"name": self.pack_name},
                    timeout=5
                ).json()

                stickers = set_res.get("result", {}).get("stickers", [])
                if not stickers:
                    self.cache[key] = {"emoji_id": None, "failed_at": time.time()}
                    self._save_cache()
                    return None

                emoji_id = stickers[-1].get("custom_emoji_id")
                if emoji_id:
                    self.cache[key] = {
                        "emoji_id": emoji_id,
                        "exe_path": exe_path,
                        "created_at": time.time()
                    }
                    self._save_cache()
                    LOG.info("Successfully added custom emoji for %s: %s", exe_name, emoji_id)
                    return emoji_id

            except Exception as e:
                LOG.warning("Exception adding emoji for %s (will negative cache for 60s): %s", exe_name, e)
                self.cache[key] = {"emoji_id": None, "failed_at": time.time()}
                self._save_cache()

            return None

    def clear_all_emojis(self):
        """Wipe local cache and reset state."""
        self.cache = {}
        self._save_cache()
        self.last_notified_exe = None
        self.last_notified_time = 0.0

    def notify_app_opened(self, exe_path, exe_name, window_title=None, send_telegram_message=False):
        key = exe_name.lower()
        if key in IGNORED_PROCESSES:
            return None

        now = time.time()
        # Debounce: don't process same app repeatedly within 10 seconds
        if key == self.last_notified_exe and (now - self.last_notified_time < 10.0):
            return self.cache.get(key, {}).get("emoji_id")

        self.last_notified_exe = key
        self.last_notified_time = now

        # Ensure icon is extracted and cached in custom emoji pack
        emoji_id = self.get_or_create_emoji_id(exe_path, exe_name)
        return emoji_id

