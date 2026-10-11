#!/usr/bin/env python3
"""Physical Router: handles callbacks for hardware button password management and Morse unlock patterns."""

import logging

LOG = logging.getLogger("pc-control-bot.routers.physical")


def route_physical_callback(app, data: str, chat_id: int, message_id: int, callback_id: str) -> bool:
    """Handle physical button and Morse code pattern callback queries. Returns True if handled, False otherwise."""
    # 1. Physical Button Menu
    if data == "physical_button":
        app.ui.answer_callback(callback_id)
        app.physical_btn.mode = None
        app.physical_btn.active_message_id = None
        app.physical_btn.input_message_id = None
        text = app.physical_btn.physical_control_text()
        markup = app.physical_btn.physical_control_markup()
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    # 2. Button Diagnostics / Test
    if data == "physical_test":
        app.ui.answer_callback(callback_id)
        app.physical_btn.mode = "test"
        app.physical_btn.input_entries = ["Ожидаю нажатия на кнопку..."]
        text = "<b>🕹 Физическая кнопка — диагностика</b>\n\nОжидаю нажатия..."
        res = app.ui.send_message(
            chat_id,
            text,
            reply_markup={"inline_keyboard": [app.ui.back_button("physical_button")]},
            message_id=message_id,
        )
        if res and res.get("message_id"):
            app.physical_btn.input_message_id = res["message_id"]
            app.physical_btn.active_message_id = res["message_id"]
        return True

    # 3. Begin Password Recording
    if data in ("physical_record_begin", "physical_password_begin"):
        app.ui.answer_callback(callback_id)
        app.mqtt.publish_esp_action("START_PHYSICAL_RECORDING")
        app.physical_btn.mode = "record_password"
        app.physical_btn.password_draft = []
        app.physical_btn.last_press_info = None
        text = app.physical_btn.password_record_text()
        markup = app.physical_btn.password_record_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            app.physical_btn.active_message_id = res["message_id"]
        return True

    # 4. Save Hardware Recording
    if data.startswith("physical_record_save:"):
        seq = data.split(":", 1)[1]
        app.ui.answer_callback(callback_id, "Сохраняю...")
        app.physical_btn.mode = None
        app.physical_btn.active_message_id = None
        app.physical_btn.start_save_password(seq, chat_id, app.save_state)
        return True

    # 5. Add Symbol to Password Draft
    if data.startswith("physical_add:"):
        app.ui.answer_callback(callback_id)
        symbol = data.split(":", 1)[1]
        if len(app.physical_btn.password_draft) < 12:
            app.physical_btn.password_draft.append(symbol)
            app.physical_btn.last_press_info = "Введено в боте"
        text = app.physical_btn.password_record_text()
        markup = app.physical_btn.password_record_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            app.physical_btn.active_message_id = res["message_id"]
        return True

    # 6. Backspace Password Symbol
    if data == "physical_back":
        app.ui.answer_callback(callback_id)
        if app.physical_btn.password_draft:
            app.physical_btn.password_draft.pop()
        text = app.physical_btn.password_record_text()
        markup = app.physical_btn.password_record_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            app.physical_btn.active_message_id = res["message_id"]
        return True

    # 7. Clear Password Draft
    if data == "physical_clear":
        app.ui.answer_callback(callback_id, "Очищено")
        app.physical_btn.password_draft = []
        app.physical_btn.last_press_info = None
        text = app.physical_btn.password_record_text()
        markup = app.physical_btn.password_record_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            app.physical_btn.active_message_id = res["message_id"]
        return True

    # 8. Save Password Draft
    if data == "physical_save":
        app.ui.answer_callback(callback_id, "Сохраняю...")
        app.physical_btn.mode = None
        app.physical_btn.active_message_id = None
        seq = "".join(app.physical_btn.password_draft)
        app.physical_btn.start_save_password(seq, chat_id, app.save_state)
        return True

    # 9. Cancel Password Recording
    if data == "physical_cancel_record":
        app.ui.answer_callback(callback_id, "Отменено")
        app.physical_btn.mode = None
        app.physical_btn.active_message_id = None
        app.physical_btn.password_draft = []
        app.physical_btn.last_press_info = None
        text = app.physical_btn.physical_control_text()
        markup = app.physical_btn.physical_control_markup()
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    # 10. Delete Stored Password
    if data == "physical_password_delete":
        app.ui.answer_callback(callback_id, "Удаляю...")
        app.physical_btn.start_delete_password(chat_id)
        return True

    # 11. Trigger Unlock Verification
    if data == "physical_unlock":
        app.ui.answer_callback(callback_id, "Разблокирую...")
        app.physical_btn.start_unlock_input(chat_id)
        return True

    # 12. Begin Morse Unlock Pattern
    if data == "physical_unlock_pattern_begin":
        app.ui.answer_callback(callback_id)
        app.physical_btn.mode = "record_unlock_pattern"
        app.physical_btn.unlock_draft = list(app.physical_btn.saved_unlock_pattern or "CC")
        app.physical_btn.last_press_info = None
        text = app.physical_btn.unlock_builder_text()
        markup = app.physical_btn.unlock_builder_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            app.physical_btn.active_message_id = res["message_id"]
        return True

    # 13. Add Symbol to Unlock Pattern
    if data.startswith("unlock_pat_add:"):
        app.ui.answer_callback(callback_id)
        symbol = data.split(":", 1)[1]
        if len(app.physical_btn.unlock_draft) < 8:
            app.physical_btn.unlock_draft.append(symbol)
            app.physical_btn.last_press_info = "Введено в боте"
        text = app.physical_btn.unlock_builder_text()
        markup = app.physical_btn.unlock_builder_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            app.physical_btn.active_message_id = res["message_id"]
        return True

    # 14. Backspace Symbol in Unlock Pattern
    if data == "unlock_pat_back":
        app.ui.answer_callback(callback_id)
        if app.physical_btn.unlock_draft:
            app.physical_btn.unlock_draft.pop()
        text = app.physical_btn.unlock_builder_text()
        markup = app.physical_btn.unlock_builder_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            app.physical_btn.active_message_id = res["message_id"]
        return True

    # 15. Reset Unlock Pattern
    if data == "unlock_pat_reset":
        app.ui.answer_callback(callback_id)
        app.physical_btn.unlock_draft = ["C", "C"]
        app.physical_btn.last_press_info = "Сброс"
        text = app.physical_btn.unlock_builder_text()
        markup = app.physical_btn.unlock_builder_markup()
        res = app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        if res and res.get("message_id"):
            app.physical_btn.active_message_id = res["message_id"]
        return True

    # 16. Save Unlock Pattern
    if data == "unlock_pat_save":
        app.ui.answer_callback(callback_id, "Сохраняю...")
        app.physical_btn.mode = None
        app.physical_btn.active_message_id = None
        seq = "".join(app.physical_btn.unlock_draft)
        app.physical_btn.saved_unlock_pattern = seq
        app.save_state()
        pat_disp = app.physical_btn.format_password_display(seq)
        text = (
            f"<b>✅ Жест разблокировки сохранён!</b>\n\n<blockquote><b>{pat_disp}</b></blockquote>\n"
            "Теперь при вводе этой комбинации на кнопке (в течение 5 минут после морзе-включения) Windows автоматически разблокируется."
        )
        app.ui.send_message(chat_id, text, reply_markup=app.physical_btn.physical_control_markup(), message_id=message_id)
        return True

    return False
