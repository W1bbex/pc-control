#!/usr/bin/env python3
"""
Media Player Management Service for PC Control Bot.
Handles Yandex Music / Windows media transport cards, debounced live updates, and business chat routing.
"""

import logging
import os
import threading
import time

LOG = logging.getLogger("pc-control-bot.player")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "0"))


class PlayerManagerMixin:
    """
    Mixin for PcControlBotApp providing media player state updates and interactive cards.
    """

    def _handle_media_status_event(self, data):
        msg_id = getattr(self, "active_player_message_id", None)
        chat_id = getattr(self, "active_player_chat_id", None)
        if not msg_id or not chat_id:
            return

        now = time.time()
        last_rendered = getattr(self, "_last_rendered_media_data", {})
        is_track_change = (
            not last_rendered
            or last_rendered.get("title") != data.get("title")
            or last_rendered.get("artist") != data.get("artist")
            or last_rendered.get("is_playing") != data.get("is_playing")
            or last_rendered.get("is_liked") != data.get("is_liked")
            or last_rendered.get("cover_url") != data.get("cover_url")
            or last_rendered.get("emoji_ids") != data.get("emoji_ids")
            or last_rendered.get("artist_avatar_id") != data.get("artist_avatar_id")
        )
        min_interval = 0.5 if is_track_change else 2.8

        with self.player_edit_lock:
            time_since = now - self.last_player_edit_time
            if time_since < min_interval:
                self._pending_player_data = data
                if not getattr(self, "_player_timer_running", False):
                    self._player_timer_running = True
                    delay = max(0.1, min_interval - time_since)
                    timer = threading.Timer(delay, self._flush_pending_player_update)
                    self._player_timer = timer
                    timer.start()
                return
            timer = getattr(self, "_player_timer", None)
            if timer and timer.is_alive():
                timer.cancel()
            self._player_timer_running = False
            self.last_player_edit_time = now
            self._pending_player_data = None
            self._last_rendered_media_data = dict(data)

        self._render_and_send_player(chat_id, msg_id, data)

    def _flush_pending_player_update(self):
        with self.player_edit_lock:
            self._player_timer_running = False
            data = getattr(self, "_pending_player_data", None)
            self._pending_player_data = None
            if not data:
                return
            self.last_player_edit_time = time.time()
            self._last_rendered_media_data = dict(data)

        msg_id = getattr(self, "active_player_message_id", None)
        chat_id = getattr(self, "active_player_chat_id", None)
        if msg_id and chat_id:
            self._render_and_send_player(chat_id, msg_id, data)

    def _update_dashboard_on_media(self, data):
        menu_msg_id = getattr(self, "active_main_menu_message_id", None)
        if not menu_msg_id or getattr(self, "current_menu_view", "main") != "main":
            return
        try:
            text = self.monitoring.dashboard_text(data)
            markup = self._main_menu_markup()
            target_user_id = getattr(self, "allowed_user_id", ALLOWED_USER_ID)
            self.ui.send_message(
                target_user_id,
                text,
                reply_markup=markup,
                message_id=menu_msg_id,
            )
        except Exception:
            LOG.exception("Failed to update dashboard on media event")

    def _delete_old_player_card(self):
        old_msg_id = getattr(self, "active_player_message_id", None)
        old_chat_id = getattr(self, "active_player_chat_id", None)
        old_conn_id = getattr(self, "active_player_business_connection_id", None)
        target_user_id = getattr(self, "allowed_user_id", ALLOWED_USER_ID)
        if old_chat_id == target_user_id:
            old_conn_id = None
        if old_msg_id and old_chat_id:
            try:
                self.ui.delete_message(old_chat_id, old_msg_id, business_connection_id=old_conn_id)
            except Exception as e:
                LOG.warning("Could not delete old player card %s in chat %s: %s", old_msg_id, old_chat_id, e)
        self.active_player_message_id = None
        self.active_player_chat_id = None
        self.active_player_business_connection_id = None
        self.active_player_is_photo = False
        self.active_player_cover_url = None

    def _send_player_card(self, chat_id, media_state, business_connection_id=None):
        target_user_id = getattr(self, "allowed_user_id", ALLOWED_USER_ID)
        if chat_id == target_user_id:
            business_connection_id = None
        self._delete_old_player_card()
        cover_url = media_state.get("cover_url") if media_state else None
        markup = self.ui.player_card_markup(media_state)

        res = None
        is_photo = False
        if cover_url:
            caption = self.ui.player_card_caption(media_state)
            res = self.ui.send_photo(
                chat_id,
                cover_url,
                caption=caption,
                reply_markup=markup,
                business_connection_id=business_connection_id,
            )
            if res and isinstance(res, dict) and "message_id" in res:
                is_photo = True
                self.failed_cover_url = None
            else:
                self.failed_cover_url = cover_url
        else:
            self.failed_cover_url = None

        if not res or not isinstance(res, dict) or "message_id" not in res:
            text = self.ui.player_card_text(media_state)
            res = self.ui.send_message(
                chat_id,
                text,
                reply_markup=markup,
                business_connection_id=business_connection_id,
            )
            is_photo = False

        if res and isinstance(res, dict) and "message_id" in res:
            self.active_player_message_id = res["message_id"]
            self.active_player_chat_id = chat_id
            self.active_player_business_connection_id = business_connection_id
            self.active_player_is_photo = is_photo
            self.active_player_cover_url = cover_url if is_photo else None
            self.last_player_edit_time = time.time()
            self.save_state()
            LOG.info("Active player set to chat %s msg %s (is_photo=%s, conn=%s)", chat_id, res["message_id"], is_photo, business_connection_id)
            return res
        return None

    def _render_and_send_player(self, chat_id, msg_id, data):
        if getattr(self, "active_player_message_id", None) != msg_id:
            return
        conn_id = getattr(self, "active_player_business_connection_id", None)
        target_user_id = getattr(self, "allowed_user_id", ALLOWED_USER_ID)
        if chat_id == target_user_id:
            conn_id = None
        markup = self.ui.player_card_markup(data)
        is_photo = getattr(self, "active_player_is_photo", False)
        cover_url = data.get("cover_url") if data else None
        failed_cover = getattr(self, "failed_cover_url", None)

        if not is_photo and cover_url and cover_url != failed_cover:
            self._send_player_card(chat_id, data, business_connection_id=conn_id)
            return

        try:
            if is_photo:
                last_cover = getattr(self, "active_player_cover_url", None)
                caption = self.ui.player_card_caption(data)
                if cover_url and cover_url != last_cover:
                    edit_res = self.ui.edit_message_media(
                        chat_id,
                        msg_id,
                        cover_url,
                        caption=caption,
                        reply_markup=markup,
                        business_connection_id=conn_id,
                    )
                    if edit_res and isinstance(edit_res, dict) and "message_id" in edit_res:
                        self.active_player_cover_url = cover_url
                        return
                self.ui.edit_message_caption(
                    chat_id,
                    msg_id,
                    caption,
                    reply_markup=markup,
                    business_connection_id=conn_id,
                )
            else:
                text = self.ui.player_card_text(data)
                self.ui.send_message(
                    chat_id,
                    text,
                    reply_markup=markup,
                    message_id=msg_id,
                    business_connection_id=conn_id,
                )
        except Exception as e:
            LOG.warning("Failed updating player card %s: %s", msg_id, e)
