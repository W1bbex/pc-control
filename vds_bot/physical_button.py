#!/usr/bin/env python3

import logging
import time

LOG = logging.getLogger("pc-control-bot.physical_button")

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
        pwd_display = self.format_password_display(self.saved_password)
        count_str = f" ({len(self.saved_password)} шагов)" if self.saved_password else ""
        unlock_display = self.format_password_display(self.saved_unlock_pattern or "CC")
        unlock_count = f" ({len(self.saved_unlock_pattern)} шага)" if self.saved_unlock_pattern else " (по умолчанию)"
        ws_pattern = (self.saved_unlock_pattern or "CC") + "HH"
        ws_display = self.format_password_display(ws_pattern)

        if self.physical_morse_unlock_eligible:
            elapsed = int(time.time() - (self.last_physical_morse_success_at or 0))
            remaining = max(0, int(MORSE_UNLOCK_WINDOW_SECONDS - elapsed))
            if remaining > 0:
                status_line = f"🟢 Активна ({remaining} сек)"
            else:
                status_line = "🔴 Истекла"
        else:
            status_line = "⚪️ Неактивна"

        return (
            "<b>🕹 Физическая кнопка</b>\n\n"
            f"<b>🔑 Пароль включения ПК (морзе):</b>\n<blockquote><b>{pwd_display}</b></blockquote>{count_str}\n\n"
            f"<b>🔓 Жест разблокировки Windows:</b>\n<blockquote><b>{unlock_display}</b></blockquote>{unlock_count}\n\n"
            f"<b>🚀 Жест разблока + рабочего сетапа:</b>\n<blockquote><b>{ws_display}</b></blockquote>\n\n"
            f"Окно морзе-разблокировки: <b>{status_line}</b>"
        )

    def physical_control_markup(self):
        return {
            "inline_keyboard": [
                [{"text": "🧪 Тест нажатий", "callback_data": "physical_test"}],
                [
                    {"text": "🔑 Пароль включения", "callback_data": "physical_password_begin"},
                    {"text": "🔓 Жест разблока", "callback_data": "physical_unlock_pattern_begin"},
                ],
                [{"text": "🎙 Натикать новый пароль включения", "callback_data": "physical_record_begin"}],
                [{"text": "🗑 Удалить пароль включения", "callback_data": "physical_password_delete"}],
                [{"text": "✅ Это я — разблокировать", "callback_data": "physical_unlock"}],
                self.ui.back_button("settings_menu"),
            ]
        }

    def physical_builder_text(self):
        sequence = self.format_password_display("".join(self.password_draft))
        return (
            "<b>🔐 Новый пароль включения кнопки</b>\n\n"
            f"<blockquote>{sequence if self.password_draft else 'Добавь шаги ниже'}</blockquote>\n"
            f"Шагов: {len(self.password_draft)} / 12 (минимум 4)"
        )

    def physical_builder_markup(self):
        keyboard = [
            [
                {"text": "CLICK", "callback_data": "physical_add:C"},
                {"text": "HOLD", "callback_data": "physical_add:H"},
            ],
            [{"text": "↩ Убрать последний", "callback_data": "physical_back"}],
        ]
        if len(self.password_draft) >= 4:
            keyboard.append([{"text": "✅ Сохранить пароль", "callback_data": "physical_save"}])
        keyboard.append([{"text": "✖ Отмена", "callback_data": "physical_button"}])
        return {"inline_keyboard": keyboard}

    def unlock_builder_text(self):
        cur = self.format_password_display(self.saved_unlock_pattern or "CC")
        draft = self.format_password_display("".join(self.unlock_draft))
        return (
            "<b>🔓 Настройка жеста разблокировки Windows</b>\n\n"
            f"Текущий жест:\n<blockquote><b>{cur}</b></blockquote>\n\n"
            f"Новый жест:\n<blockquote><b>{draft if self.unlock_draft else 'Добавь шаги ниже'}</b></blockquote>\n"
            f"Шагов: {len(self.unlock_draft)} / 8 (минимум 2, по умолчанию 2 клика)\n\n"
            "<i>Этот жест вводится на кнопке для разблокировки Windows в течение 5 минут после морзе-включения.</i>"
        )

    def unlock_builder_markup(self):
        keyboard = [
            [
                {"text": "CLICK (короткое)", "callback_data": "unlock_pat_add:C"},
                {"text": "HOLD (длинное)", "callback_data": "unlock_pat_add:H"},
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

        if cmd_id == self.pending_command_id:
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

            self.input_entries.append(f"{kind.title()} {duration} ms")
            self.input_entries = self.input_entries[-12:]
            if self.input_message_id:
                text = "<b>🕹 Физическая кнопка — диагностика</b>\n\n" + "\n".join(self.input_entries)
                res = self.ui.send_message(chat_id, text, reply_markup={"inline_keyboard": [self.ui.back_button("physical_button")]}, message_id=self.input_message_id)
                if res and res.get("message_id"):
                    self.input_message_id = res["message_id"]

            # Only track gestures IF Morse unlock is currently eligible OR if within 6s combo window after unlock
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
            self.gesture_buffer = [(s, t) for (s, t) in self.gesture_buffer if now - t <= 6.0]
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
                    self.last_unlock_gesture_at = now
                    self._handle_unlock_and_workspace_gesture_triggered(chat_id)
                    return

            # 2. Check unlock pattern (e.g. CC)
            # Note: hold+hold ('HH') alone matches neither ws_pattern nor unlock_pattern,
            # ensuring 'HH' alone on an unlocked or locked screen produces no action.
            pattern_len = len(unlock_pattern)
            if len(self.gesture_buffer) >= pattern_len:
                recent = "".join(s for (s, t) in self.gesture_buffer[-pattern_len:])
                if recent == unlock_pattern:
                    # Require at least pattern_len new presses since last unlock to prevent
                    # a single click from pairing with an older click in the buffer.
                    consumed = getattr(self, "last_unlock_consumed_count", 0)
                    new_presses = len(self.gesture_buffer) - min(consumed, len(self.gesture_buffer))
                    if (now - getattr(self, "last_unlock_triggered_at", 0.0) > 3.0) and new_presses >= pattern_len:
                        self.last_unlock_triggered_at = now
                        self.last_unlock_consumed_count = len(self.gesture_buffer)
                        self.last_unlock_gesture_at = now
                        self._handle_unlock_gesture_triggered(chat_id)

        elif event_type == "success":
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
            labels = self.format_password_display(seq)
            text = (
                f"<b>🎙 Пароль на кнопке записан ({len(seq)} шагов):</b>\n\n<blockquote>{labels}</blockquote>\n\n"
                "Сохранить эту комбинацию?"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "💾 Сохранить", "callback_data": f"physical_record_save:{seq}"}],
                    [{"text": "🔁 Натикать заново", "callback_data": "physical_record_begin"}],
                    self.ui.back_button("physical_button"),
                ]
            }
            self.ui.send_message(chat_id, text, reply_markup=markup)

        elif event_type == "failed":
            # If a valid unlock gesture was accepted in the last 10 seconds,
            # ESP may treat those gesture clicks as an incomplete Morse attempt.
            # Suppress error message and reset ESP lock in that case only.
            if now - getattr(self, "last_unlock_gesture_at", 0.0) <= 10.0:
                LOG.info("Suppressing ESP failed event caused by recent unlock gesture, resetting ESP lock")
                self.mqtt.publish_esp_action("UNLOCK_PHYSICAL_INPUT")
                return

            attempts = event_data.get("failed_attempts", 1)
            LOG.warning("Physical button wrong password attempt: %s/3", attempts)
            msg = f"❌ <b>Неверный пароль на физической кнопке</b> (попытка {attempts} из 3)."
            self.ui.send_message(chat_id, msg, reply_markup=self.physical_control_markup())

        elif event_type in {"locked", "record_too_short", "record_timeout"}:
            messages = {
                "locked": "🔒 <b>Физическая кнопка заблокирована</b> после 3 ошибок.",
                "record_too_short": "❌ Слишком короткая запись: нужно минимум 4 нажатия.",
                "record_timeout": "⌛ Запись пароля отменена: 60 секунд без завершения.",
            }
            markup = self.physical_control_markup()
            if event_type == "locked":
                markup = {"inline_keyboard": [[{"text": "✅ Это я — разблокировать", "callback_data": "physical_unlock"}], self.ui.back_button("physical_button")]}
            self.ui.send_message(chat_id, messages.get(event_type, event_type), reply_markup=markup)
