#!/usr/bin/env python3
"""Player Router: handles callbacks for media player controls, tracks, and sound/volume."""

import logging
import time

LOG = logging.getLogger("pc-control-bot.routers.player")


def route_player_callback(app, data: str, chat_id: int, message_id: int, callback_id: str, business_connection_id: str = None) -> bool:
    """Handle media player and sound callback queries. Returns True if handled, False otherwise."""
    # 1. Open Player Card
    if data == "menu_player":
        app.ui.answer_callback(callback_id)
        if message_id:
            try:
                app.ui.delete_message(chat_id, message_id)
            except Exception:
                pass

        media_state = getattr(app, "latest_media_state", None) or getattr(app.mqtt, "last_media_state", {})
        app._send_player_card(chat_id, media_state, business_connection_id=None)
        app.mqtt.send_media_command("refresh")
        return True

    # 2. Player Controls
    if data.startswith("player_"):
        action = data.replace("player_", "")
        app.active_player_message_id = message_id
        app.active_player_chat_id = chat_id
        app.active_player_business_connection_id = None if chat_id == app.allowed_user_id else business_connection_id

        if action != "toggle_like":
            app.ui.answer_callback(callback_id)

        # Optimistic state update for instant UI button feedback (<50ms)
        media_state = dict(getattr(app, "latest_media_state", None) or getattr(app.mqtt, "last_media_state", {}) or {})
        optimistic_changed = False

        if action == "toggle_play":
            app.mqtt.send_media_command("toggle_play")
            if media_state:
                media_state["is_playing"] = not media_state.get("is_playing", False)
                optimistic_changed = True
        elif action == "seek_fwd_10":
            app.mqtt.send_media_command("seek", delta=10)
            if media_state:
                dur = float(media_state.get("duration") or 0.0)
                cur_pos = float(media_state.get("position") or 0.0)
                media_state["position"] = min(dur, cur_pos + 10.0) if dur > 0 else cur_pos + 10.0
                optimistic_changed = True
        elif action == "seek_back_10":
            app.mqtt.send_media_command("seek", delta=-10)
            if media_state:
                cur_pos = float(media_state.get("position") or 0.0)
                media_state["position"] = max(0.0, cur_pos - 10.0)
                optimistic_changed = True
        elif action == "next":
            app.mqtt.send_media_command("next")
            with app.player_edit_lock:
                app.last_player_edit_time = 0.0
        elif action == "prev":
            app.mqtt.send_media_command("prev")
            with app.player_edit_lock:
                app.last_player_edit_time = 0.0
        elif action == "wave":
            app.mqtt.send_media_command("wave")
            with app.player_edit_lock:
                app.last_player_edit_time = 0.0
        elif action == "toggle_like":
            app.mqtt.send_media_command("toggle_like")
            if media_state:
                new_liked = not media_state.get("is_liked", False)
                media_state["is_liked"] = new_liked
                optimistic_changed = True
                toast = "❤️ Добавлено в Мою волну / Любимые" if new_liked else "💔 Удалено из любимых"
                app.ui.answer_callback(callback_id, text=toast)
            else:
                app.ui.answer_callback(callback_id)

        if optimistic_changed and message_id:
            app.latest_media_state = media_state
            with app.player_edit_lock:
                app.last_player_edit_time = time.time()
            app._render_and_send_player(chat_id, message_id, media_state)
        return True

    # 3. Sound Menu Navigation
    if data == "sound_menu":
        app.ui.answer_callback(callback_id)
        vol = None
        if hasattr(app, "monitoring") and app.monitoring.latest_telemetry:
            vol = app.monitoring.latest_telemetry.get("volume")
        app.ui.send_message(
            chat_id,
            app.ui.sound_menu_text(volume=vol),
            reply_markup=app.ui.sound_menu_markup(),
            message_id=message_id,
        )
        return True

    # 4. Volume Adjustment
    if data.startswith("volume:"):
        value = data.split(":", 1)[1]
        if value == "toggle":
            args = {"toggle": True}
        elif value in {"25", "50", "75", "100"}:
            args = {"percent": int(value)}
        else:
            app.ui.answer_callback(callback_id, "Неизвестный уровень громкости", alert=True)
            return True
        app.ui.answer_callback(callback_id, "Меняю громкость...")
        app.mqtt.publish_desktop_action("volume", args)
        return True

    return False
