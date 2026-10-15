#!/usr/bin/env python3

import logging
import time

LOG = logging.getLogger("pc-control-bot.physical_button")

try:
    from .telegram_ui import ICONS
except ImportError:
    from telegram_ui import ICONS

MORSE_UNLOCK_WINDOW_SECONDS = 300.0  # 5 minutes


class PhysicalButtonManager:
    def __init__(self, telegram_ui, mqtt_gateway):
        self.ui = telegram_ui
        self.mqtt = mqtt_gateway
        self.saved_password = None
        self.saved_unlock_pattern = "CC"  # Default: 2 clicks ('C', 'C')
        self.password_draft = []
        self.unlock_draft = []
        self.input_entries = []
        self.input_message_id = None
        self.pending_command_id = None
        self.pending_password_value = None
        self.pending_action_kind = "save"

        # Interactive live modes
        self.mode = None  # None, "record_password", "record_unlock_pattern", "test"
        self.active_message_id = None
        self.last_press_info = None
        self.esp_locked = False

        # Security constraints:
        # Only active if unlocked/powered on via physical Morse password within 5 minutes.
        self.last_physical_morse_success_at = None
        self.physical_morse_unlock_eligible = False

        self.gesture_buffer = []  # list of (symbol, timestamp)
        self.on_trigger_unlock_cb = None
        self.on_trigger_unlock_and_workspace_cb = None
        self.on_state_change_cb = None
        self.last_unlock_triggered_at = 0.0
        self.last_unlock_gesture_at = 0.0

    def set_saved_password(self, password):
        self.saved_password = password

    def revoke_morse_eligibility(self):
        self.physical_morse_unlock_eligible = False
        self.gesture_buffer.clear()
        if self.on_state_change_cb:
            self.on_state_change_cb()

    def is_morse_unlock_eligible(self, now=None):
        if not self.physical_morse_unlock_eligible:
            return False, "Пароль включения на кнопке не вводился (или уже использован)."
        ts = time.time() if now is None else float(now)
        if self.last_physical_morse_success_at is None:
            return False, "Пароль включения на кнопке не зафиксирован."
        elapsed = ts - self.last_physical_morse_success_at
        if elapsed > MORSE_UNLOCK_WINDOW_SECONDS:
            return False, "Прошло более 5 минут с момента включения ПК кнопкой."
        return True, ""

    def format_password_display(self, password_str):
        if not password_str:
            return "<i>Не задан</i>"
        return " · ".join("CLICK" if c == "C" else "HOLD" for c in password_str)

    def physical_control_text(self):
        game_id = ICONS.get("game")
        game_tag = f'<tg-emoji emoji-id="{game_id}">🎮</tg-emoji> ' if game_id else ""
        pwd_display = self.format_password_display(self.saved_password)
        count_str = f" ({len(self.saved_password)} шагов)" if self.saved_password else ""
        unlock_display = self.format_password_display(self.saved_unlock_pattern or "CC")
        unlock_count = f" ({len(self.saved_unlock_pattern)} шага)" if self.saved_unlock_pattern else " (по умолчанию)"
        ws_pattern = (self.saved_unlock_pattern or "CC") + "HH"
        ws_display = self.format_password_display(ws_pattern)

        pulse_green = ICONS.get("status_active") or "5345809221961167372"
        pulse_red = ICONS.get("status_inactive") or "5345825096160291312"

        if self.physical_morse_unlock_eligible:
            elapsed = int(time.time() - (self.last_physical_morse_success_at or 0))
            remaining = max(0, int(MORSE_UNLOCK_WINDOW_SECONDS - elapsed))
            if remaining > 0:
                icon_tag = f'<tg-emoji emoji-id="{pulse_green}">🟢</tg-emoji> '
                status_line = f"{icon_tag}Активна ({remaining} сек)"
            else:
                icon_tag = f'<tg-emoji emoji-id="{pulse_red}">🔴</tg-emoji> '
                status_line = f"{icon_tag}Истекла"
        else:
            icon_tag = f'<tg-emoji emoji-id="{pulse_red}">🔴</tg-emoji> '
            status_line = f"{icon_tag}Неактивна"

        lock_id = ICONS.get("lock")
        lock_tag = f'<tg-emoji emoji-id="{lock_id}">🔒</tg-emoji> ' if lock_id else ""

        return (
            f"{game_tag}<b>Физическая кнопка</b>\n\n"
            f"{lock_tag}<b>Морзе-управление (ESP8266)</b>\n"
            f" ├ Пароль включения: {pwd_display}{count_str}\n"
            f" ├ Жест разблокировки: {unlock_display}{unlock_count}\n"
            f" ├ Жест сетапа: {ws_display}\n"
            f" └ Окно разблокировки: {status_line}"
        )

    def physical_control_markup(self):
        keyboard = []
        pwd_btn_text = "Изменить пароль включения" if self.saved_password else "Задать пароль включения"
        pwd_btn = {"text": pwd_btn_text, "style": "primary", "callback_data": "physical_record_begin"}
        if ICONS.get("pencil"):
            pwd_btn["icon_custom_emoji_id"] = ICONS.get("pencil")
        keyboard.append([pwd_btn])

        unlock_btn = {"text": "Жест разблокировки", "style": "primary", "callback_data": "physical_unlock_pattern_begin"}
        if ICONS.get("lock"):
            unlock_btn["icon_custom_emoji_id"] = ICONS.get("lock")

        test_btn = {"text": "Тест нажатий", "style": "primary", "callback_data": "physical_test"}
        if ICONS.get("game"):
            test_btn["icon_custom_emoji_id"] = ICONS.get("game")

        keyboard.append([unlock_btn, test_btn])

        if self.saved_password:
            del_btn = {"text": "Удалить пароль включения", "style": "danger", "callback_data": "physical_password_delete"}
            if ICONS.get("trash"):
                del_btn["icon_custom_emoji_id"] = ICONS.get("trash")
            keyboard.append([del_btn])

        if getattr(self, "esp_locked", False):
            unl_btn = {"text": "Кнопка заблокирована — Разблокировать", "style": "success", "callback_data": "physical_unlock"}
            if ICONS.get("check"):
                unl_btn["icon_custom_emoji_id"] = ICONS.get("check")
            keyboard.append([unl_btn])

        keyboard.append(self.ui.back_button("settings_menu"))
        return {"inline_keyboard": keyboard}

    def password_record_text(self):
        if self.password_draft:
            seq_display = self.format_password_display("".join(self.password_draft))
        else:
            seq_display = "<i>Нажимай физическую кнопку на корпусе ПК...</i>"

        count = len(self.password_draft)
        status_steps = f"{count} / 12 шагов"
        check_id = ICONS.get("check")
        if count < 4:
            status_steps += " (минимум 4)"
        else:
            status_steps += f' <tg-emoji emoji-id="{check_id}">✅</tg-emoji>' if check_id else " (готово)"

        last_press = f"\nПоследнее нажатие: <code>{self.last_press_info}</code>" if self.last_press_info else ""
        pencil_id = ICONS.get("pencil")
        pencil_tag = f'<tg-emoji emoji-id="{pencil_id}">✏️</tg-emoji> ' if pencil_id else ""

        return (
            f"{pencil_tag}<b>Запись пароля включения ПК</b>\n\n"
            "Нажимай кнопку на корпусе ПК или используй кнопки ниже:\n"
            "• <b>CLICK</b> — короче 500 мс\n"
            "• <b>HOLD</b> — от 500 мс\n\n"
            f"Текущая комбинация:\n<blockquote><b>{seq_display}</b></blockquote>\n"
            f"Шагов: <b>{status_steps}</b>{last_press}\n\n"
            "<i>Пауза 3 сек сохранит комбинацию, либо нажми кнопку ниже.</i>"
        )

    def password_record_markup(self):
        keyboard = [
            [
                {"text": "+ CLICK", "style": "primary", "callback_data": "physical_add:C"},
                {"text": "+ HOLD", "style": "primary", "callback_data": "physical_add:H"},
            ]
        ]
        edit_row = []
        if self.password_draft:
            back_btn = {"text": "Стереть шаг", "style": "primary", "callback_data": "physical_back"}
            if ICONS.get("back"):
                back_btn["icon_custom_emoji_id"] = ICONS.get("back")
            edit_row.append(back_btn)

            clear_btn = {"text": "Очистить", "style": "primary", "callback_data": "physical_clear"}
            if ICONS.get("trash") or ICONS.get("refresh"):
                clear_btn["icon_custom_emoji_id"] = ICONS.get("trash") or ICONS.get("refresh")
            edit_row.append(clear_btn)
        if edit_row:
            keyboard.append(edit_row)

        action_row = []
        if len(self.password_draft) >= 4:
            save_btn = {"text": "Сохранить", "style": "success", "callback_data": "physical_save"}
            if ICONS.get("check"):
                save_btn["icon_custom_emoji_id"] = ICONS.get("check")
            action_row.append(save_btn)
        cancel_btn = {"text": "Отмена", "style": "danger", "callback_data": "physical_cancel_record"}
        if ICONS.get("cross"):
            cancel_btn["icon_custom_emoji_id"] = ICONS.get("cross")
        action_row.append(cancel_btn)
        keyboard.append(action_row)
        return {"inline_keyboard": keyboard}

    def physical_builder_text(self):
        return self.password_record_text()

    def physical_builder_markup(self):
        return self.password_record_markup()

    def unlock_builder_text(self):
        cur = self.format_password_display(self.saved_unlock_pattern or "CC")
        draft = self.format_password_display("".join(self.unlock_draft))
        last_press = f"\n⚡️ Последнее нажатие: <code>{self.last_press_info}</code>" if self.last_press_info else ""
        return (
            "<b>🔓 Настройка жеста разблокировки Windows</b>\n\n"
            f"Текущий жест:\n<blockquote><b>{cur}</b></blockquote>\n\n"
            f"Новый жест:\n<blockquote><b>{draft if self.unlock_draft else 'Нажимай кнопку на корпусе или кнопки ниже'}</b></blockquote>\n"
            f"Шагов: {len(self.unlock_draft)} / 8 (минимум 2, по умолчанию 2 клика){last_press}\n\n"
            "<i>Этот жест вводится на кнопке для разблокировки Windows в течение 5 минут после морзе-включения.</i>"
        )

    def unlock_builder_markup(self):
        keyboard = [
            [
                {"text": "➕ CLICK (короткое)", "callback_data": "unlock_pat_add:C"},
                {"text": "➕ HOLD (длинное)", "callback_data": "unlock_pat_add:H"},
            ],
            [
                {"text": "↩ Убрать", "callback_data": "unlock_pat_back"},
                {"text": "🔄 Сброс (2 клика)", "callback_data": "unlock_pat_reset"},
            ],
        ]
        if len(self.unlock_draft) >= 2:
            keyboard.append([{"text": "✅ Сохранить жест", "callback_data": "unlock_pat_save"}])
        keyboard.append([{"text": "✖ Отмена", "callback_data": "physical_button"}])
        return {"inline_keyboard": keyboard}

    def start_save_password(self, seq, chat_id, on_save_state_cb=None):
        cmd_id = self.mqtt.publish_esp_action("SET_PHYSICAL_PASSWORD", value=seq)
        self.pending_command_id = cmd_id
        self.pending_password_value = seq
        self.pending_action_kind = "save"
        self.ui.send_message(chat_id, "⏳ Сохраняю пароль в ESP...")

    def start_delete_password(self, chat_id):
        cmd_id = self.mqtt.publish_esp_action("DELETE_PHYSICAL_PASSWORD")
        self.pending_command_id = cmd_id
        self.pending_password_value = None
        self.pending_action_kind = "delete"
        self.ui.send_message(chat_id, "⏳ Удаляю пароль из ESP...")

    def start_unlock_input(self, chat_id):
        cmd_id = self.mqtt.publish_esp_action("UNLOCK_PHYSICAL_INPUT")
        self.pending_command_id = cmd_id
        self.pending_password_value = None
        self.pending_action_kind = "unlock_input"
        self.ui.send_message(chat_id, "⏳ Снимаю блокировку физической кнопки...")

    def handle_ack(self, ack_data, chat_id, on_state_change_cb=None):
        cmd_id = ack_data.get("id")
        result = ack_data.get("result")

        if self.pending_command_id and cmd_id == self.pending_command_id:
            action_kind = getattr(self, "pending_action_kind", "save")
            self.pending_command_id = None
            if result == "executed":
                if action_kind == "save":
                    self.saved_password = self.pending_password_value
                    if on_state_change_cb:
                        on_state_change_cb()
                    self.ui.send_message(chat_id, "✅ Пароль физической кнопки сохранён в ESP.", reply_markup=self.physical_control_markup())
                elif action_kind == "delete":
                    self.saved_password = None
                    if on_state_change_cb:
                        on_state_change_cb()
                    self.ui.send_message(chat_id, "✅ Пароль физической кнопки удалён.", reply_markup=self.physical_control_markup())
                elif action_kind == "unlock_input":
                    self.esp_locked = False
                    if on_state_change_cb:
                        on_state_change_cb()
                    self.ui.send_message(chat_id, "✅ Физическая кнопка разблокирована.", reply_markup=self.physical_control_markup())
            else:
                self.ui.send_message(chat_id, f"❌ ESP не выполнила действие: {result}", reply_markup=self.physical_control_markup())

    def _handle_unlock_gesture_triggered(self, chat_id):
        eligible, reason = self.is_morse_unlock_eligible()
        if not eligible:
            LOG.info("Unlock gesture rejected: %s", reason)
            self.ui.send_message(
                chat_id,
                f"⚠️ <b>Морзе-разблокировка отклонена</b>\n\n{reason}",
            )
            return

        # Security passed!
        self.physical_morse_unlock_eligible = False
        self.last_unlock_gesture_at = time.time()
        if self.on_state_change_cb:
            self.on_state_change_cb()
        LOG.info("Unlock gesture validated! Triggering unlock flow.")

        if self.on_trigger_unlock_cb:
            self.on_trigger_unlock_cb(chat_id)
        else:
            self.ui.send_message(chat_id, "🔓 <b>Жест разблокировки принят!</b>")

    def _handle_unlock_and_workspace_gesture_triggered(self, chat_id):
        recent_unlock = (time.time() - getattr(self, "last_unlock_gesture_at", 0.0) <= 6.0)
        eligible, reason = self.is_morse_unlock_eligible()
        if not eligible and not recent_unlock:
            LOG.info("Unlock+Workspace gesture rejected: %s", reason)
            self.ui.send_message(
                chat_id,
                f"⚠️ <b>Морзе-разблокировка отклонена</b>\n\n{reason}",
            )
            return

        self.physical_morse_unlock_eligible = False
        self.last_unlock_gesture_at = time.time()
        if self.on_state_change_cb:
            self.on_state_change_cb()
        LOG.info("Unlock+Workspace gesture validated! Triggering combo flow.")

        if self.on_trigger_unlock_and_workspace_cb:
            self.on_trigger_unlock_and_workspace_cb(chat_id)
        else:
            self.ui.send_message(chat_id, "🚀 <b>Жест разблокировки + рабочий сетап принят!</b>")

    def handle_event(self, event_data, chat_id):
        event_type = event_data.get("event")
        now = time.time()

        if event_type == "press":
            kind = "CLICK" if event_data.get("kind") == "click" else "HOLD"
            duration = event_data.get("duration_ms", 0)
            symbol = "C" if kind == "CLICK" else "H"
            self.last_press_info = f"{kind.title()} ({duration} ms)"

            # 1. Diagnostics mode
            if self.mode == "test" or self.input_message_id:
                self.input_entries.append(f"{kind.title()} {duration} ms")
                self.input_entries = self.input_entries[-12:]
                target_msg_id = self.input_message_id or self.active_message_id
                if target_msg_id:
                    text = "<b>🕹 Физическая кнопка — диагностика</b>\n\n" + "\n".join(self.input_entries)
                    res = self.ui.send_message(chat_id, text, reply_markup={"inline_keyboard": [self.ui.back_button("physical_button")]}, message_id=target_msg_id)
                    if res and res.get("message_id"):
                        self.input_message_id = res["message_id"]
                        self.active_message_id = res["message_id"]
                return

            # 2. Live password recording mode via physical button
            if self.mode == "record_password" and self.active_message_id:
                if len(self.password_draft) < 12:
                    self.password_draft.append(symbol)
                text = self.password_record_text()
                markup = self.password_record_markup()
                res = self.ui.send_message(chat_id, text, reply_markup=markup, message_id=self.active_message_id)
                if res and res.get("message_id"):
                    self.active_message_id = res["message_id"]
                return

            # 3. Live unlock pattern recording mode via physical button
            if self.mode == "record_unlock_pattern" and self.active_message_id:
                if len(self.unlock_draft) < 8:
                    self.unlock_draft.append(symbol)
                text = self.unlock_builder_text()
                markup = self.unlock_builder_markup()
                res = self.ui.send_message(chat_id, text, reply_markup=markup, message_id=self.active_message_id)
                if res and res.get("message_id"):
                    self.active_message_id = res["message_id"]
                return

            # 4. Normal gesture recognition (only when not in configuration modes)
            within_combo_window = (now - getattr(self, "last_unlock_gesture_at", 0.0) <= 6.0 and len(self.gesture_buffer) > 0)
            if not self.physical_morse_unlock_eligible and not within_combo_window:
                return

            if self.physical_morse_unlock_eligible:
                eligible, _ = self.is_morse_unlock_eligible(now=now)
                if not eligible:
                    self.physical_morse_unlock_eligible = False
                    if self.on_state_change_cb:
                        self.on_state_change_cb()
                    return

            # Track gesture buffer (keep only presses from last 6.0 seconds to allow 2 clicks + 2 holds)
            old_buffer_len = len(self.gesture_buffer)
            self.gesture_buffer = [(s, t) for (s, t) in self.gesture_buffer if now - t <= 6.0]
            if len(self.gesture_buffer) < old_buffer_len or (now - getattr(self, "last_unlock_triggered_at", 0.0) > 6.0):
                self.last_unlock_consumed_count = 0
            self.gesture_buffer.append((symbol, now))

            unlock_pattern = self.saved_unlock_pattern or "CC"
            ws_pattern = unlock_pattern + "HH"

            # 1. Check workspace combo pattern first (e.g. CCHH)
            ws_len = len(ws_pattern)
            if len(self.gesture_buffer) >= ws_len:
                recent_ws = "".join(s for (s, t) in self.gesture_buffer[-ws_len:])
                if recent_ws == ws_pattern:
                    self.gesture_buffer.clear()
                    self.last_unlock_consumed_count = 0
                    self._handle_unlock_and_workspace_gesture_triggered(chat_id)
                    return

            # 2. Check unlock pattern (e.g. CC)
            pattern_len = len(unlock_pattern)
            if len(self.gesture_buffer) >= pattern_len:
                recent = "".join(s for (s, t) in self.gesture_buffer[-pattern_len:])
                if recent == unlock_pattern:
                    consumed = getattr(self, "last_unlock_consumed_count", 0)
                    new_presses = len(self.gesture_buffer) - min(consumed, len(self.gesture_buffer))
                    if (now - getattr(self, "last_unlock_triggered_at", 0.0) > 3.0) and new_presses >= pattern_len:
                        self.last_unlock_triggered_at = now
                        self.last_unlock_consumed_count = len(self.gesture_buffer)
                        self._handle_unlock_gesture_triggered(chat_id)

        elif event_type == "success":
            self.esp_locked = False
            self.last_physical_morse_success_at = now
            self.physical_morse_unlock_eligible = True
            if self.on_state_change_cb:
                self.on_state_change_cb()
            msg = (
                "✅ <b>Физический пароль принят — отправляю нажатие POWER.</b>\n\n"
                "<i>🔓 Морзе-разблокировка активна в течение 5 минут. Нажми жест на кнопке для разблокировки Windows.</i>"
            )
            self.ui.send_message(chat_id, msg, reply_markup=self.physical_control_markup())

        elif event_type == "recorded":
            seq = event_data.get("sequence", "")
            if seq:
                self.password_draft = list(seq)
            target_seq = "".join(self.password_draft) or seq
            labels = self.format_password_display(target_seq)
            self.mode = None
            self.last_press_info = None
            text = (
                f"<b>🎙 Пароль на кнопке зафиксирован ({len(target_seq)} шагов):</b>\n\n"
                f"<blockquote><b>{labels}</b></blockquote>\n\n"
                "Сохранить эту комбинацию в память ESP?"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "💾 Сохранить в ESP", "callback_data": f"physical_record_save:{target_seq}"}],
                    [{"text": "🔁 Натикать заново", "callback_data": "physical_record_begin"}],
                    self.ui.back_button("physical_button"),
                ]
            }
            msg_id = self.active_message_id or None
            self.active_message_id = None
            self.ui.send_message(chat_id, text, reply_markup=markup, message_id=msg_id)

        elif event_type == "failed":
            if self.mode in ("test", "record_password", "record_unlock_pattern") or (now - getattr(self, "last_unlock_gesture_at", 0.0) <= 10.0):
                LOG.info("Suppressing ESP failed event caused by active diagnostic/record mode or recent unlock gesture, resetting ESP lock")
                self.mqtt.publish_esp_action("UNLOCK_PHYSICAL_INPUT")
                return

            attempts = event_data.get("failed_attempts", 1)
            if attempts >= 3 or event_data.get("locked"):
                self.esp_locked = True
                if self.on_state_change_cb:
                    self.on_state_change_cb()
            LOG.warning("Physical button wrong password attempt: %s/3 (locked=%s)", attempts, self.esp_locked)
            msg = f"❌ <b>Неверный пароль на физической кнопке</b> (попытка {attempts} из 3)."
            self.ui.send_message(chat_id, msg, reply_markup=self.physical_control_markup())

        elif event_type in {"locked", "record_too_short", "record_timeout"}:
            if event_type == "locked":
                self.esp_locked = True
                if self.on_state_change_cb:
                    self.on_state_change_cb()
            elif event_type in ("record_too_short", "record_timeout"):
                self.mode = None
                self.active_message_id = None

            messages = {
                "locked": "🔒 <b>Физическая кнопка заблокирована</b> после 3 ошибок.",
                "record_too_short": "❌ Слишком короткая запись: нужно минимум 4 нажатия.",
                "record_timeout": "⌛ Запись пароля отменена: 60 секунд без завершения.",
            }
            markup = self.physical_control_markup()
            if event_type == "locked":
                markup = {"inline_keyboard": [[{"text": "✅ Это я — разблокировать", "callback_data": "physical_unlock"}], self.ui.back_button("physical_button")]}
            self.ui.send_message(chat_id, messages.get(event_type, event_type), reply_markup=markup)
