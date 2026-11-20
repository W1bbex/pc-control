"""Tests for PcControlBotApp lockfile, polling resilience, Telegram UI sanitization, routers, history, backup, and deploy manifest."""

import json
import os
import re
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil

import scripts.auto_backup as auto_backup
import scripts.deploy_vds as deploy_vds
import vds_bot.bot as bot_mod
from vds_bot.bot import PcControlBotApp
from vds_bot.health_engine import PCState
from vds_bot.history import TelemetryHistory
from vds_bot.routers.command_router import route_message
from vds_bot.routers.monitoring_router import route_monitoring_callback
from vds_bot.routers.power_router import route_power_callback
from vds_bot.routers.workspace_router import route_workspace_callback
from vds_bot.telegram_ui import EMOJI_PREFIX_RE, TelegramBotApi
from vds_bot.ui_markups import UiMarkupsMixin
from vds_bot.ui_templates import UiTemplatesMixin

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_bot_single_instance_lock_creates_and_releases_file(tmp_path, monkeypatch):
    lock_file = tmp_path / "bot.lock"
    monkeypatch.setattr(bot_mod, "LOCK_PATH", lock_file)

    app = PcControlBotApp.__new__(PcControlBotApp)
    app._acquire_lock()
    assert lock_file.exists()
    assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())

    app._release_lock()
    app2 = PcControlBotApp.__new__(PcControlBotApp)
    app2._acquire_lock()
    assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())
    app2._release_lock()


def test_bot_acquire_lock_overwrites_pid_owned_by_unrelated_process(tmp_path, monkeypatch):
    lock_file = tmp_path / "bot.lock"
    lock_file.write_text("99999", encoding="utf-8")
    monkeypatch.setattr(bot_mod, "LOCK_PATH", lock_file)

    bot = PcControlBotApp.__new__(PcControlBotApp)
    mock_proc = MagicMock()
    mock_proc.name.return_value = "redis-server"
    mock_proc.cmdline.return_value = ["/usr/bin/redis-server"]

    with patch("psutil.pid_exists", return_value=True), \
         patch("psutil.Process", return_value=mock_proc), \
         patch("sys.exit") as mock_exit:
        bot._acquire_lock()
        assert not mock_exit.called
        assert lock_file.read_text(encoding="utf-8") == str(os.getpid())


def test_bot_acquire_lock_recovers_when_stale_pid_disappears(tmp_path, monkeypatch):
    lock_file = tmp_path / "bot.lock"
    lock_file.write_text("999999", encoding="utf-8")
    monkeypatch.setattr(bot_mod, "LOCK_PATH", lock_file)

    app = PcControlBotApp.__new__(PcControlBotApp)
    with patch("psutil.pid_exists", return_value=True), \
         patch("psutil.Process", side_effect=psutil.NoSuchProcess(999999)):
        app._acquire_lock()
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_bot_register_commands_registers_all_slash_commands():
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()

    app.register_commands()
    assert app.ui.call.called
    call_args = app.ui.call.call_args
    assert call_args[0][0] == "setMyCommands"
    registered_cmds = [c["command"] for c in call_args[0][1]["commands"]]
    for expected in ("menu", "player", "stealth", "activity", "antigravity", "esc", "console", "cmd", "ps", "shutdown", "start"):
        assert expected in registered_cmds


def test_bot_uses_dedicated_polling_session_without_unused_base64():
    import requests
    import vds_bot.bot as bot_mod

    assert not hasattr(bot_mod, "base64")
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.ui.session = requests.Session()
    app.polling_session = requests.Session()
    app.offset = 0
    app.save_state = MagicMock()
    app.handle_update = MagicMock()
    try:
        mock_res = MagicMock(status_code=200)
        mock_res.json.return_value = {"ok": True, "result": []}
        with patch.object(app.polling_session, "post", return_value=mock_res) as post_mock:
            app.poll_updates()
        assert app.polling_session is not app.ui.session
        post_mock.assert_called_once()
        app.ui.session.post.assert_not_called() if hasattr(app.ui.session.post, "assert_not_called") else None
    finally:
        app.ui.session.close()
        app.polling_session.close()


def test_poll_updates_continues_remaining_updates_after_exception():
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.polling_session = MagicMock()
    app.offset = 100
    app.save_state = MagicMock()

    fake_updates = [
        {"update_id": 100, "message": {"text": "bad"}},
        {"update_id": 101, "message": {"text": "good"}},
    ]
    app.polling_session.post.return_value.status_code = 200
    app.polling_session.post.return_value.json.return_value = {"ok": True, "result": fake_updates}

    processed = []

    def fake_handle_message(msg):
        if msg.get("text") == "bad":
            raise ValueError("Boom!")
        processed.append(msg.get("text"))

    app.handle_message = fake_handle_message
    app.poll_updates()

    assert processed == ["good"]
    assert app.offset == 102
    assert app.save_state.called


def test_bot_ignores_group_commands_from_unauthorized_users():
    bot = PcControlBotApp.__new__(PcControlBotApp)
    bot.ui = MagicMock()
    bot.mqtt = MagicMock()
    bot.latest_media_state = None
    bot._send_player_card = MagicMock()

    bot.handle_message({
        "chat": {"id": -100999999, "type": "supergroup"},
        "from": {"id": 9999999},
        "text": "/player",
    })
    bot._send_player_card.assert_not_called()


def test_bot_action_callback_does_not_overwrite_current_menu_view(monkeypatch):
    monkeypatch.setattr(bot_mod, "ALLOWED_USER_ID", 12345)
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

    assert app.current_menu_view == "main"


def test_bot_handle_callback_tolerates_none_message(monkeypatch):
    monkeypatch.setattr(bot_mod, "ALLOWED_USER_ID", 12345)
    bot = PcControlBotApp.__new__(PcControlBotApp)
    bot.allowed_user_id = 12345
    bot.ui = MagicMock()
    bot.current_menu_view = "menu"
    bot.monitoring = MagicMock()
    bot.save_state = MagicMock()
    bot._main_menu_markup = MagicMock(return_value={})

    bot.handle_callback({
        "id": "cb_999",
        "data": "menu",
        "message": None,
        "from": {"id": 12345},
    })
    bot.ui.answer_callback.assert_called_with("cb_999")


def test_scheduler_tick_discards_stale_scheduled_shutdown():
    bot = PcControlBotApp.__new__(PcControlBotApp)
    bot.ui = MagicMock()
    bot.mqtt = MagicMock()
    bot.monitoring = MagicMock()
    bot.monitoring.telemetry_age.return_value = 100.0
    bot.pending_screenshots = {}
    bot.pending_shutdown_cmd_id = None
    bot.scheduled_shutdown_ts = time.time() - 700.0
    bot.last_schedule_attempt_at = 0.0
    bot.last_dashboard_refresh_at = time.time()
    bot.dashboard_refresh_interval = 60.0
    bot.save_state = MagicMock()
    bot._request_shutdown = MagicMock()

    bot.scheduler_tick()
    assert bot.scheduled_shutdown_ts is None
    bot.save_state.assert_called_once()
    bot._request_shutdown.assert_not_called()


def test_telegram_ui_configures_http_adapter_and_thread_local_sessions():
    api = TelegramBotApi("123456:FAKE_TOKEN")
    main_session = api.session
    assert "https://" in main_session.adapters
    adapter = main_session.adapters["https://"]
    assert getattr(adapter, "_pool_connections", 0) >= 10
    assert api.session is main_session

    worker_sessions = []

    def _collect():
        worker_sessions.append(api.session)

    t1 = threading.Thread(target=_collect)
    t2 = threading.Thread(target=_collect)
    t1.start()
    t2.start()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert len(worker_sessions) == 2
    assert worker_sessions[0] is not worker_sessions[1]
    assert worker_sessions[0] is not main_session
    assert worker_sessions[1] is not main_session

    override = MagicMock()
    api.session = override
    assert api.session is override
    api.session = None
    assert api.session is main_session
    api.close_sessions()


def test_bot_stop_closes_mqtt_and_telegram_sessions():
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.running = True
    app.mqtt = MagicMock()
    app.ui = MagicMock()
    app._release_lock = MagicMock()

    app.stop()
    assert app.running is False
    app.mqtt.stop.assert_called_once()
    app.ui.close_sessions.assert_called_once()
    app._release_lock.assert_called_once()


def test_telegram_ui_sanitize_error_redacts_bot_token():
    ui = TelegramBotApi("123456:SECRET_TEST_TOKEN")
    err_msg = "Failed at https://api.telegram.org/bot123456:SECRET_TEST_TOKEN/sendMessage?chat_id=123"
    sanitized = ui.sanitize_error(err_msg)
    assert "SECRET_TEST_TOKEN" not in sanitized
    assert "[REDACTED_BOT_TOKEN]" in sanitized


def test_telegram_api_call_raw_and_call_redact_token_on_exception():
    fake_token = "9876543210:SECRET_TOKEN_ABCXYZ"
    api = TelegramBotApi(fake_token)

    with patch.object(api.session, "post", side_effect=Exception(f"ConnectionError to https://api.telegram.org/bot{fake_token}/getMe")):
        result = api.call_raw("getMe")
        assert result["ok"] is False
        assert fake_token not in result["description"]
        assert "[REDACTED_BOT_TOKEN]" in result["description"]

    with patch.object(api.session, "post", side_effect=Exception(f"Timeout connecting to /bot{fake_token}/sendMessage")):
        assert api.call("sendMessage", {"chat_id": 123, "text": "test"}) is None


def test_player_cards_tolerate_null_position_and_duration():
    ui = TelegramBotApi("123456:FAKE_TOKEN")
    null_state = {
        "active": True,
        "title": "Song",
        "artist": "Artist",
        "position": None,
        "duration": None,
        "is_playing": False,
    }
    assert "Song" in ui.player_card_text(null_state)
    assert "Song" in ui.player_card_caption(null_state)


def test_player_card_caption_matches_player_card_text():
    ui = UiTemplatesMixin()
    state = {"active": True, "title": "Song", "artist": "Artist", "position": 10, "duration": 100}
    assert ui.player_card_caption(state) == ui.player_card_text(state)


def test_edit_message_media_strips_invalid_custom_emoji_fields():
    ui = TelegramBotApi.__new__(TelegramBotApi)
    ui.api_url = "https://api.telegram.org/bot123456:fake"
    ui.session = MagicMock()
    mock_post = MagicMock()
    mock_post.json.return_value = {"ok": True, "result": {}}
    ui.session.post.return_value = mock_post

    dirty_markup = {
        "inline_keyboard": [[
            {"text": "Test", "callback_data": "t", "icon_custom_emoji_id": None}
        ]]
    }
    ui.edit_message_media(12345, 67890, "https://example.com/pic.jpg", reply_markup=dirty_markup)
    called_data = ui.session.post.call_args[1]["data"]
    parsed_markup = json.loads(called_data["reply_markup"])
    btn = parsed_markup["inline_keyboard"][0][0]
    assert "icon_custom_emoji_id" not in btn


def test_telegram_ui_sanitizer_strips_leading_unicode_emojis():
    markup = {
        "inline_keyboard": [
            [
                {"text": "🔮 Профили AG", "style": "primary", "icon_custom_emoji_id": "5348024810905581079", "callback_data": "antigravity_menu"},
                {"text": "⚡ Нажать ESC", "style": "primary", "icon_custom_emoji_id": "5346172035028531557", "callback_data": "press_esc"},
            ],
            [
                {"text": "📊 Активность за день", "style": "primary", "icon_custom_emoji_id": "5330473500064912704", "callback_data": "menu_activity"},
                {"text": "🕶️ Скрытность (Blackout)", "style": "primary", "icon_custom_emoji_id": "5346102804450681939", "callback_data": "stealth_on"},
            ],
        ]
    }
    sanitized = TelegramBotApi._sanitize_reply_markup(markup)
    rows = sanitized["inline_keyboard"]
    assert rows[0][0]["text"] == "Профили AG"
    assert rows[0][0]["icon_custom_emoji_id"] == "5348024810905581079"
    assert rows[0][1]["text"] == "Нажать ESC"
    assert rows[0][1]["icon_custom_emoji_id"] == "5346172035028531557"
    assert rows[1][0]["text"] == "Активность за день"
    assert rows[1][1]["text"] == "Скрытность (Blackout)"


def test_emoji_prefix_regex_strips_unicode_symbols_and_selectors():
    samples = [
        ("🔮 Crystal", "Crystal"),
        ("⚡ Lightning", "Lightning"),
        ("📊 Chart", "Chart"),
        ("✖ Cross", "Cross"),
        ("☀️ Sun", "Sun"),
        ("🕶️ Shades", "Shades"),
        ("🔄 Restart", "Restart"),
        ("⚙️ Gear", "Gear"),
        ("⌨️ ⚡ Double Emoji", "Double Emoji"),
        ("Already Clean", "Already Clean"),
    ]
    for raw, expected in samples:
        assert EMOJI_PREFIX_RE.sub("", raw).strip() == expected


def test_apps_menu_markup_removes_profiles_ag_and_exposes_heal():
    mixin = UiMarkupsMixin()
    markup = mixin.apps_menu_markup()
    keyboard = markup.get("inline_keyboard", [])

    all_button_texts = [btn["text"] for row in keyboard for btn in row if isinstance(btn, dict) and "text" in btn]
    all_callbacks = [btn.get("callback_data") for row in keyboard for btn in row if isinstance(btn, dict)]

    assert not any("профили ag" in t.lower() for t in all_button_texts)
    assert not any("🔮" in t for t in all_button_texts)
    assert "Нажать ESC" in all_button_texts
    assert not any("⚡ нажать esc" in t.lower() for t in all_button_texts)
    assert any("лечение" in t.lower() for t in all_button_texts)
    assert "ag_heal" in all_callbacks


def test_standard_ui_markups_have_no_leading_unicode_emojis():
    emoji_check = re.compile(r"^[\U0001f000-\U0001faff\u2600-\u27bf\u2300-\u23ff\u2b50]")
    mixin = UiMarkupsMixin()
    menus = [
        mixin.apps_menu_markup(),
        mixin.monitoring_menu_markup(),
        mixin.power_menu_markup(session="locked", system_status="online", stealth_active=False),
        mixin.power_menu_markup(session="unlocked", system_status="online", stealth_active=True),
        mixin.stealth_menu_markup(active=True),
        mixin.stealth_menu_markup(active=False),
        mixin.emergency_power_off_confirm_markup(),
        mixin.antigravity_card_markup({"accounts": [{"id": "1", "email": "test@gmail.com", "is_active": True}]}),
    ]

    for m in menus:
        for row in m.get("inline_keyboard", []):
            for btn in row:
                if isinstance(btn, dict) and "text" in btn:
                    assert not emoji_check.search(btn["text"]), f"Raw emoji found in button text: {btn['text']!r}"


def test_antigravity_card_back_button_routes_to_main_menu():
    ui = UiMarkupsMixin()
    markup = ui.antigravity_card_markup({"accounts": []})
    all_callbacks = [btn.get("callback_data") for row in markup.get("inline_keyboard", []) for btn in row]
    assert "menu" in all_callbacks
    assert "main_menu" not in all_callbacks


def test_antigravity_card_wraps_tg_emoji_and_handles_nulls():
    ui = UiTemplatesMixin()
    null_data = {
        "accounts": [
            {"email": None, "name": None, "is_active": True},
            {"email": None, "name": None, "is_active": False},
        ],
        "active": {"email": None, "name": None, "reset_time": None},
        "is_running": False,
    }
    card = ui.antigravity_card_text(null_data)
    assert "Antigravity • Управление аккаунтами" in card
    assert "Без имени" in card or "—" in card
    if "<tg-emoji" in card:
        assert "></tg-emoji>" not in card


def test_main_menu_markup_shows_shutdown_when_online_unlocked():
    class Ui(UiMarkupsMixin):
        pass

    ui = Ui()
    markup = ui.main_menu_markup(pc_state="READY", session="unlocked", system_status="online")
    flat_buttons = [btn for row in markup["inline_keyboard"] for btn in row]
    assert any(b.get("callback_data") == "shutdown_pc" for b in flat_buttons)


def test_dynamic_main_and_power_menu_buttons_by_pc_state():
    ui = TelegramBotApi("123456:TEST_TOKEN")

    unlocked_bottom = ui.main_menu_markup(session="unlocked")["inline_keyboard"][-1][0]
    assert unlocked_bottom["callback_data"] == "shutdown_pc"
    assert "Выключить" in unlocked_bottom["text"]

    locked_bottom = ui.main_menu_markup(session="locked")["inline_keyboard"][-1][0]
    assert locked_bottom["callback_data"] == "unlock_windows_manual"
    assert "Разблокировать" in locked_bottom["text"]

    login_bottom = ui.main_menu_markup(session="login_screen")["inline_keyboard"][-1][0]
    assert login_bottom["callback_data"] == "unlock_windows_manual"
    assert "Разблокировать" in login_bottom["text"]

    offline_bottom = ui.main_menu_markup(system_status="offline")["inline_keyboard"][-1][0]
    assert offline_bottom["callback_data"] == "power_on"
    assert "Включить" in offline_bottom["text"]

    main_callbacks = [btn["callback_data"] for row in ui.main_menu_markup(session="unlocked")["inline_keyboard"] for btn in row]
    assert "workspace_start_direct" not in main_callbacks

    apps_first = ui.apps_menu_markup()["inline_keyboard"][0][0]
    assert apps_first["callback_data"] == "workspace_start_direct"
    assert "рабочий сетап" in apps_first["text"].lower()

    pm_offline = ui.power_menu_markup(system_status="offline")
    assert [btn["callback_data"] for row in pm_offline["inline_keyboard"] for btn in row] == ["power_on", "menu"]
    assert pm_offline["inline_keyboard"][0][0]["text"] == "Включить ПК"
    assert pm_offline["inline_keyboard"][0][0]["style"] == "success"

    pm_unlocked = ui.power_menu_markup(session="unlocked", system_status="online")
    pm_unlocked_cbs = [btn["callback_data"] for row in pm_unlocked["inline_keyboard"] for btn in row]
    assert "shutdown_pc" in pm_unlocked_cbs
    assert "lock_windows" in pm_unlocked_cbs
    assert "power_on" not in pm_unlocked_cbs
    assert "unlock_windows_manual" not in pm_unlocked_cbs
    assert pm_unlocked["inline_keyboard"][0][0]["callback_data"] == "shutdown_pc"

    pm_locked = ui.power_menu_markup(session="locked", system_status="online")
    pm_locked_cbs = [btn["callback_data"] for row in pm_locked["inline_keyboard"] for btn in row]
    assert "shutdown_pc" in pm_locked_cbs
    assert "unlock_windows_manual" in pm_locked_cbs
    assert "power_on" not in pm_locked_cbs
    assert "lock_windows" not in pm_locked_cbs

    mock_state = MagicMock()
    mock_state.state = PCState.SHUTDOWN_OBSERVED
    mock_state.session_status = "offline"
    mock_state.system_status = "offline"
    obs_bottom = ui.main_menu_markup(pc_state=mock_state)["inline_keyboard"][-1][0]
    assert obs_bottom["callback_data"] == "power_on"
    assert "Включить" in obs_bottom["text"]


def test_reboot_menu_markup_returns_valid_2d_keyboard():
    ui = TelegramBotApi("123:test_token")
    keyboard = ui.reboot_menu_markup().get("inline_keyboard", [])
    assert isinstance(keyboard, list)
    for row in keyboard:
        assert isinstance(row, list)
        for btn in row:
            assert isinstance(btn, dict)
            assert "text" in btn and "callback_data" in btn


def test_workspace_router_offline_fallback_returns_2d_keyboard():
    app = MagicMock()
    app.mqtt.health.evaluate_pc_state.return_value = MagicMock(state="OFFLINE", label="Выключен", reason="ПК выключен")
    app.ui.workspace_vpn_choice_markup = TelegramBotApi("123:test_token").workspace_vpn_choice_markup

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
    assert [button["callback_data"] for button in keyboard[0]] == ["power_start:incy", "power_start:happ"]
    for row in keyboard:
        assert isinstance(row, list)
        for btn in row:
            assert isinstance(btn, dict) and "text" in btn


def test_workspace_router_escapes_html_in_state_message():
    app = MagicMock()
    app.monitoring.telemetry_age = MagicMock(return_value=5.0)
    pc_state = MagicMock()
    pc_state.session_status = "locked"
    pc_state.state = "OFFLINE"
    pc_state.label = "Bad <Tag> & Label"
    pc_state.reason = "Crash <Error>"
    app.mqtt.health.evaluate_pc_state = MagicMock(return_value=pc_state)
    app.ui.back_button = MagicMock(return_value=[])

    assert route_workspace_callback(app, "game_start_direct", chat_id=123, message_id=456, callback_id="cb_1") is True
    sent_text = app.ui.send_message.call_args[0][1]
    assert "&lt;Tag&gt;" in sent_text
    assert "&amp;" in sent_text
    assert "<Tag>" not in sent_text
    assert "<Error>" not in sent_text


def test_power_router_rejects_power_on_when_esp_offline():
    app = MagicMock()
    app.mqtt.health.evaluate_pc_state.return_value = MagicMock(state="OFFLINE")
    app.mqtt.system_status = "offline"
    app.mqtt.desktop_status = "offline"
    app.mqtt.esp_status = "offline"

    assert route_power_callback(app=app, data="power_on", chat_id=12345, message_id=99, callback_id="cb_power") is True
    app.ui.answer_callback.assert_called_with("cb_power", "❌ Модуль питания (ESP8266) не в сети!", alert=True)
    app.power_flow.start_power_on.assert_not_called()


def test_monitoring_router_sends_history_chart_with_png_filename():
    app = MagicMock()
    app.monitoring = MagicMock()
    app.monitoring.history.render_chart.return_value = (b"\x89PNG\r\n\x1a\nfake", 10)

    with patch("vds_bot.routers.monitoring_router.threading.Thread") as mock_thread:
        route_monitoring_callback(app, "history:24h", 12345, 999, "cb_id")
        thread_target = mock_thread.call_args[1]["target"]
        thread_target()

        assert app.ui.send_photo.called
        assert app.ui.send_photo.call_args[1].get("filename") == "chart.png"


def test_command_router_esc_requires_slash_command_token():
    app = MagicMock()
    app.allowed_user_id = 12345

    route_message(app, {"chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}, "text": "escape from tarkov"})
    assert not app.mqtt.publish_desktop_action.called

    route_message(app, {"chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}, "text": "/esc 3"})
    assert app.mqtt.publish_desktop_action.called
    assert app.mqtt.publish_desktop_action.call_args[0][1] == {"count": 3}


def test_command_router_scheduled_shutdown_preempts_console_input():
    app = MagicMock()
    app.allowed_user_id = 12345
    app.awaiting_shutdown_time_chat_id = 12345
    app.console_active_chat_id = 12345
    app.console_target = "host"
    app.console_shell = "powershell"

    route_message(app, {"chat": {"id": 12345}, "from": {"id": 12345}, "text": "23:45", "message_id": 99})
    assert app._show_shutdown_schedule_confirmation.called
    assert not app.execute_console_command.called


def test_command_router_plain_shutdown_opens_shutdown_menu():
    app = MagicMock()
    app.ui.shutdown_menu_markup.return_value = {"inline_keyboard": []}
    app.scheduled_shutdown_ts = None
    app.awaiting_shutdown_time_chat_id = None
    app.active_stealth_card = None
    app.allowed_user_id = 12345
    app.console_active_chat_id = None

    route_message(app, {"chat": {"id": 12345}, "text": "/shutdown", "message_id": 42})
    app.ui.send_message.assert_called_once()
    assert "Выключение ПК" in app.ui.send_message.call_args[0][1]


def test_command_router_ignores_messages_missing_chat_dict():
    app = MagicMock()
    route_message(app, {})
    assert not app.ui.send_message.called
    route_message(app, {"chat": None})
    assert not app.ui.send_message.called
    route_message(app, {"chat": {}})
    assert not app.ui.send_message.called


def test_command_router_handles_antigravity_command():
    app = MagicMock()
    app.allowed_user_id = 12345
    app.ui = MagicMock()
    app.latest_media_state = None
    app.latest_antigravity_data = None
    app.mqtt = MagicMock()
    app.mqtt.desktop_status = "offline"
    route_message(app, {"chat": {"id": 12345, "type": "private"}, "from": {"id": 12345}, "text": "/antigravity"})
    app.ui.send_message.assert_called_once()
    args, kwargs = app.ui.send_message.call_args
    assert args[0] == 12345
    assert "не в сети" in args[1].lower()
    assert "reply_markup" in kwargs


def test_command_router_cancels_shutdown_time_input():
    app = MagicMock()
    app.allowed_user_id = 123
    app.awaiting_shutdown_time_chat_id = 123
    app._main_menu_markup = MagicMock(return_value={})

    route_message(app, {"chat": {"id": 123}, "text": "отмена", "from": {"id": 123}})
    assert app.awaiting_shutdown_time_chat_id is None
    app.ui.send_message.assert_called_with(123, "Ввод времени отменён.", reply_markup={})


def test_history_records_ts_field_correctly(tmp_path):
    hist = TelemetryHistory(tmp_path / "telem.db")
    test_ts = int(time.time()) - 30
    hist.insert({"ts": test_ts, "host": "test-pc", "cpu": {"load_percent": 15.0}})

    conn = hist._connect()
    try:
        rows = conn.execute("SELECT ts, host, cpu_load FROM telemetry").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == test_ts
        assert rows[0][1] == "test-pc"
        assert rows[0][2] == 15.0
    finally:
        conn.close()


def test_auto_backup_imports_os_and_collects_code_files():
    assert hasattr(auto_backup, "os") and auto_backup.os is not None
    files = auto_backup.collect_local_code_files(auto_backup.BASE_DIR / "core")
    assert isinstance(files, list)


def test_auto_backup_excludes_env_files():
    assert ".env" not in auto_backup.CODE_EXTENSIONS
    assert ".env" in auto_backup.EXCLUDE_FILES
    assert "*.env" in auto_backup.EXCLUDE_FILES
    assert "pc-control-bot.env" not in auto_backup.VDS_MANIFEST_FILES

    assert auto_backup.is_excluded_file(Path(".env"))
    assert auto_backup.is_excluded_file(Path("sub/.env"))
    assert auto_backup.is_excluded_file(Path("config.env"))
    assert not auto_backup.is_excluded_file(Path(".gitignore"))
    assert not auto_backup.is_excluded_file(Path("pc-control-bot.env.example"))


def test_auto_backup_and_gitignore_exclude_secrets_and_firmware_builds():
    import subprocess

    assert auto_backup.is_excluded_file(Path("hardware/esp8266/pc_remote_mqtt/secrets.h"))
    assert auto_backup.is_excluded_file(Path("data/activity.db"))
    proc = subprocess.run(
        [
            "git",
            "check-ignore",
            "hardware/esp8266/pc_remote_mqtt/secrets.h",
            "hardware/esp8266/pc_remote_mqtt/build/sketch.bin",
            "flow_state.json",
            "bot.lock",
            "data/admin-sensors.json",
        ],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    ignored = proc.stdout.replace("\\", "/")
    assert "hardware/esp8266/pc_remote_mqtt/secrets.h" in ignored
    assert "hardware/esp8266/pc_remote_mqtt/build/sketch.bin" in ignored
    assert "flow_state.json" in ignored
    assert "bot.lock" in ignored


def test_deploy_vds_manifest_includes_all_files_with_sha256():
    manifest = deploy_vds.compute_manifest()
    assert "timestamp" in manifest
    assert "files" in manifest
    for fn in deploy_vds.MANIFEST_FILES:
        assert fn in manifest["files"]
        meta = manifest["files"][fn]
        assert meta["size_bytes"] > 0
        assert len(meta["sha256"]) == 64


def test_poll_updates_sanitizes_telegram_token_in_error_log(caplog, monkeypatch):
    import logging

    secret_token = "99887766:AAFF_SECRET_TOKEN_XYZ"
    monkeypatch.setattr(bot_mod, "TELEGRAM_TOKEN", secret_token)
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.offset = 0
    app.ui = TelegramBotApi(secret_token)
    app.polling_session = MagicMock()
    app.polling_session.post.side_effect = RuntimeError(
        f"HTTPSConnectionPool(url='https://api.telegram.org/bot{secret_token}/getUpdates')"
    )

    with caplog.at_level(logging.ERROR), patch("time.sleep"):
        app.poll_updates()

    assert secret_token not in caplog.text
    assert "[REDACTED_BOT_TOKEN]" in caplog.text


def test_bot_stop_closes_polling_session_and_cancels_player_timer():
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.running = True
    app.mqtt = MagicMock()
    app._executor = MagicMock()
    app.ui = MagicMock()
    app.polling_session = MagicMock()
    mock_timer = MagicMock()
    app._player_timer = mock_timer
    app._player_timer_running = True

    with patch.object(app, "_release_lock"):
        app.stop()

    mock_timer.cancel.assert_called_once()
    app.polling_session.close.assert_called_once()
    app.ui.close_sessions.assert_called_once()
    assert app._player_timer is None
    assert app._player_timer_running is False


def test_player_manager_imports_with_empty_allowed_user_id_env(monkeypatch):
    import importlib
    import vds_bot.player_manager as pm

    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "")
    reloaded = importlib.reload(pm)
    assert reloaded.ALLOWED_USER_ID == 0
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)
    importlib.reload(pm)
