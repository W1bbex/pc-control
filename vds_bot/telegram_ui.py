#!/usr/bin/env python3

import html
import json
import logging
import threading
import time
import requests
from requests.adapters import HTTPAdapter

LOG = logging.getLogger("pc-control-bot.telegram_ui")

CANONICAL_ICONS = {
    "power": "5330077297921795474",
    "screenshot": "5330238646958204875",
    "sound": "5330140476890718558",
    "sound_mute": "5330388528431935587",
    "screen": "5330015235644368681",
    "apps": "5330130731609929749",
    "game": "5330130731609929749",
    "gear": "5330260680140431237",
    "cpu": "5330128631370914549",
    "ram": "5330429420815559823",
    "disk": "5330487329859612207",
    "network": "5330474032640861878",
    "lock": "5330152648828033682",
    "light": "5330514620081809665",
    "music": "5330477356945551054",
    "telegram": "5330467899427564107",
    "chrome": "5330191582706576698",
    "terminal": "5330563608478787902",
    "pc": "5330499003580721030",
    "user": "5330478284658488376",
    "folder": "5330394360997517972",
    "back": "5330209080403340437",
    "refresh": "5330544792227063795",
    "cross": "5330015179809794606",
    "trash": "5330144857757362833",
    "pencil": "5330305068627436798",
    "search": "5330079544189695031",
    "play": "5330015871299527534",
    "pause": "5330570905628222348",
    "fwd10": "5330317725896059016",
    "back10": "5330294859490179153",
    "next": "5330255053733276330",
    "prev": "5330363570376977789",
    "repeat": "5330448799707998190",
    "heart": "5330276670303674347",
    "headphones": "5330526830673831881",
    "dot": "5330193051585391352",
    "check": "5330273230034871570",
    "clock": "5330473500064912704",
    "chart": "5330382442463271794",
    "wifi": "5330160856510540663",
    "battery": "5330277593721644293",
    "status_active": "5330273230034871570",
    "status_inactive": "5330015179809794606",
    "wave": "5330448799707998190",
    "linux": "5332695978136874502",
    "windows": "5332280534540264957",
    "app_telegram": "5251740431632344455",
    "app_yandex_music": "5251544533879006440",
    "app_happ": "5251339414830880848",
    "app_incy": "5319035276306196317",
    "app_chrome": "5251317871274929314",
    "app_antigravity": "5251292384938994108",
    "app_dolphin": "5278321743709124204",
}

def _load_canonical_icons():
    from pathlib import Path
    icons = dict(CANONICAL_ICONS)
    for candidate in [
        Path(__file__).resolve().parent / "resources" / "icons.json",
        Path(__file__).resolve().parent.parent / "resources" / "icons.json",
    ]:
        if candidate.exists():
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        icons.update(data)
                        return icons
            except Exception:
                pass
    return icons

ICONS = _load_canonical_icons()

EMOJI_BTN_POWER = ICONS.get("power", "5330077297921795474")
EMOJI_BTN_SCREENSHOT = ICONS.get("screenshot", "5330238646958204875")
EMOJI_BTN_APPS = ICONS.get("apps", "5330130731609929749")
EMOJI_BTN_SOUND = ICONS.get("sound", "5330140476890718558")
EMOJI_BTN_SETTINGS = ICONS.get("gear", "5330260680140431237")

DEFAULT_PLAYER_ICONS = {
    "btn_play": ICONS.get("play", "5330015871299527534"),
    "btn_pause": ICONS.get("pause", "5330570905628222348"),
    "btn_next": ICONS.get("next", "5330255053733276330"),
    "btn_prev": ICONS.get("prev", "5330363570376977789"),
    "btn_fwd10": ICONS.get("fwd10", "5330317725896059016"),
    "btn_back10": ICONS.get("back10", "5330294859490179153"),
    "btn_wave": ICONS.get("wave", "5330448799707998190"),
    "tag_note": ICONS.get("music", "5330477356945551054"),
    "tag_artist": ICONS.get("headphones", "5330526830673831881"),
    "tag_status_play": ICONS.get("play", "5330015871299527534"),
    "tag_status_pause": ICONS.get("pause", "5330570905628222348"),
    "tag_clock": ICONS.get("clock", "5330473500064912704"),
    "tag_slider_dot": ICONS.get("dot", "5330193051585391352"),
    "tag_heart_liked": ICONS.get("heart", "5330276670303674347"),
    "tag_heart_unliked": ICONS.get("heart", "5330276670303674347"),
}


class TelegramBotApi:
    def __init__(self, token):
        self.token = token
        self.api_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=3)
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
            with self._lock:
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
            data["reply_markup"] = json.dumps(reply_markup) if isinstance(reply_markup, dict) else reply_markup
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

    @staticmethod
    def back_button(target="menu"):
        btn = {"text": "Назад", "callback_data": target}
        if ICONS.get("back"):
            btn["icon_custom_emoji_id"] = ICONS.get("back")
        return [btn]

    def main_menu_markup(self, pc_state=None, session=None, system_status=None):
        state_val = ""
        sess_val = ""
        sys_val = ""

        if pc_state is not None:
            raw_state = getattr(pc_state, "state", pc_state)
            state_val = getattr(raw_state, "value", str(raw_state))
            sess_val = getattr(pc_state, "session_status", "")
            sys_val = getattr(pc_state, "system_status", "")

        if session is not None:
            sess_val = session
        if system_status is not None:
            sys_val = system_status

        if sess_val == "unlocked" or state_val in ("READY", "DEGRADED"):
            bottom_btn = {"text": "Выключить", "style": "danger", "callback_data": "shutdown_pc"}
        elif sess_val in ("locked", "login_screen") or state_val == "LOCKED":
            bottom_btn = {"text": "Разблокировать ПК", "style": "success", "callback_data": "unlock_windows_manual"}
        elif state_val in ("SHUTDOWN_OBSERVED", "UNREACHABLE", "OFFLINE") or sys_val == "offline":
            bottom_btn = {"text": "Включить ПК", "style": "success", "callback_data": "power_on"}
        else:
            if sys_val == "offline":
                bottom_btn = {"text": "Включить ПК", "style": "success", "callback_data": "power_on"}
            else:
                bottom_btn = {"text": "Выключить", "style": "danger", "callback_data": "shutdown_pc"}

        buttons = [
            [{"text": "Питание", "style": "success", "callback_data": "power_menu", "icon_custom_emoji_id": EMOJI_BTN_POWER}],
            [
                {"text": "Скриншот", "style": "primary", "callback_data": "screenshot", "icon_custom_emoji_id": EMOJI_BTN_SCREENSHOT},
                {"text": "Программы", "style": "primary", "callback_data": "apps_menu", "icon_custom_emoji_id": EMOJI_BTN_APPS},
            ],
            [
                {"text": "Звук", "style": "primary", "callback_data": "sound_menu", "icon_custom_emoji_id": EMOJI_BTN_SOUND},
                {"text": "Настройки", "style": "primary", "callback_data": "settings_menu", "icon_custom_emoji_id": EMOJI_BTN_SETTINGS},
            ],
            [bottom_btn],
        ]

        return {"inline_keyboard": buttons}

    def settings_menu_markup(self):
        return {
            "inline_keyboard": [
                [
                    {"text": "Мониторинг", "style": "primary", "icon_custom_emoji_id": ICONS.get("cpu"), "callback_data": "monitoring_menu"},
                    {"text": "Графики", "style": "primary", "icon_custom_emoji_id": ICONS.get("chart"), "callback_data": "history_menu"},
                ],
                [
                    {"text": "Физическая кнопка", "style": "primary", "icon_custom_emoji_id": ICONS.get("game"), "callback_data": "physical_button"},
                    {"text": "Активность", "style": "primary", "icon_custom_emoji_id": ICONS.get("apps"), "callback_data": "menu_activity"},
                ],
                [
                    {"text": "Подсветка (ARGB)", "style": "primary", "icon_custom_emoji_id": ICONS.get("light"), "callback_data": "led_menu"},
                    {"text": "Консоль", "style": "primary", "icon_custom_emoji_id": ICONS.get("terminal"), "callback_data": "console_open"},
                ],
                self.back_button("menu"),
            ]
        }

    def led_menu_markup(self):
        return {
            "inline_keyboard": [
                [
                    {"text": "Для глаз (Идеал)", "style": "success", "icon_custom_emoji_id": ICONS.get("light"), "callback_data": "led_action:preset:eye"},
                    {"text": "Для фото (Камера)", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "led_action:preset:photo"},
                ],
                [
                    {"text": "1", "style": "primary", "callback_data": "led_action:preset:1"},
                    {"text": "2", "style": "primary", "callback_data": "led_action:preset:2"},
                    {"text": "3", "style": "primary", "callback_data": "led_action:preset:3"},
                ],
                [
                    {"text": "4", "style": "primary", "callback_data": "led_action:preset:4"},
                    {"text": "5", "style": "primary", "callback_data": "led_action:preset:5"},
                    {"text": "6", "style": "primary", "callback_data": "led_action:preset:6"},
                ],
                [
                    {"text": "7", "style": "primary", "callback_data": "led_action:preset:7"},
                    {"text": "8", "style": "primary", "callback_data": "led_action:preset:8"},
                    {"text": "9", "style": "primary", "callback_data": "led_action:preset:9"},
                ],
                [
                    {"text": "10 (Идеал)", "style": "primary", "callback_data": "led_action:preset:10"},
                    {"text": "11", "style": "primary", "callback_data": "led_action:preset:11"},
                    {"text": "12", "style": "primary", "callback_data": "led_action:preset:12"},
                ],
                [
                    {"text": "Включить", "style": "success", "icon_custom_emoji_id": ICONS.get("light"), "callback_data": "led_action:on"},
                    {"text": "Выключить", "style": "danger", "callback_data": "led_action:off"},
                ],
                [
                    {"text": "30%", "style": "primary", "callback_data": "led_action:bright:80"},
                    {"text": "60%", "style": "primary", "callback_data": "led_action:bright:160"},
                    {"text": "100%", "style": "primary", "callback_data": "led_action:bright:255"},
                ],
                self.back_button("settings_menu"),
            ]
        }

    def sound_menu_markup(self):
        return {
            "inline_keyboard": [
                [{"text": "Вкл / Выкл звук", "style": "primary", "icon_custom_emoji_id": ICONS.get("sound"), "callback_data": "volume:toggle"}],
                [
                    {"text": "25%", "style": "primary", "callback_data": "volume:25"},
                    {"text": "50%", "style": "primary", "callback_data": "volume:50"},
                    {"text": "75%", "style": "primary", "callback_data": "volume:75"},
                    {"text": "100%", "style": "primary", "callback_data": "volume:100"},
                ],
                self.back_button("menu"),
            ]
        }

    def power_menu_markup(self, pc_state=None, session=None, system_status=None):
        state_val = ""
        sess_val = ""
        sys_val = ""

        if pc_state is not None:
            raw_state = getattr(pc_state, "state", pc_state)
            state_val = getattr(raw_state, "value", str(raw_state))
            sess_val = getattr(pc_state, "session_status", "")
            sys_val = getattr(pc_state, "system_status", "")

        if session is not None:
            sess_val = session
        if system_status is not None:
            sys_val = system_status

        is_pc_on = (
            sess_val in ("unlocked", "locked", "login_screen")
            or state_val in ("READY", "DEGRADED", "LOCKED", "BOOTING")
            or sys_val == "online"
        )

        if not is_pc_on:
            return {
                "inline_keyboard": [
                    [{"text": "Включить ПК", "style": "success", "callback_data": "power_on"}],
                    self.back_button("menu"),
                ]
            }

        # PC is ON: 1 main power button, 1 adaptive lock button
        if sess_val == "unlocked":
            lock_btn = {"text": "Заблокировать Windows", "style": "danger", "callback_data": "lock_windows"}
        else:
            lock_btn = {"text": "Разблокировать Windows", "style": "success", "callback_data": "unlock_windows_manual"}
        if ICONS.get("lock"):
            lock_btn["icon_custom_emoji_id"] = ICONS.get("lock")

        shutdown_btn = {"text": "Выключить ПК", "style": "danger", "callback_data": "shutdown_pc"}
        if ICONS.get("power"):
            shutdown_btn["icon_custom_emoji_id"] = ICONS.get("power")

        screen_btn = {"text": "Выключить экран", "style": "primary", "callback_data": "toggle_screen"}
        if ICONS.get("screen"):
            screen_btn["icon_custom_emoji_id"] = ICONS.get("screen")

        reboot_btn = {"text": "Перезагрузить", "style": "primary", "callback_data": "reboot_pc"}
        if ICONS.get("refresh"):
            reboot_btn["icon_custom_emoji_id"] = ICONS.get("refresh")

        return {
            "inline_keyboard": [
                [shutdown_btn],
                [lock_btn],
                [
                    screen_btn,
                    {"text": "Свернуть окна", "style": "primary", "callback_data": "minimize_windows"},
                ],
                [
                    {"text": "Сон", "style": "primary", "callback_data": "sleep_pc"},
                    reboot_btn,
                ],
                self.back_button("menu"),
            ]
        }

    def shutdown_menu_markup(self, scheduled_ts=None):
        rows = [
            [{"text": "Выключить сейчас", "style": "danger", "callback_data": "shutdown_now_confirm"}],
            [
                {"text": "15 мин", "style": "primary", "callback_data": "shutdown_after:15"},
                {"text": "30 мин", "style": "primary", "callback_data": "shutdown_after:30"},
                {"text": "1 час", "style": "primary", "callback_data": "shutdown_after:60"},
            ],
            [{"text": "Указать HH:MM (МСК)", "style": "primary", "callback_data": "shutdown_at_time"}],
        ]
        if scheduled_ts:
            rows.append([{"text": "Отменить активный таймер", "style": "danger", "callback_data": "shutdown_schedule_cancel"}])
        rows.append(self.back_button("power_menu"))
        return {"inline_keyboard": rows}

    def reboot_menu_markup(self):
        reboot_unlock_btn = {
            "text": "Перезагрузка + Разблок",
            "style": "success",
            "callback_data": "reboot_unlock",
        }
        if ICONS.get("refresh"):
            reboot_unlock_btn["icon_custom_emoji_id"] = ICONS.get("refresh")

        reboot_plain_btn = {
            "text": "Только перезагрузка",
            "style": "primary",
            "callback_data": "reboot_confirm",
        }
        if ICONS.get("refresh"):
            reboot_plain_btn["icon_custom_emoji_id"] = ICONS.get("refresh")

        return {
            "inline_keyboard": [
                [reboot_unlock_btn],
                [reboot_plain_btn],
                [self.back_button("power_menu")],
            ]
        }

    def apps_menu_markup(self):
        return {
            "inline_keyboard": [
                [{"text": "Запустить рабочий сетап", "style": "success", "icon_custom_emoji_id": ICONS.get("apps") or ICONS.get("game"), "callback_data": "workspace_start_direct"}],
                [
                    {"text": "Telegram", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_telegram") or ICONS.get("telegram"), "callback_data": "app:Telegram"},
                    {"text": "Музыка", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_yandex_music") or ICONS.get("music"), "callback_data": "app:Музыка"},
                ],
                [
                    {"text": "Dolphin", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_dolphin"), "callback_data": "app:Dolphin"},
                    {"text": "Chrome", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_chrome") or ICONS.get("chrome"), "callback_data": "app:Chrome"},
                ],
                [
                    {"text": "Happ", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_happ") or ICONS.get("network"), "callback_data": "app:Happ"},
                    {"text": "INCY", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_incy") or ICONS.get("network"), "callback_data": "app:INCY"},
                ],
                [
                    {"text": "Antigravity", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_antigravity") or ICONS.get("terminal"), "callback_data": "app:Antigravity"},
                    {"text": "Консоль (Терминал)", "style": "primary", "icon_custom_emoji_id": ICONS.get("terminal"), "callback_data": "console_open"},
                ],
                self.back_button("menu"),
            ]
        }

    def workspace_vpn_choice_markup(self, active_vpn="incy"):
        happ_label = "Happ (VPN)"
        incy_label = "INCY (VPN)"
        happ_icon = ICONS.get("app_happ") or ICONS.get("network")
        incy_icon = ICONS.get("app_incy") or ICONS.get("network")
        return {
            "inline_keyboard": [
                [
                    {
                        "text": incy_label,
                        "style": "success" if active_vpn == "incy" else "primary",
                        "icon_custom_emoji_id": incy_icon,
                        "callback_data": "workspace_vpn:incy",
                    },
                    {
                        "text": happ_label,
                        "style": "success" if active_vpn == "happ" else "primary",
                        "icon_custom_emoji_id": happ_icon,
                        "callback_data": "workspace_vpn:happ",
                    },
                ],
                [{"text": "Отмена", "style": "danger", "icon_custom_emoji_id": ICONS.get("cross"), "callback_data": "menu"}],
            ]
        }

    def monitoring_menu_markup(self):
        return {
            "inline_keyboard": [
                [{"text": "Обновить состояние", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": "telemetry_now"}],
                [{"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"}],
                [{"text": "Графики", "style": "primary", "icon_custom_emoji_id": ICONS.get("chart"), "callback_data": "history_menu"}],
                self.back_button("settings_menu"),
            ]
        }

    def history_menu_markup(self):
        return {
            "inline_keyboard": [
                [
                    {"text": "1 час", "style": "primary", "callback_data": "history:1h"},
                    {"text": "6 часов", "style": "primary", "callback_data": "history:6h"},
                    {"text": "24 часа", "style": "primary", "callback_data": "history:24h"},
                ],
                [
                    {"text": "7 дней", "style": "primary", "callback_data": "history:7d"},
                    {"text": "30 дней", "style": "primary", "callback_data": "history:30d"},
                ],
                self.back_button("monitoring_menu"),
            ]
        }

    def player_card_text(self, media_state):
        if not media_state or not media_state.get("active"):
            return "🎵 <b>Яндекс Музыка</b>\n\n<i>Сейчас ничего не играет или плеер закрыт.</i>"

        title = media_state.get("title") or "Неизвестный трек"
        artist = media_state.get("artist") or "Неизвестный исполнитель"
        pos = float(media_state.get("position") or 0.0)
        dur = float(media_state.get("duration") or 0.0)
        is_playing = media_state.get("is_playing", False)
        icons = dict(DEFAULT_PLAYER_ICONS)
        if media_state and media_state.get("ui_icons"):
            icons.update(media_state["ui_icons"])

        tag_note = icons.get("tag_note")
        tag_artist = icons.get("tag_artist")
        note_badge = f'<tg-emoji emoji-id="{tag_note}">🎵</tg-emoji> ' if tag_note else ""

        # Artist Avatar: Use artist circular avatar custom emoji if available, fallback to white headphone tag
        artist_avatar_id = media_state.get("artist_avatar_id") if media_state else None
        if artist_avatar_id:
            artist_badge = f'<tg-emoji emoji-id="{artist_avatar_id}">👤</tg-emoji> '
        elif tag_artist:
            artist_badge = f'<tg-emoji emoji-id="{tag_artist}">🎧</tg-emoji> '
        else:
            artist_badge = ""

        # Title and artist formatting
        safe_title = html.escape(str(title).strip(), quote=False)
        safe_artist = html.escape(str(artist).strip(), quote=False)

        # 2. Format timeline & progress bar
        # Add +2s latency compensation when playing, and +1s to duration
        display_pos = (pos + 2.0) if is_playing else pos
        display_dur = (dur + 1.0) if dur > 0 else 0.0
        if display_dur > 0:
            display_pos = min(display_dur, display_pos)

        def format_time(seconds):
            m = int(round(seconds)) // 60
            s = int(round(seconds)) % 60
            return f"{m:02d}:{s:02d}"

        pos_str = format_time(display_pos)
        dur_str = format_time(display_dur) if display_dur > 0 else "--:--"

        tag_dot = icons.get("tag_slider_dot") or ICONS.get("dot", "5330193051585391352")
        dot = f'<tg-emoji emoji-id="{tag_dot}">⚪</tg-emoji>'

        bar_len = 12
        if display_dur > 0:
            pct = min(1.0, max(0.0, display_pos / display_dur))
            filled = int(round(pct * (bar_len - 1)))
            bar = "━" * filled + dot + "━" * (bar_len - 1 - filled)
        else:
            bar = "━" * (bar_len // 2) + dot + "━" * (bar_len // 2)

        # Vector play/pause status icon matching the minimalist button style
        btn_play = icons.get("btn_play") or ICONS.get("play", "5330015871299527534")
        btn_pause = icons.get("btn_pause") or ICONS.get("pause", "5330570905628222348")
        status_eid = (btn_play if is_playing else btn_pause) or icons.get("tag_status_play" if is_playing else "tag_status_pause")
        if status_eid:
            status_badge = f'<tg-emoji emoji-id="{status_eid}">{"▶️" if is_playing else "⏸"}</tg-emoji>'
        else:
            status_badge = "▶️" if is_playing else "⏸"

        text = (
            f'{note_badge}<b>{safe_title}</b>\n'
            f'{artist_badge}<i>{safe_artist}</i>\n\n'
            f'{bar}\n'
            f'{status_badge} <b>{pos_str}</b> / {dur_str}'
        )
        return text

    def player_card_caption(self, media_state):
        if not media_state or not media_state.get("active"):
            return "🎵 <b>Яндекс Музыка</b>\n\n<i>Сейчас ничего не играет или плеер закрыт.</i>"

        title = media_state.get("title") or "Неизвестный трек"
        artist = media_state.get("artist") or "Неизвестный исполнитель"
        pos = float(media_state.get("position") or 0.0)
        dur = float(media_state.get("duration") or 0.0)
        is_playing = media_state.get("is_playing", False)
        icons = dict(DEFAULT_PLAYER_ICONS)
        if media_state and media_state.get("ui_icons"):
            icons.update(media_state["ui_icons"])

        tag_note = icons.get("tag_note")
        tag_artist = icons.get("tag_artist")
        note_badge = f'<tg-emoji emoji-id="{tag_note}">🎵</tg-emoji> ' if tag_note else ""

        artist_avatar_id = media_state.get("artist_avatar_id") if media_state else None
        if artist_avatar_id:
            artist_badge = f'<tg-emoji emoji-id="{artist_avatar_id}">👤</tg-emoji> '
        elif tag_artist:
            artist_badge = f'<tg-emoji emoji-id="{tag_artist}">🎧</tg-emoji> '
        else:
            artist_badge = ""

        safe_title = html.escape(str(title).strip(), quote=False)
        safe_artist = html.escape(str(artist).strip(), quote=False)

        display_pos = (pos + 2.0) if is_playing else pos
        display_dur = (dur + 1.0) if dur > 0 else 0.0
        if display_dur > 0:
            display_pos = min(display_dur, display_pos)

        def format_time(seconds):
            m = int(round(seconds)) // 60
            s = int(round(seconds)) % 60
            return f"{m:02d}:{s:02d}"

        pos_str = format_time(display_pos)
        dur_str = format_time(display_dur) if display_dur > 0 else "--:--"

        tag_dot = icons.get("tag_slider_dot") or ICONS.get("dot", "5330193051585391352")
        dot = f'<tg-emoji emoji-id="{tag_dot}">⚪</tg-emoji>'

        bar_len = 12
        if display_dur > 0:
            pct = min(1.0, max(0.0, display_pos / display_dur))
            filled = int(round(pct * (bar_len - 1)))
            bar = "━" * filled + dot + "━" * (bar_len - 1 - filled)
        else:
            bar = "━" * (bar_len // 2) + dot + "━" * (bar_len // 2)

        btn_play = icons.get("btn_play") or ICONS.get("play", "5330015871299527534")
        btn_pause = icons.get("btn_pause") or ICONS.get("pause", "5330570905628222348")
        status_eid = (btn_play if is_playing else btn_pause) or icons.get("tag_status_play" if is_playing else "tag_status_pause")
        if status_eid:
            status_badge = f'<tg-emoji emoji-id="{status_eid}">{"▶️" if is_playing else "⏸"}</tg-emoji>'
        else:
            status_badge = "▶️" if is_playing else "⏸"

        text = (
            f'{note_badge}<b>{safe_title}</b>\n'
            f'{artist_badge}<i>{safe_artist}</i>\n\n'
            f'{bar}\n'
            f'{status_badge} <b>{pos_str}</b> / {dur_str}'
        )
        return text

    def player_card_markup(self, media_state):
        is_playing = media_state.get("is_playing", False) if media_state else False
        icons = dict(DEFAULT_PLAYER_ICONS)
        if media_state and media_state.get("ui_icons"):
            icons.update(media_state["ui_icons"])

        btn_back10 = icons.get("btn_back10")
        btn_fwd10 = icons.get("btn_fwd10")
        btn_prev = icons.get("btn_prev")
        btn_next = icons.get("btn_next")
        btn_wave = icons.get("btn_wave")
        btn_play_pause = icons.get("btn_pause" if is_playing else "btn_play")

        # Row 1: 10s back, Play/Pause, 10s fwd
        r1 = [
            {"text": "10с", "callback_data": "player_seek_back_10"},
            {"text": "Пауза" if is_playing else "Плей", "style": "primary", "callback_data": "player_toggle_play"},
            {"text": "10с", "callback_data": "player_seek_fwd_10"},
        ]
        if btn_back10:
            r1[0]["icon_custom_emoji_id"] = btn_back10
        if btn_play_pause:
            r1[1]["icon_custom_emoji_id"] = btn_play_pause
        if btn_fwd10:
            r1[2]["icon_custom_emoji_id"] = btn_fwd10

        # Row 2: Prev, Wave, Next (3 buttons, 'Волна' fits perfectly on mobile without truncation)
        r2 = [
            {"text": "Пред", "callback_data": "player_prev"},
            {"text": "Волна", "style": "success", "callback_data": "player_wave"},
            {"text": "След", "callback_data": "player_next"},
        ]
        if btn_prev:
            r2[0]["icon_custom_emoji_id"] = btn_prev
        if btn_wave:
            r2[1]["icon_custom_emoji_id"] = btn_wave
        if btn_next:
            r2[2]["icon_custom_emoji_id"] = btn_next

        # Row 3: Single full-width Like button
        is_liked = media_state.get("is_liked", False) if media_state else False
        btn_heart = icons.get("tag_heart_liked" if is_liked else "tag_heart_unliked")
        heart_text = "В любимых" if is_liked else "В избранное"
        r3_btn = {"text": heart_text, "callback_data": "player_toggle_like"}
        if btn_heart:
            r3_btn["icon_custom_emoji_id"] = btn_heart
        r3 = [r3_btn]

        return {"inline_keyboard": [r1, r2, r3]}

    def activity_card_text(self, summary_data, date_str):
        if not summary_data:
            return f"📊 <b>Активность за {date_str}</b>\n\n<i>Записей за этот день пока нет.</i>"
        lines = [f"📊 <b>Активность за {date_str}</b>:\n"]
        for i, item in enumerate(summary_data[:12], 1):
            app = item.get("app_name", "unknown")
            dur = float(item.get("total_duration") or 0.0)
            eid = item.get("emoji_id")
            emoji_tag = f'<tg-emoji emoji-id="{eid}">🪟</tg-emoji> ' if eid else "📱 "
            m = int(dur) // 60
            h = m // 60
            rem_m = m % 60
            time_str = f"{h}ч {rem_m}м" if h > 0 else f"{rem_m}м"
            lines.append(f"{i}. {emoji_tag}<b>{app}</b> — {time_str}")
        return "\n".join(lines)

    def console_card_text(self, target="desktop", shell="powershell", last_cmd=None, is_executing=False):
        shell_name = "PowerShell" if (shell or "").lower() == "powershell" else "CMD"
        target_name = "Desktop (User)" if (target or "").lower() == "desktop" else "SYSTEM (Admin)"
        
        status_act_id = ICONS.get("status_active") or ICONS.get("check")
        status_clock_id = ICONS.get("clock")
        
        if is_executing:
            status_icon = f'<tg-emoji emoji-id="{status_clock_id}">🕒</tg-emoji> Выполняется...' if status_clock_id else "⏳ Выполняется..."
        else:
            status_icon = f'<tg-emoji emoji-id="{status_act_id}">✔️</tg-emoji> Готова к вводу' if status_act_id else "🟢 Готова к вводу"

        term_id = ICONS.get("terminal")
        header_icon = f'<tg-emoji emoji-id="{term_id}">⌨️</tg-emoji> ' if term_id else "💻 "

        text = (
            f"{header_icon}<b>Консоль Windows</b>\n\n"
            f"• <b>Окружение:</b> <code>{target_name}</code>\n"
            f"• <b>Оболочка:</b> <code>{shell_name}</code>\n"
            f"• <b>Статус:</b> {status_icon}\n\n"
        )
        if is_executing and last_cmd:
            safe_cmd = html.escape(str(last_cmd))
            text += f"<i>Текущая команда:</i> <code>{safe_cmd}</code>\n\n"
        text += (
            "Отправьте команду обычным сообщением в чат.\n"
            "Быстрые команды:\n"
            "• <code>/cmd &lt;команда&gt;</code> — в CMD\n"
            "• <code>/ps &lt;команда&gt;</code> — в PowerShell\n"
            "• <code>/exit</code> — закрыть консоль"
        )
        return text

    def console_menu_markup(self, target="desktop", shell="powershell"):
        shell_btn_text = "Shell: PowerShell" if (shell or "").lower() == "powershell" else "Shell: CMD"
        target_btn_text = "Цель: Desktop" if (target or "").lower() == "desktop" else "Цель: SYSTEM"
        close_btn = {"text": "Закрыть консоль", "callback_data": "console_close"}
        if ICONS.get("cross") or ICONS.get("back"):
            close_btn["icon_custom_emoji_id"] = ICONS.get("cross") or ICONS.get("back")
        return {
            "inline_keyboard": [
                [
                    {"text": shell_btn_text, "style": "primary", "callback_data": "console_toggle_shell"},
                    {"text": target_btn_text, "style": "primary", "callback_data": "console_toggle_target"},
                ],
                [
                    {"text": "whoami", "callback_data": "console_quick:whoami"},
                    {"text": "ipconfig", "callback_data": "console_quick:ipconfig"},
                    {"text": "tasklist", "callback_data": "console_quick:tasklist"},
                    {"text": "dir", "callback_data": "console_quick:dir"},
                ],
                [
                    close_btn,
                ],
            ]
        }

