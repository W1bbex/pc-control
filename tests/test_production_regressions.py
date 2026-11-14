"""Regression tests covering VDS bot UI/routers/flows, deployment scripts, and Windows core helpers."""

import itertools
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "vds_bot"))

from vds_bot.telegram_ui import TelegramBotApi
from vds_bot.power_flow import PowerFlowStage, PowerFlowManager
from vds_bot.physical_button import PhysicalButtonManager
from vds_bot.console_service import ConsoleServiceMixin
from vds_bot.routers.monitoring_router import route_monitoring_callback
from vds_bot.routers.antigravity_router import route_antigravity_callback, _resolve_account_id
from vds_bot.routers.command_router import route_message
from vds_bot.routers.console_router import route_console_callback
from vds_bot.routers.player_router import route_player_callback
from vds_bot.routers.workspace_router import route_workspace_callback
from vds_bot.routers.physical_router import route_physical_callback


def test_telegram_ui_omits_reply_markup_when_none_on_edit():
    api = TelegramBotApi("123:SECRET_TOKEN")
    with patch.object(api, "call_raw", return_value={"ok": True, "result": {}}) as call_mock:
        api.send_message(100, "hello", reply_markup=None, message_id=55)
    method, payload = call_mock.call_args[0]
    assert method == "editMessageText"
    assert "reply_markup" not in payload
    api.close_sessions()


def test_telegram_ui_prunes_dead_thread_sessions_and_masks_tokens():
    api = TelegramBotApi("98765:TOKEN_TO_MASK")

    def worker():
        _ = api.session

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    # Main thread session access triggers dead-thread session pruning
    _ = api.session
    with api._sessions_lock:
        assert len(api._thread_sessions) == 1
        assert len(api._sessions) == 1
        assert api._thread_sessions[0][0]() is threading.current_thread()

    err_exc = requests.exceptions.ConnectionError("https://api.telegram.org/bot98765:TOKEN_TO_MASK/sendPhoto failed")
    with patch.object(api.session, "post", side_effect=err_exc):
        res = api.send_photo(100, b"img", caption="pic")
    assert res is None

    ok_resp = MagicMock(status_code=400)
    ok_resp.json.return_value = {"ok": False, "description": "Bad Request: message is not modified"}
    with patch.object(api.session, "post", return_value=ok_resp):
        assert api.edit_message_media(100, 10, b"img", caption="pic") == {"message_id": 10, "not_modified": True}

    api.close_sessions()


def test_ui_templates_escape_html_clamp_limits_and_hash_long_callbacks():
    api = TelegramBotApi("123:TOKEN")
    cap = api.player_card_caption({
        "active": True,
        "title": "X" * 900,
        "artist": "Y" * 400,
        "position": -5,
        "duration": -10,
    })
    assert len(cap) <= 1024
    assert "-0" not in cap

    ag_text = api.antigravity_card_text({
        "accounts": [
            {
                "id": "a" * 80,
                "email": "user@example.com",
                "is_active": True,
            }
        ],
        "active": {
            "name": "Acc",
            "email": "user@example.com",
            "reset_time": "<soon>",
            "models_quota": {
                "custom-id": {"name": "<Model&Test>", "percentage": None},
            },
        },
    })
    assert "<Model&Test>" not in ag_text
    assert "&lt;Model&amp;Test&gt;" in ag_text
    assert "&lt;soon&gt;" in ag_text

    long_id = "account-" + ("z" * 80)
    markup = api.antigravity_card_markup({
        "accounts": [{"id": long_id, "email": "user@example.com", "is_active": False}]
    })
    switch_cbs = [
        btn["callback_data"]
        for row in markup["inline_keyboard"]
        for btn in row
        if btn.get("callback_data", "").startswith("ag_switch:") and btn["callback_data"] != "ag_switch:auto"
    ]
    assert len(switch_cbs) == 1
    assert len(switch_cbs[0].encode("utf-8")) <= 64
    assert switch_cbs[0].startswith("ag_switch:#")

    app = MagicMock()
    app.latest_antigravity_data = {"accounts": [{"id": long_id, "email": "user@example.com"}]}
    assert _resolve_account_id(app, switch_cbs[0].split(":", 1)[1]) == long_id
    api.close_sessions()


def test_monitoring_and_antigravity_offline_markups_and_led_brightness():
    app = MagicMock()
    app.allowed_user_id = 123
    app.ui = TelegramBotApi("123:TOKEN")
    app.mqtt.desktop_status = "offline"
    app.mqtt.esp_status = "online"
    app.latest_antigravity_data = None

    with patch.object(app.ui, "send_message") as send_mock, patch.object(app.ui, "answer_callback"):
        route_monitoring_callback(app, "menu_activity", 123, 55, "cb1")
        assert send_mock.call_args.kwargs["reply_markup"]["inline_keyboard"] == [app.ui.back_button("menu")]

        route_antigravity_callback(app, "antigravity_menu", 123, 56, "cb2")
        assert send_mock.call_args.kwargs["reply_markup"]["inline_keyboard"] == [app.ui.back_button("menu")]

    # LED invalid brightness & clamping percentage display
    with patch.object(app.ui, "answer_callback") as ans_mock:
        route_monitoring_callback(app, "led_action:bright:notanint", 123, 57, "cb3")
        ans_mock.assert_called_with("cb3", "Некорректная яркость", alert=True)

        app.mqtt.publish_esp_action.return_value = "cmd-led"
        route_monitoring_callback(app, "led_action:bright:300", 123, 57, "cb4")
        app.mqtt.publish_esp_action.assert_called_with("LED_BRIGHTNESS", value="255")
        assert "100%" in ans_mock.call_args[0][1]

    # Screenshot timeout is aligned to 35s
    app.mqtt.desktop_status = "online"
    app.mqtt.publish_desktop_action.return_value = "cmd-shot"
    app.pending_screenshots = {}
    with patch.object(app.ui, "send_message"), patch.object(app.ui, "answer_callback"):
        route_monitoring_callback(app, "screenshot", 123, 58, "cb5")
    app.mqtt.publish_desktop_action.assert_called_with("screenshot", timeout=35)
    app.ui.close_sessions()


def test_command_and_console_router_clear_shutdown_prompt_and_track_menu_id():
    app = MagicMock()
    app.allowed_user_id = 123
    app.awaiting_shutdown_time_chat_id = 123
    app.latest_media_state = None
    app.console_active_chat_id = None
    app.active_main_menu_message_id = None
    app._main_menu_markup.return_value = {}

    route_message(app, {"chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": "/console"})
    assert app.awaiting_shutdown_time_chat_id is None

    route_console_callback(app, "console_close", 123, 888, "cb1")
    assert app.active_main_menu_message_id == 888
    app.save_state.assert_called()

    from vds_bot.bot import resolve_menu_view
    assert resolve_menu_view("console_close") == "main"


def test_player_router_aborts_optimistic_update_when_mqtt_fails():
    app = MagicMock()
    app.allowed_user_id = 123
    app.player_edit_lock = threading.Lock()
    app.latest_media_state = {"active": True, "is_playing": True, "is_liked": False, "title": "Song", "artist": "Art"}
    app.mqtt.send_media_command.return_value = None

    with patch("vds_bot.routers.player_router.time.sleep"):
        route_player_callback(app, "player_toggle_play", 123, 77, "cb1")
    assert app.latest_media_state["is_playing"] is True
    assert "не доставлена" in app.ui.send_message.call_args[0][1]

    app.latest_media_state = {"active": False}
    route_player_callback(app, "player_toggle_like", 123, 77, "cb2")
    assert app.mqtt.send_media_command.call_count == 2
    assert "MQTT недоступен" in app.ui.answer_callback.call_args.kwargs["text"]


def test_workspace_router_enforces_online_unlocked_and_escapes_html():
    app = MagicMock()
    app.allowed_user_id = 123
    app.mqtt.desktop_status = "offline"
    app.mqtt.system_session = "locked"
    app.power_flow.stage = PowerFlowStage.IDLE

    route_workspace_callback(app, "direct_game_vpn:custom:happ", 123, 44, "cb1")
    assert "не в сети" in app.ui.answer_callback.call_args[0][1]

    app.mqtt.desktop_status = "online"
    route_workspace_callback(app, "workspace_vpn:incy", 123, 44, "cb2")
    assert "заблокирована" in app.ui.answer_callback.call_args[0][1].lower()

    route_workspace_callback(app, "direct_game:<Bad&Game>", 123, 44, "cb3")
    sent_text = app.ui.send_message.call_args[0][1]
    assert "<Bad&Game>" not in sent_text
    assert "&lt;Bad&amp;Game&gt;" in sent_text


def test_power_flow_ttl_and_reboot_session_transitions(tmp_path):
    ui = MagicMock()
    mqtt = MagicMock()
    with patch("vds_bot.power_flow.FLOW_STATE_PATH", tmp_path / "flow.json"):
        pf = PowerFlowManager(telegram_ui=ui, mqtt_gateway=mqtt)
        pf.handle_unlock_decision("flow_unlock_yes", 123, message_id=99)
        assert pf.stage == PowerFlowStage.UNLOCKING
        assert pf.expires_at > pf.created_at > 0
        assert pf.flow_id

        pf.stage = PowerFlowStage.POWERING_ON
        pf.chat_id = 123
        pf.selecting_mode = False
        mqtt.system_status = "online"
        pf.on_system_session_update("unlocked", 123)
        assert pf.stage == PowerFlowStage.WAITING_WORKSPACE_DECISION

        # REBOOTING must wait for system agent offline, not only desktop offline
        pf.stage = PowerFlowStage.REBOOTING
        pf.reboot_offline_detected = False
        pf.reboot_started_at = time.time()
        mqtt.system_status = "online"
        mqtt.desktop_status = "offline"
        pf.on_system_session_update("locked", 123)
        assert pf.reboot_offline_detected is False
        mqtt.system_status = "offline"
        pf.on_system_session_update("unknown", 123)
        assert pf.reboot_offline_detected is True


def test_bot_state_and_pid_lock_resilience(tmp_path):
    from vds_bot.bot import PcControlBotApp
    import vds_bot.bot as bot_mod

    lock_path = tmp_path / "bot.lock"
    state_path = tmp_path / "state.json"
    app = PcControlBotApp.__new__(PcControlBotApp)
    app._state_lock = threading.Lock()
    app.offset = 0
    app.last_alert_state = "normal"
    app.chosen_vpn = "happ"
    app.active_player_card = None
    app.active_main_menu_message_id = None
    app.awaiting_shutdown_time_chat_id = None
    app.last_stealth_prompt = None
    app.antigravity_accounts_cache = None

    with patch.object(bot_mod, "LOCK_PATH", lock_path), patch.object(bot_mod, "STATE_PATH", state_path):
        app._acquire_lock()
        assert lock_path.read_text(encoding="utf-8") == str(os.getpid())
        # Same PID re-acquire must reclaim cleanly rather than exiting
        app._acquire_lock()
        assert lock_path.read_text(encoding="utf-8") == str(os.getpid())
        # Stale/dead PID is replaced safely
        lock_path.write_text("99999999", encoding="utf-8")
        app._acquire_lock()
        assert lock_path.read_text(encoding="utf-8") == str(os.getpid())
        app._release_lock()

        state_path.write_text(
            '{"offset": "bad-int", "chosen_vpn": "incy", "active_main_menu_message_id": 777}',
            encoding="utf-8",
        )
        app.load_state()
        assert app.chosen_vpn == "incy"
        assert app.active_main_menu_message_id == 777


def test_physical_button_none_attempts_and_delete_confirm():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.esp_status = "online"
    mqtt.publish_esp_action.return_value = "cmd-esp-1"
    pb = PhysicalButtonManager(telegram_ui=ui, mqtt_gateway=mqtt)

    pb.handle_event({"event": "failed", "failed_attempts": None}, 123)
    assert "попытка 1 из 3" in ui.send_message.call_args[0][1]

    app = MagicMock()
    app.physical_btn = pb
    app.mqtt = mqtt
    app.ui = ui
    route_physical_callback(app, "physical_password_delete", 123, 55, "cb1")
    assert "physical_password_delete_confirm" in str(ui.send_message.call_args.kwargs["reply_markup"])
    mqtt.publish_esp_action.assert_not_called()

    route_physical_callback(app, "physical_password_delete_confirm", 123, 55, "cb2")
    mqtt.publish_esp_action.assert_called_once_with("DELETE_PHYSICAL_PASSWORD")


def test_console_service_respects_html_escaped_4096_limit():
    class Host(ConsoleServiceMixin):
        def __init__(self):
            self.ui = MagicMock()

    svc = Host()
    raw_output = "<&>" * 1500  # Escapes to > 6000 chars
    pending = {"chat_id": 123, "command": "echo hi", "target": "desktop", "shell": "powershell", "started_at": time.time()}
    svc._handle_console_ack(pending, {"result": "executed", "data": {"exit_code": 0, "stdout": raw_output}})
    sent_text = svc.ui.send_message.call_args[0][1]
    assert len(sent_text) <= 4096
    svc.ui.send_document.assert_called_once()


def test_player_manager_async_dashboard_dispatch_and_power_scheduler_timeout():
    from vds_bot.bot import PcControlBotApp

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.allowed_user_id = 123
    app.state_lock = threading.RLock()
    app.async_dispatch = True
    app._executor = ThreadPoolExecutor(max_workers=2)
    app.player_edit_lock = threading.Lock()
    app.ui = MagicMock()
    app.mqtt = MagicMock()
    app.mqtt.system_status = "online"
    app.mqtt.desktop_status = "online"
    app.mqtt.system_session = "unlocked"
    app.user_led_state = "on"
    app.user_led_color = None
    app.last_auto_led_state = "on"
    app.monitoring = MagicMock()
    app.monitoring.dashboard_text.return_value = "new-text"
    app.monitoring.telemetry_age.return_value = None
    app.active_main_menu_message_id = 42
    app.current_menu_view = "main"
    app._main_menu_markup = MagicMock(return_value={})
    app.power_flow = MagicMock(stage=PowerFlowStage.IDLE)
    app.pending_screenshots = {}
    app.pending_telemetry_cmd_ids = {}
    app.pending_antigravity_cmd_ids = {}
    app.pending_cmd_timestamps = {}
    app.pending_shutdown_cmd_id = None
    app.pending_shutdown_started_at = None
    app.scheduled_shutdown_ts = None
    app.pending_activity_chat_id = 123
    app.pending_activity_msg_id = 99
    app.pending_activity_requested_at = time.time() - 20

    try:
        app._update_dashboard_on_media({})
        app.scheduler_tick()
        time.sleep(0.2)
    finally:
        app._executor.shutdown(wait=True)

    assert app.pending_activity_chat_id is None
    assert app.pending_activity_requested_at is None
    assert app.ui.send_message.call_count >= 2


def test_deploy_vds_retry_stderr_and_manifest_check():
    from scripts import deploy_vds

    fail1 = MagicMock(returncode=255, stdout=b"", stderr=b"primary err")
    fail2 = MagicMock(returncode=255, stdout=b"", stderr=b"retry err")
    with patch.object(deploy_vds.subprocess, "run", side_effect=[fail1, fail2]), \
         patch.object(deploy_vds, "get_bind_ip", return_value="192.168.1.10"):
        with pytest.raises(RuntimeError) as exc:
            deploy_vds.run_ssh("echo hi")
    assert "retry err" in str(exc.value)
    assert "primary err" in str(exc.value)

    with patch.object(deploy_vds, "run_ssh") as ssh_mock:
        ssh_mock.return_value = MagicMock(stdout="ERRORS: Manifest file list mismatch", stderr="")
        with pytest.raises(RuntimeError):
            deploy_vds.verify_remote_hashes("/opt/pc-control-bot/releases/r1", {"files": {"a.py": {"sha256": "abc"}}})


def test_auto_backup_rejects_tar_exit_2_and_cleans_temp_zip(tmp_path):
    from scripts import auto_backup

    logger = MagicMock()
    bad_proc = MagicMock(returncode=2, stdout=b"corrupt", stderr=b"fatal tar err")
    with patch.object(auto_backup.subprocess, "run", return_value=bad_proc), \
         patch.object(auto_backup, "run_ssh_command", return_value=(1, "", "err")):
        res = auto_backup.collect_vds_code_files(logger)
    assert res == {}

    archive_path = tmp_path / "out.zip"
    temp_path = archive_path.with_suffix(".tmp.zip")
    with patch.object(auto_backup.zipfile, "ZipFile", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            auto_backup.create_backup_archive(archive_path, [], {}, {}, tmp_path, logger)
    assert not temp_path.exists()


def test_windows_helpers_newest_snapshot_codex_v3_boot_retry_and_activity_db(tmp_path):
    from desktop.sensors import SensorHelper
    from desktop import codex_launch
    from system import session_manager, action_dispatcher
    from core.activity_db import ActivityDB

    # Newest admin-sensors.json wins by st_mtime
    helper = SensorHelper(tmp_path / "bin" / "AdminSensorCollector.exe")
    data_snap = tmp_path / "data" / "admin-sensors.json"
    hw_snap = tmp_path / "bin" / "admin-sensors.json"
    data_snap.parent.mkdir(parents=True)
    hw_snap.parent.mkdir(parents=True)
    data_snap.write_text('[{"name": "old"}]', encoding="utf-8")
    hw_snap.write_text('[{"name": "fresh"}]', encoding="utf-8")
    os.utime(data_snap, (1000, 1000))
    os.utime(hw_snap, (time.time(), time.time()))
    with patch("desktop.sensors.BASE_DIR", tmp_path):
        assert helper._find_admin_snapshot() == hw_snap
        assert helper.snapshot() == [{"name": "fresh"}]

    # Codex variant 3 launches without local alias file
    with patch.object(codex_launch, "_codex_processes", side_effect=itertools.chain([[]], itertools.repeat([555]))), \
         patch.object(codex_launch.subprocess, "Popen", return_value=MagicMock()) as popen_mock, \
         patch.object(codex_launch.time, "sleep"):
        res = codex_launch.diagnose_codex_launch("3", "%LOCALAPPDATA%\\Missing\\Alias.exe", lambda _: True, timeout=3.0)
    assert res["state"] == "window_visible"
    assert popen_mock.called

    # get_boot_info does not permanently latch unknown across retries
    orig_info = session_manager._cached_boot_info
    orig_time = session_manager._cached_boot_info_time
    try:
        session_manager._cached_boot_info = None
        session_manager._cached_boot_info_time = 0.0
        with patch.object(session_manager.subprocess, "run", side_effect=[
            OSError("timeout"),
            MagicMock(
                returncode=0,
                stdout='<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><EventData><Data Name="BootType">0</Data></EventData></Event>',
            ),
        ]):
            info1 = session_manager.get_boot_info()
            assert info1["boot_type"] == "unknown"
            session_manager._cached_boot_info_time -= 65.0
            info2 = session_manager.get_boot_info()
            assert info2["boot_type"] == "cold_boot"
    finally:
        session_manager._cached_boot_info = orig_info
        session_manager._cached_boot_info_time = orig_time

    # System ActionDispatcherMixin raises explicitly when no interactive session exists
    disp = action_dispatcher.ActionDispatcherMixin()
    disp.acknowledge = MagicMock()
    disp.config = {}
    with patch("system.action_dispatcher.find_active_interactive_session_id", return_value=None):
        disp.execute_action("cmd-lock-fail", "lock", {})
    assert disp.acknowledge.call_args[0][1] == "failed"

    # ActivityDB propagates SQLite failure instead of returning None silently
    db = ActivityDB(tmp_path / "act.db")
    with patch.object(db, "_get_connection") as get_conn_mock:
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.execute.side_effect = sqlite3.OperationalError("readonly")
        get_conn_mock.return_value = mock_conn
        with pytest.raises(sqlite3.OperationalError):
            db.record_session("app.exe", "title", "C:\\app.exe", 1_700_000_000.0, 1_700_000_100.0)
    db.close()
