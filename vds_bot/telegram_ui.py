#!/usr/bin/env python3

"""Telegram Bot API HTTP client with thread-pooled non-blocking network calls."""

import html
import json
import logging
import threading
import time
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


class TelegramBotApi(UiTemplatesMixin):
    def __init__(self, token):
        self.token = token
        self.api_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25, max_retries=3)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._lock = threading.Lock()

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
                            if "icon_custom_emoji_id" in btn_copy:
                                val = btn_copy["icon_custom_emoji_id"]
                                val_str = str(val).strip() if val is not None else ""
                                if not val_str or val_str.lower() in ("none", "null") or not val_str.isdigit():
                                    btn_copy.pop("icon_custom_emoji_id", None)
                                else:
                                    btn_copy["icon_custom_emoji_id"] = val_str
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
            if reply_markup is None:
                payload["reply_markup"] = {"inline_keyboard": []}
            else:
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

    def send_photo(
        self,
        chat_id,
        photo,
        caption=None,
        reply_markup=None,
        business_connection_id=None,
        filename="cover.jpg",
    ):
        url = f"{self.api_url}/sendPhoto"
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
            try:
                res = self.session.post(url, data=data, timeout=15)
                res.raise_for_status()
                return res.json().get("result")
            except Exception as e:
                LOG.error("send_photo URL failed: %s", self.sanitize_error(e))
                return None
        else:
            content_type = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"
            files = {"photo": (filename, photo, content_type)}
            try:
                res = self.session.post(url, data=data, files=files, timeout=25)
                res.raise_for_status()
                return res.json().get("result")
            except Exception as e:
                LOG.error("send_photo bytes failed: %s", self.sanitize_error(e))
                return None

    def send_document(
        self,
        chat_id,
        document,
        filename="output.txt",
        caption=None,
        reply_markup=None,
        business_connection_id=None,
    ):
        url = f"{self.api_url}/sendDocument"
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
        try:
            res = self.session.post(url, data=data, files=files, timeout=30)
            res.raise_for_status()
            return res.json().get("result")
        except Exception as e:
            LOG.error("send_document failed: %s", self.sanitize_error(e))
            return None

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
        LOG.warning("editMessageCaption failed: %s", data)
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
        url = f"{self.api_url}/editMessageMedia"
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
            try:
                res = self.session.post(url, data=data, timeout=15)
                res.raise_for_status()
                return res.json().get("result")
            except Exception as e:
                LOG.warning("editMessageMedia URL failed: %s", self.sanitize_error(e))
                return None
        else:
            media_dict["media"] = "attach://photo"
            data["media"] = json.dumps(media_dict)
            content_type = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"
            files = {"photo": (filename, photo, content_type)}
            try:
                res = self.session.post(url, data=data, files=files, timeout=25)
                res.raise_for_status()
                return res.json().get("result")
            except Exception as e:
                LOG.warning("editMessageMedia bytes failed: %s", self.sanitize_error(e))
                return None

    def answer_callback(self, callback_id, text=None, alert=False):
        if not callback_id:
            return
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = alert
        self.call("answerCallbackQuery", payload, timeout=8)
