"""Small regression checks for the dashboard telemetry contract."""

import json
import time

from health_engine import HealthEngine
from monitoring import MonitoringManager
from mqtt_gateway import MqttGateway, TOPIC_TELEMETRY


class FakeMqtt:
    system_status = "online"
    system_session = "unlocked"
    desktop_status = "online"
    esp_status = "online"
    mqtt_online = True

    def __init__(self):
        self.health = HealthEngine()
        self.health.update("mqtt_broker", "online")
        self.health.update("windows_system", "online", ts=time.time())
        self.health.update("windows_desktop", "online", ts=time.time())
        self.health.update("windows_session", "unlocked", ts=time.time())
        self.health.update("telemetry", "fresh", ts=time.time())
        self.health.update("esp", "online", ts=time.time())


class FakeHistory:
    def insert(self, payload):
        self.payload = payload


def test_dashboard_uses_real_nested_agent_schema():
    manager = MonitoringManager.__new__(MonitoringManager)
    manager.mqtt = FakeMqtt()
    manager.history = FakeHistory()
    manager.alerts = {}
    manager.latest_telemetry = None
    manager.latest_received_at = 0.0
    manager.process_telemetry(
        {
            "timestamp": 1_700_000_000,
            "uptime_seconds": 2820,
            "session_seconds": 2760,
            "user_active": True,
            "active_application": "Google Chrome",
            "cpu": {"load_percent": 9, "temp_c": 47},
            "gpu": {"load_percent": 14, "temp_c": 37, "power_w": 9.7},
            "memory": {"used_percent": 51, "used_gb": 8.7, "total_gb": 17.1},
            "disks": [
                {"drive": "C:", "used_percent": 87.5, "free_gb": 63.9},
                {"drive": "E:", "used_percent": 55.4, "free_gb": 114.1},
            ],
        }
    )
    text = manager.dashboard_text()
    for expected in ("Google Chrome", "CPU: 9% · 47°C", "GPU: 14% · 37°C · 9.7 Вт", "[C:] 87.5%", "[E:] 55.4%"):
        assert expected in text, text


def test_gateway_accepts_legacy_telemetry_timestamp():
    gateway = MqttGateway.__new__(MqttGateway)
    gateway.health = HealthEngine()
    events = []
    gateway._emit_health_state = lambda: None
    gateway._emit = lambda name, data: events.append((name, data))

    message = type(
        "Message",
        (),
        {
            "topic": TOPIC_TELEMETRY,
            "payload": json.dumps({"timestamp": int(time.time())}).encode(),
            "retain": False,
        },
    )()
    gateway._on_message(None, None, message)

    assert gateway.health.get_status("telemetry") == "fresh"
    assert events and events[-1][0] == "telemetry"


def test_dashboard_renders_media_playback_when_active():
    manager = MonitoringManager.__new__(MonitoringManager)
    fake_mqtt = FakeMqtt()
    fake_mqtt.last_media_state = {
        "active": True,
        "title": "ZAN PREPORAN",
        "artist": "home4circus",
        "is_playing": True,
        "is_liked": True,
    }
    manager.mqtt = fake_mqtt
    manager.history = FakeHistory()
    manager.alerts = {}
    manager.latest_telemetry = None
    manager.latest_received_at = 0.0
    manager.latest_desktop_at = 0.0
    manager.process_telemetry({"active_application": "Visual Studio Code"})
    text = manager.dashboard_text()
    import monitoring
    assert f"├ Открыт: <tg-emoji emoji-id=\"{monitoring.EMOJI_DEFAULT_APP}\">🪟</tg-emoji> Visual Studio Code" in text


def test_dashboard_omits_media_when_inactive():
    manager = MonitoringManager.__new__(MonitoringManager)
    fake_mqtt = FakeMqtt()
    fake_mqtt.last_media_state = {"active": False}
    manager.mqtt = fake_mqtt
    manager.history = FakeHistory()
    manager.alerts = {}
    manager.latest_telemetry = None
    manager.latest_received_at = 0.0
    manager.latest_desktop_at = 0.0
    manager.process_telemetry({"active_application": "Visual Studio Code"})
    text = manager.dashboard_text()
    assert "Музыка:" not in text
    import monitoring
    assert f"└ Открыт: <tg-emoji emoji-id=\"{monitoring.EMOJI_DEFAULT_APP}\">🪟</tg-emoji> Visual Studio Code" in text


def test_main_menu_markup_excludes_player_and_activity():
    from telegram_ui import TelegramBotApi
    ui = TelegramBotApi("dummy_token")
    markup = ui.main_menu_markup()
    callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
    assert "menu_player" not in callbacks
    assert "menu_activity" not in callbacks


def test_main_menu_markup_structure_and_custom_emojis():
    import telegram_ui
    ui = telegram_ui.TelegramBotApi("dummy_token")
    markup = ui.main_menu_markup()
    rows = markup["inline_keyboard"]
    assert len(rows) == 4

    # Row 1: Питание
    assert rows[0][0]["text"] == "Питание"
    assert rows[0][0]["callback_data"] == "power_menu"
    assert rows[0][0]["icon_custom_emoji_id"] == telegram_ui.EMOJI_BTN_POWER

    # Row 2: Скриншот, Программы
    assert rows[1][0]["text"] == "Скриншот"
    assert rows[1][0]["callback_data"] == "screenshot"
    assert rows[1][0]["icon_custom_emoji_id"] == telegram_ui.EMOJI_BTN_SCREENSHOT
    assert rows[1][1]["text"] == "Программы"
    assert rows[1][1]["callback_data"] == "apps_menu"
    assert rows[1][1]["icon_custom_emoji_id"] == telegram_ui.EMOJI_BTN_APPS

    # Row 3: Звук, Настройки
    assert rows[2][0]["text"] == "Звук"
    assert rows[2][0]["callback_data"] == "sound_menu"
    assert rows[2][0]["icon_custom_emoji_id"] == telegram_ui.EMOJI_BTN_SOUND
    assert rows[2][1]["text"] == "Настройки"
    assert rows[2][1]["callback_data"] == "settings_menu"
    assert rows[2][1]["icon_custom_emoji_id"] == telegram_ui.EMOJI_BTN_SETTINGS

    # Row 4: Adaptive power/lock button
    assert rows[3][0]["callback_data"] in ("shutdown_pc", "unlock_windows_manual", "power_on")


def test_settings_menu_markup():
    from telegram_ui import TelegramBotApi
    ui = TelegramBotApi("dummy_token")
    markup = ui.settings_menu_markup()
    callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
    assert "monitoring_menu" in callbacks
    assert "history_menu" in callbacks
    assert "physical_button" in callbacks
    assert "menu" in callbacks


def test_is_user_in_telegram_on_pc():
    from unittest.mock import MagicMock
    from bot import PcControlBotApp
    import time

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.mqtt = MagicMock()
    app.monitoring = MagicMock()

    # Case 1: Active in Telegram.exe, unlocked
    app.mqtt.system_session = "unlocked"
    app.monitoring.telemetry_age.return_value = 5.0
    app.monitoring.latest_telemetry = {
        "active_application": "Telegram.exe",
        "idle_seconds": 10,
    }
    assert app._is_user_in_telegram_on_pc() is True

    # Case 2: In Telegram.exe, unlocked
    app.monitoring.latest_telemetry["active_application"] = "telegram.exe"
    assert app._is_user_in_telegram_on_pc() is True

    # Case 3: In Chrome (not in Telegram)
    app.monitoring.latest_telemetry["active_application"] = "chrome.exe"
    assert app._is_user_in_telegram_on_pc() is False

    # Case 4: In AyuGram, but PC is locked
    app.monitoring.latest_telemetry["active_application"] = "Telegram.exe"
    app.mqtt.system_session = "locked"
    assert app._is_user_in_telegram_on_pc() is False

    # Case 5: In AyuGram, unlocked, but idle for 10 minutes (away)
    app.mqtt.system_session = "unlocked"
    app.monitoring.latest_telemetry["idle_seconds"] = 600
    assert app._is_user_in_telegram_on_pc() is False

    # Case 6: Telemetry stale (>30s)
    app.monitoring.latest_telemetry["idle_seconds"] = 10
    app.monitoring.telemetry_age.return_value = 45.0
    assert app._is_user_in_telegram_on_pc() is False

    # Case 7: system_session unknown, but desktop_status online and fresh
    app.mqtt.system_session = "unknown"
    app.mqtt.desktop_status = "online"
    app.monitoring.telemetry_age.return_value = 5.0
    assert app._is_user_in_telegram_on_pc() is True


def test_dashboard_auto_refresh_every_10s():
    from unittest.mock import MagicMock
    from bot import PcControlBotApp
    import time

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.mqtt = MagicMock()
    app.monitoring = MagicMock()
    app.ui = MagicMock()
    app._main_menu_markup = MagicMock(return_value={"inline_keyboard": []})
    app.latest_media_state = {}

    app.active_main_menu_message_id = 999
    app.current_menu_view = "main"
    app.last_dashboard_auto_refresh = 100.0

    # Mock user active in Telegram
    app._is_user_in_telegram_on_pc = MagicMock(return_value=True)
    app.monitoring.dashboard_text.return_value = "Dashboard Updated"

    # Tick at 105s (<10s elapsed) -> should not refresh
    app._check_dashboard_auto_refresh(105.0)
    assert app.ui.send_message.call_count == 0

    # Tick at 110.1s (>=10s elapsed) -> should refresh
    app._check_dashboard_auto_refresh(110.1)
    assert app.ui.send_message.call_count == 1
    assert app.last_dashboard_auto_refresh == 110.1

    # If user leaves Telegram (_is_user_in_telegram_on_pc is False) -> should not refresh
    app._is_user_in_telegram_on_pc.return_value = False
    app._check_dashboard_auto_refresh(125.0)
    assert app.ui.send_message.call_count == 1  # Still 1, no new call!

    # If user navigates to settings_menu submenu -> should not refresh
    app._is_user_in_telegram_on_pc.return_value = True
    app.current_menu_view = "settings_menu"
    app._check_dashboard_auto_refresh(135.0)
    assert app.ui.send_message.call_count == 1  # Still 1, no new call!


def test_system_telemetry_does_not_wipe_desktop_session_fields():
    manager = MonitoringManager.__new__(MonitoringManager)
    manager.mqtt = FakeMqtt()
    manager.history = FakeHistory()
    manager.alerts = {}
    manager.latest_telemetry = None
    manager.latest_received_at = 0.0
    manager.latest_desktop_at = 0.0

    # 1. Desktop telemetry arrives
    desktop_data = {
        "agent_mode": "desktop",
        "session_seconds": 3600,
        "active_application": "Happ.exe",
        "active_application_emoji_id": "5251339414830880848",
        "user_active": True,
        "cpu": {"load_percent": 10, "temp_c": 45},
    }
    manager.process_telemetry(desktop_data)
    assert manager.latest_telemetry["session_seconds"] == 3600
    assert manager.latest_telemetry["active_application"] == "Happ.exe"
    assert manager.latest_telemetry["user_active"] is True

    # 2. System telemetry arrives 2 seconds later (Session 0 service, no GUI info)
    system_data = {
        "agent_mode": "system",
        "cpu": {"load_percent": 15, "temp_c": 48},
        "uptime_seconds": 7200,
    }
    manager.process_telemetry(system_data)

    # Must preserve desktop session & GUI fields!
    assert manager.latest_telemetry["session_seconds"] == 3600
    assert manager.latest_telemetry["active_application"] == "Happ.exe"
    assert manager.latest_telemetry["active_application_emoji_id"] == "5251339414830880848"
    assert manager.latest_telemetry["user_active"] is True
    # Hardware updated
    assert manager.latest_telemetry["cpu"]["load_percent"] == 15
    assert manager.latest_telemetry["uptime_seconds"] == 7200


def test_dashboard_formats_telegram_chat_title():
    manager = MonitoringManager.__new__(MonitoringManager)
    manager.mqtt = FakeMqtt()
    manager.history = FakeHistory()
    manager.alerts = {}
    manager.latest_telemetry = None
    manager.latest_received_at = 0.0
    manager.latest_desktop_at = 0.0

    manager.process_telemetry({
        "agent_mode": "desktop",
        "active_application": "Telegram.exe",
        "active_window_title": "(1) PC Control @ W1bbex! (2029400)",
        "active_application_emoji_id": "5251740431632344455",
    })
    text = manager.dashboard_text()
    assert "Telegram — PC Control" in text


def test_dashboard_auto_refresh_every_5s_when_in_pc_control():
    from unittest.mock import MagicMock
    from bot import PcControlBotApp
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.mqtt = MagicMock()
    app.monitoring = MagicMock()
    app.ui = MagicMock()
    app._main_menu_markup = MagicMock(return_value={"inline_keyboard": []})
    app.latest_media_state = {}

    app.active_main_menu_message_id = 999
    app.current_menu_view = "main"
    app.last_dashboard_auto_refresh = 100.0

    app._is_user_in_telegram_on_pc = MagicMock(return_value=True)
    app.monitoring.latest_telemetry = {"in_pc_control_chat": True}
    app.monitoring.dashboard_text.return_value = "Dashboard Fast"

    # Tick at 104s (<5s elapsed) -> should not refresh
    app._check_dashboard_auto_refresh(104.0)
    assert app.ui.send_message.call_count == 0

    # Tick at 105.1s (>=5s elapsed) -> should refresh!
    app._check_dashboard_auto_refresh(105.1)
    assert app.ui.send_message.call_count == 1
    assert app.last_dashboard_auto_refresh == 105.1


def test_session_lines_states():
    manager = MonitoringManager.__new__(MonitoringManager)
    fake_mqtt = FakeMqtt()
    manager.mqtt = fake_mqtt
    manager.history = FakeHistory()
    manager.alerts = {}
    manager.latest_received_at = time.time()

    # Case 1: system_session unlocked, user active
    fake_mqtt.system_session = "unlocked"
    icon, dur = manager._session_lines({"user_active": True, "session_seconds": 3600})
    assert "🟢" in icon
    assert dur == "1 ч"

    # Case 2: system_session unknown, but desktop_status is online and fresh
    fake_mqtt.system_session = "unknown"
    fake_mqtt.desktop_status = "online"
    icon, dur = manager._session_lines({"user_active": True, "session_seconds": 120})
    assert "🟢" in icon
    assert dur == "2 мин"

    # Case 3: system_session unknown, desktop_status online, user idle
    icon, dur = manager._session_lines({"user_active": False, "session_seconds": 60})
    assert "🟡" in icon
    assert dur == "1 мин"

    # Case 4: system_session locked
    fake_mqtt.system_session = "locked"
    icon, dur = manager._session_lines({"user_active": True, "session_seconds": 120})
    assert "🔴" in icon
    assert "5345825096160291312" in icon
    assert dur == "н/д"

    # Case 5: system_session unknown and desktop offline
    fake_mqtt.system_session = "unknown"
    fake_mqtt.desktop_status = "offline"
    manager.latest_received_at = 0.0
    icon, dur = manager._session_lines({})
    assert "🔴" in icon
    assert "5345825096160291312" in icon
    assert dur == "н/д"


def test_sound_menu_markup_has_valid_custom_emojis():
    from telegram_ui import TelegramBotApi
    ui = TelegramBotApi("dummy_token")
    markup = ui.sound_menu_markup()
    buttons = [btn for row in markup["inline_keyboard"] for btn in row]
    for btn in buttons:
        if "icon_custom_emoji_id" in btn:
            val = btn["icon_custom_emoji_id"]
            assert val is not None, f"Button {btn.get('text')} has null icon_custom_emoji_id"
            assert str(val).isdigit(), f"Button {btn.get('text')} has non-numeric icon_custom_emoji_id: {val}"


def test_sanitize_reply_markup_strips_null_or_invalid_emojis():
    from telegram_ui import TelegramBotApi
    bad_markup = {
        "inline_keyboard": [
            [{"text": "Bad Null", "icon_custom_emoji_id": None, "callback_data": "test:1"}],
            [{"text": "Bad String Null", "icon_custom_emoji_id": "null", "callback_data": "test:2"}],
            [{"text": "Bad Empty", "icon_custom_emoji_id": "", "callback_data": "test:3"}],
            [{"text": "Good Numeric", "icon_custom_emoji_id": "5330140476890718558", "callback_data": "test:4"}],
            [{"text": "Good Int", "icon_custom_emoji_id": 5330140476890718558, "callback_data": "test:5"}],
        ]
    }
    cleaned = TelegramBotApi._sanitize_reply_markup(bad_markup)
    rows = cleaned["inline_keyboard"]
    assert "icon_custom_emoji_id" not in rows[0][0]
    assert "icon_custom_emoji_id" not in rows[1][0]
    assert "icon_custom_emoji_id" not in rows[2][0]
    assert rows[3][0]["icon_custom_emoji_id"] == "5330140476890718558"
    assert rows[4][0]["icon_custom_emoji_id"] == "5330140476890718558"


def test_dashboard_renders_adaptive_os_emoji_for_windows_and_linux():
    manager = MonitoringManager.__new__(MonitoringManager)
    manager.mqtt = FakeMqtt()
    manager.history = FakeHistory()
    manager.alerts = {}
    manager.latest_telemetry = None
    manager.latest_received_at = 0.0

    # 1. Windows telemetry
    manager.process_telemetry({
        "timestamp": int(time.time()),
        "os": "windows",
        "windows": "Windows 10 Pro",
        "user_active": True,
        "cpu": {"load_percent": 12},
    })
    assert manager.get_os_type() == "windows"
    win_tag = manager.header_os_tag()
    assert "5332280534540264957" in win_tag
    assert "🪟" in win_tag
    win_dashboard = manager.dashboard_text()
    assert f'{win_tag} <b>PC Control</b>' in win_dashboard

    # 2. Linux telemetry
    manager.process_telemetry({
        "timestamp": int(time.time()),
        "os": "linux",
        "user_active": True,
        "cpu": {"load_percent": 5},
    })
    assert manager.get_os_type() == "linux"
    linux_tag = manager.header_os_tag()
    assert "5332695978136874502" in linux_tag
    assert "🐧" in linux_tag
    linux_dashboard = manager.dashboard_text()
    assert f'{linux_tag} <b>PC Control</b>' in linux_dashboard

    # 3. Unreachable state displays correct OS tag and label
    manager.mqtt.health.update("windows_system", "offline")
    manager.mqtt.health.update("windows_desktop", "offline")
    manager.mqtt.health.update("telemetry", "stale")
    unreachable_text = manager.dashboard_text()
    assert f'{linux_tag} <b>PC Control</b> — ' in unreachable_text
    assert "Linux: " in unreachable_text


def test_dashboard_renders_yellow_pulse_when_esp_offline():
    import monitoring
    manager = MonitoringManager.__new__(MonitoringManager)
    fake_mqtt = FakeMqtt()
    fake_mqtt.health.update("mqtt_broker", "online")
    fake_mqtt.health.update("windows_system", "online", ts=time.time())
    fake_mqtt.health.update("windows_desktop", "online", ts=time.time())
    fake_mqtt.health.update("windows_session", "unlocked", ts=time.time())
    fake_mqtt.health.update("telemetry", "fresh", ts=time.time())
    fake_mqtt.health.update("esp", "offline", ts=time.time())
    fake_mqtt.esp_status = "offline"

    manager.mqtt = fake_mqtt
    manager.history = FakeHistory()
    manager.alerts = {}
    manager.latest_telemetry = None
    manager.latest_received_at = 0.0
    manager.latest_desktop_at = 0.0

    manager.process_telemetry({
        "timestamp": int(time.time()),
        "user_active": True,
        "session_seconds": 120,
        "cpu": {"load_percent": 15, "temp_c": 45},
        "memory": {"used_percent": 30, "used_gb": 4.8, "total_gb": 16.0},
        "disks": [{"drive": "C:", "used_percent": 50, "free_gb": 200}],
        "active_application": "Chrome",
    })

    dashboard = manager.dashboard_text()
    assert monitoring.EMOJI_STATUS_WARNING in dashboard
    assert "В сети (ESP недоступна)" in dashboard
    assert "Модуль ESP8266 недоступен" in dashboard


if __name__ == "__main__":
    test_dashboard_uses_real_nested_agent_schema()
    test_dashboard_renders_adaptive_os_emoji_for_windows_and_linux()
    test_dashboard_renders_media_playback_when_active()
    test_dashboard_omits_media_when_inactive()
    test_dashboard_formats_telegram_chat_title()
    test_system_telemetry_does_not_wipe_desktop_session_fields()
    test_main_menu_markup_excludes_player_and_activity()
    test_main_menu_markup_structure_and_custom_emojis()
    test_settings_menu_markup()
    test_sound_menu_markup_has_valid_custom_emojis()
    test_sanitize_reply_markup_strips_null_or_invalid_emojis()
    test_is_user_in_telegram_on_pc()
    test_dashboard_auto_refresh_every_10s()
    test_dashboard_auto_refresh_every_5s_when_in_pc_control()
    test_dashboard_renders_yellow_pulse_when_esp_offline()
    print("All UI and monitoring tests passed")

