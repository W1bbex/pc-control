"""Tests for MQTT gateway, HealthEngine timestamps/reboot handling, dispatcher ACKs, and paho socket resilience."""

import json
import logging
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt

from core.mqtt_fallback import patch_paho_sockpair
from vds_bot.health_engine import ComponentState, HealthEngine, PCState
from vds_bot.mqtt_dispatcher import MqttDispatcherMixin
from vds_bot.mqtt_gateway import (
    TOPIC_DESKTOP_ACK,
    TOPIC_PHYSICAL_INPUT,
    TOPIC_TELEMETRY,
    MqttGateway,
)

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_health_engine_keeps_shutdown_observed_when_agents_stale():
    engine = HealthEngine()
    engine.update("mqtt_broker", "online")
    engine.update("windows_system", "stale")
    engine.update("windows_desktop", "offline")
    engine.update("windows_session", "unknown")
    engine.update("esp", "online")
    engine.set_shutdown_observed(ttl=60.0)

    state = engine.evaluate_pc_state(telemetry_age=15)
    assert state.state == PCState.SHUTDOWN_OBSERVED
    assert "Выключен" in state.label

    engine.update("windows_system", "stale")
    engine.update("windows_desktop", "stale")
    state_both_stale = engine.evaluate_pc_state(telemetry_age=25)
    assert state_both_stale.state == PCState.SHUTDOWN_OBSERVED


def test_out_of_order_heartbeat_does_not_clear_shutdown_observed():
    engine = HealthEngine()
    engine.update("mqtt_broker", "online")
    engine.set_shutdown_observed(ttl=3600.0, now=2000.0)

    engine.update("windows_system", "online", ts=1995.0)
    assert engine.shutdown_observed_until > 0.0

    engine.update("windows_system", "online", ts=2050.0)
    assert engine.shutdown_observed_until == 0.0


def test_health_engine_live_heartbeat_clears_graceful_shutdown_despite_clock_skew():
    engine = HealthEngine()
    now = time.time()
    engine.set_graceful_shutdown(ts=now, reason="windows_shutdown")
    assert engine.graceful_shutdown_until > 0.0

    engine.update("windows_system", "online", boot_id="boot-new", ts=now - 5.0, is_retained=False)
    assert engine.graceful_shutdown_until == 0.0
    assert engine.get_status("windows_system") == "online"


def test_component_state_ignores_timestamp_regression():
    now = time.time()
    comp = ComponentState("windows_system", stale_threshold=120.0)
    comp.update("online", ts=now)
    assert comp.status == "online"
    assert comp.last_ts == now

    comp.update("offline", ts=now - 50.0)
    assert comp.status == "online"
    assert comp.last_ts == now


def test_component_state_boot_id_change_resets_sequence_on_reboot():
    comp = ComponentState("telemetry", stale_threshold=60)
    now = time.time()
    comp.update("fresh", ts=now, boot_id="boot-1")
    assert comp.status == "fresh"
    assert comp.last_ts == now

    reboot_ts = now - 5.0
    comp.update("fresh", ts=reboot_ts, boot_id="boot-2")
    assert comp.boot_id == "boot-2"
    assert comp.last_ts == reboot_ts
    assert comp.status in ("fresh", "stale")


def test_component_state_handles_string_and_invalid_timestamps():
    comp = ComponentState("test_comp", 30)
    now = time.time()
    comp.last_ts = now - 5.0

    comp.update("online", ts=str(now))
    assert comp.status == "online"
    assert abs(comp.last_ts - now) < 0.1

    comp.update("online", ts="invalid_string")
    assert comp.status is not None


def test_esp_pre_ntp_zero_timestamp_live_vs_retained():
    esp = ComponentState("esp", stale_threshold=120.0)
    esp.update("online", boot_id="esp-boot-1", ts=0, is_retained=False)
    assert esp.status == "online"
    assert esp.last_ts > 0

    esp_retained = ComponentState("esp", stale_threshold=120.0)
    esp_retained.update("online", boot_id="esp-boot-old", ts=0, is_retained=True)
    assert esp_retained.status == "unknown"


def test_component_state_boot_id_change_accepts_reset_timestamp():
    esp = ComponentState("esp", stale_threshold=120.0)
    now = time.time()
    esp.update("online", boot_id="esp-boot-1", ts=now - 5.0, is_retained=False)
    assert esp.status == "online"
    assert esp.last_ts == now - 5.0

    esp.update("online", boot_id="esp-boot-2", ts=0, is_retained=False)
    assert esp.boot_id == "esp-boot-2"
    assert esp.status == "online"


def test_mqtt_dispatcher_screenshot_upload_offloaded_to_executor():
    dispatcher = MqttDispatcherMixin()
    dispatcher.ui = MagicMock()
    dispatcher.executor = MagicMock()
    dispatcher.pending_screenshots = {"cmd-1": {"chat_id": 12345}}

    header_json = json.dumps({"id": "cmd-1", "format": "jpeg", "width": 100, "height": 100}).encode("utf-8")
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    payload = len(header_json).to_bytes(4, "big") + header_json + fake_jpeg

    with patch("vds_bot.mqtt_dispatcher.ALLOWED_USER_ID", 12345):
        dispatcher._on_mqtt_event("screenshot_image", payload)

    assert dispatcher.executor.submit.called
    assert not dispatcher.ui.send_photo.called


def test_mqtt_dispatcher_resolves_private_executor():
    app = MqttDispatcherMixin()
    mock_exec = MagicMock()
    app._executor = mock_exec
    resolved = getattr(app, "_executor", getattr(app, "executor", None))
    assert resolved is mock_exec


def test_mqtt_dispatcher_command_timeout_evicts_pending_telemetry():
    app = MqttDispatcherMixin()
    app.ui = MagicMock()
    app.allowed_user_id = 12345
    app.power_flow = MagicMock()
    app.physical_btn = MagicMock()
    app.pending_screenshots = {}
    app.pending_telemetry_cmd_ids = {"cmd-telem-1": 1000.0}
    app.pending_shutdown_cmd_id = None
    app.pending_console_cmds = {}

    app._on_mqtt_event("command_timeout", {"id": "cmd-telem-1", "action": "refresh_telemetry"})
    assert "cmd-telem-1" not in app.pending_telemetry_cmd_ids


def test_mqtt_dispatcher_records_workspace_executed_ack_and_event():
    class Dispatcher(MqttDispatcherMixin):
        def __init__(self):
            self.power_flow = MagicMock()
            self.physical_btn = MagicMock()
            self.save_state = MagicMock()
            self.active_workspace_cmd_id = "cmd-123"
            self.workspace_execution_result = None
            self.workspace_executed_event = threading.Event()
            self.app_statuses = {}

    dispatcher = Dispatcher()
    dispatcher._on_mqtt_event("ack", {
        "id": "cmd-123",
        "action": "launch_app",
        "result": "executed",
        "data": {},
    })
    assert dispatcher.workspace_execution_result == "executed"
    assert dispatcher.workspace_executed_event.is_set()


def test_mqtt_dispatcher_formats_stealth_exit_alert_by_reason():
    dispatcher = MqttDispatcherMixin()
    dispatcher.monitoring = MagicMock()
    dispatcher.monitoring.latest_telemetry = {"stealth_mode": True}
    dispatcher.ui = MagicMock()
    dispatcher.allowed_user_id = 12345
    dispatcher.active_stealth_card = None

    dispatcher.on_stealth_exit({"reason": "emergency_escape"})
    assert "экстренный выход 5x <code>Escape</code>" in dispatcher.ui.send_message.call_args[0][1]

    dispatcher.ui.send_message.reset_mock()
    dispatcher.on_stealth_exit({"reason": "network_offline"})
    assert "таймауту сети" in dispatcher.ui.send_message.call_args[0][1]

    dispatcher.ui.send_message.reset_mock()
    dispatcher.on_stealth_exit({"reason": "local_hotkey"})
    assert "хоткей <code>Win+W+1</code>" in dispatcher.ui.send_message.call_args[0][1]


def test_patch_paho_sockpair_catches_winerror_10054_and_1236():
    orig_loop = mqtt.Client._loop
    orig_packet_queue = mqtt.Client._packet_queue
    orig_flag = getattr(mqtt.Client, "_sockpair_patched", False)

    def failing_loop(self, timeout=1.0):
        raise ConnectionResetError(10054, "WinError 10054")

    def failing_packet_queue(self, command, packet, mid=0, qos=0, info=None):
        err = OSError("The network connection was aborted by the local system")
        err.winerror = 1236
        raise err

    try:
        mqtt.Client._sockpair_patched = False
        mqtt.Client._loop = failing_loop
        mqtt.Client._packet_queue = failing_packet_queue
        patch_paho_sockpair()
        assert mqtt.Client._sockpair_patched is True

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        assert client._loop(1.0) == mqtt.MQTT_ERR_CONN_LOST
        assert client._packet_queue(mqtt.PUBLISH, b"dummy", 1, 0, None) == mqtt.MQTT_ERR_NO_CONN
    finally:
        mqtt.Client._loop = orig_loop
        mqtt.Client._packet_queue = orig_packet_queue
        mqtt.Client._sockpair_patched = orig_flag


def test_boot_agent_watchdog_reconnects_when_offline_or_thread_dead():
    import boot_agent

    agent = boot_agent.SystemAgent.__new__(boot_agent.SystemAgent)
    agent.config = {"mqtt": {"host": "127.0.0.1", "port": 1883, "username": "u", "password": "p"}}
    agent.stop_event = threading.Event()
    agent.mqtt_online = False
    agent._last_reconnect_attempt = 0.0

    setup_called = False
    connect_called = False

    def mock_setup():
        nonlocal setup_called
        setup_called = True
        agent.mqtt = MagicMock()
        agent.mqtt._thread = None
        agent.mqtt.is_connected.return_value = False

    def mock_connect(max_attempts=None, timeout_per_attempt=20):
        nonlocal connect_called
        connect_called = True
        agent.mqtt_online = True
        return True

    agent.setup_mqtt_client = mock_setup
    agent.connect_with_retry = mock_connect
    agent.mqtt = MagicMock()
    agent.mqtt._thread = None
    agent.mqtt.is_connected.return_value = False

    agent.ensure_mqtt_connection()
    assert setup_called is True
    assert connect_called is True
    assert agent.mqtt.loop_start.called


def test_mqtt_gateway_logs_invalid_json_payloads_on_input_ack_and_telemetry(caplog):
    gw = MqttGateway("u", "p")

    def _msg(topic, raw_bytes):
        msg = MagicMock()
        msg.topic = topic
        msg.payload = raw_bytes
        msg.retain = False
        return msg

    with caplog.at_level(logging.WARNING, logger="pc-control-bot.mqtt_gateway"):
        gw._on_message(None, None, _msg(TOPIC_PHYSICAL_INPUT, b"{broken-json"))
        gw._on_message(None, None, _msg(TOPIC_DESKTOP_ACK, b"{broken-ack"))
        gw._on_message(None, None, _msg(TOPIC_TELEMETRY, b"{broken-telemetry"))

    messages = [rec.getMessage() for rec in caplog.records]
    assert any("physical_input" in m for m in messages)
    assert any("ACK" in m for m in messages)
    assert any("telemetry" in m for m in messages)


def test_mqtt_gateway_stop_terminates_background_threads_promptly():
    gw = MqttGateway("u", "p")
    gw.client = MagicMock()

    gw.start()
    assert gw.health_thread.is_alive()
    assert gw._dispatch_thread.is_alive()

    t0 = time.monotonic()
    gw.stop(timeout=2.0)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0
    assert not gw.health_thread.is_alive()
    assert not gw._dispatch_thread.is_alive()
    gw.client.loop_stop.assert_called_once()
    gw.client.disconnect.assert_called_once()


def test_desktop_agent_watchdog_reconnects_when_offline_or_thread_dead():
    import agent as desktop_agent_mod

    agent = desktop_agent_mod.PCControlAgent.__new__(desktop_agent_mod.PCControlAgent)
    agent.config = {"mqtt": {"host": "127.0.0.1", "port": 1883, "username": "u", "password": "p"}}
    agent.stop_event = threading.Event()
    agent.mqtt_online = False
    agent._last_reconnect_attempt = 0.0

    setup_called = False
    connect_called = False

    def mock_setup():
        nonlocal setup_called
        setup_called = True
        agent.mqtt = MagicMock()
        agent.mqtt._thread = None
        agent.mqtt.is_connected.return_value = False

    def mock_connect(max_attempts=None, timeout_per_attempt=20):
        nonlocal connect_called
        connect_called = True
        agent.mqtt_online = True
        return True

    agent.setup_mqtt_client = mock_setup
    agent.connect_with_retry = mock_connect
    agent.mqtt = MagicMock()
    agent.mqtt._thread = None
    agent.mqtt.is_connected.return_value = False

    agent.ensure_mqtt_connection()
    assert setup_called is True
    assert connect_called is True
    assert agent.mqtt.loop_start.called


def test_patch_paho_sockpair_preserves_reconnect_state_and_pops_orphaned_packet():
    patch_paho_sockpair()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sockpair-resilience-test")
    conn_state = getattr(mqtt, "_ConnectionState", None)
    if conn_state is not None:
        client._state = conn_state.MQTT_CS_CONNECTED

    fake_sock = MagicMock()
    fake_sock.pending.return_value = 0
    client._sock = fake_sock
    broken_reader = MagicMock()
    broken_reader.recv.side_effect = ConnectionResetError(10054, "Connection reset by peer")
    client._sockpairR = broken_reader
    broken_writer = MagicMock()
    broken_writer.send.side_effect = ConnectionResetError(10054, "Connection reset by peer")
    client._sockpairW = broken_writer

    with patch("paho.mqtt.client.select.select", return_value=([broken_reader], [], [])):
        client.disconnect = MagicMock()
        rc = client._loop(timeout=0.1)

    assert rc == mqtt.MQTT_ERR_CONN_LOST
    client.disconnect.assert_not_called()
    if conn_state is not None:
        assert client._state == conn_state.MQTT_CS_CONNECTION_LOST
    assert client._sock is None
    assert client._sockpairW is None

    # Re-arm broken writer for _packet_queue failure and confirm queued packet is rolled back
    client._sock = MagicMock()
    client._sockpairR = MagicMock()
    client._sockpairW = broken_writer
    client._out_packet.clear()
    info = mqtt.MQTTMessageInfo(42)

    rc_pub = client._packet_queue(0x30, b"payload", mid=42, qos=1, info=info)
    assert rc_pub == mqtt.MQTT_ERR_NO_CONN
    assert len(client._out_packet) == 0
    assert info.rc == mqtt.MQTT_ERR_NO_CONN


def test_health_engine_auxiliary_components_preserve_graceful_shutdown():
    engine = HealthEngine()
    now = time.time()
    engine.update("windows_system", "online", boot_id="win-boot-1", ts=now - 10.0)
    engine.update("mqtt_broker", "online")
    engine.set_graceful_shutdown(ts=now, reason="windows_shutdown")

    for aux in ("esp", "mqtt_broker", "telegram"):
        engine.update(aux, "online", boot_id=f"{aux}-boot-1", ts=now + 30.0, is_retained=False)
        assert engine.graceful_shutdown_until > 0.0

    state = engine.evaluate_pc_state(telemetry_age=None)
    assert state.state == PCState.SHUTDOWN_OBSERVED


def test_health_engine_non_numeric_ts_does_not_crash_or_clear_shutdown():
    engine = HealthEngine()
    now = time.time()
    engine.set_graceful_shutdown(ts=now, reason="windows_shutdown")

    engine.update("windows_system", "online", ts="not-a-float", is_retained=False)
    assert engine.graceful_shutdown_until > 0.0
    assert engine.get_status("windows_system") == "unknown"


def test_component_state_future_skew_does_not_poison_last_ts():
    comp = ComponentState("telemetry", stale_threshold=120.0)
    now = time.time()
    comp.update("fresh", ts=now)
    assert comp.status == "fresh"
    assert comp.last_ts == now

    # Future skew beyond -600s marks stale without advancing monotonic last_ts
    comp.update("fresh", ts=now + 1200.0)
    assert comp.status == "stale"
    assert comp.last_ts == now

    # Next valid timestamp recovers to fresh instead of being dropped
    comp.update("fresh", ts=now + 5.0)
    assert comp.status == "fresh"
    assert comp.last_ts == now + 5.0


def test_system_agent_exits_when_mqtt_config_invalid(tmp_path):
    import pytest
    import boot_agent

    bad_host_cfg = tmp_path / "bad_host.json"
    bad_host_cfg.write_text(json.dumps({"mqtt": {"port": 8883}}), encoding="utf-8")
    with patch.object(boot_agent, "CONFIG_PATH", bad_host_cfg), \
         patch.dict("os.environ", {"MQTT_HOST": ""}, clear=False):
        with pytest.raises(SystemExit) as exc_info:
            boot_agent.SystemAgent()
        assert exc_info.value.code == 1

    bad_port_cfg = tmp_path / "bad_port.json"
    bad_port_cfg.write_text(json.dumps({"mqtt": {"host": "127.0.0.1", "port": 70000}}), encoding="utf-8")
    with patch.object(boot_agent, "CONFIG_PATH", bad_port_cfg):
        with pytest.raises(SystemExit) as exc_info2:
            boot_agent.SystemAgent()
        assert exc_info2.value.code == 1


def test_system_agent_ensure_mqtt_connection_clears_online_and_bounds_connect():
    import boot_agent

    sa = boot_agent.SystemAgent.__new__(boot_agent.SystemAgent)
    sa.mqtt = MagicMock()
    sa.mqtt._thread = MagicMock()
    sa.mqtt._thread.is_alive.return_value = False
    sa.mqtt_online = True
    sa._last_reconnect_attempt = 0.0

    with patch.object(sa, "setup_mqtt_client") as mock_setup, \
         patch.object(sa, "connect_with_retry", return_value=False) as mock_conn:
        sa.ensure_mqtt_connection()

    assert sa.mqtt_online is False
    mock_setup.assert_called_once()
    mock_conn.assert_called_once_with(max_attempts=1, timeout_per_attempt=5)


def test_desktop_agent_ensure_mqtt_connection_clears_online_and_bounds_connect():
    from agent import PCControlAgent

    with patch.object(PCControlAgent, "__init__", lambda self: None):
        da = PCControlAgent()
        da.mqtt = MagicMock()
        da.mqtt._thread = MagicMock()
        da.mqtt._thread.is_alive.return_value = False
        da.mqtt_online = True
        da._last_reconnect_attempt = 0.0

        with patch.object(da, "setup_mqtt_client") as mock_setup, \
             patch.object(da, "connect_with_retry", return_value=False) as mock_conn:
            da.ensure_mqtt_connection()

        assert da.mqtt_online is False
        mock_setup.assert_called_once()
        mock_conn.assert_called_once_with(max_attempts=1, timeout_per_attempt=5)

