import ctypes
import json
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import vds_bot.bot as bot_mod
from core.stealth_guard import parse_hotkey, StealthManager, StealthOverlay
from vds_bot.telegram_ui import TelegramBotApi
from vds_bot.bot import PcControlBotApp


def test_parse_hotkey():
    mods, vks = parse_hotkey("Ctrl+Alt+Shift+Escape")
    assert mods == {"ctrl", "alt", "shift"}
    assert vks == {0x1B}

    mods, vks = parse_hotkey("Win+W+1")
    assert mods == {"win"}
    assert vks == {ord("W"), ord("1")}

    mods, vks = parse_hotkey("Ctrl+Shift+F12")
    assert mods == {"ctrl", "shift"}
    assert vks == {0x7B}

    mods, vks = parse_hotkey("Win+Alt+K")
    assert mods == {"win", "alt"}
    assert vks == {ord("K")}

    mods, vks = parse_hotkey("Escape")
    assert mods == set()
    assert vks == {0x1B}


def test_stealth_manager_init():
    cfg = {
        "stealth_mode": {
            "secret_hotkey": "Ctrl+Shift+F10",
            "watchdog_timeout_minutes": 120,
            "mute_audio": False,
            "turn_off_displays": False,
        }
    }
    mgr = StealthManager(cfg)
    assert mgr.default_hotkey == "Ctrl+Shift+F10"
    assert mgr.default_timeout_minutes == 120
    assert mgr.mute_audio is False
    assert mgr.turn_off_displays is False
    assert not mgr.is_active()


def test_stealth_manager_lifecycle():
    mgr = StealthManager()
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 9999
        mock_popen.return_value = mock_proc

        assert mgr.activate() is True
        assert mgr.is_active() is True
        status = mgr.get_status()
        assert status["active"] is True
        assert status["pid"] == 9999

        assert mgr.deactivate() is True
        assert mgr.is_active() is False


def test_agent_handle_stealth_action():
    from agent import PCControlAgent

    with patch.object(PCControlAgent, "__init__", lambda self: None):
        agent = PCControlAgent()
        agent.stealth_mgr = MagicMock()
        agent.stealth_mgr.mute_audio = True
        agent.stealth_mgr.turn_off_displays = True
        agent.stealth_mgr.is_active.return_value = False
        agent._prev_mute_state = None
        agent.get_volume = MagicMock(return_value={"muted": False, "percent": 50})
        agent.set_volume = MagicMock()
        agent.turn_off_displays = MagicMock()
        agent.wake_displays = MagicMock()

        # Test ON
        res_on = agent.handle_stealth_action("on")
        assert res_on["active"] is True
        assert agent._prev_mute_state is False
        agent.set_volume.assert_called_with(mute=True)
        agent.stealth_mgr.activate.assert_called_once()
        agent.turn_off_displays.assert_called_once()

        # Test OFF
        agent.stealth_mgr.is_active.return_value = True
        res_off = agent.handle_stealth_action("off")
        assert res_off["active"] is False
        agent.stealth_mgr.deactivate.assert_called_once()
        agent.set_volume.assert_called_with(mute=False)
        agent.wake_displays.assert_called_once()


def test_handle_stealth_action_rolls_back_mute_on_activation_failure():
    from agent import PCControlAgent

    with patch.object(PCControlAgent, "__init__", lambda self: None):
        agent = PCControlAgent()
        agent.stealth_mgr = MagicMock()
        agent.stealth_mgr.mute_audio = True
        agent.stealth_mgr.turn_off_displays = True
        agent.stealth_mgr.activate.return_value = False
        agent.get_volume = MagicMock(return_value={"muted": False, "percent": 50})
        agent.set_volume = MagicMock()
        agent.turn_off_displays = MagicMock()

        res = agent.handle_stealth_action("on")

        assert res["active"] is False
        assert res["stealth"] == "failed"
        assert agent._stealth_is_active is False
        assert agent._prev_mute_state is None
        assert [c.kwargs for c in agent.set_volume.call_args_list] == [{"mute": True}, {"mute": False}]
        agent.turn_off_displays.assert_not_called()


def test_check_stealth_process_exit_ignored_during_activation():
    from agent import PCControlAgent

    with patch.object(PCControlAgent, "__init__", lambda self: None):
        agent = PCControlAgent()
        agent.stealth_mgr = MagicMock()
        agent.stealth_mgr.mute_audio = True
        agent.stealth_mgr.is_active.return_value = False
        agent._stealth_activating = True
        agent._stealth_is_active = True
        agent._prev_mute_state = False
        agent.set_volume = MagicMock()
        agent.wake_displays = MagicMock()
        agent.mqtt = MagicMock()

        agent.check_stealth_process_exit()

        agent.set_volume.assert_not_called()
        agent.wake_displays.assert_not_called()
        agent.mqtt.publish.assert_not_called()


def test_telegram_ui_stealth_markups():
    ui = TelegramBotApi("token")
    pm_off = ui.power_menu_markup(pc_state="READY", session="unlocked", stealth_active=False)
    buttons_off = [btn for row in pm_off["inline_keyboard"] for btn in row]
    assert any(b.get("callback_data") == "stealth_on" for b in buttons_off)

    pm_on = ui.power_menu_markup(pc_state="READY", session="unlocked", stealth_active=True)
    buttons_on = [btn for row in pm_on["inline_keyboard"] for btn in row]
    assert any(b.get("callback_data") == "stealth_off" for b in buttons_on)

    sm_off = ui.stealth_menu_markup(active=False)
    assert any(b.get("callback_data") == "stealth_on" for row in sm_off["inline_keyboard"] for b in row)

    sm_on = ui.stealth_menu_markup(active=True)
    assert any(b.get("callback_data") == "stealth_off" for row in sm_on["inline_keyboard"] for b in row)

    mon_markup = ui.monitoring_menu_markup()
    mon_btns = [btn for row in mon_markup["inline_keyboard"] for btn in row]
    assert any(b.get("callback_data") == "menu_activity" for b in mon_btns)


def test_bot_stealth_callbacks_and_commands(monkeypatch):
    uid = 12345
    monkeypatch.setattr(bot_mod, "ALLOWED_USER_ID", uid)
    ui = TelegramBotApi("token")
    ui.send_message = MagicMock()
    ui.answer_callback = MagicMock()
    mqtt = MagicMock()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.allowed_user_id = uid
    app.ui = ui
    app.mqtt = mqtt
    app.monitoring = MagicMock()
    app.monitoring.latest_telemetry = {"stealth_mode": False}

    # Test callback stealth_toggle
    cb_query = {
        "id": "cb1",
        "from": {"id": uid},
        "message": {"chat": {"id": uid}, "message_id": 10},
        "data": "stealth_toggle",
    }
    app.handle_callback(cb_query)
    app.mqtt.publish_desktop_action.assert_called_with("stealth_mode", {"state": "toggle"})

    # Test command /stealth on
    msg_on = {
        "chat": {"id": uid, "type": "private"},
        "from": {"id": uid},
        "text": "/stealth on",
        "message_id": 11,
    }
    app.handle_message(msg_on)
    app.mqtt.publish_desktop_action.assert_called_with("stealth_mode", {"state": "on"})

    # Test command /stealth off
    msg_off = {
        "chat": {"id": uid, "type": "private"},
        "from": {"id": uid},
        "text": "/stealth off",
        "message_id": 12,
    }
    app.handle_message(msg_off)
    app.mqtt.publish_desktop_action.assert_called_with("stealth_mode", {"state": "off"})


def test_vk_normalization_and_russian():
    from core.stealth_guard import normalize_vk, vk_matches

    assert normalize_vk(0x61) == 0x31  # Numpad 1 -> '1'
    assert normalize_vk(0x60) == 0x30  # Numpad 0 -> '0'
    assert normalize_vk(0x69) == 0x39  # Numpad 9 -> '9'
    assert vk_matches(0x61, 0x31) is True
    assert vk_matches(ord("W"), ord("W")) is True

    # Russian layout parsing
    mods, vks = parse_hotkey("Win+Ц+1")
    assert mods == {"win"}
    assert vks == {ord("W"), ord("1")}

    # Numpad alias parsing
    mods, vks = parse_hotkey("Win+W+numpad1")
    assert mods == {"win"}
    assert vks == {ord("W"), ord("1")}


def _send_mock_key(overlay, vk, is_up=False):
    import ctypes
    from core.stealth_guard import KBDLLHOOKSTRUCT, WM_KEYDOWN, WM_KEYUP

    flags = 0x80 if is_up else 0
    wparam = WM_KEYUP if is_up else WM_KEYDOWN
    kb = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=flags, time=0, dwExtraInfo=None)
    lparam = ctypes.addressof(kb)
    return overlay._keyboard_callback(0, wparam, lparam)


def test_stealth_overlay_simultaneous_hotkey():
    overlay = StealthOverlay("Win+W+1")
    overlay.stop = MagicMock()

    # Simulate Win key held down via GetAsyncKeyState
    def mock_async_keystate(vk):
        if vk in (0x5B, 0x5C):
            return 0x8000
        return 0

    with patch("ctypes.windll.user32.GetAsyncKeyState", side_effect=mock_async_keystate):
        _send_mock_key(overlay, ord("W"), is_up=False)
        assert not overlay.stop.called

        _send_mock_key(overlay, ord("1"), is_up=False)
        assert overlay.stop.called


def test_stealth_overlay_sequential_rolling_hotkey():
    overlay = StealthOverlay("Win+W+1")
    overlay.stop = MagicMock()

    def mock_async_keystate(vk):
        if vk in (0x5B, 0x5C):
            return 0x8000
        return 0

    with patch("ctypes.windll.user32.GetAsyncKeyState", side_effect=mock_async_keystate):
        # Press W, then release W
        _send_mock_key(overlay, ord("W"), is_up=False)
        _send_mock_key(overlay, ord("W"), is_up=True)
        assert not overlay.stop.called

        # Now press 1 while Win is still held
        _send_mock_key(overlay, ord("1"), is_up=False)
        assert overlay.stop.called


def test_stealth_overlay_numpad_hotkey():
    overlay = StealthOverlay("Win+W+1")
    overlay.stop = MagicMock()

    def mock_async_keystate(vk):
        if vk in (0x5B, 0x5C):
            return 0x8000
        return 0

    with patch("ctypes.windll.user32.GetAsyncKeyState", side_effect=mock_async_keystate):
        _send_mock_key(overlay, ord("W"), is_up=False)
        _send_mock_key(overlay, 0x61, is_up=False)  # Numpad 1
        assert overlay.stop.called


def test_stealth_overlay_5x_escape_emergency_exit():
    overlay = StealthOverlay("Win+W+1")
    overlay.stop = MagicMock()

    with patch("ctypes.windll.user32.GetAsyncKeyState", return_value=0):
        for _ in range(4):
            _send_mock_key(overlay, 0x1B, is_up=False)
            assert not overlay.stop.called

        _send_mock_key(overlay, 0x1B, is_up=False)
        assert overlay.stop.called


def test_stealth_manager_offline_timeout_args():
    cfg = {
        "stealth_mode": {
            "secret_hotkey": "Win+W+1",
            "watchdog_timeout_minutes": 120,
            "offline_timeout_minutes": 15,
        }
    }
    mgr = StealthManager(cfg)
    assert mgr.default_offline_timeout_minutes == 15

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 1234
        mock_popen.return_value = mock_proc

        mgr.activate()
        assert mock_popen.called
        cmd_args = mock_popen.call_args[0][0]
        assert "--offline-timeout" in cmd_args
        idx = cmd_args.index("--offline-timeout")
        assert cmd_args[idx + 1] == str(15 * 60)


def test_agent_offline_watchdog_timeout():
    import time
    from agent import PCControlAgent

    with patch.object(PCControlAgent, "__init__", lambda self: None):
        agent = PCControlAgent()
        agent.config = {"stealth_mode": {"offline_timeout_minutes": 10}}
        agent.stealth_mgr = MagicMock()
        agent.stealth_mgr.is_active.return_value = True
        agent.stealth_mgr.mute_audio = True
        agent.stealth_mgr.turn_off_displays = True
        agent._prev_mute_state = False
        agent.set_volume = MagicMock()
        agent.turn_off_displays = MagicMock()
        agent.wake_displays = MagicMock()

        # PC is offline
        agent.mqtt_online = False
        # Simulating 601 seconds elapsed offline
        agent._stealth_offline_since = time.time() - 601

        agent.check_stealth_offline_watchdog()

        # Should deactivate overlay
        agent.stealth_mgr.deactivate.assert_called_once()
        # Should restore audio mute
        agent.set_volume.assert_called_with(mute=False)
        # Should turn off displays via DPMS
        agent.turn_off_displays.assert_called_once()
        # MUST NOT wake displays
        assert not agent.wake_displays.called
        # Should reset timer
        assert agent._stealth_offline_since is None


def test_agent_offline_watchdog_resets_on_reconnect():
    import time
    from agent import PCControlAgent

    with patch.object(PCControlAgent, "__init__", lambda self: None):
        agent = PCControlAgent()
        agent.config = {"stealth_mode": {"offline_timeout_minutes": 10}}
        agent.stealth_mgr = MagicMock()
        agent.stealth_mgr.is_active.return_value = True
        agent.turn_off_displays = MagicMock()
        agent.wake_displays = MagicMock()

        # Connection restored
        agent.mqtt_online = True
        agent._stealth_offline_since = time.time() - 300

        agent.check_stealth_offline_watchdog()

        # Timer should be reset, no deactivation
        assert agent._stealth_offline_since is None
        assert not agent.stealth_mgr.deactivate.called
        assert not agent.turn_off_displays.called


def test_stealth_overlay_cleanup_restores_window_and_cursor():
    overlay = StealthOverlay("Win+W+1")
    overlay._mouse_hook = 101
    overlay._keyboard_hook = 102
    overlay._root = MagicMock()
    overlay._prev_foreground_hwnd = 54321

    with patch("ctypes.windll.user32.UnhookWindowsHookEx") as mock_unhook, \
         patch("ctypes.windll.user32.ClipCursor") as mock_clip, \
         patch("ctypes.windll.user32.IsWindow", return_value=True), \
         patch("ctypes.windll.user32.SetForegroundWindow") as mock_set_fg, \
         patch("ctypes.windll.user32.GetSystemMetrics", return_value=1920), \
         patch("ctypes.windll.user32.SetCursorPos") as mock_setpos, \
         patch("ctypes.windll.user32.mouse_event") as mock_mouse_event, \
         patch("ctypes.windll.user32.LoadCursorW") as mock_load_cursor, \
         patch("ctypes.windll.user32.SetCursor") as mock_set_cursor, \
         patch("ctypes.windll.user32.ShowCursor") as mock_show_cursor:

        overlay._cleanup()

        assert mock_unhook.call_count == 2
        mock_clip.assert_called_with(None)
        mock_set_fg.assert_called_with(54321)
        mock_setpos.assert_called_with(960, 960)
        assert mock_mouse_event.call_count == 2
        mock_show_cursor.assert_called_with(True)


def test_agent_check_stealth_process_exit_publishes_event_and_telemetry():
    import json
    from agent import PCControlAgent, TOPIC_STATUS

    with patch.object(PCControlAgent, "__init__", lambda self: None):
        agent = PCControlAgent()
        agent.stealth_mgr = MagicMock()
        agent.stealth_mgr.is_active.return_value = False
        agent.stealth_mgr.mute_audio = True
        agent._stealth_is_active = True
        agent._prev_mute_state = False
        agent._stealth_offline_since = None
        agent.boot_id = "test-boot-123"
        agent.get_session_state = MagicMock(return_value="unlocked")
        agent.set_volume = MagicMock()
        agent.wake_displays = MagicMock()
        agent.publish_telemetry = MagicMock()
        agent.mqtt = MagicMock()

        agent.check_stealth_process_exit()

        assert agent._stealth_is_active is False
        assert agent._prev_mute_state is None
        agent.set_volume.assert_called_with(mute=False)
        agent.wake_displays.assert_called_once()
        agent.publish_telemetry.assert_called_once()
        assert agent.mqtt.publish.called

        topic = agent.mqtt.publish.call_args[0][0]
        payload = json.loads(agent.mqtt.publish.call_args[0][1])
        assert topic == TOPIC_STATUS
        assert payload["event"] == "stealth_exit"
        assert payload["reason"] == "local_hotkey"
        assert payload["stealth_mode"] is False


def test_bot_on_stealth_exit_updates_card_and_sends_notification():
    uid = 12345
    ui = TelegramBotApi("token")
    ui.send_message = MagicMock()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.allowed_user_id = uid
    app.ui = ui
    app.monitoring = MagicMock()
    app.monitoring.latest_telemetry = {"stealth_mode": True}
    app.active_stealth_card = (uid, 999)

    app.on_stealth_exit({"reason": "local_hotkey"})

    # Check that latest_telemetry was updated
    assert app.monitoring.latest_telemetry["stealth_mode"] is False
    # Check that active_stealth_card was cleared
    assert app.active_stealth_card is None

    # Check calls to ui.send_message:
    # 1. Edit card 999 with stealth_menu_markup(active=False)
    # 2. Send push notification to uid
    assert ui.send_message.call_count == 2

    first_call = ui.send_message.call_args_list[0]
    assert first_call[0][0] == uid
    assert first_call[1]["message_id"] == 999
    markup = first_call[1]["reply_markup"]
    buttons = [btn for row in markup["inline_keyboard"] for btn in row]
    assert any(b.get("callback_data") == "stealth_on" for b in buttons)

    second_call = ui.send_message.call_args_list[1]
    assert second_call[0][0] == uid
    assert "отключен локально" in second_call[0][1]


def test_bot_on_stealth_exit_fallback_when_no_card():
    uid = 12345
    ui = TelegramBotApi("token")
    ui.send_message = MagicMock()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.allowed_user_id = uid
    app.ui = ui
    app.monitoring = MagicMock()
    app.monitoring.latest_telemetry = {"stealth_mode": True}
    app.active_stealth_card = None

    app.on_stealth_exit()

    assert ui.send_message.call_count == 1
    call = ui.send_message.call_args_list[0]
    assert call[0][0] == uid
    assert "отключен локально" in call[0][1]
    markup = call[1]["reply_markup"]
    buttons = [btn for row in markup["inline_keyboard"] for btn in row]
    assert any(b.get("callback_data") == "stealth_on" for b in buttons)


def test_mqtt_gateway_emits_stealth_exit():
    import json
    from vds_bot.mqtt_gateway import MqttGateway, TOPIC_DESKTOP_STATUS

    gw = MqttGateway.__new__(MqttGateway)
    gw.client = MagicMock()
    gw.health = MagicMock()
    emitted = []
    gw._emit = lambda evt, data: emitted.append((evt, data))

    msg = MagicMock()
    msg.topic = TOPIC_DESKTOP_STATUS
    msg.retain = False
    msg.payload = json.dumps({"event": "stealth_exit", "reason": "local_hotkey"}).encode("utf-8")

    gw._on_message(gw.client, None, msg)

    assert any(e[0] == "stealth_exit" for e in emitted)
    stealth_evt = next(e for e in emitted if e[0] == "stealth_exit")
    assert stealth_evt[1]["reason"] == "local_hotkey"


def test_power_router_tracks_active_stealth_card():
    from vds_bot.routers.power_router import route_power_callback

    uid = 12345
    ui = TelegramBotApi("token")
    ui.answer_callback = MagicMock()
    ui.send_message = MagicMock(return_value={"message_id": 777})
    mqtt = MagicMock()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.allowed_user_id = uid
    app.ui = ui
    app.mqtt = mqtt
    app.monitoring = MagicMock()
    app.monitoring.latest_telemetry = {"stealth_mode": False}
    app.active_stealth_card = None

    # Test stealth_on callback
    handled = route_power_callback(app, "stealth_on", uid, 50, "cb_id_1")
    assert handled is True
    assert app.active_stealth_card == (uid, 777)

    # Test stealth_off callback
    handled_off = route_power_callback(app, "stealth_off", uid, 777, "cb_id_2")
    assert handled_off is True
    assert app.active_stealth_card is None


def test_stealth_guard_hookproc_returns_pointer_sized_lresult():
    from core import stealth_guard

    assert stealth_guard.HOOKPROC._restype_ == ctypes.c_ssize_t


def test_stealth_guard_modifier_vk_codes_for_shift_and_ctrl():
    from core.stealth_guard import MODIFIER_VK_MAP

    assert MODIFIER_VK_MAP[0xA0] == "shift"
    assert MODIFIER_VK_MAP[0xA1] == "shift"
    assert MODIFIER_VK_MAP[0xA2] == "ctrl"
    assert MODIFIER_VK_MAP[0xA3] == "ctrl"


def test_check_network_connectivity_closes_socket_on_timeout():
    from core.stealth_guard import check_network_connectivity

    mock_sock = MagicMock()
    mock_sock.connect.side_effect = socket.timeout("Timed out")
    with patch("core.stealth_guard.socket.socket", return_value=mock_sock):
        assert check_network_connectivity(timeout=0.1) is False
        assert mock_sock.close.called


def test_stealth_manager_deactivate_kills_unresponsive_process():
    import threading

    mgr = StealthManager.__new__(StealthManager)
    mgr._lock = threading.Lock()
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="overlay", timeout=2.0),
        subprocess.TimeoutExpired(cmd="overlay", timeout=1.0),
        0,
    ]
    mgr._process = mock_proc

    assert mgr.deactivate() is True
    assert mock_proc.terminate.called
    assert mock_proc.kill.called
    assert mgr._process is None


def test_parse_hotkey_sequence_extracts_ordered_non_modifier_keys():
    from core.stealth_guard import parse_hotkey_sequence

    assert parse_hotkey_sequence("Win+W+1") == [0x57, 0x31]
    assert parse_hotkey_sequence("Ctrl+Alt+Shift+Escape") == [0x1B]
    assert parse_hotkey_sequence("Win+A+B+C") == [0x41, 0x42, 0x43]


def test_stealth_overlay_extraneous_key_resets_hotkey_sequence():
    overlay = StealthOverlay(hotkey="Win+W+1")
    overlay.stop = MagicMock()

    with patch.object(overlay, "_are_modifiers_matching", return_value=True):
        _send_mock_key(overlay, 0x57, is_up=False)
        _send_mock_key(overlay, 0x57, is_up=True)
        assert overlay._seq_idx == 1
        assert not overlay.stop.called

        _send_mock_key(overlay, 0x45, is_up=False)
        _send_mock_key(overlay, 0x45, is_up=True)
        assert overlay._seq_idx == 0
        assert not overlay.stop.called

        _send_mock_key(overlay, 0x31, is_up=False)
        assert not overlay.stop.called


def test_stealth_overlay_sequential_hotkey_triggers_local_exit():
    overlay = StealthOverlay(hotkey="Win+W+1")
    overlay.stop = MagicMock()

    with patch.object(overlay, "_are_modifiers_matching", return_value=True):
        _send_mock_key(overlay, 0x57, is_up=False)
        assert overlay._seq_idx == 1
        _send_mock_key(overlay, 0x31, is_up=False)
        overlay.stop.assert_called_once_with(reason="local_hotkey")


def test_stealth_overlay_five_escapes_trigger_emergency_exit_reason():
    overlay = StealthOverlay(hotkey="Win+W+1")
    overlay.stop = MagicMock()

    for _ in range(5):
        _send_mock_key(overlay, 0x1B, is_up=False)

    overlay.stop.assert_called_once_with(reason="emergency_escape")


def test_stealth_overlay_persists_exit_reason_to_file(tmp_path):
    test_reason_file = tmp_path / "stealth_exit_reason.json"
    with patch("core.stealth_guard.EXIT_REASON_FILE", test_reason_file):
        overlay = StealthOverlay(hotkey="Win+W+1")
        overlay.stop(reason="network_offline")
        assert overlay.exit_reason == "network_offline"
        assert StealthManager().get_last_exit_reason() == "network_offline"


def test_stealth_overlay_only_repositions_cursor_on_startup():
    overlay = StealthOverlay(hotkey="Win+W+1")
    with patch("core.stealth_guard.user32.SetCursorPos") as mock_set_cursor, \
         patch("core.stealth_guard.user32.ClipCursor"):
        overlay._lock_cursor_and_topmost(set_pos=False)
        assert not mock_set_cursor.called

        overlay._lock_cursor_and_topmost(set_pos=True)
        mock_set_cursor.assert_called_once_with(0, 0)


def test_desktop_stealth_publishes_exit_reason_from_manager():
    from desktop.stealth import StealthMixin

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


def test_stealth_manager_deactivate_unclips_cursor():
    mgr = StealthManager()
    mgr._process = None

    with patch("core.stealth_guard.user32") as mock_user32:
        mock_user32.ShowCursor.return_value = 0
        mgr.deactivate()
        mock_user32.ClipCursor.assert_called_with(None)


def test_minimize_all_windows_initializes_and_uninitializes_com():
    from desktop.stealth import StealthMixin

    with patch("pythoncom.CoInitialize") as mock_init, \
         patch("pythoncom.CoUninitialize") as mock_uninit, \
         patch("win32com.client.Dispatch") as mock_dispatch, \
         patch("time.sleep"):
        mock_shell = MagicMock()
        mock_dispatch.return_value = mock_shell

        StealthMixin.minimize_all_windows()
        assert mock_init.called
        assert mock_uninit.called
        assert mock_shell.MinimizeAll.call_count == 3


def test_wake_displays_reports_unknown_and_logs_warnings_when_both_methods_fail(caplog):
    import logging
    from desktop.stealth import StealthMixin

    mock_pag = MagicMock()
    mock_pag.moveRel.side_effect = RuntimeError("No GUI input")

    with caplog.at_level(logging.WARNING, logger="pc-control-agent"), \
         patch("ctypes.windll.user32.SendMessageTimeoutW", return_value=0), \
         patch.dict(sys.modules, {"pyautogui": mock_pag}):
        res = StealthMixin.wake_displays()

    assert res == {"displays": "unknown", "error": "wake_failed"}
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("SendMessageTimeoutW" in m for m in msgs)
    assert any("Mouse jitter" in m for m in msgs)
    assert any("Both display wake-up methods failed" in m for m in msgs)


def test_wake_displays_succeeds_via_mouse_fallback_when_win32_fails(caplog):
    import logging
    from desktop.stealth import StealthMixin

    mock_pag = MagicMock()

    with caplog.at_level(logging.WARNING, logger="pc-control-agent"), \
         patch("ctypes.windll.user32.SendMessageTimeoutW", side_effect=OSError("Win32 unavailable")), \
         patch.dict(sys.modules, {"pyautogui": mock_pag}):
        res = StealthMixin.wake_displays()

    assert res == {"displays": "on"}
    assert mock_pag.moveRel.call_count == 2
    assert any("Win32 unavailable" in rec.getMessage() for rec in caplog.records)


def test_stealth_manager_preserves_zero_offline_timeout_and_locks_get_status():
    cfg = {
        "stealth_mode": {
            "secret_hotkey": "Win+W+1",
            "watchdog_timeout_minutes": 60,
            "offline_timeout_minutes": 10,
        }
    }
    mgr = StealthManager(cfg)
    mock_lock = MagicMock()
    mock_lock.__enter__ = MagicMock(return_value=None)
    mock_lock.__exit__ = MagicMock(return_value=False)
    mgr._lock = mock_lock

    with patch("subprocess.Popen") as mock_popen:
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 4321
        mock_popen.return_value = proc

        assert mgr.activate(offline_timeout_minutes=0) is True
        cmd = mock_popen.call_args[0][0]
        assert cmd[cmd.index("--offline-timeout") + 1] == "0"

        status = mgr.get_status()
        assert status["active"] is True
        assert status["pid"] == 4321
        assert mock_lock.__enter__.call_count >= 2


def test_stealth_overlay_worker_thread_stop_does_not_call_tk_from_background(tmp_path):
    import threading

    overlay = StealthOverlay(hotkey="Win+W+1")
    overlay._root = MagicMock()
    overlay._lock_cursor_and_topmost = MagicMock()

    done = threading.Event()

    def _worker_stop():
        overlay.stop(reason="remote_command")
        done.set()

    with patch("core.stealth_guard.EXIT_REASON_FILE", tmp_path / "exit_reason.json"):
        th = threading.Thread(target=_worker_stop)
        th.start()
        th.join(timeout=2.0)

    assert done.is_set()
    overlay._root.after.assert_not_called()
    overlay._root.quit.assert_not_called()

    overlay._periodic_guard()
    overlay._root.quit.assert_called_once()


def test_desktop_stealth_skips_wake_displays_on_network_offline_exit():
    from desktop.stealth import StealthMixin

    agent = StealthMixin()
    agent.stealth_mgr = MagicMock()
    agent.stealth_mgr.is_active.return_value = False
    agent.stealth_mgr.get_last_exit_reason.return_value = "network_offline"
    agent.stealth_mgr.mute_audio = False
    agent._stealth_is_active = True
    agent._prev_mute_state = None
    agent.wake_displays = MagicMock()
    agent.publish_telemetry = MagicMock()
    agent.get_session_seconds = MagicMock(return_value=10)
    agent.mqtt = MagicMock()

    agent.check_stealth_process_exit()
    agent.wake_displays.assert_not_called()

