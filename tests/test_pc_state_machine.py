"""Tests for HealthEngine PC state transitions, monitoring status honesty, and history gap filling."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from vds_bot.health_engine import HealthEngine, PCState
from vds_bot.history import TelemetryHistory
from vds_bot.monitoring import MonitoringManager
from vds_bot.mqtt_gateway import MqttGateway, TOPIC_SYSTEM_STATUS


class FakeMqtt:
    def __init__(self):
        self.health = HealthEngine()
        self.mqtt_online = True
        self.system_session = "unlocked"

    @property
    def system_status(self):
        return self.health.get_status("windows_system")

    @property
    def desktop_status(self):
        return self.health.get_status("windows_desktop")

    @property
    def esp_status(self):
        return self.health.get_status("esp")


class FakeHistory:
    def insert(self, payload):
        pass


class TestPcStateMachine(unittest.TestCase):
    def setUp(self):
        self.health = HealthEngine()
        self.health.update("mqtt_broker", "online")

    def test_unknown_agents_report_unreachable_instead_of_off(self):
        self.health.update("windows_system", "unknown")
        self.health.update("windows_desktop", "unknown")

        info = self.health.evaluate_pc_state(telemetry_age=None)
        self.assertEqual(info.state, PCState.UNREACHABLE)
        self.assertIn("Связь потеряна", info.label)
        self.assertNotIn("выключен", info.label.lower())
        self.assertIn("состояние ПК неизвестно", info.reason)

    def test_stale_system_with_online_desktop_is_degraded(self):
        now = time.time()
        self.health.update("windows_system", "stale", ts=now - 200)
        self.health.update("windows_desktop", "online", ts=now)
        self.health.update("windows_session", "unlocked", ts=now)
        self.health.update("telemetry", "fresh", ts=now)

        info = self.health.evaluate_pc_state(telemetry_age=5.0)
        self.assertEqual(info.state, PCState.DEGRADED)
        self.assertIn("Ограниченная работа", info.label)
        self.assertIn("ПК включён", info.reason)

    def test_offline_system_with_online_desktop_is_degraded(self):
        now = time.time()
        self.health.update("windows_system", "offline")
        self.health.update("windows_desktop", "online", ts=now)
        self.health.update("telemetry", "fresh", ts=now)

        info = self.health.evaluate_pc_state(telemetry_age=10.0)
        self.assertEqual(info.state, PCState.DEGRADED)
        self.assertIn("Ограниченная работа", info.label)
        self.assertIn("ПК включён", info.reason)

    def test_both_agents_offline_without_shutdown_ack_is_unreachable(self):
        self.health.update("windows_system", "offline")
        self.health.update("windows_desktop", "offline")

        info = self.health.evaluate_pc_state(telemetry_age=None)
        self.assertEqual(info.state, PCState.UNREACHABLE)
        self.assertIn("Связь потеряна", info.label)
        self.assertIn("нет подтверждения выключения", info.reason)

    def test_shutdown_observed_transitions_to_unreachable_after_ttl(self):
        now = time.time()
        self.health.update("windows_system", "offline")
        self.health.update("windows_desktop", "offline")
        self.health.set_shutdown_observed(ttl=100.0, now=now)

        info = self.health.evaluate_pc_state(telemetry_age=30.0, now=now + 10)
        self.assertEqual(info.state, PCState.SHUTDOWN_OBSERVED)
        self.assertIn("Выключен (наблюдается)", info.label)
        self.assertIn("ПК перестал отвечать после принятой команды выключения", info.reason)

        info_expired = self.health.evaluate_pc_state(telemetry_age=150.0, now=now + 105)
        self.assertEqual(info_expired.state, PCState.UNREACHABLE)
        self.assertIn("Связь потеряна", info_expired.label)

    def test_fresh_heartbeat_clears_shutdown_observed(self):
        now = time.time()
        self.health.update("windows_system", "offline")
        self.health.update("windows_desktop", "offline")
        self.health.set_shutdown_observed(ttl=3600.0, now=now)
        self.assertEqual(self.health.evaluate_pc_state(now=now + 5).state, PCState.SHUTDOWN_OBSERVED)

        self.health.update("windows_system", "online", ts=now + 10)
        self.health.update("windows_session", "login_screen", ts=now + 10)

        info = self.health.evaluate_pc_state(now=now + 10)
        self.assertEqual(info.state, PCState.LOCKED)
        self.assertEqual(self.health.shutdown_observed_until, 0.0)

    def test_mqtt_disconnect_forces_unreachable_state(self):
        now = time.time()
        self.health.update("windows_system", "online", ts=now)
        self.health.update("windows_desktop", "online", ts=now)
        self.health.update("windows_session", "unlocked", ts=now)
        self.health.update("telemetry", "fresh", ts=now)
        self.assertEqual(self.health.evaluate_pc_state(telemetry_age=5.0, now=now).state, PCState.READY)

        self.health.update("mqtt_broker", "unknown")
        self.health.set_all_unknown(exclude=["telegram"])

        info = self.health.evaluate_pc_state(now=now)
        self.assertEqual(info.state, PCState.UNREACHABLE)
        self.assertFalse(info.mqtt_online)
        self.assertIn("нет связи с системой", info.label.lower())

    def test_online_agents_without_telemetry_report_degraded(self):
        now = time.time()
        self.health.update("windows_system", "online", ts=now)
        self.health.update("windows_desktop", "online", ts=now)
        self.health.update("windows_session", "unlocked", ts=now)

        info = self.health.evaluate_pc_state(telemetry_age=None, now=now)
        self.assertEqual(info.state, PCState.DEGRADED)
        self.assertIn("телеметрия ещё не получена", info.reason)

    def test_monitoring_dashboard_shows_waiting_then_fresh_telemetry(self):
        mqtt = FakeMqtt()
        mqtt.health.update("mqtt_broker", "online")
        mqtt.health.update("windows_system", "online", ts=time.time())
        mqtt.health.update("windows_desktop", "online", ts=time.time())
        mqtt.health.update("windows_session", "unlocked", ts=time.time())
        mqtt.health.update("telemetry", "fresh", ts=time.time())

        manager = MonitoringManager.__new__(MonitoringManager)
        manager.mqtt = mqtt
        manager.history = FakeHistory()
        manager.alerts = {}
        manager.latest_telemetry = None
        manager.latest_received_at = 0.0

        text_empty = manager.dashboard_text()
        self.assertIn("🟡", text_empty)
        self.assertIn("Ожидаю получение первой телеметрии", text_empty)

        manager.process_telemetry({"timestamp": int(time.time()), "user_active": True, "cpu": {"load_percent": 10}})
        text_fresh = manager.dashboard_text()
        self.assertIn("🟢", text_fresh)
        self.assertIn("Сессия", text_fresh)

    def test_history_render_chart_handles_missing_bucket_intervals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_hist.db"
            hist = TelemetryHistory(db_path)
            now = int(time.time())
            hist.insert({"timestamp": now - 3600, "cpu": {"load_percent": 25.0}})
            hist.insert({"timestamp": now, "cpu": {"load_percent": 15.0}})

            try:
                _, count = hist.render_chart(3600, "1 hour test")
                self.assertGreater(count, 2)
            except RuntimeError as exc:
                if "matplotlib" not in str(exc):
                    raise

    def test_graceful_shutdown_marks_shutdown_observed_label(self):
        now = time.time()
        self.health.update("windows_system", "online", ts=now - 50)
        self.health.update("windows_desktop", "online", ts=now - 50)

        self.health.set_graceful_shutdown(ts=now, reason="windows_shutdown")
        info = self.health.evaluate_pc_state(telemetry_age=None, now=now)
        self.assertEqual(info.state, PCState.SHUTDOWN_OBSERVED)
        self.assertEqual(info.label, "🔴 Выключен (штатно)")
        self.assertIn("ПК штатно завершил работу через Windows", info.reason)

    def test_newer_boot_heartbeat_clears_graceful_shutdown(self):
        now = time.time()
        self.health.set_graceful_shutdown(ts=now, reason="windows_shutdown")
        self.assertGreater(self.health.graceful_shutdown_until, 0.0)

        self.health.update("windows_system", "online", ts=now - 10)
        self.assertGreater(self.health.graceful_shutdown_until, 0.0)

        self.health.update("windows_system", "online", ts=now + 5)
        self.health.update("windows_session", "login_screen", ts=now + 5)
        self.assertEqual(self.health.graceful_shutdown_until, 0.0)
        info = self.health.evaluate_pc_state(telemetry_age=None, now=now + 5)
        self.assertEqual(info.state, PCState.LOCKED)

    def test_mqtt_gateway_handles_graceful_shutdown_payload(self):
        gateway = MqttGateway.__new__(MqttGateway)
        gateway.health = HealthEngine()
        gateway.health.update("mqtt_broker", "online")
        events = []
        gateway._emit_health_state = lambda: None
        gateway._emit = lambda name, data: events.append((name, data))

        now = time.time()
        payload = json.dumps({
            "state": "offline",
            "session": "offline",
            "event": "graceful_shutdown",
            "reason": "windows_shutdown",
            "agent": "system",
            "ts": now,
            "boot_id": "boot-test-123",
        }).encode("utf-8")

        message = type(
            "Message",
            (),
            {
                "topic": TOPIC_SYSTEM_STATUS,
                "payload": payload,
                "retain": True,
            },
        )()
        gateway._on_message(None, None, message)

        self.assertGreater(gateway.health.graceful_shutdown_until, 0.0)
        info = gateway.health.evaluate_pc_state(telemetry_age=None, now=now)
        self.assertEqual(info.state, PCState.SHUTDOWN_OBSERVED)
        self.assertTrue(any(ev[0] == "graceful_shutdown" for ev in events))

    def test_online_pc_with_offline_esp_evaluates_degraded(self):
        now = time.time()
        self.health.update("windows_system", "online", ts=now)
        self.health.update("windows_desktop", "online", ts=now)
        self.health.update("windows_session", "unlocked", ts=now)
        self.health.update("telemetry", "fresh", ts=now)
        self.health.update("esp", "offline", ts=now)

        info = self.health.evaluate_pc_state(telemetry_age=5.0, now=now)
        self.assertEqual(info.state, PCState.DEGRADED)
        self.assertEqual(info.label, "🟡 ESP недоступна")
        self.assertIn("модуль питания ESP8266 недоступен", info.reason)
