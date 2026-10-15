"""
Module for checking Telegram Premium status of users with persistent 24-hour caching.
Prevents redundant calls to Telegram Bot API and enables graceful UI adaptation.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

LOG = logging.getLogger("pc-control-bot.premium_checker")

DEFAULT_CACHE_TTL = 86400  # 24 hours


class PremiumChecker:
    def __init__(self, cache_path: Optional[Path] = None, ttl_seconds: int = DEFAULT_CACHE_TTL):
        self.ttl = ttl_seconds
        if cache_path is None:
            base_dir = Path(__file__).resolve().parent.parent / "data"
            self.cache_path = base_dir / "premium_cache.json"
        else:
            self.cache_path = Path(cache_path)
            
        self._lock = threading.Lock()
        self._memory_cache: Dict[int, Dict[str, Any]] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.items():
                    try:
                        self._memory_cache[int(k)] = v
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            LOG.warning("Failed to load premium cache from %s: %s", self.cache_path, e)

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.cache_path.with_suffix(f".tmp.{os.getpid()}")
            tmp_path.write_text(
                json.dumps(self._memory_cache, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp_path, self.cache_path)
        except Exception as e:
            LOG.warning("Failed to save premium cache to %s: %s", self.cache_path, e)

    def update_from_user_obj(self, user_id: int, user_obj: Optional[Dict[str, Any]]) -> bool:
        """Update cache directly from an incoming Telegram User object (e.g. from update)."""
        if not user_obj:
            return self.get_cached_status(user_id)

        is_premium = bool(user_obj.get("is_premium", False))
        with self._lock:
            self._memory_cache[int(user_id)] = {
                "is_premium": is_premium,
                "expires_at": time.time() + self.ttl,
                "source": "incoming_event"
            }
            self._save_cache()
        return is_premium

    def get_cached_status(self, user_id: int) -> bool:
        """Returns cached status if still valid; otherwise returns last known or False."""
        uid = int(user_id)
        with self._lock:
            entry = self._memory_cache.get(uid)
            if entry and entry.get("expires_at", 0) > time.time():
                return bool(entry.get("is_premium", False))
            if entry:
                return bool(entry.get("is_premium", False))
        return False

    def is_user_premium(
        self,
        user_id: int,
        user_obj: Optional[Dict[str, Any]] = None,
        bot_api: Any = None
    ) -> bool:
        """
        Determines if user has Telegram Premium:
        1. Uses user_obj if provided (zero API calls).
        2. Returns fresh cached value if TTL has not expired.
        3. Falls back to bot_api.call('getChat', ...) if cache is expired/missing.
        """
        uid = int(user_id)

        if user_obj is not None:
            return self.update_from_user_obj(uid, user_obj)

        with self._lock:
            entry = self._memory_cache.get(uid)
            if entry and entry.get("expires_at", 0) > time.time():
                return bool(entry.get("is_premium", False))

        # Need to query API if bot_api provided
        if bot_api:
            try:
                chat = bot_api.call("getChat", {"chat_id": uid})
                if chat and isinstance(chat, dict):
                    # Check explicit is_premium or indicators available only to Premium accounts
                    is_prem = bool(
                        chat.get("is_premium")
                        or chat.get("emoji_status_custom_emoji_id")
                        or (chat.get("accent_color_id") is not None and chat.get("accent_color_id") > 6)
                    )
                    with self._lock:
                        self._memory_cache[uid] = {
                            "is_premium": is_prem,
                            "expires_at": time.time() + self.ttl,
                            "source": "getChat"
                        }
                        self._save_cache()
                    return is_prem
            except Exception as e:
                LOG.warning("Failed getChat check for user %s: %s", uid, e)

        # Fallback to stale cached status or False
        return self.get_cached_status(uid)
