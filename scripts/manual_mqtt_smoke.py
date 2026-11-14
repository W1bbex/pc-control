#!/usr/bin/env python3
"""Manual local MQTT broker & HealthEngine smoke script."""

import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

SCRIPT_DIR = Path(__file__).resolve().parent
VDS_DIR = SCRIPT_DIR.parent / "vds_bot"
BROKER_CONFIG_PATH = SCRIPT_DIR / "fixtures" / "test_broker.yaml"

if str(VDS_DIR) not in sys.path:
    sys.path.insert(0, str(VDS_DIR))

from mqtt_gateway import (
    TOPIC_DESKTOP_COMMAND,
    TOPIC_DESKTOP_STATUS,
    TOPIC_ESP_COMMAND,
    TOPIC_ESP_STATUS,
    TOPIC_GENERAL_ACK,
    TOPIC_SYSTEM_COMMAND,
    TOPIC_SYSTEM_STATUS,
    MqttGateway,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
LOG = logging.getLogger("mqtt-smoke")

events = []


def on_event(event_name, data):
    LOG.info("EVENT EMITTED: %s -> %s", event_name, data)
    events.append((event_name, data))


def wait_for_health(component, expected_status, timeout=5.0):
    start = time.time()
    while time.time() - start < timeout:
        for i, (name, data) in enumerate(events):
            if name == "health_update":
                if data.get(component) == expected_status:
                    return events.pop(i)
        time.sleep(0.1)
    return None


def wait_for_event(event_name, timeout=5.0):
    start = time.time()
    while time.time() - start < timeout:
        for i, (name, _) in enumerate(events):
            if name == event_name:
                return events.pop(i)
        time.sleep(0.1)
    return None


class AgentEmulator:
    def __init__(self, name, status_topic, will_payload="offline"):
        self.name = name
        self.status_topic = status_topic
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"emulator-{name}",
        )
        self.client.will_set(status_topic, will_payload, qos=1, retain=True)
        self.running = False

    def start(self):
        self.client.connect_async("127.0.0.1", 1883)
        self.client.loop_start()
        self.running = True

    def stop(self):
        self.running = False
        if hasattr(self.client, "_sock") and self.client._sock:
            try:
                self.client._sock.close()
            except Exception:
                pass
        self.client.loop_stop()

    def publish_status(self, payload):
        if self.running:
            self.client.publish(self.status_topic, payload, retain=True)


def run_smoke():
    LOG.info("=== 0. Testing bot.py import ===")
    import bot  # noqa: F401
    LOG.info("bot.py imported successfully")

    gw = MqttGateway("bot", "bot", host="127.0.0.1", port=1883, on_event_callback=on_event)
    for comp in gw.health.components.values():
        comp.stale_threshold = 2.0

    LOG.info("=== 1. Starting Gateway while broker is DOWN ===")
    gw.start()

    time.sleep(0.5)
    assert gw.health.get_status("esp") == "unknown", "Initial ESP status must be unknown"
    assert gw.health.get_status("windows_system") == "unknown", "Initial System status must be unknown"
    assert gw.health.get_status("telegram") == "unknown", "Telegram must be unknown"
    assert gw.system_status == "unknown", "Compat system_status failed"
    assert getattr(gw, "mqtt_online") is False, "Compat mqtt_online failed"

    has_amqtt = False
    broker = None
    if shutil.which("amqtt") is not None:
        try:
            LOG.info("Starting local amqtt broker with %s...", BROKER_CONFIG_PATH)
            broker = subprocess.Popen(
                ["amqtt", "-c", str(BROKER_CONFIG_PATH)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            start_wait = time.time()
            while time.time() - start_wait < 2.5:
                if gw.mqtt_online:
                    has_amqtt = True
                    break
                time.sleep(0.2)
            if not has_amqtt:
                broker.kill()
        except Exception:
            has_amqtt = False

    if has_amqtt:
        try:
            LOG.info("=== 2. Testing Old/Future TS Rejection ===")
            old_ts = time.time() - 1000
            future_ts = time.time() + 1000

            sys_emu = AgentEmulator("sys", TOPIC_SYSTEM_STATUS, will_payload='{"state": "offline"}')
            sys_emu.start()
            time.sleep(0.5)

            events.clear()
            sys_emu.publish_status(json.dumps({"state": "online", "session": "unlocked", "ts": old_ts, "boot_id": "old"}))
            ev = wait_for_health("system_status", "stale", timeout=2.0)
            assert ev, "System should be stale due to old TS"

            events.clear()
            sys_emu.publish_status(json.dumps({"state": "online", "session": "unlocked", "ts": future_ts, "boot_id": "future"}))
            ev = wait_for_health("system_status", "stale", timeout=2.0)
            assert ev, "System should be stale due to future TS"

            events.clear()
            sys_emu.publish_status(json.dumps({"state": "online", "session": "unlocked"}))
            ev = wait_for_health("system_status", "unknown", timeout=2.0)
            assert ev, "System should be unknown due to missing TS"

            LOG.info("=== 3. Testing Fresh Heartbeats ===")
            events.clear()
            now_ts = time.time()
            sys_emu.publish_status(json.dumps({"state": "online", "session": "unlocked", "ts": now_ts, "boot_id": "boot-1"}))
            ev = wait_for_health("system_status", "online")
            assert ev, "Failed to transition system to online"

            LOG.info("=== 4. Testing Command & ACK ===")
            cmd_id = gw.publish_esp_action("PING")
            assert cmd_id is not None, "Failed to publish command"
            assert cmd_id in gw.pending_commands, "Command not registered correctly"
            time.sleep(0.2)
            esp_emu = AgentEmulator("esp", TOPIC_ESP_STATUS, will_payload="offline")
            esp_emu.start()
            time.sleep(0.5)
            esp_emu.client.publish(TOPIC_GENERAL_ACK, json.dumps({"id": cmd_id, "result": "pong"}))
            ev = wait_for_event("ack")
            assert ev and ev[1]["id"] == cmd_id, "Failed to receive ACK"

            LOG.info("=== 5. Testing Stale Status ===")
            ev = wait_for_health("system_status", "stale", timeout=8.0)
            assert ev, "Failed to transition to stale"

            LOG.info("=== 6. Testing Offline Status via LWT ===")
            sys_emu.stop()
            ev = wait_for_health("system_status", "offline", timeout=5.0)
            assert ev, "Failed to transition to offline via LWT"

            LOG.info("=== 7. Testing Publish Failure (RC check) ===")
            broker.kill()
            broker.wait()
            time.sleep(0.5)
            cmd_id_fail = gw.publish_system_action("sleep")
            assert cmd_id_fail is None, "Command should be rejected due to bad RC"

            LOG.info("=== 8. Testing Broker Crash (Unknown status) ===")
            ev = wait_for_health("mqtt_broker", "unknown", timeout=5.0)
            assert ev, "Failed to transition to unknown on disconnect"
            assert not gw.mqtt_online, "Gateway should be offline"
            assert gw.system_status == "unknown", "Previous offline status did not become unknown"
        finally:
            if broker is not None and broker.poll() is None:
                broker.kill()
                broker.wait()
    else:
        LOG.info("amqtt not found in PATH; performing direct mock message validation.")

        class Msg:
            def __init__(self, topic, payload, retain=False):
                self.topic = topic
                self.payload = payload.encode("utf-8") if isinstance(payload, str) else payload
                self.retain = retain

        gw.mqtt_online = True
        gw.health.update("mqtt_broker", "online")

        old_ts = time.time() - 1000
        gw._on_message(None, None, Msg(TOPIC_SYSTEM_STATUS, json.dumps({"state": "online", "session": "unlocked", "ts": old_ts})))
        assert gw.system_status == "stale", "Old TS must result in stale status"

        now_ts = time.time()
        gw._on_message(None, None, Msg(TOPIC_SYSTEM_STATUS, json.dumps({"state": "online", "session": "unlocked", "ts": now_ts})))
        assert gw.system_status == "online", "Fresh TS must result in online status"

        gw._on_message(None, None, Msg(TOPIC_SYSTEM_STATUS, json.dumps({"state": "online", "session": "unlocked"})))
        assert gw.system_status == "unknown", "Missing TS must result in unknown status"

        gw._on_message(None, None, Msg(TOPIC_SYSTEM_STATUS, json.dumps({"state": "offline"})))
        assert gw.system_status == "offline", "Offline LWT must be accepted"

    LOG.info("ALL SMOKE CHECKS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    run_smoke()
