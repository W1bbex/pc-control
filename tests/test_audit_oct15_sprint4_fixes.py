"""
Regression tests for Audit Findings (October 15, 2026 - Sprint 4 Stealth Chain & P0/P1 Fixes).
Covers:
1. core/stealth_guard.py:
   - parse_hotkey_sequence parses ordered keys.
   - Strict sequential and chord matching for unlock hotkey.
   - Extraneous key breaks sequence (no false positive in games/typing).
   - 5x Escape triggers emergency_escape.
   - Persistent exit reason file written and read via get_last_exit_reason.
   - DPMS idle timer protection (SetCursorPos called only on startup).
2. desktop/stealth.py:
   - check_stealth_process_exit retrieves dynamic reason from stealth_mgr.
3. vds_bot/mqtt_dispatcher.py:
   - Dynamic message formatting for local_hotkey, emergency_escape, network_offline, watchdog.
   - Telemetry duplicate stealth exit debounce (5s).
4. agent.py:
   - 64-bit ctypes signatures for acquire_single_instance.
   - Non-blocking acknowledge.
5. system/action_dispatcher.py:
   - Session 0 lock targeting active interactive session.
6. vds_bot/power_flow.py:
   - Awaiting session transition on result == 'executed' without false unlock failure.
   - WAITING_WORKSPACE_DECISION exempted from timeout.
7. vds_bot/bot.py:
   - current_menu_view not clobbered by arbitrary action callbacks.
8. core/media_manager.py:
   - Safe null handling for durationMs, title, artists.
   - _request_with_retry recovers from network OSError.
9. vds_bot/physical_button.py:
   - handle_ack ignores ACK when pending_command_id is None.
"""

import ctypes
from ctypes import wintypes
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.media_manager import MediaManager
from core.stealth_guard import (
    EXIT_REASON_FILE,
    KBDLLHOOKSTRUCT,
    StealthManager,
    StealthOverlay,
    parse_hotkey,
    parse_hotkey_sequence,
)
from desktop.stealth import StealthMixin
from vds_bot.bot import PcControlBotApp
from vds_bot.mqtt_dispatcher import MqttDispatcherMixin
from vds_bot.physical_button import PhysicalButtonManager
from vds_bot.power_flow import PowerFlowManager, PowerFlowStage


def _send_mock_key(overlay, vk, is_up=False):
    flags = 0x80 if is_up else 0
    wparam = 0x0101 if is_up else 0x0100
    kb = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=flags, time=0, dwExtraInfo=None)
    lparam = ctypes.addressof(kb)
    return overlay._keyboard_callback(0, wparam, lparam)


# -----------------------------------------------------------------------------
# 1. Stealth Mode & Hotkey Tests
# -----------------------------------------------------------------------------

def test_parse_hotkey_sequence():
    """1.1: Verify parse_hotkey_sequence extracts ordered VKs excluding modifiers."""
    seq = parse_hotkey_sequence("Win+W+1")
    assert seq == [0x57, 0x31]

    seq_esc = parse_hotkey_sequence("Ctrl+Alt+Shift+Escape")
    assert seq_esc == [0x1B]

    seq_mixed = parse_hotkey_sequence("Win+A+B+C")
    assert seq_mixed == [0x41, 0x42, 0x43]


def test_stealth_overlay_strict_sequence_vs_extraneous_keys():
    """1.2: Verify strict sequential matching: extraneous key breaks sequence."""
    overlay = StealthOverlay(hotkey="Win+W+1")
    overlay.stop = MagicMock()

    # Simulate Win physically held down
    with patch.object(overlay, "_are_modifiers_matching", return_value=True):
        # Step 1: Tap 'W' (0x57) down and up
        _send_mock_key(overlay, 0x57, is_up=False)
        _send_mock_key(overlay, 0x57, is_up=True)
        assert overlay._seq_idx == 1
        assert not overlay.stop.called

        # Step 2: Extraneous key 'E' (0x45) typed while Win held down (e.g. gaming WASD)
        _send_mock_key(overlay, 0x45, is_up=False)
        _send_mock_key(overlay, 0x45, is_up=True)
        # Sequence MUST be reset to 0!
        assert overlay._seq_idx == 0
        assert not overlay.stop.called

        # Step 3: Now type '1' (0x31)
        _send_mock_key(overlay, 0x31, is_up=False)
        # Since sequence was reset and W was released, typing 1 should NOT trigger unlock!
        assert not overlay.stop.called


def test_stealth_overlay_correct_sequence_triggers_exit():
    """1.3: Verify correct Win held + W then 1 triggers exit with reason local_hotkey."""
    overlay = StealthOverlay(hotkey="Win+W+1")
    overlay.stop = MagicMock()

    with patch.object(overlay, "_are_modifiers_matching", return_value=True):
        # Step 1: 'W'
        _send_mock_key(overlay, 0x57, is_up=False)
        assert overlay._seq_idx == 1

        # Step 2: '1'
        _send_mock_key(overlay, 0x31, is_up=False)
        overlay.stop.assert_called_once_with(reason="local_hotkey")


def test_stealth_overlay_5x_escape_triggers_emergency_escape():
    """1.4: Verify 5x Escape triggers exit with reason emergency_escape."""
    overlay = StealthOverlay(hotkey="Win+W+1")
    overlay.stop = MagicMock()

    for _ in range(5):
        _send_mock_key(overlay, 0x1B, is_up=False)

    overlay.stop.assert_called_once_with(reason="emergency_escape")


def test_stealth_overlay_persists_exit_reason(tmp_path):
    """1.5: Verify stop() persists exit reason to EXIT_REASON_FILE and manager reads it."""
    test_reason_file = tmp_path / "stealth_exit_reason.json"
    with patch("core.stealth_guard.EXIT_REASON_FILE", test_reason_file):
        overlay = StealthOverlay(hotkey="Win+W+1")
        overlay.stop(reason="network_offline")
        assert overlay.exit_reason == "network_offline"

        mgr = StealthManager()
        reason = mgr.get_last_exit_reason()
        assert reason == "network_offline"


def test_stealth_overlay_dpms_protection():
    """1.6: Verify SetCursorPos is only called when set_pos=True (startup), not periodically."""
    overlay = StealthOverlay(hotkey="Win+W+1")
    with patch("core.stealth_guard.user32.SetCursorPos") as mock_set_cursor, \
         patch("core.stealth_guard.user32.ClipCursor"):
        overlay._lock_cursor_and_topmost(set_pos=False)
        assert not mock_set_cursor.called

        overlay._lock_cursor_and_topmost(set_pos=True)
        mock_set_cursor.assert_called_once_with(0, 0)


# -----------------------------------------------------------------------------
# 2. Desktop Stealth & VDS Bot Reason Tests
# -----------------------------------------------------------------------------

def test_desktop_stealth_publishes_dynamic_reason():
    """2.1: Verify check_stealth_process_exit publishes real exit reason."""
    agent = StealthMixin()
    agent.stealth_mgr = MagicMock()
    agent.stealth_mgr.is_active.return_value = False
    agent.stealth_mgr.get_last_exit_reason.return_value = "emergency_escape"
    agent.stealth_mgr.mute_audio = False
    agent._stealth_is_active = True
    agent._prev_mute_state = None
    agent.wake_displays = MagicMock()
    agent.publish_telemetry = MagicMock()
    agent.get_session_seconds = MagicMock(return_value=123)
    agent.mqtt = MagicMock()

    agent.check_stealth_process_exit()

    assert agent.mqtt.publish.called
    payload = json.loads(agent.mqtt.publish.call_args[0][1])
    assert payload["event"] == "stealth_exit"
    assert payload["reason"] == "emergency_escape"


def test_vds_bot_on_stealth_exit_dynamic_reason_formatting():
    """2.2: Verify on_stealth_exit formats alerts dynamically by reason."""
    dispatcher = MqttDispatcherMixin()
    dispatcher.monitoring = MagicMock()
    dispatcher.monitoring.latest_telemetry = {"stealth_mode": True}
    dispatcher.ui = MagicMock()
    dispatcher.allowed_user_id = 12345
    dispatcher.active_stealth_card = None

    # Test emergency_escape
    dispatcher.on_stealth_exit({"reason": "emergency_escape"})
    msg_call = dispatcher.ui.send_message.call_args[0]
    assert "экстренный выход 5x <code>Escape</code>" in msg_call[1]

    # Test network_offline
    dispatcher.ui.send_message.reset_mock()
    dispatcher.on_stealth_exit({"reason": "network_offline"})
    msg_call = dispatcher.ui.send_message.call_args[0]
    assert "таймауту сети" in msg_call[1]

    # Test local_hotkey
    dispatcher.ui.send_message.reset_mock()
    dispatcher.on_stealth_exit({"reason": "local_hotkey"})
    msg_call = dispatcher.ui.send_message.call_args[0]
    assert "хоткей <code>Win+W+1</code>" in msg_call[1]


# -----------------------------------------------------------------------------
# 3. Agent & System Action Dispatcher Tests
# -----------------------------------------------------------------------------

def test_agent_acquire_single_instance_64bit_ctypes():
    """3.1: Verify CreateMutexW, ReleaseMutex, CloseHandle have explicit restype/argtypes."""
    import agent
    with patch("ctypes.WinDLL") as mock_windll:
        mock_kernel32 = MagicMock()
        mock_windll.return_value = mock_kernel32
        with patch("ctypes.get_last_error", return_value=0):
            agent.acquire_single_instance()
            assert mock_kernel32.CreateMutexW.restype == wintypes.HANDLE
            assert mock_kernel32.ReleaseMutex.restype == wintypes.BOOL
            assert mock_kernel32.CloseHandle.restype == wintypes.BOOL


def test_agent_acknowledge_is_non_blocking():
    """3.2: Verify acknowledge does not call wait_for_publish(timeout=5)."""
    import agent
    app = agent.PCControlAgent.__new__(agent.PCControlAgent)
    app.mqtt = MagicMock()
    mock_publish_info = MagicMock()
    app.mqtt.publish.return_value = mock_publish_info

    app.acknowledge("test-cmd", "executed", {"data": 123})

    assert app.mqtt.publish.called
    assert not mock_publish_info.wait_for_publish.called


def test_system_action_dispatcher_lock_targets_interactive_session():
    """3.3: Verify system action == 'lock' targets active interactive session."""
    from system.action_dispatcher import ActionDispatcherMixin
    dispatcher = ActionDispatcherMixin()
    dispatcher.acknowledge = MagicMock()
    dispatcher.config = {}

    with patch("system.action_dispatcher.find_active_interactive_session_id", return_value=2), \
         patch("subprocess.Popen") as mock_popen, \
         patch("shutil.which", return_value="C:\\Windows\\System32\\tsdiscon.exe"):
        dispatcher.execute_action("cmd-1", "lock", {})
        dispatcher.acknowledge.assert_called_once_with("cmd-1", "executed")
        assert mock_popen.called


# -----------------------------------------------------------------------------
# 4. Power Flow & Bot Navigation Tests
# -----------------------------------------------------------------------------

def test_power_flow_handle_ack_does_not_fail_on_executed_keystrokes():
    """4.1: Verify handle_ack does not fail unlock if result == 'executed' while session is login_screen."""
    flow = PowerFlowManager(telegram_ui=MagicMock(), mqtt_gateway=MagicMock())
    flow.stage = PowerFlowStage.UNLOCKING
    flow.pending_unlock_command_id = "unlock-1"
    flow.unlock_attempts = 1
    flow.chat_id = 123
    flow.message_id = 456
    flow.ui = MagicMock()
    flow.mqtt = MagicMock()
    flow.mqtt.system_session = "login_screen"
    flow.on_unlock_failed = MagicMock()
    flow.on_unlock_success = MagicMock()

    flow.handle_ack({"id": "unlock-1", "result": "executed", "data": {"session": "login_screen"}})

    # Must NOT fail immediately - it awaits session transition to unlocked
    assert not flow.on_unlock_failed.called
    assert not flow.on_unlock_success.called


def test_power_flow_check_timeout_ignores_waiting_workspace_decision():
    """4.2: Verify check_timeout returns False during WAITING_WORKSPACE_DECISION."""
    flow = PowerFlowManager(telegram_ui=MagicMock(), mqtt_gateway=MagicMock())
    flow.stage = PowerFlowStage.WAITING_WORKSPACE_DECISION
    flow.expires_at = time.time() - 100  # technically past expired
    assert flow.check_timeout(now=time.time()) is False


def test_bot_current_menu_view_not_clobbered():
    """4.3: Verify bot callback query does not overwrite current_menu_view with arbitrary action data."""
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.current_menu_view = "main"
    app.allowed_user_id = 12345
    app.ui = MagicMock()

    callback = {
        "id": "cb1",
        "from": {"id": 12345},
        "message": {"chat": {"id": 12345}, "message_id": 999},
        "data": "kill_process:5432",
    }

    with patch("vds_bot.bot.route_power_callback", return_value=True):
        app.handle_callback(callback)

    # current_menu_view must remain "main", NOT "kill_process:5432"
    assert app.current_menu_view == "main"


# -----------------------------------------------------------------------------
# 5. Media Manager & Physical Button Tests
# -----------------------------------------------------------------------------

def test_media_manager_safe_null_handling():
    """5.1: Verify MediaManager handles None durationMs, title, artists without crash."""
    mgr = MediaManager.__new__(MediaManager)
    mgr._lock = threading.Lock()
    mgr._last_title = "Test Song"
    mgr._last_artist = "Test Artist"
    mgr._last_duration = 180.0
    mgr._last_liked_fetch = 0.0
    mgr._resolve_cache = {}
    mgr.liked_track_ids = set()
    mgr._get_yandex_headers = MagicMock(return_value={})

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "result": {
            "tracks": {
                "results": [
                    {
                        "id": "12345",
                        "realId": "12345",
                        "title": None,  # None title
                        "durationMs": None,  # None duration
                        "artists": [{"name": None}, None],  # None artist name / item
                    }
                ]
            }
        }
    }
    mgr._request_with_retry = MagicMock(return_value=mock_resp)

    # Must execute cleanly without TypeError or AttributeError
    res = mgr._resolve_track("Test Song", "Test Artist", 180.0)
    assert isinstance(res, tuple)


def test_media_manager_request_retry_on_oserror():
    """5.2: Verify _request_with_retry catches OSError and falls back to clean session."""
    mgr = MediaManager.__new__(MediaManager)
    mock_session = MagicMock()
    mock_session.get.side_effect = OSError(10049, "The requested address is not valid in its context")
    mgr._http_session = mock_session

    fallback_session = MagicMock()
    fallback_session.get.return_value = MagicMock(status_code=200)

    with patch("requests.Session", return_value=fallback_session):
        res = mgr._request_with_retry("GET", "https://api.music.yandex.net/test")
        assert res.status_code == 200
        assert mgr._http_session == fallback_session


def test_physical_button_handle_ack_ignores_none_pending_command():
    """5.3: Verify physical_button handle_ack ignores ACK when pending_command_id is None."""
    btn_mgr = PhysicalButtonManager(telegram_ui=MagicMock(), mqtt_gateway=MagicMock())
    btn_mgr.pending_command_id = None
    btn_mgr.saved_password = "EXISTING_PASSWORD"
    btn_mgr.ui = MagicMock()

    # ACK with id None must not match None == None
    btn_mgr.handle_ack({"id": None, "result": "executed"}, chat_id=123)

    assert btn_mgr.saved_password == "EXISTING_PASSWORD"
    assert not btn_mgr.ui.send_message.called
