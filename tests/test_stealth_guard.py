import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.stealth_guard import parse_hotkey, StealthManager, StealthOverlay
from vds_bot.telegram_ui import TelegramBotApi
from vds_bot.bot import ALLOWED_USER_ID, PcControlBotApp


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


def test_bot_stealth_callbacks_and_commands():
    ui = TelegramBotApi("token")
    ui.send_message = MagicMock()
    ui.answer_callback = MagicMock()
    mqtt = MagicMock()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.monitoring = MagicMock()
    app.monitoring.latest_telemetry = {"stealth_mode": False}

    # Test callback stealth_toggle
    cb_query = {
        "id": "cb1",
        "from": {"id": ALLOWED_USER_ID},
        "message": {"chat": {"id": ALLOWED_USER_ID}, "message_id": 10},
        "data": "stealth_toggle",
    }
    app.handle_callback(cb_query)
    app.mqtt.publish_desktop_action.assert_called_with("stealth_mode", {"state": "toggle"})

    # Test command /stealth on
    msg_on = {
        "chat": {"id": ALLOWED_USER_ID, "type": "private"},
        "from": {"id": ALLOWED_USER_ID},
        "text": "/stealth on",
        "message_id": 11,
    }
    app.handle_message(msg_on)
    app.mqtt.publish_desktop_action.assert_called_with("stealth_mode", {"state": "on"})

    # Test command /stealth off
    msg_off = {
        "chat": {"id": ALLOWED_USER_ID, "type": "private"},
        "from": {"id": ALLOWED_USER_ID},
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
    ui = TelegramBotApi("token")
    ui.send_message = MagicMock()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.monitoring = MagicMock()
    app.monitoring.latest_telemetry = {"stealth_mode": True}
    app.active_stealth_card = (ALLOWED_USER_ID, 999)

    app.on_stealth_exit({"reason": "local_hotkey"})

    # Check that latest_telemetry was updated
    assert app.monitoring.latest_telemetry["stealth_mode"] is False
    # Check that active_stealth_card was cleared
    assert app.active_stealth_card is None

    # Check calls to ui.send_message:
    # 1. Edit card 999 with stealth_menu_markup(active=False)
    # 2. Send push notification to ALLOWED_USER_ID
    assert ui.send_message.call_count == 2

    first_call = ui.send_message.call_args_list[0]
    assert first_call[0][0] == ALLOWED_USER_ID
    assert first_call[1]["message_id"] == 999
    markup = first_call[1]["reply_markup"]
    buttons = [btn for row in markup["inline_keyboard"] for btn in row]
    assert any(b.get("callback_data") == "stealth_on" for b in buttons)

    second_call = ui.send_message.call_args_list[1]
    assert second_call[0][0] == ALLOWED_USER_ID
    assert "отключен локально" in second_call[0][1]


def test_bot_on_stealth_exit_fallback_when_no_card():
    ui = TelegramBotApi("token")
    ui.send_message = MagicMock()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.monitoring = MagicMock()
    app.monitoring.latest_telemetry = {"stealth_mode": True}
    app.active_stealth_card = None

    app.on_stealth_exit()

    assert ui.send_message.call_count == 1
    call = ui.send_message.call_args_list[0]
    assert call[0][0] == ALLOWED_USER_ID
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

    ui = TelegramBotApi("token")
    ui.answer_callback = MagicMock()
    ui.send_message = MagicMock(return_value={"message_id": 777})
    mqtt = MagicMock()

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.monitoring = MagicMock()
    app.monitoring.latest_telemetry = {"stealth_mode": False}
    app.active_stealth_card = None

    # Test stealth_on callback
    handled = route_power_callback(app, "stealth_on", ALLOWED_USER_ID, 50, "cb_id_1")
    assert handled is True
    assert app.active_stealth_card == (ALLOWED_USER_ID, 777)

    # Test stealth_off callback
    handled_off = route_power_callback(app, "stealth_off", ALLOWED_USER_ID, 777, "cb_id_2")
    assert handled_off is True
    assert app.active_stealth_card is None


