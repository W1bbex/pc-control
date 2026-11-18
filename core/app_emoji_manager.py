import json
import logging
import os
import threading
import time
from pathlib import Path
import requests

try:
    from .icon_extractor import extract_app_icon_png
except ImportError:
    from icon_extractor import extract_app_icon_png

LOG = logging.getLogger("app_emoji_manager")

_parent = Path(__file__).resolve().parent
BASE_DIR = _parent.parent if _parent.name == "core" else _parent
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
try:
    DEFAULT_USER_ID = int(_def_tg_uid) if _def_tg_uid else 0
except (ValueError, TypeError):
    DEFAULT_USER_ID = 0
DEFAULT_PACK_NAME = os.getenv("TELEGRAM_APPS_EMOJI_PACK", "pc_apps_pack")

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
        self.token = DEFAULT_TOKEN if token is None else token
        self.user_id = DEFAULT_USER_ID if user_id is None else user_id
        self.pack_name = pack_name or DEFAULT_PACK_NAME
        self.cache = self._load_cache()
        self.last_notified_exe = None
        self.last_notified_time = 0.0
        self.upload_lock = threading.Lock()
        self._sticker_set_lock = threading.Lock()
        self._resolved_pack_name = None
        self._inflight = {}
        self._cache_epoch = 0

    def _get_sticker_set_lock(self):
        lock = getattr(self, "_sticker_set_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._sticker_set_lock = lock
        return lock

    def _get_inflight(self):
        inflight = getattr(self, "_inflight", None)
        if inflight is None:
            inflight = {}
            self._inflight = inflight
        return inflight

    def _resolve_pack_name(self):
        if not self._resolved_pack_name:
            if "_by_" in self.pack_name.lower():
                self._resolved_pack_name = self.pack_name
            else:
                data = requests.get(f"https://api.telegram.org/bot{self.token}/getMe", timeout=5).json()
                username = data.get("result", {}).get("username")
                if not data.get("ok") or not username:
                    raise ValueError("Could not resolve the Telegram bot username for the app icon pack")
                self._resolved_pack_name = f"{self.pack_name}_by_{username.lower()}"
        return self._resolved_pack_name

    def get_cached_emoji_id(self, key: str):
        normalized = (key or "").lower()
        if not normalized:
            return None
        with self.upload_lock:
            entry = self.cache.get(normalized)
            return entry.get("emoji_id") if isinstance(entry, dict) else None

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
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp = CACHE_FILE.with_suffix(f".tmp.{os.getpid()}")
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, indent=2, ensure_ascii=False)
            temp.replace(CACHE_FILE)
        except Exception as e:
            LOG.error("Failed to save icon cache: %s", e)

    def get_or_create_emoji_id(self, exe_path, exe_name):
        token = getattr(self, "token", None)
        user_id = getattr(self, "user_id", None)
        if not token or not user_id:
            return None

        key = (exe_name or "").lower()
        if not key:
            return None
        wait_event = None
        my_event = None
        epoch = 0

        with self.upload_lock:
            if key in self.cache:
                entry = self.cache[key]
                if entry.get("emoji_id"):
                    return entry["emoji_id"]
                if entry.get("failed_at") and (time.time() - entry["failed_at"] < 60.0):
                    return None

            if not exe_path:
                return None

            inflight = self._get_inflight()
            if key in inflight:
                wait_event = inflight[key]
            else:
                epoch = getattr(self, "_cache_epoch", 0)
                my_event = threading.Event()
                inflight[key] = my_event

        if wait_event is not None:
            wait_event.wait(timeout=15.0)
            with self.upload_lock:
                entry = self.cache.get(key)
                return entry.get("emoji_id") if isinstance(entry, dict) else None

        emoji_id = None
        should_negative_cache = False
        try:
            if not os.path.exists(exe_path):
                return None

            # Extract real 100x100 transparent PNG outside upload_lock
            png_bytes = extract_app_icon_png(exe_path)
            if not png_bytes:
                LOG.warning("Could not extract icon for %s (%s)", exe_name, exe_path)
                should_negative_cache = True
            else:
                with self._get_sticker_set_lock():
                    try:
                        pack_name = self._resolve_pack_name()
                        # Upload to Telegram Sticker Pack outside upload_lock
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
                                "name": pack_name,
                                "sticker": json.dumps(stickers_data)
                            },
                            files=files,
                            timeout=5
                        )
                        data = res.json()
                        if not data.get("ok"):
                            desc = data.get("description", "")
                            if ("STICKERSET_INVALID" in desc.upper()
                                    or "invalid sticker set name" in desc.lower()):
                                LOG.info("Sticker set invalid/missing, recreating set via createNewStickerSet...")
                                c_res = requests.post(
                                    f"https://api.telegram.org/bot{self.token}/createNewStickerSet",
                                    data={
                                        "user_id": self.user_id,
                                        "name": pack_name,
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
                                should_negative_cache = True

                        if not should_negative_cache:
                            # Get sticker set to find newest custom_emoji_id
                            set_res = requests.get(
                                f"https://api.telegram.org/bot{self.token}/getStickerSet",
                                params={"name": pack_name},
                                timeout=5
                            ).json()

                            stickers = set_res.get("result", {}).get("stickers", [])
                            if not stickers:
                                should_negative_cache = True
                            else:
                                emoji_id = stickers[-1].get("custom_emoji_id")
                                if not emoji_id:
                                    should_negative_cache = True
                    except Exception as e:
                        err_msg = str(e)
                        if self.token and self.token in err_msg:
                            err_msg = err_msg.replace(self.token, "[REDACTED]")
                        LOG.warning("Exception adding emoji for %s (will negative cache for 60s): %s", exe_name, err_msg)
                        should_negative_cache = True

            if emoji_id:
                with self.upload_lock:
                    if getattr(self, "_cache_epoch", 0) == epoch:
                        self.cache[key] = {
                            "emoji_id": emoji_id,
                            "exe_path": exe_path,
                            "created_at": time.time()
                        }
                        self._save_cache()
                LOG.info("Successfully added custom emoji for %s: %s", exe_name, emoji_id)
                return emoji_id

            if should_negative_cache:
                with self.upload_lock:
                    if getattr(self, "_cache_epoch", 0) == epoch:
                        self.cache[key] = {"emoji_id": None, "failed_at": time.time()}
                        self._save_cache()
            return None
        finally:
            with self.upload_lock:
                inflight = self._get_inflight()
                if inflight.get(key) is my_event:
                    inflight.pop(key, None)
            if my_event is not None:
                my_event.set()

    def clear_all_emojis(self):
        """Wipe local cache and reset state."""
        with self.upload_lock:
            self._cache_epoch = getattr(self, "_cache_epoch", 0) + 1
            self.cache = {}
            self._save_cache()
            self.last_notified_exe = None
            self.last_notified_time = 0.0

    def notify_app_opened(self, exe_path, exe_name, window_title=None):
        key = exe_name.lower()
        if key in IGNORED_PROCESSES:
            return None

        now = time.time()
        with self.upload_lock:
            # Debounce: don't process same app repeatedly within 10 seconds
            if key == self.last_notified_exe and (now - self.last_notified_time < 10.0):
                entry = self.cache.get(key)
                return entry.get("emoji_id") if isinstance(entry, dict) else None

            self.last_notified_exe = key
            self.last_notified_time = now

        # Ensure icon is extracted and cached in custom emoji pack
        emoji_id = self.get_or_create_emoji_id(exe_path, exe_name)
        return emoji_id


def read_cached_emoji_id(emoji_manager, exe_key: str):
    """Safely read an emoji_id from AppEmojiManager or compatible test mock."""
    if emoji_manager is None or not exe_key:
        return None
    if hasattr(type(emoji_manager), "get_cached_emoji_id"):
        return emoji_manager.get_cached_emoji_id(exe_key)
    entry = getattr(emoji_manager, "cache", {}).get(exe_key.lower())
    return entry.get("emoji_id") if isinstance(entry, dict) else None

