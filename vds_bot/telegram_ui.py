#!/usr/bin/env python3

import html
import json
import logging
import time
import requests

LOG = logging.getLogger("pc-control-bot.telegram_ui")

DEFAULT_PLAYER_ICONS = {
    "btn_play": "5251372554798539686",
    "btn_pause": "5251752543440119050",
    "btn_next": "5251687758153423658",
    "btn_prev": "5251293527400291945",
    "btn_fwd10": "5251438890568425035",
    "btn_back10": "5251409985438522351",
    "btn_wave": "5251393273720776324",
    "tag_note": "5251570222078401376",
    "tag_artist": "5251415160874116452",
    "tag_status_play": "5251372554798539686",
    "tag_status_pause": "5251752543440119050",
    "tag_clock": "5251754463290498132",
    "tag_slider_dot": "5249027614324074999",
    "tag_heart_liked": "5249190011332502742",
    "tag_heart_unliked": "5251547334197684560",
}

EMOJI_BTN_POWER = "5843553939672274145"
EMOJI_BTN_SCREENSHOT = "5890744068203352126"
EMOJI_BTN_APPS = "5316728625465146646"
EMOJI_BTN_SOUND = "5890997763331591703"
EMOJI_BTN_SETTINGS = "5260696263078212024"


class TelegramBotApi:
    def __init__(self, token):
        self.token = token
        self.api_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def call_raw(self, method, payload=None, timeout=25):
        url = f"{self.api_url}/{method}"
        try:
            response = self.session.post(url, json=payload or {}, timeout=timeout)
            try:
                data = response.json()
                return data
            except Exception:
                return {"ok": False, "description": f"HTTP {response.status_code}"}
        except Exception as e:
            raw_err = str(e)
            if self.token:
                raw_err = raw_err.replace(self.token, "[REDACTED_BOT_TOKEN]")
            return {"ok": False, "description": raw_err}

    def call(self, method, payload=None, timeout=25):
        data = self.call_raw(method, payload, timeout=timeout)
        if data.get("ok"):
            return data.get("result")
        safe_data = dict(data)
        if "description" in safe_data and self.token:
            safe_data["description"] = str(safe_data["description"]).replace(self.token, "[REDACTED_BOT_TOKEN]")
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
            data["reply_markup"] = json.dumps(reply_markup) if isinstance(reply_markup, dict) else reply_markup
        if business_connection_id:
            data["business_connection_id"] = business_connection_id

        if isinstance(photo, str):
            data["photo"] = photo
            try:
                res = self.session.post(url, data=data, timeout=15)
                res.raise_for_status()
                return res.json().get("result")
            except Exception as e:
                LOG.error("send_photo URL failed: %s", e)
                return None
        else:
            content_type = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"
            files = {"photo": (filename, photo, content_type)}
            try:
                res = self.session.post(url, data=data, files=files, timeout=25)
                res.raise_for_status()
                return res.json().get("result")
            except Exception as e:
                LOG.error("send_photo bytes failed: %s", e)
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
            data["reply_markup"] = json.dumps(reply_markup) if isinstance(reply_markup, dict) else reply_markup
        if business_connection_id:
            data["business_connection_id"] = business_connection_id

        doc_bytes = document.encode("utf-8") if isinstance(document, str) else document
        files = {"document": (filename, doc_bytes, "text/plain; charset=utf-8")}
        try:
            res = self.session.post(url, data=data, files=files, timeout=30)
            res.raise_for_status()
            return res.json().get("result")
        except Exception as e:
            LOG.error("send_document failed: %s", e)
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
                LOG.warning("editMessageMedia URL failed: %s", e)
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
                LOG.warning("editMessageMedia bytes failed: %s", e)
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
        return [{"text": "↩ Назад", "callback_data": target}]

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
                    {"text": "📊 Мониторинг", "style": "primary", "callback_data": "monitoring_menu"},
                    {"text": "📈 Графики", "style": "primary", "callback_data": "history_menu"},
                ],
                [
                    {"text": "🕹 Физическая кнопка", "style": "primary", "callback_data": "physical_button"},
                    {"text": "⏱ Активность", "style": "primary", "callback_data": "menu_activity"},
                ],
                [
                    {"text": "💡 Подсветка кулера (ARGB)", "style": "primary", "callback_data": "led_menu"},
                    {"text": "💻 Консоль", "style": "primary", "callback_data": "console_open"},
                ],
                self.back_button("menu"),
            ]
        }

    def led_menu_markup(self):
        return {
            "inline_keyboard": [
                [
                    {"text": "👁 Для глаз (Идеал)", "style": "success", "callback_data": "led_action:preset:eye"},
                    {"text": "📸 Для фото (Камера)", "style": "primary", "callback_data": "led_action:preset:photo"},
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
                    {"text": "⭐️ 10", "style": "primary", "callback_data": "led_action:preset:10"},
                    {"text": "11", "style": "primary", "callback_data": "led_action:preset:11"},
                    {"text": "12", "style": "primary", "callback_data": "led_action:preset:12"},
                ],
                [
                    {"text": "💡 Вкл", "style": "success", "callback_data": "led_action:on"},
                    {"text": "🌑 Выкл", "style": "danger", "callback_data": "led_action:off"},
                ],
                [
                    {"text": "🔆 30%", "style": "primary", "callback_data": "led_action:bright:80"},
                    {"text": "🔆 60%", "style": "primary", "callback_data": "led_action:bright:160"},
                    {"text": "🔆 100%", "style": "primary", "callback_data": "led_action:bright:255"},
                ],
                self.back_button("settings_menu"),
            ]
        }

    def sound_menu_markup(self):
        return {
            "inline_keyboard": [
                [{"text": "🔇 Включить / выключить звук", "style": "primary", "callback_data": "volume:toggle"}],
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

        return {
            "inline_keyboard": [
                [{"text": "Выключить ПК", "style": "danger", "callback_data": "shutdown_pc"}],
                [lock_btn],
                [
                    {"text": "Выключить экран", "style": "primary", "callback_data": "toggle_screen"},
                    {"text": "Свернуть окна", "style": "primary", "callback_data": "minimize_windows"},
                ],
                [
                    {"text": "Сон", "style": "primary", "callback_data": "sleep_pc"},
                    {"text": "Перезагрузить", "style": "primary", "callback_data": "reboot_pc"},
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

    def apps_menu_markup(self):
        return {
            "inline_keyboard": [
                [{"text": "🚀 Запустить рабочий сетап", "style": "success", "callback_data": "workspace_start_direct"}],
                [
                    {"text": "💬 Telegram", "style": "primary", "callback_data": "app:Telegram"},
                    {"text": "🎵 Музыка", "style": "primary", "callback_data": "app:Музыка"},
                ],
                [
                    {"text": "🐬 Dolphin", "style": "primary", "callback_data": "app:Dolphin"},
                    {"text": "🌐 Chrome", "style": "primary", "callback_data": "app:Chrome"},
                ],
                [
                    {"text": "⚡ Happ", "style": "primary", "callback_data": "app:Happ"},
                    {"text": "🚀 Antigravity", "style": "primary", "callback_data": "app:Antigravity"},
                ],
                [
                    {"text": "💻 Консоль (Терминал)", "style": "primary", "callback_data": "console_open"},
                ],
                self.back_button("menu"),
            ]
        }

    def monitoring_menu_markup(self):
        return {
            "inline_keyboard": [
                [{"text": "🔄 Обновить состояние", "style": "primary", "callback_data": "telemetry_now"}],
                [{"text": "🖼️ Скриншот", "style": "primary", "callback_data": "screenshot"}],
                [{"text": "📈 Графики", "style": "primary", "callback_data": "history_menu"}],
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
        pos = float(media_state.get("position", 0.0))
        dur = float(media_state.get("duration", 0.0))
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

        tag_dot = icons.get("tag_slider_dot") or "5249027614324074999"
        dot = f'<tg-emoji emoji-id="{tag_dot}">⚪</tg-emoji>'

        bar_len = 12
        if display_dur > 0:
            pct = min(1.0, max(0.0, display_pos / display_dur))
            filled = int(round(pct * (bar_len - 1)))
            bar = "━" * filled + dot + "━" * (bar_len - 1 - filled)
        else:
            bar = "━" * (bar_len // 2) + dot + "━" * (bar_len // 2)

        # White play/pause status icon matching the minimalist white button style
        btn_play = icons.get("btn_play") or "5251372554798539686"
        btn_pause = icons.get("btn_pause") or "5251752543440119050"
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
        pos = float(media_state.get("position", 0.0))
        dur = float(media_state.get("duration", 0.0))
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

        tag_dot = icons.get("tag_slider_dot") or "5249027614324074999"
        dot = f'<tg-emoji emoji-id="{tag_dot}">⚪</tg-emoji>'

        bar_len = 12
        if display_dur > 0:
            pct = min(1.0, max(0.0, display_pos / display_dur))
            filled = int(round(pct * (bar_len - 1)))
            bar = "━" * filled + dot + "━" * (bar_len - 1 - filled)
        else:
            bar = "━" * (bar_len // 2) + dot + "━" * (bar_len // 2)

        btn_play = icons.get("btn_play") or "5251372554798539686"
        btn_pause = icons.get("btn_pause") or "5251752543440119050"
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
            dur = float(item.get("total_duration", 0))
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
        status_icon = "⏳ Выполняется..." if is_executing else "🟢 Готова к вводу"

        text = (
            "💻 <b>Консоль Windows</b>\n\n"
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
        shell_btn_text = "🐚 Shell: PowerShell" if (shell or "").lower() == "powershell" else "🐚 Shell: CMD"
        target_btn_text = "🎯 Цель: Desktop" if (target or "").lower() == "desktop" else "🎯 Цель: SYSTEM"
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
                    {"text": "↩ Закрыть консоль", "callback_data": "console_close"},
                ],
            ]
        }

