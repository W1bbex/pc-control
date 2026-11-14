#!/usr/bin/env python3

"""Telegram Bot API HTTP client with thread-pooled non-blocking network calls."""

import json
import logging
import re
import threading
import weakref
import requests
from requests.adapters import HTTPAdapter

try:
    from .ui_templates import (
        CANONICAL_ICONS,
        ICONS,
        EMOJI_BTN_POWER,
        EMOJI_BTN_SCREENSHOT,
        EMOJI_BTN_APPS,
        EMOJI_BTN_SOUND,
        EMOJI_BTN_SETTINGS,
        DEFAULT_PLAYER_ICONS,
        UiTemplatesMixin,
    )
except ImportError:
    from ui_templates import (
        CANONICAL_ICONS,
        ICONS,
        EMOJI_BTN_POWER,
        EMOJI_BTN_SCREENSHOT,
        EMOJI_BTN_APPS,
        EMOJI_BTN_SOUND,
        EMOJI_BTN_SETTINGS,
        DEFAULT_PLAYER_ICONS,
        UiTemplatesMixin,
    )

LOG = logging.getLogger("pc-control-bot.telegram_ui")

# Strategic sanitizer: strips leading Unicode emojis, variation selectors, and joiners
# from button labels when icon_custom_emoji_id is used, preventing duplicate icons in Telegram UI.
EMOJI_PREFIX_RE = re.compile(
    r"^[\s\u200d\ufe0e\ufe0f\U0001f000-\U0001faff\u2600-\u27bf\u2300-\u23ff\u2b50\u25aa-\u25fe\u2190-\u21ff\u2900-\u297f\u2b00-\u2bff]+\s*"
)


class TelegramBotApi(UiTemplatesMixin):
    def __init__(self, token):
        self.token = token
        self.api_url = f"https://api.telegram.org/bot{token}"
        self._local = threading.local()
        self._session_override = None
        self._sessions = []
        self._thread_sessions = []
        self._sessions_lock = threading.Lock()

    @staticmethod
    def _build_session():
        sess = requests.Session()
        adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25, max_retries=3)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        return sess

    @staticmethod
    def _close_session_quietly(sess):
        try:
            sess.close()
        except Exception:
            pass

    def _prune_dead_sessions_locked(self):
        alive = []
        for thread_ref, sess in getattr(self, "_thread_sessions", []):
            thread = thread_ref() if callable(thread_ref) else None
            if thread is None or (hasattr(thread, "is_alive") and not thread.is_alive()):
                self._close_session_quietly(sess)
            else:
                alive.append((thread_ref, sess))
        self._thread_sessions = alive
        self._sessions = [sess for _, sess in alive]

    @property
    def session(self):
        override = getattr(self, "_session_override", None)
        if override is not None:
            return override
        local = getattr(self, "_local", None)
        if local is None:
            local = threading.local()
            self._local = local
        sess = getattr(local, "session", None)
        if sess is None:
            sess = self._build_session()
            local.session = sess
            cur_thread = threading.current_thread()
            try:
                weakref.finalize(cur_thread, self._close_session_quietly, sess)
            except TypeError:
                pass
            lock = getattr(self, "_sessions_lock", None)
            if lock is not None:
                with lock:
                    if not hasattr(self, "_thread_sessions"):
                        self._thread_sessions = []
                    self._prune_dead_sessions_locked()
                    self._thread_sessions.append((weakref.ref(cur_thread), sess))
                    self._sessions = [s for _, s in self._thread_sessions]
        return sess

    @session.setter
    def session(self, value):
        self._session_override = value

    def close_sessions(self):
        lock = getattr(self, "_sessions_lock", None)
        if lock is not None:
            with lock:
                sessions = list(getattr(self, "_sessions", ()))
                for _, sess in getattr(self, "_thread_sessions", ()):
                    if sess not in sessions:
                        sessions.append(sess)
                self._sessions.clear()
                if hasattr(self, "_thread_sessions"):
                    self._thread_sessions.clear()
        else:
            sessions = []
        for sess in sessions:
            self._close_session_quietly(sess)
        local = getattr(self, "_local", None)
        if local is not None and hasattr(local, "session"):
            delattr(local, "session")

    def sanitize_error(self, err):
        s = str(err)
        if self.token:
            s = s.replace(self.token, "[REDACTED_BOT_TOKEN]")
        return s

    @staticmethod
    def _sanitize_reply_markup(reply_markup):
        if not isinstance(reply_markup, dict):
            return reply_markup
        sanitized = dict(reply_markup)
        if "inline_keyboard" in sanitized and isinstance(sanitized["inline_keyboard"], list):
            new_rows = []
            for row in sanitized["inline_keyboard"]:
                if isinstance(row, list):
                    new_row = []
                    for btn in row:
                        if isinstance(btn, dict):
                            btn_copy = {k: v for k, v in btn.items() if v is not None}
                            if "style" in btn_copy and btn_copy["style"] not in {"primary", "success", "danger"}:
                                btn_copy.pop("style", None)
                            has_custom_icon = False
                            if "icon_custom_emoji_id" in btn_copy:
                                val = btn_copy["icon_custom_emoji_id"]
                                val_str = str(val).strip() if val is not None else ""
                                if not val_str or val_str.lower() in ("none", "null") or not val_str.isdigit():
                                    btn_copy.pop("icon_custom_emoji_id", None)
                                else:
                                    btn_copy["icon_custom_emoji_id"] = val_str
                                    has_custom_icon = True
                            # Strategic fix: when custom emoji chip is displayed (or in all styled buttons),
                            # strip redundant leading unicode emojis from button text so they never double-render.
                            if has_custom_icon and "text" in btn_copy and isinstance(btn_copy["text"], str):
                                cleaned_text = EMOJI_PREFIX_RE.sub("", btn_copy["text"]).strip()
                                if cleaned_text:
                                    btn_copy["text"] = cleaned_text
                            new_row.append(btn_copy)
                        else:
                            new_row.append(btn)
                    new_rows.append(new_row)
                else:
                    new_rows.append(row)
            sanitized["inline_keyboard"] = new_rows
        return sanitized

    def call_raw(self, method, payload=None, timeout=25):
        url = f"{self.api_url}/{method}"
        if payload and isinstance(payload, dict) and "reply_markup" in payload:
            payload = dict(payload)
            payload["reply_markup"] = self._sanitize_reply_markup(payload["reply_markup"])
        try:
            response = self.session.post(url, json=payload or {}, timeout=timeout)
            try:
                data = response.json()
                return data
            except Exception:
                return {"ok": False, "description": f"HTTP {response.status_code}"}
        except Exception as e:
            return {"ok": False, "description": self.sanitize_error(e)}

    def call(self, method, payload=None, timeout=25):
        data = self.call_raw(method, payload, timeout=timeout)
        if data.get("ok"):
            return data.get("result")
        safe_data = dict(data)
        if "description" in safe_data:
            safe_data["description"] = self.sanitize_error(safe_data["description"])
        LOG.warning("Telegram API %s error: %s", method, safe_data)
        return None

    def send_message(self, chat_id, text, reply_markup=None, message_id=None, business_connection_id=None):
        if message_id:
            payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
            }
            if business_connection_id:
                payload["business_connection_id"] = business_connection_id
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup

            data = self.call_raw("editMessageText", payload, timeout=12)
            if data.get("ok"):
                return data.get("result")

            desc = (data.get("description") or "").lower()
            if "message is not modified" in desc:
                LOG.info("Telegram message %s not modified, treating as success.", message_id)
                return {"message_id": message_id, "not_modified": True}

            if "message to edit not found" in desc or "message can't be edited" in desc:
                LOG.warning("editMessageText failed (%s), message deleted/unfound, falling back to sendMessage", desc)
            else:
                LOG.warning("editMessageText failed (%s), NOT falling back to sendMessage to avoid clutter", desc)
                return None

        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload, timeout=15)

    def delete_message(self, chat_id, message_id, business_connection_id=None):
        if not chat_id or not message_id:
            return None
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        return self.call("deleteMessage", payload, timeout=10)

    def _post_multipart(self, method, data, files=None, timeout=15, message_id=None, warn_on_error=False):
        url = f"{self.api_url}/{method}"
        try:
            res = self.session.post(url, data=data, files=files, timeout=timeout)
            try:
                resp_data = res.json()
            except Exception:
                resp_data = {"ok": False, "description": f"HTTP {res.status_code}"}
        except Exception as e:
            msg = self.sanitize_error(e)
            log_fn = LOG.warning if warn_on_error else LOG.error
            log_fn("%s failed: %s", method, msg)
            return None

        if isinstance(resp_data, dict) and resp_data.get("ok"):
            return resp_data.get("result")

        desc = str(resp_data.get("description") or "") if isinstance(resp_data, dict) else str(resp_data)
        if message_id is not None and "message is not modified" in desc.lower():
            return {"message_id": message_id, "not_modified": True}

        safe_resp = dict(resp_data) if isinstance(resp_data, dict) else {"raw": self.sanitize_error(resp_data)}
        if "description" in safe_resp:
            safe_resp["description"] = self.sanitize_error(safe_resp["description"])
        log_fn = LOG.warning if warn_on_error else LOG.error
        log_fn("%s API error: %s", method, safe_resp)
        return None

    def send_photo(
        self,
        chat_id,
        photo,
        caption=None,
        reply_markup=None,
        business_connection_id=None,
        filename="cover.jpg",
    ):
        data = {
            "chat_id": chat_id,
            "parse_mode": "HTML",
        }
        if caption:
            data["caption"] = caption
        if reply_markup is not None:
            clean_markup = self._sanitize_reply_markup(reply_markup)
            data["reply_markup"] = json.dumps(clean_markup) if isinstance(clean_markup, dict) else clean_markup
        if business_connection_id:
            data["business_connection_id"] = business_connection_id

        if isinstance(photo, str):
            data["photo"] = photo
            return self._post_multipart("sendPhoto", data, timeout=15)
        content_type = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"
        files = {"photo": (filename, photo, content_type)}
        return self._post_multipart("sendPhoto", data, files=files, timeout=25)

    def send_document(
        self,
        chat_id,
        document,
        filename="output.txt",
        caption=None,
        reply_markup=None,
        business_connection_id=None,
    ):
        data = {
            "chat_id": chat_id,
            "parse_mode": "HTML",
        }
        if caption:
            data["caption"] = caption
        if reply_markup is not None:
            clean_markup = self._sanitize_reply_markup(reply_markup)
            data["reply_markup"] = json.dumps(clean_markup) if isinstance(clean_markup, dict) else clean_markup
        if business_connection_id:
            data["business_connection_id"] = business_connection_id

        doc_bytes = document.encode("utf-8") if isinstance(document, str) else document
        files = {"document": (filename, doc_bytes, "text/plain; charset=utf-8")}
        return self._post_multipart("sendDocument", data, files=files, timeout=30)

    def edit_message_caption(
        self,
        chat_id,
        message_id,
        caption,
        reply_markup=None,
        business_connection_id=None,
    ):
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id

        data = self.call_raw("editMessageCaption", payload, timeout=10)
        if data.get("ok"):
            return data.get("result")
        desc = (data.get("description") or "").lower()
        if "message is not modified" in desc:
            return {"message_id": message_id, "not_modified": True}
        safe_data = dict(data)
        if "description" in safe_data:
            safe_data["description"] = self.sanitize_error(safe_data["description"])
        LOG.warning("editMessageCaption failed: %s", safe_data)
        return None

    def edit_message_media(
        self,
        chat_id,
        message_id,
        photo,
        caption=None,
        reply_markup=None,
        business_connection_id=None,
        filename="cover.jpg",
    ):
        media_dict = {
            "type": "photo",
            "parse_mode": "HTML",
        }
        if caption:
            media_dict["caption"] = caption

        data = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        if reply_markup is not None:
            clean_markup = self._sanitize_reply_markup(reply_markup)
            data["reply_markup"] = json.dumps(clean_markup) if isinstance(clean_markup, dict) else clean_markup
        if business_connection_id:
            data["business_connection_id"] = business_connection_id

        if isinstance(photo, str):
            media_dict["media"] = photo
            data["media"] = json.dumps(media_dict)
            return self._post_multipart("editMessageMedia", data, timeout=15, message_id=message_id, warn_on_error=True)
        media_dict["media"] = "attach://photo"
        data["media"] = json.dumps(media_dict)
        content_type = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"
        files = {"photo": (filename, photo, content_type)}
        return self._post_multipart("editMessageMedia", data, files=files, timeout=25, message_id=message_id, warn_on_error=True)

    def answer_callback(self, callback_id, text=None, alert=False):
        if not callback_id:
            return
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = alert
        self.call("answerCallbackQuery", payload, timeout=8)
