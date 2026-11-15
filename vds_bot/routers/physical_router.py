#!/usr/bin/env python3
"""Physical Router: handles callbacks for hardware button password management and Morse unlock patterns."""

import logging

LOG = logging.getLogger("pc-control-bot.routers.physical")


def route_physical_callback(app, data: str, chat_id: int, message_id: int, callback_id: str) -> bool:
    """Handle physical button and Morse code pattern callback queries. Returns True if handled, False otherwise."""
    pbtn = app.physical_btn

    # 1. Physical Button Menu
    if data == "physical_button":
        app.ui.answer_callback(callback_id)
        with pbtn._lock:
            was_test = getattr(pbtn, "mode", None) == "test"
            pbtn.mode = None
            pbtn.active_message_id = None
            pbtn.input_message_id = None
            text = pbtn.physical_control_text()
            markup = pbtn.physical_control_markup()
        if was_test:
            app.mqtt.publish_esp_action("STOP_PHYSICAL_TEST")
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    # 2. Button Diagnostics / Test
    if data == "physical_test":
        app.ui.answer_callback(callback_id)
        with pbtn._lock:
            pbtn.mode = "test"
            pbtn.input_entries = ["Ожидаю нажатия на кнопку..."]
        app.mqtt.publish_esp_action("START_PHYSICAL_TEST")
        text = "<b>🕹 Физическая кнопка — диагностика</b>\n\nОжидаю нажатия..."
        res = app.ui.send_message(
            chat_id,
            text,
            reply_markup={"inline_keyboard": [app.ui.back_button("physical_button")]},
            message_id=message_id,
        )
        if res and res.get("message_id"):
            with pbtn._lock:
                pbtn.input_message_id = res["message_id"]
                pbtn.active_message_id = res["message_id"]
        return True

    # 3. Begin Password Recording
    if data == "physical_record_begin":
        esp_status = getattr(app.mqtt, "esp_status", None)
        if esp_status == "offline":
            app.ui.answer_callback(callback_id, "❌ Контроллер ESP8266 не в сети!", alert=True)
            return True
        cmd_id = app.mqtt.publish_esp_action("START_PHYSICAL_RECORDING")
        if not cmd_id:
            app.ui.answer_callback(callback_id, "❌ Не удалось отправить команду в ESP: MQTT недоступен.", alert=True)
            return True
        app.ui.answer_callback(callback_id)
        with pbtn._lock:
            pbtn.mode = "record_password"
            pbtn.password_draft = []
            pbtn.last_press_info = None
            text = pbtn.password_record_text()
            markup = pbtn.password_record_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            with pbtn._lock:
                pbtn.active_message_id = res["message_id"]
        return True

    # 4. Save Hardware Recording
    if data.startswith("physical_record_save:"):
        seq = data.split(":", 1)[1]
        app.ui.answer_callback(callback_id, "Сохраняю...")
        with pbtn._lock:
            pbtn.mode = None
            pbtn.active_message_id = None
        pbtn.start_save_password(seq, chat_id, message_id=message_id)
        return True

    # 5. Add Symbol to Password Draft
    if data.startswith("physical_add:"):
        app.ui.answer_callback(callback_id)
        symbol = data.split(":", 1)[1]
        with pbtn._lock:
            if len(pbtn.password_draft) < 12:
                pbtn.password_draft.append(symbol)
                pbtn.last_press_info = "Введено в боте"
            text = pbtn.password_record_text()
            markup = pbtn.password_record_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            with pbtn._lock:
                pbtn.active_message_id = res["message_id"]
        return True

    # 6. Backspace Password Symbol
    if data == "physical_back":
        app.ui.answer_callback(callback_id)
        with pbtn._lock:
            if pbtn.password_draft:
                pbtn.password_draft.pop()
            text = pbtn.password_record_text()
            markup = pbtn.password_record_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            with pbtn._lock:
                pbtn.active_message_id = res["message_id"]
        return True

    # 7. Clear Password Draft
    if data == "physical_clear":
        app.ui.answer_callback(callback_id, "Очищено")
        with pbtn._lock:
            pbtn.password_draft = []
            pbtn.last_press_info = None
            text = pbtn.password_record_text()
            markup = pbtn.password_record_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            with pbtn._lock:
                pbtn.active_message_id = res["message_id"]
        return True

    # 8. Save Password Draft
    if data == "physical_save":
        app.ui.answer_callback(callback_id, "Сохраняю...")
        with pbtn._lock:
            pbtn.mode = None
            pbtn.active_message_id = None
            seq = "".join(pbtn.password_draft)
        pbtn.start_save_password(seq, chat_id, message_id=message_id)
        return True

    # 9. Cancel Password Recording
    if data == "physical_cancel_record":
        app.ui.answer_callback(callback_id, "Отменено")
        if getattr(app.mqtt, "esp_status", None) == "online":
            app.mqtt.publish_esp_action("CANCEL_PHYSICAL_RECORDING")
        with pbtn._lock:
            pbtn.mode = None
            pbtn.active_message_id = None
            pbtn.password_draft = []
            pbtn.last_press_info = None
            text = pbtn.physical_control_text()
            markup = pbtn.physical_control_markup()
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    # 10. Delete Stored Password (with confirmation & ESP online guard)
    if data == "physical_password_delete":
        if getattr(app.mqtt, "esp_status", None) != "online":
            app.ui.answer_callback(callback_id, "❌ Контроллер ESP8266 не в сети!", alert=True)
            return True
        app.ui.answer_callback(callback_id)
        markup = {
            "inline_keyboard": [
                [{"text": "🗑 Да, удалить пароль", "style": "danger", "callback_data": "physical_password_delete_confirm"}],
                app.ui.back_button("physical_button"),
            ]
        }
        app.ui.send_message(
            chat_id,
            "<b>⚠️ Удалить физический пароль из памяти ESP?</b>\n\nКнопка начнёт включать ПК одним нажатием без ввода пароля.",
            reply_markup=markup,
            message_id=message_id,
        )
        return True

    if data == "physical_password_delete_confirm":
        if getattr(app.mqtt, "esp_status", None) != "online":
            app.ui.answer_callback(callback_id, "❌ Контроллер ESP8266 не в сети!", alert=True)
            return True
        app.ui.answer_callback(callback_id, "Удаляю...")
        pbtn.start_delete_password(chat_id, message_id=message_id)
        return True

    # 11. Trigger Unlock Verification
    if data == "physical_unlock":
        if getattr(app.mqtt, "esp_status", None) != "online":
            app.ui.answer_callback(callback_id, "❌ Контроллер ESP8266 не в сети!", alert=True)
            return True
        app.ui.answer_callback(callback_id, "Разблокирую...")
        pbtn.start_unlock_input(chat_id, message_id=message_id)
        return True

    # 12. Begin Morse Unlock Pattern
    if data == "physical_unlock_pattern_begin":
        app.ui.answer_callback(callback_id)
        with pbtn._lock:
            pbtn.mode = "record_unlock_pattern"
            pbtn.unlock_draft = list(pbtn.saved_unlock_pattern or "CC")
            pbtn.last_press_info = None
            text = pbtn.unlock_builder_text()
            markup = pbtn.unlock_builder_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            with pbtn._lock:
                pbtn.active_message_id = res["message_id"]
        return True

    # 13. Add Symbol to Unlock Pattern
    if data.startswith("unlock_pat_add:"):
        app.ui.answer_callback(callback_id)
        symbol = data.split(":", 1)[1]
        with pbtn._lock:
            if len(pbtn.unlock_draft) < 8:
                pbtn.unlock_draft.append(symbol)
                pbtn.last_press_info = "Введено в боте"
            text = pbtn.unlock_builder_text()
            markup = pbtn.unlock_builder_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            with pbtn._lock:
                pbtn.active_message_id = res["message_id"]
        return True

    # 14. Backspace Symbol in Unlock Pattern
    if data == "unlock_pat_back":
        app.ui.answer_callback(callback_id)
        with pbtn._lock:
            if pbtn.unlock_draft:
                pbtn.unlock_draft.pop()
            text = pbtn.unlock_builder_text()
            markup = pbtn.unlock_builder_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            with pbtn._lock:
                pbtn.active_message_id = res["message_id"]
        return True

    # 15. Reset Unlock Pattern
    if data == "unlock_pat_reset":
        app.ui.answer_callback(callback_id)
        with pbtn._lock:
            pbtn.unlock_draft = ["C", "C"]
            pbtn.last_press_info = "Сброс"
            text = pbtn.unlock_builder_text()
            markup = pbtn.unlock_builder_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            with pbtn._lock:
                pbtn.active_message_id = res["message_id"]
        return True

    # 16. Save Unlock Pattern
    if data == "unlock_pat_save":
        app.ui.answer_callback(callback_id, "Сохраняю...")
        with pbtn._lock:
            pbtn.mode = None
            pbtn.active_message_id = None
            seq = "".join(pbtn.unlock_draft)
            pbtn.saved_unlock_pattern = seq
            markup = pbtn.physical_control_markup()
            pat_disp = pbtn.format_password_display(seq)
        app.save_state()
        text = (
            f"<b>✅ Жест разблокировки сохранён!</b>\n\n<blockquote><b>{pat_disp}</b></blockquote>\n"
            "Теперь при вводе этой комбинации на кнопке (в течение 5 минут после морзе-включения) Windows автоматически разблокируется."
        )
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    return False
