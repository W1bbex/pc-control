#!/usr/bin/env python3
"""
Regression tests verifying core stability, win32 API types, and router resilience:
1. monitoring.py: dashboard_text works without NameError when media is paused.
2. telegram_ui.py: reboot_menu_markup returns strict 2D inline keyboard matrix.
3. workspace_router.py: power-on fallback returns strict 2D inline keyboard matrix.
4. power_router.py: power_on does not start power flow when ESP8266 is offline.
5. bot.py: poll_updates handles per-update exceptions without dropping subsequent updates.
6. boot_agent.py: WTS functions have Win64-safe argtypes and restype.
7. stealth_guard.py: HOOKPROC returns pointer-sized LRESULT (c_ssize_t).
"""

import ctypes
from ctypes import wintypes
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "vds_bot"))

from monitoring import MonitoringManager
from telegram_ui import TelegramBotApi
from routers.workspace_router import route_workspace_callback
from routers.power_router import route_power_callback
from core import stealth_guard
import boot_agent


def test_monitoring_dashboard_paused_no_name_error():
    """Verify that paused player does not raise NameError on pause_eid."""
    mgr = MonitoringManager.__new__(MonitoringManager)
    mgr.mqtt = MagicMock()
    mgr.mqtt.health.evaluate_pc_state.return_value = MagicMock(state="READY", label="В сети")
    mgr.latest_telemetry = {"cpu": {"load": 10}, "timestamp": 12345}
    mgr.latest_received_at = 12345.0
    mgr.is_shutting_down = lambda: False
    mgr.get_os_type = lambda t=None: "windows"
    mgr.header_os_tag = lambda t=None: "🪟"

    media_state = {
        "active": True,
        "title": "Test Track",
        "artist": "Test Artist",
        "is_playing": False,
        "is_liked": True,
    }
    # Should render cleanly without NameError: name 'pause_eid' is not defined
    text = mgr.dashboard_text(media_state)
    assert "Test Track" in text
    assert "⏸️" in text


def test_reboot_menu_markup_is_valid_2d():
    """Verify that reboot_menu_markup returns a valid 2D matrix of rows and buttons."""
    ui = TelegramBotApi("123:test_token")
    markup = ui.reboot_menu_markup()
    keyboard = markup.get("inline_keyboard", [])
    assert isinstance(keyboard, list)
    for row in keyboard:
        assert isinstance(row, list)
        for btn in row:
            assert isinstance(btn, dict), f"Button must be dict, got {type(btn)}"
            assert "text" in btn
            assert "callback_data" in btn


def test_workspace_router_fallback_markup_is_valid_2d():
    """Verify that fallback keyboard in workspace_router has proper 2D rows."""
    app = MagicMock()
    app.mqtt.health.evaluate_pc_state.return_value = MagicMock(
        state="OFFLINE",
        label="Выключен",
        reason="ПК выключен",
    )
    ui_inst = TelegramBotApi("123:test_token")
    app.ui.back_button = ui_inst.back_button

    route_workspace_callback(
        app=app,
        data="workspace_start_direct",
        chat_id=12345,
        message_id=99,
        callback_id="cb_test",
    )

    assert app.ui.send_message.called
    sent_args, sent_kwargs = app.ui.send_message.call_args
    markup = sent_kwargs.get("reply_markup") or sent_args[2]
    keyboard = markup.get("inline_keyboard", [])
    assert len(keyboard) == 2
    for row in keyboard:
        assert isinstance(row, list)
        for btn in row:
            assert isinstance(btn, dict)
            assert "text" in btn


def test_power_router_rejects_power_on_when_esp_offline():
    """Verify that power_on does NOT trigger start_power_on when ESP8266 is offline."""
    app = MagicMock()
    app.mqtt.health.evaluate_pc_state.return_value = MagicMock(state="OFFLINE")
    app.mqtt.system_status = "offline"
    app.mqtt.desktop_status = "offline"
    app.mqtt.esp_status = "offline"

    handled = route_power_callback(
        app=app,
        data="power_on",
        chat_id=12345,
        message_id=99,
        callback_id="cb_power",
    )
    assert handled is True
    app.ui.answer_callback.assert_called_with(
        "cb_power",
        "❌ Модуль питания (ESP8266) не в сети!",
        alert=True,
    )
    # Critical: start_power_on must NOT be called when ESP is offline
    app.power_flow.start_power_on.assert_not_called()


def test_bot_poll_updates_per_update_resilience():
    """Verify that an exception in one update does not drop remaining updates in batch."""
    from bot import PcControlBotApp
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.polling_session = MagicMock()
    app.offset = 100
    app.save_state = MagicMock()

    # Two updates in the batch
    fake_updates = [
        {"update_id": 100, "message": {"text": "bad"}},
        {"update_id": 101, "message": {"text": "good"}},
    ]
    app.polling_session.post.return_value.status_code = 200
    app.polling_session.post.return_value.json.return_value = {
        "ok": True,
        "result": fake_updates,
    }

    processed = []
    def fake_handle_message(msg):
        if msg.get("text") == "bad":
            raise ValueError("Boom!")
        processed.append(msg.get("text"))

    app.handle_message = fake_handle_message
    app.poll_updates()

    # The second update should still have been processed!
    assert processed == ["good"]
    assert app.offset == 102
    assert app.save_state.called


def test_boot_agent_wts_signatures():
    """Verify that boot_agent WTS APIs have declared 64-bit safe argtypes and restype."""
    # Trigger loading/configuration by inspecting active_console_session
    boot_agent.active_console_session(ttl=0)
    wts = ctypes.windll.wtsapi32
    assert wts.WTSQuerySessionInformationW.argtypes is not None
    assert wts.WTSQuerySessionInformationW.restype == wintypes.BOOL
    assert wts.WTSFreeMemory.argtypes == [ctypes.c_void_p]
    assert wts.WTSFreeMemory.restype is None


def test_stealth_guard_hookproc_return_type():
    """Verify that HOOKPROC return type is 64-bit LRESULT (c_ssize_t)."""
    assert stealth_guard.HOOKPROC._restype_ == ctypes.c_ssize_t
