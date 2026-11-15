#!/usr/bin/env python3
"""Player Router: handles callbacks for media player controls, tracks, and sound/volume."""

import logging
import time

LOG = logging.getLogger("pc-control-bot.routers.player")


def route_player_callback(app, data: str, chat_id: int, message_id: int, callback_id: str, business_connection_id: str = None) -> bool:
    """Handle media player and sound callback queries. Returns True if handled, False otherwise."""
    # 1. Player Controls
    if data.startswith("player_"):
        action = data.replace("player_", "")
        player_lock = getattr(app, "player_edit_lock", None)

        if player_lock:
            with player_lock:
                app.active_player_message_id = message_id
                app.active_player_chat_id = chat_id
                app.active_player_business_connection_id = None if chat_id == app.allowed_user_id else business_connection_id
                media_state = dict(getattr(app, "latest_media_state", None) or getattr(app.mqtt, "last_media_state", {}) or {})
        else:
            app.active_player_message_id = message_id
            app.active_player_chat_id = chat_id
            app.active_player_business_connection_id = None if chat_id == app.allowed_user_id else business_connection_id
            media_state = dict(getattr(app, "latest_media_state", None) or getattr(app.mqtt, "last_media_state", {}) or {})

        if action != "toggle_like":
            app.ui.answer_callback(callback_id)

        # Optimistic state update for instant UI button feedback (<50ms)
        is_active = bool(media_state.get("active"))
        optimistic_changed = False
        sent_ok = True

        if action == "toggle_play":
            sent_ok = bool(app.mqtt.send_media_command("toggle_play"))
            if sent_ok and is_active:
                media_state["is_playing"] = not media_state.get("is_playing", False)
                optimistic_changed = True
        elif action == "seek_fwd_10":
            sent_ok = bool(app.mqtt.send_media_command("seek", delta=10))
            if sent_ok and is_active:
                dur = float(media_state.get("duration") or 0.0)
                cur_pos = float(media_state.get("position") or 0.0)
                media_state["position"] = min(dur, cur_pos + 10.0) if dur > 0 else cur_pos + 10.0
                optimistic_changed = True
        elif action == "seek_back_10":
            sent_ok = bool(app.mqtt.send_media_command("seek", delta=-10))
            if sent_ok and is_active:
                cur_pos = float(media_state.get("position") or 0.0)
                media_state["position"] = max(0.0, cur_pos - 10.0)
                optimistic_changed = True
        elif action == "next":
            sent_ok = bool(app.mqtt.send_media_command("next"))
            if sent_ok:
                with app.player_edit_lock:
                    app.last_player_edit_time = 0.0
        elif action == "prev":
            sent_ok = bool(app.mqtt.send_media_command("prev"))
            if sent_ok:
                with app.player_edit_lock:
                    app.last_player_edit_time = 0.0
        elif action == "wave":
            sent_ok = bool(app.mqtt.send_media_command("wave"))
            if sent_ok:
                with app.player_edit_lock:
                    app.last_player_edit_time = 0.0
        elif action == "toggle_like":
            sent_ok = bool(app.mqtt.send_media_command("toggle_like"))
            if sent_ok and is_active:
                new_liked = not media_state.get("is_liked", False)
                media_state["is_liked"] = new_liked
                optimistic_changed = True
                toast = "❤️ Добавлено в Мою волну / Любимые" if new_liked else "💔 Удалено из любимых"
                app.ui.answer_callback(callback_id, text=toast)
            elif not sent_ok:
                app.ui.answer_callback(callback_id, text="❌ Команда не отправлена: MQTT недоступен", alert=True)
            else:
                app.ui.answer_callback(callback_id)

        if not sent_ok and action != "toggle_like":
            app.ui.send_message(
                chat_id,
                "❌ Команда управления плеером не доставлена (MQTT/ПК недоступен).",
                business_connection_id=business_connection_id,
            )

        if optimistic_changed and message_id:
            render_state = dict(media_state)
            if player_lock:
                with player_lock:
                    current = dict(getattr(app, "latest_media_state", None) or {})
                    current.update(media_state)
                    app.latest_media_state = current
                    app.last_player_edit_time = time.time()
                    render_state = dict(current)
            else:
                current = dict(getattr(app, "latest_media_state", None) or {})
                current.update(media_state)
                app.latest_media_state = current
                app.last_player_edit_time = time.time()
                render_state = dict(current)
            app._render_and_send_player(chat_id, message_id, render_state)
        return True

    # 3. Sound Menu Navigation
    if data == "sound_menu":
        app.current_menu_view = "sound_menu"
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
        cmd_id = app.mqtt.publish_desktop_action("volume", args)
        if not cmd_id:
            app.ui.send_message(chat_id, "❌ Не удалось изменить громкость: MQTT недоступен.")
        return True

    return False
