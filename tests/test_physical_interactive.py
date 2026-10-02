#!/usr/bin/env python3
"""
Unit and integration tests for interactive physical button workflows:
- Live physical button press tracking during password recording
- Live physical button press tracking during Windows unlock pattern setup
- ESP 'recorded' event finalization
- Gesture recognition isolation during active configuration modes
- Dynamic markup generation (delete password and emergency unlock buttons)
"""

from pathlib import Path
import sys
from unittest.mock import MagicMock

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "vds_bot"))

from vds_bot.physical_button import PhysicalButtonManager


def test_record_password_live_physical_presses():
    """Verify physical button presses dynamically update message in record_password mode."""
    mock_ui = MagicMock()
    mock_ui.send_message.return_value = {"message_id": 999}
    mock_mqtt = MagicMock()
    mgr = PhysicalButtonManager(mock_ui, mock_mqtt)

    mgr.mode = "record_password"
    mgr.active_message_id = 999
    mgr.password_draft = []

    # Press 1: Click 164 ms
    mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 164}, chat_id=123)
    assert mgr.password_draft == ["C"]
    assert mgr.last_press_info == "Click (164 ms)"
    assert mock_ui.send_message.call_count == 1
    args, kwargs = mock_ui.send_message.call_args
    assert kwargs.get("message_id") == 999
    assert "CLICK" in args[1]
    assert "164 ms" in args[1]
    assert "1 / 12" in args[1]

    # Press 2: Hold 935 ms
    mgr.handle_event({"event": "press", "kind": "hold", "duration_ms": 935}, chat_id=123)
    assert mgr.password_draft == ["C", "H"]
    assert mgr.last_press_info == "Hold (935 ms)"
    assert mock_ui.send_message.call_count == 2
    args, kwargs = mock_ui.send_message.call_args
    assert "CLICK · HOLD" in args[1]
    assert "935 ms" in args[1]
    assert "2 / 12" in args[1]


def test_record_password_recorded_event_finalization():
    """Verify ESP 'recorded' event finalizes draft and prompts for saving to ESP."""
    mock_ui = MagicMock()
    mock_ui.send_message.return_value = {"message_id": 999}
    mock_mqtt = MagicMock()
    mgr = PhysicalButtonManager(mock_ui, mock_mqtt)

    mgr.mode = "record_password"
    mgr.active_message_id = 999
    mgr.password_draft = ["C", "H", "C", "C"]

    mgr.handle_event({"event": "recorded", "sequence": "CHCC"}, chat_id=123)
    assert mgr.mode is None
    assert mgr.active_message_id is None
    assert mgr.password_draft == ["C", "H", "C", "C"]

    mock_ui.send_message.assert_called_once()
    args, kwargs = mock_ui.send_message.call_args
    assert "Пароль на кнопке зафиксирован (4 шагов)" in args[1]
    markup = kwargs.get("reply_markup", {})
    inline = markup.get("inline_keyboard", [])
    assert any("physical_record_save:CHCC" in btn.get("callback_data", "") for row in inline for btn in row)


def test_unlock_pattern_live_physical_presses():
    """Verify physical button presses dynamically update message in record_unlock_pattern mode."""
    mock_ui = MagicMock()
    mock_ui.send_message.return_value = {"message_id": 888}
    mock_mqtt = MagicMock()
    mgr = PhysicalButtonManager(mock_ui, mock_mqtt)

    mgr.mode = "record_unlock_pattern"
    mgr.active_message_id = 888
    mgr.unlock_draft = []

    mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 120}, chat_id=123)
    mgr.handle_event({"event": "press", "kind": "hold", "duration_ms": 750}, chat_id=123)
    assert mgr.unlock_draft == ["C", "H"]
    assert mgr.last_press_info == "Hold (750 ms)"
    args, kwargs = mock_ui.send_message.call_args
    assert "CLICK · HOLD" in args[1]
    assert "750 ms" in args[1]
    assert kwargs.get("message_id") == 888


def test_gestures_isolated_during_active_configuration_modes():
    """Verify normal unlock gestures never fire while in record_password, record_unlock_pattern, or test modes."""
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    mgr = PhysicalButtonManager(mock_ui, mock_mqtt)
    triggered = []
    mgr.on_trigger_unlock_cb = lambda cid: triggered.append(cid)
    mgr.physical_morse_unlock_eligible = True

    # In record_password mode:
    mgr.mode = "record_password"
    mgr.active_message_id = 100
    mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
    mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
    assert len(triggered) == 0, "Unlock must not trigger during password recording!"

    # In test mode:
    mgr.mode = "test"
    mgr.active_message_id = 101
    mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
    mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
    assert len(triggered) == 0, "Unlock must not trigger during diagnostic test!"


def test_physical_control_markup_dynamic_buttons():
    """Verify clean physical button menu shows buttons conditionally."""
    mock_ui = MagicMock()
    mock_ui.back_button.return_value = [{"text": "⬅ Назад", "callback_data": "settings_menu"}]
    mock_mqtt = MagicMock()
    mgr = PhysicalButtonManager(mock_ui, mock_mqtt)

    # 1. Default state: No password, not locked
    markup = mgr.physical_control_markup()
    flat_buttons = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
    flat_callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]

    assert "🔑 Задать пароль включения" in flat_buttons
    assert "physical_record_begin" in flat_callbacks
    assert not any("Удалить" in t for t in flat_buttons)
    assert not any("Разблокировать" in t for t in flat_buttons)

    # 2. Password set
    mgr.saved_password = "CHCC"
    markup2 = mgr.physical_control_markup()
    flat_buttons2 = [btn["text"] for row in markup2["inline_keyboard"] for btn in row]
    assert "🔑 Изменить пароль включения" in flat_buttons2
    assert any("Удалить" in t for t in flat_buttons2)
    assert not any("Разблокировать" in t for t in flat_buttons2)

    # 3. ESP Locked
    mgr.esp_locked = True
    markup3 = mgr.physical_control_markup()
    flat_buttons3 = [btn["text"] for row in markup3["inline_keyboard"] for btn in row]
    assert any("Разблокировать" in t for t in flat_buttons3)


def test_bot_callback_workflow():
    """Verify bot callback handling for new unified recording actions."""
    import bot
    user_id = bot.ALLOWED_USER_ID
    app = bot.PcControlBotApp.__new__(bot.PcControlBotApp)
    app.ui = MagicMock()
    app.mqtt = MagicMock()
    app.physical_btn = PhysicalButtonManager(app.ui, app.mqtt)
    app.save_state = MagicMock()

    # 1. Start unified recording via physical_record_begin
    app.ui.send_message.return_value = {"message_id": 555}
    cb = {
        "id": "cb1",
        "data": "physical_record_begin",
        "from": {"id": user_id},
        "message": {"chat": {"id": user_id}, "message_id": 500},
    }
    app.handle_callback(cb)
    app.mqtt.publish_esp_action.assert_called_with("START_PHYSICAL_RECORDING")
    assert app.physical_btn.mode == "record_password"
    assert app.physical_btn.active_message_id == 555

    # 2. Add via bot button
    cb_add = {
        "id": "cb2",
        "data": "physical_add:C",
        "from": {"id": user_id},
        "message": {"chat": {"id": user_id}, "message_id": 555},
    }
    app.handle_callback(cb_add)
    assert app.physical_btn.password_draft == ["C"]
    assert app.physical_btn.last_press_info == "Введено в боте"

    # 3. Clear draft
    cb_clear = {
        "id": "cb3",
        "data": "physical_clear",
        "from": {"id": user_id},
        "message": {"chat": {"id": user_id}, "message_id": 555},
    }
    app.handle_callback(cb_clear)
    assert app.physical_btn.password_draft == []

    # 4. Cancel recording
    cb_cancel = {
        "id": "cb4",
        "data": "physical_cancel_record",
        "from": {"id": user_id},
        "message": {"chat": {"id": user_id}, "message_id": 555},
    }
    app.handle_callback(cb_cancel)
    assert app.physical_btn.mode is None
    assert app.physical_btn.active_message_id is None
