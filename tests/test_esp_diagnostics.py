import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import Mock

from mqtt_gateway import MqttGateway, TOPIC_ESP_STATUS


def message(data, retained=False):
    return SimpleNamespace(topic=TOPIC_ESP_STATUS,
                           payload=json.dumps(data).encode(), retain=retained)


def test_diagnostic_replay_does_not_replace_live_health(caplog):
    gateway = MqttGateway("test", "unused")
    gateway.health.update("esp", "online", ts=time.time())
    gateway._emit_health_state = Mock()
    with caplog.at_level(logging.INFO):
        gateway._on_message(None, None, message({
            "event": "mqtt_connect_failed", "uptime_ms": 3000,
            "detail": "mqtt=-2 tls=-40", "heap": 7000,
        }))
    assert gateway.esp_status == "online"
    gateway._emit_health_state.assert_not_called()
    assert "mqtt_connect_failed" in caplog.text
    assert "tls=-40" in caplog.text


def test_live_heartbeat_keeps_metrics_in_server_log(caplog):
    gateway = MqttGateway("test", "unused")
    gateway._emit_health_state = Mock()
    with caplog.at_level(logging.INFO):
        gateway._on_message(None, None, message({
            "state": "online", "boot_id": "test-boot", "ts": time.time(),
            "heap": 12345, "rssi": -65, "version": "2.1",
        }))
    assert gateway.esp_status == "online"
    assert '"heap": 12345' in caplog.text
    gateway._emit_health_state.assert_called_once()


def test_lwt_still_marks_esp_offline():
    gateway = MqttGateway("test", "unused")
    gateway._emit_health_state = Mock()
    gateway._on_message(None, None, message({"state": "offline"}))
    assert gateway.esp_status == "offline"
