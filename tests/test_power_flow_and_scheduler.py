"""Tests for PowerFlowManager state transitions, PowerSchedulerMixin watchdogs, and cooler LED persistence."""

import json
import threading
import time
from unittest.mock import MagicMock, patch

import vds_bot.bot as bot_mod
import vds_bot.power_flow as power_flow_mod
from vds_bot.bot import PcControlBotApp
from vds_bot.health_engine import HealthEngine, PCState
from vds_bot.power_flow import PowerFlowManager, PowerFlowStage
from vds_bot.power_scheduler import PowerSchedulerMixin
from vds_bot.routers.command_router import route_message
from vds_bot.routers.monitoring_router import route_monitoring_callback
from vds_bot.routers.workspace_router import route_workspace_callback
from vds_bot.workspace_runner import WorkspaceRunnerMixin


def test_power_flow_manager_initializes_thread_lock():
    manager = PowerFlowManager(telegram_ui=MagicMock(), mqtt_gateway=MagicMock())
    assert hasattr(manager, "_lock")
    assert isinstance(manager._lock, type(threading.Lock()))


def test_power_flow_load_state_survives_telegram_error_during_unlock_recovery(tmp_path, monkeypatch):
    state_file = tmp_path / "flow_state.json"
    state_file.write_text(
        json.dumps({
            "stage": "UNLOCKING",
            "chat_id": 12345,
            "message_id": 67890,
            "expires_at": time.time() + 300,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(power_flow_mod, "FLOW_STATE_PATH", state_file)

    fake_ui = MagicMock()
    fake_ui.send_message.side_effect = ConnectionError("Telegram API down")
    flow = PowerFlowManager(fake_ui, MagicMock())
    assert flow.stage == PowerFlowStage.UNLOCK_FAILED


def test_power_flow_unlock_executed_ack_awaits_unlocked_session():
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
    assert not flow.on_unlock_failed.called
    assert not flow.on_unlock_success.called


def test_power_flow_check_timeout_ignores_waiting_workspace_decision():
    flow = PowerFlowManager(telegram_ui=MagicMock(), mqtt_gateway=MagicMock())
    flow.stage = PowerFlowStage.WAITING_WORKSPACE_DECISION
    flow.expires_at = time.time() - 100
    assert flow.check_timeout(now=time.time()) is False


def test_power_flow_auto_launch_workspace_returns_without_waiting_decision():
    mgr = PowerFlowManager(telegram_ui=MagicMock(), mqtt_gateway=MagicMock())
    mgr.stage = PowerFlowStage.UNLOCKING
    mgr.auto_launch_workspace = True
    mgr.on_workspace_start_cb = None

    mgr.on_system_session_update("unlocked", 123)
    assert mgr.stage == PowerFlowStage.WORKSPACE_RUNNING
    assert mgr.auto_launch_workspace is False


def test_power_flow_workspace_callback_exception_invoked_once():
    mgr = PowerFlowManager(telegram_ui=MagicMock(), mqtt_gateway=MagicMock())
    mgr.stage = PowerFlowStage.UNLOCKING
    mgr.auto_launch_workspace = True

    call_count = 0

    def faulty_callback(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("Callback crashed!")

    mgr.on_workspace_start_cb = faulty_callback
    mgr.on_unlock_success(123)
    assert call_count == 1
    assert mgr.stage == PowerFlowStage.WORKSPACE_RUNNING


def test_workspace_runner_resets_power_flow_on_crash():
    class App(WorkspaceRunnerMixin):
        def __init__(self):
            self.power_flow = MagicMock(spec=PowerFlowManager)
            self.chosen_vpn = "incy"

    app = App()
    app.run_workspace_setup_steps = MagicMock(side_effect=RuntimeError("Runner crashed!"))
    app.start_async_workspace_setup(123)
    time.sleep(0.15)
    assert app.power_flow.reset_flow.called


def test_selecting_workspace_mode_blocks_periodic_launch_card_overwrite():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.esp_status = "online"

    flow = PowerFlowManager(ui, mqtt)
    flow.chat_id = 123456
    flow.message_id = 999
    flow.stage = PowerFlowStage.POWERING_ON
    flow.last_card_update_at = time.time() - 10

    app = MagicMock()
    app.power_flow = flow
    app.ui = ui
    app.chosen_vpn = "incy"

    assert route_workspace_callback(app, "flow_unlock_and_workspace", 123456, 999, "cb_1") is True
    assert flow.selecting_mode is True

    ui.send_message.reset_mock()
    flow._update_launch_card(force=False)
    ui.send_message.assert_not_called()

    assert route_workspace_callback(app, "flow_workspace_vpn:incy", 123456, 999, "cb_2") is True
    assert flow.selecting_mode is False
    assert flow.auto_launch_workspace is True
    assert flow.target_workspace_vpn == "incy"


def test_selecting_game_and_vpn_clears_mode_lock_and_sets_auto_launch():
    ui = MagicMock()
    mqtt = MagicMock()
    flow = PowerFlowManager(ui, mqtt)
    app = MagicMock()
    app.power_flow = flow
    app.ui = ui

    route_workspace_callback(app, "flow_unlock_and_game", 123456, 999, "cb_g1")
    assert flow.selecting_mode is True

    route_workspace_callback(app, "flow_game:minecraft", 123456, 999, "cb_g2")
    assert flow.selecting_mode is True

    route_workspace_callback(app, "flow_game_vpn:minecraft:happ", 123456, 999, "cb_g3")
    assert flow.selecting_mode is False
    assert flow.auto_launch_game is True
    assert flow.target_game == "minecraft"
    assert flow.target_game_vpn == "happ"


def test_cooler_led_off_persists_and_blocks_led_on_at_power_on():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.esp_status = "online"

    app = MagicMock()
    app.ui = ui
    app.mqtt = mqtt
    app.user_led_state = "on"
    app.user_led_color = "255,150,125"
    app.save_state = MagicMock()

    assert route_monitoring_callback(app, "led_action:off", 123456, 999, "cb_off") is True
    assert app.user_led_state == "off"
    app.save_state.assert_called()
    mqtt.publish_esp_action.assert_called_with("LED_OFF")

    flow = PowerFlowManager(ui, mqtt)
    flow.app = app
    mqtt.publish_esp_action.reset_mock()

    flow.start_power_on(123456)
    for call_args in mqtt.publish_esp_action.call_args_list:
        assert call_args[0][0] != "LED_ON"
        assert call_args[0][0] != "LED_COLOR"


def test_cooler_led_custom_color_persists_and_restores_on_power_on():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.esp_status = "online"

    app = MagicMock()
    app.ui = ui
    app.mqtt = mqtt
    app.user_led_state = "off"
    app.user_led_color = None
    app.save_state = MagicMock()

    assert route_monitoring_callback(app, "led_action:preset:3", 123456, 999, "cb_color") is True
    assert app.user_led_state == "on"
    assert app.user_led_color == "255,130,155"
    app.save_state.assert_called()
    mqtt.publish_esp_action.assert_called_with("LED_COLOR", value="255,130,155")

    flow = PowerFlowManager(ui, mqtt)
    flow.app = app
    mqtt.publish_esp_action.reset_mock()

    flow.start_power_on(123456)
    mqtt.publish_esp_action.assert_called_with("LED_COLOR", value="255,130,155")


def test_led_slash_commands_persist_user_led_state():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.esp_status = "online"

    app = MagicMock()
    app.ui = ui
    app.mqtt = mqtt
    app.user_led_state = "on"
    app.allowed_user_id = 123456
    app.save_state = MagicMock()

    route_message(app, {"chat": {"id": 123456}, "from": {"id": 123456}, "text": "/led_off"})
    assert app.user_led_state == "off"
    app.save_state.assert_called()
    mqtt.publish_esp_action.assert_called_with("LED_OFF")

    app.save_state.reset_mock()
    mqtt.publish_esp_action.reset_mock()
    route_message(app, {"chat": {"id": 123456}, "from": {"id": 123456}, "text": "/led_on"})
    assert app.user_led_state == "on"
    app.save_state.assert_called()


def test_power_flow_blocks_duplicate_start_power_on_while_powering_on():
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    mock_mqtt.system_status = "offline"
    mock_mqtt.desktop_status = "offline"
    mock_mqtt.esp_status = "online"
    mock_mqtt.publish_esp_action = MagicMock(return_value="cmd_1")
    pc_state = MagicMock()
    pc_state.state = "OFFLINE"
    mock_mqtt.health.evaluate_pc_state = MagicMock(return_value=pc_state)

    flow = PowerFlowManager(mock_ui, mock_mqtt)
    flow.reset_flow()
    flow.start_power_on(123)
    assert flow.stage == PowerFlowStage.POWERING_ON
    assert mock_mqtt.publish_esp_action.call_count == 2

    flow.start_power_on(123)
    assert mock_mqtt.publish_esp_action.call_count == 2
    flow.reset_flow()


def test_power_flow_auto_launch_game_sets_workspace_id_for_cancel():
    flow = PowerFlowManager(MagicMock(), MagicMock())
    flow.reset_flow()
    flow.auto_launch_game = True
    flow.target_game = "gta5"
    flow.message_id = 999
    flow.on_unlock_success(123)

    assert flow.stage == PowerFlowStage.WORKSPACE_RUNNING
    assert flow.active_workspace_id is not None
    assert flow.active_workspace_id.startswith("game-")

    flow.cancel_workspace(123)
    flow.mqtt.publish_desktop_action.assert_called_with("cancel_workspace", {"workspace_id": flow.active_workspace_id})
    flow.reset_flow()


def test_power_scheduler_evicts_stale_telemetry_and_antigravity_cmd_ids():
    class DummyBot(PowerSchedulerMixin):
        def __init__(self):
            self.running = True
            self.monitoring = MagicMock()
            self.monitoring.telemetry_age = MagicMock(return_value=1.0)
            self.mqtt = MagicMock()
            self.mqtt.system_status = "online"
            self.pending_screenshots = {}
            self.pending_shutdown_cmd_id = None
            self.scheduled_shutdown_ts = None
            self.pending_telemetry_cmd_ids = {
                None: (123, 456),
                "stale_telem": (123, 456),
                "fresh_telem": (123, 456),
            }
            self.pending_antigravity_cmd_ids = {
                None: (123, 456, "list"),
                "stale_ag": (123, 456, "list"),
                "fresh_ag": (123, 456, "list"),
            }
            self.pending_cmd_timestamps = {
                "stale_telem": time.time() - 120.0,
                "fresh_telem": time.time() - 5.0,
                "stale_ag": time.time() - 120.0,
                "fresh_ag": time.time() - 5.0,
            }

    bot = DummyBot()
    bot.scheduler_tick()

    assert None not in bot.pending_telemetry_cmd_ids
    assert "stale_telem" not in bot.pending_telemetry_cmd_ids
    assert "fresh_telem" in bot.pending_telemetry_cmd_ids

    assert None not in bot.pending_antigravity_cmd_ids
    assert "stale_ag" not in bot.pending_antigravity_cmd_ids
    assert "fresh_ag" in bot.pending_antigravity_cmd_ids


def test_unlock_failed_recovers_to_waiting_workspace_decision():
    with patch.object(PowerFlowManager, "load_state"), patch.object(PowerFlowManager, "save_state"):
        flow = PowerFlowManager(MagicMock(), MagicMock())
        flow.stage = PowerFlowStage.UNLOCK_FAILED
        flow.chat_id = 12345
        flow.on_system_session_update("unlocked", 12345)
        assert flow.stage == PowerFlowStage.WAITING_WORKSPACE_DECISION


def test_on_unlock_success_auto_launches_workspace_when_enabled():
    with patch.object(PowerFlowManager, "load_state"), patch.object(PowerFlowManager, "save_state"):
        flow = PowerFlowManager(MagicMock(), MagicMock())
        ws_started = []
        flow.on_workspace_start_cb = lambda cid, card_message_id=None, workspace_id=None, vpn="incy": ws_started.append((cid, workspace_id, vpn))
        flow.auto_launch_workspace = True
        flow.target_workspace_vpn = "happ"
        flow.stage = PowerFlowStage.UNLOCKING

        flow.on_unlock_success(12345)
        assert flow.stage == PowerFlowStage.WORKSPACE_RUNNING
        assert flow.auto_launch_workspace is False
        assert len(ws_started) == 1
        assert ws_started[0][0] == 12345
        assert ws_started[0][2] == "happ"


def test_on_unlock_success_prompts_workspace_decision_when_disabled():
    mock_ui = MagicMock()
    with patch.object(PowerFlowManager, "load_state"), patch.object(PowerFlowManager, "save_state"):
        flow = PowerFlowManager(mock_ui, MagicMock())
        ws_started = []
        flow.on_workspace_start_cb = lambda cid, card_message_id=None, workspace_id=None: ws_started.append(cid)
        flow.auto_launch_workspace = False
        flow.stage = PowerFlowStage.UNLOCKING

        flow.on_unlock_success(12345)
        assert flow.stage == PowerFlowStage.WAITING_WORKSPACE_DECISION
        assert len(ws_started) == 0
        assert mock_ui.send_message.called


def test_start_async_workspace_setup_blocks_concurrent_runs():
    with patch.object(PcControlBotApp, "__init__", return_value=None):
        app = PcControlBotApp()
        app._workspace_start_lock = threading.Lock()
        app._workspace_running = False
        runs = []

        def fake_runner(chat_id, card_message_id=None, workspace_id=None):
            runs.append(workspace_id)
            time.sleep(0.1)

        app.run_workspace_setup_steps = fake_runner
        app.start_async_workspace_setup(123, workspace_id="ws-1")
        time.sleep(0.02)
        assert app._workspace_running is True

        app.start_async_workspace_setup(123, workspace_id="ws-2")
        assert len(runs) == 1

        time.sleep(0.15)
        assert app._workspace_running is False


def test_power_flow_blocks_duplicate_in_flight_unlock():
    mqtt = MagicMock()
    mqtt.system_status = "online"
    mqtt.system_session = "locked"
    mqtt.publish_system_action.return_value = "sys-unlock-1"

    flow = PowerFlowManager(MagicMock(), mqtt)
    flow.stage = PowerFlowStage.UNLOCKING
    flow.pending_unlock_command_id = "sys-unlock-1"
    flow.unlock_attempts = 1

    flow.check_and_trigger_unlock(12345)
    assert mqtt.publish_system_action.call_count == 0

    flow.start_manual_unlock(12345)
    assert mqtt.publish_system_action.call_count == 0


def test_power_flow_stops_and_fails_after_two_unlock_attempts():
    mqtt = MagicMock()
    mqtt.system_status = "online"
    mqtt.system_session = "locked"
    mqtt.publish_system_action.return_value = "sys-unlock-cmd"

    flow = PowerFlowManager(MagicMock(), mqtt)
    flow.stage = PowerFlowStage.UNLOCKING
    flow.unlock_attempts = 2
    flow.pending_unlock_command_id = None

    flow.check_and_trigger_unlock(12345)
    assert flow.stage == PowerFlowStage.UNLOCK_FAILED
    assert mqtt.publish_system_action.call_count == 0


def test_power_flow_retries_unlock_after_45s_timeout():
    mqtt = MagicMock()
    mqtt.system_status = "online"
    mqtt.system_session = "locked"

    flow = PowerFlowManager(MagicMock(), mqtt)
    flow.stage = PowerFlowStage.UNLOCKING
    flow.pending_unlock_command_id = "sys-unlock-old"
    flow.pending_unlock_sent_at = time.time() - 50.0
    flow.unlock_attempts = 1

    flow.check_and_trigger_unlock(12345)
    assert flow.pending_unlock_command_id != "sys-unlock-old"
    assert flow.unlock_attempts == 2


def test_shutdown_sustain_requires_full_20_seconds():
    mqtt = MagicMock()
    mqtt.mqtt_online = True
    mqtt.system_status = "offline"
    mqtt.desktop_status = "offline"
    mqtt.health = HealthEngine()
    mqtt.health.update("mqtt_broker", "online")

    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.mqtt = mqtt
    app.monitoring = MagicMock()
    app.monitoring.telemetry_age.return_value = 25.0
    app.pending_shutdown_cmd_id = "sys-shutdown-1"
    app.pending_shutdown_started_at = 1000.0
    app.shutdown_acknowledged = True
    app.shutdown_offline_detected_at = None

    assert app._check_shutdown_confirmation(now=1005.0) is False
    assert app.shutdown_offline_detected_at == 1005.0
    assert app._check_shutdown_confirmation(now=1020.0) is False
    assert app._check_shutdown_confirmation(now=1025.5) is True
    assert mqtt.health.evaluate_pc_state(now=1025.5).state == PCState.SHUTDOWN_OBSERVED


def test_start_power_on_rejects_when_esp_offline():
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    mock_mqtt.health.evaluate_pc_state.return_value = MagicMock(state="SHUTDOWN_OBSERVED", label="Выключен")
    mock_mqtt.system_status = "offline"
    mock_mqtt.desktop_status = "offline"
    mock_mqtt.esp_status = "offline"

    flow = PowerFlowManager(mock_ui, mock_mqtt)
    flow.start_power_on(12345)

    assert flow.stage == PowerFlowStage.IDLE
    mock_mqtt.publish_esp_action.assert_not_called()
    sent_text = mock_ui.send_message.call_args[0][1]
    assert "ESP8266" in sent_text
    assert "не в сети" in sent_text


def test_start_power_on_dispatches_press_and_led_on_when_esp_online():
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    mock_mqtt.health.evaluate_pc_state.return_value = MagicMock(state="SHUTDOWN_OBSERVED", label="Выключен")
    mock_mqtt.system_status = "offline"
    mock_mqtt.desktop_status = "offline"
    mock_mqtt.esp_status = "online"
    mock_mqtt.publish_esp_action.return_value = "esp-press-cmd-1"

    flow = PowerFlowManager(mock_ui, mock_mqtt)
    flow.start_power_on(12345)

    assert flow.stage == PowerFlowStage.POWERING_ON
    mock_mqtt.publish_esp_action.assert_any_call("PRESS")
    mock_mqtt.publish_esp_action.assert_any_call("LED_ON")


def test_power_flow_timeout_expires_after_five_minutes():
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    mock_mqtt.health.evaluate_pc_state.return_value = MagicMock(state="SHUTDOWN_OBSERVED", label="Выключен")
    mock_mqtt.system_status = "offline"
    mock_mqtt.desktop_status = "offline"
    mock_mqtt.esp_status = "online"
    mock_mqtt.publish_esp_action.return_value = "cmd-1"

    flow = PowerFlowManager(mock_ui, mock_mqtt)
    flow.reset_flow()
    now = 1000.0
    with patch("time.time", return_value=now):
        flow.start_power_on(12345)

    assert flow.stage == PowerFlowStage.POWERING_ON
    assert flow.expires_at == now + 300.0
    assert not flow.check_timeout(now=1240.0)
    assert flow.check_timeout(now=1301.0)
    assert flow.stage == PowerFlowStage.IDLE
    flow.reset_flow()
    assert "Время ожидания запуска ПК истекло" in mock_ui.send_message.call_args[0][1]


def test_esp_reconnect_resyncs_cooler_led_on():
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.mqtt = MagicMock()
    app.mqtt.system_status = "online"
    app.mqtt.desktop_status = "online"
    app.mqtt.system_session = "unlocked"
    app.mqtt.esp_status = "offline"
    app.power_flow = MagicMock()
    app.monitoring = MagicMock()
    app.physical_btn = MagicMock()
    app._check_shutdown_confirmation = MagicMock()
    app.last_esp_status = "offline"
    app.last_auto_led_state = "on"

    app._on_mqtt_event("health_update", {
        "esp_status": "online",
        "system_status": "online",
        "desktop_status": "online",
        "session": "unlocked",
    })

    app.mqtt.publish_esp_action.assert_called_with("LED_ON")
    assert app.last_esp_status == "online"
    assert app.last_auto_led_state == "on"


def test_led_action_rejects_when_esp_offline(monkeypatch):
    monkeypatch.setattr(bot_mod, "ALLOWED_USER_ID", 12345)
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.mqtt = MagicMock()
    app.mqtt.esp_status = "offline"

    app.handle_callback({
        "id": "cb-123",
        "data": "led_action:on",
        "from": {"id": 12345},
        "message": {"chat": {"id": 12345}, "message_id": 99},
    })
    app.ui.answer_callback.assert_called_with(
        "cb-123",
        "❌ Модуль управления кулером (ESP8266) не в сети!",
        alert=True,
    )
    app.mqtt.publish_esp_action.assert_not_called()

    app.handle_message({
        "chat": {"id": 12345},
        "from": {"id": 12345},
        "text": "/led_on",
    })
    app.ui.send_message.assert_called_with(
        12345,
        "❌ Модуль управления кулером (ESP8266) не в сети!",
    )


def test_esp_reconnect_respects_user_led_state_off():
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.mqtt = MagicMock()
    app.mqtt.system_status = "online"
    app.mqtt.desktop_status = "online"
    app.mqtt.system_session = "unlocked"
    app.mqtt.esp_status = "offline"
    app.power_flow = MagicMock()
    app.monitoring = MagicMock()
    app.physical_btn = MagicMock()
    app._check_shutdown_confirmation = MagicMock()

    app.user_led_state = "off"
    app.last_esp_status = "offline"
    app.last_auto_led_state = None

    app._on_mqtt_event("health_update", {
        "esp_status": "online",
        "system_status": "online",
        "desktop_status": "online",
        "session": "unlocked",
    })

    app.mqtt.publish_esp_action.assert_called_with("LED_OFF")
    assert app.last_auto_led_state == "off"


def test_scheduler_watchdog_enforces_user_led_state_off():
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.mqtt = MagicMock()
    app.mqtt.system_status = "online"
    app.mqtt.desktop_status = "online"
    app.mqtt.system_session = "unlocked"
    app.monitoring = MagicMock()
    app.monitoring.telemetry_age.return_value = 5.0
    app.power_flow = MagicMock()
    app.pending_screenshots = {}
    app.pending_telemetry_cmd_ids = {}
    app.pending_antigravity_cmd_ids = {}
    app.pending_cmd_timestamps = {}
    app._check_dashboard_auto_refresh = MagicMock()

    app.user_led_state = "off"
    app.last_auto_led_state = "on"

    app.scheduler_tick(time.time())
    app.mqtt.publish_esp_action.assert_called_with("LED_OFF")
    assert app.last_auto_led_state == "off"

    app.mqtt.publish_esp_action.reset_mock()
    app.scheduler_tick(time.time())
    app.mqtt.publish_esp_action.assert_not_called()


def test_power_flow_concurrent_check_and_trigger_unlock_dispatches_once():
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    mock_mqtt.system_status = "online"
    mock_mqtt.system_session = "locked"

    def _slow_publish(action):
        time.sleep(0.05)
        return "sys-unlock-once"

    mock_mqtt.publish_system_action.side_effect = _slow_publish

    with patch.object(PowerFlowManager, "load_state"), patch.object(PowerFlowManager, "save_state"):
        flow = PowerFlowManager(mock_ui, mock_mqtt)
        flow.stage = PowerFlowStage.UNLOCKING
        flow.chat_id = 12345
        flow.message_id = 777

        barrier = threading.Barrier(5)
        errors = []

        def _worker():
            try:
                barrier.wait(timeout=2.0)
                flow.check_and_trigger_unlock(12345)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=3.0)

        assert not errors
        assert mock_mqtt.publish_system_action.call_count == 1
        assert flow.unlock_attempts == 1
        assert flow.pending_unlock_command_id == "sys-unlock-once"


def test_power_flow_concurrent_on_unlock_success_invokes_workspace_once():
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    cb_calls = []

    with patch.object(PowerFlowManager, "load_state"), patch.object(PowerFlowManager, "save_state"):
        flow = PowerFlowManager(mock_ui, mock_mqtt)
        flow.stage = PowerFlowStage.UNLOCKING
        flow.auto_launch_workspace = True
        flow.target_workspace_vpn = "incy"
        flow.on_workspace_start_cb = lambda *args, **kwargs: cb_calls.append(kwargs.get("workspace_id"))

        barrier = threading.Barrier(5)
        errors = []

        def _worker():
            try:
                barrier.wait(timeout=2.0)
                flow.on_unlock_success(12345)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=3.0)

        assert not errors
        assert len(cb_calls) == 1
        assert flow.stage == PowerFlowStage.WORKSPACE_RUNNING


def test_power_flow_set_auto_launch_workspace_saves_state_once():
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    mock_mqtt.system_status = "offline"
    mock_mqtt.system_session = "unknown"

    with patch.object(PowerFlowManager, "load_state"), patch.object(PowerFlowManager, "save_state") as mock_save:
        flow = PowerFlowManager(mock_ui, mock_mqtt)
        flow.message_id = 999
        flow.set_auto_launch_workspace(enable=True, vpn="happ", chat_id=12345)
        assert mock_save.call_count == 1
        assert flow.target_workspace_vpn == "happ"


def test_auto_led_dispatcher_and_watchdog_consistent_without_holding_lock():
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.mqtt = MagicMock()
    app.mqtt.system_status = "online"
    app.mqtt.desktop_status = "offline"
    app.mqtt.system_session = "unlocked"
    app.mqtt.esp_status = "online"
    app.power_flow = MagicMock()
    app.monitoring = MagicMock()
    app.monitoring.telemetry_age.return_value = 90.0  # Stale telemetry (>45s)
    app.physical_btn = MagicMock()
    app._check_shutdown_confirmation = MagicMock()
    app.save_state = MagicMock()
    app._main_menu_markup = MagicMock(return_value={"inline_keyboard": []})
    app.state_lock = threading.Lock()
    app.user_led_state = "on"
    app.last_esp_status = "online"
    app.last_auto_led_state = "on"
    app.scheduled_shutdown_ts = None
    app.pending_screenshots = {}
    app.pending_telemetry_cmd_ids = {}
    app.pending_antigravity_cmd_ids = {}
    app.pending_cmd_timestamps = {}

    lock_unlocked_during_io = []

    def _check_lock_during_publish(action, value=None):
        acquired = app.state_lock.acquire(blocking=False)
        lock_unlocked_during_io.append(acquired)
        if acquired:
            app.state_lock.release()
        return True

    app.mqtt.publish_esp_action.side_effect = _check_lock_during_publish

    app._on_mqtt_event("health_update", {
        "system_status": "online",
        "desktop_status": "offline",
        "esp_status": "online",
        "session": "unlocked",
    })
    assert app.last_auto_led_state == "off"
    assert lock_unlocked_during_io == [True]
    app.mqtt.publish_esp_action.assert_called_once_with("LED_OFF")

    app.mqtt.publish_esp_action.reset_mock()
    app.scheduler_tick(time.time())
    app.mqtt.publish_esp_action.assert_not_called()


def test_check_shutdown_confirmation_accepts_unknown_desktop_status():
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.mqtt = MagicMock()
    app.mqtt.mqtt_online = True
    app.mqtt.system_status = "offline"
    app.mqtt.desktop_status = "unknown"
    app.monitoring = MagicMock()
    app.monitoring.telemetry_age.return_value = 40.0
    app.monitoring.latest_received_at = 100.0
    app._main_menu_markup = MagicMock(return_value={"inline_keyboard": []})
    app.save_state = MagicMock()
    app.pending_shutdown_cmd_id = "sd-cmd-1"
    app.pending_shutdown_started_at = 110.0
    app.shutdown_acknowledged = True
    app.shutdown_offline_detected_at = 120.0

    confirmed = app._check_shutdown_confirmation(now=145.0)
    assert confirmed is True
    app.mqtt.health.set_shutdown_observed.assert_called_once()
    assert app.pending_shutdown_cmd_id is None


def test_mqtt_dispatcher_ignores_none_shutdown_cmd_and_non_dict_ack_data():
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = MagicMock()
    app.mqtt = MagicMock()
    app.power_flow = MagicMock()
    app.physical_btn = MagicMock()
    app.monitoring = MagicMock()
    app.save_state = MagicMock()
    app._power_menu_markup = MagicMock(return_value={"inline_keyboard": []})
    app.pending_shutdown_cmd_id = None
    app.pending_console_cmds = {}

    app._on_mqtt_event("ack", {
        "id": None,
        "result": "failed",
        "data": ["unexpected-list"],
    })
    app.ui.send_message.assert_not_called()
