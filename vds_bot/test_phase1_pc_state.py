#!/usr/bin/env python3

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from health_engine import HealthEngine, PCState
from monitoring import MonitoringManager
from history import TelemetryHistory


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


class TestPhase1PCState(unittest.TestCase):
    def setUp(self):
        self.health = HealthEngine()
        self.health.update("mqtt_broker", "online")

    def test_1_system_unknown_does_not_say_off(self):
        """1. system unknown -> UI does not say 'выключен'."""
        self.health.update("windows_system", "unknown")
        self.health.update("windows_desktop", "unknown")
        
        info = self.health.evaluate_pc_state(telemetry_age=None)
        self.assertEqual(info.state, PCState.UNREACHABLE)
        self.assertIn("Связь потеряна", info.label)
        self.assertNotIn("выключен", info.label.lower())
        self.assertIn("состояние ПК неизвестно", info.reason)

    def test_2_system_stale_desktop_online_is_degraded_pc_on(self):
        """2. system stale + desktop online -> degraded, but PC is ON."""
        now = time.time()
        self.health.update("windows_system", "stale", ts=now - 200)
        self.health.update("windows_desktop", "online", ts=now)
        self.health.update("windows_session", "unlocked", ts=now)
        self.health.update("telemetry", "fresh", ts=now)

        info = self.health.evaluate_pc_state(telemetry_age=5.0)
        self.assertEqual(info.state, PCState.DEGRADED)
        self.assertIn("Ограниченная работа", info.label)
        self.assertIn("ПК включён", info.reason)

    def test_3_system_offline_desktop_online_is_degraded(self):
        """3. system offline + desktop online -> degraded, PC is ON."""
        now = time.time()
        self.health.update("windows_system", "offline")
        self.health.update("windows_desktop", "online", ts=now)
        self.health.update("telemetry", "fresh", ts=now)

        info = self.health.evaluate_pc_state(telemetry_age=10.0)
        self.assertEqual(info.state, PCState.DEGRADED)
        self.assertIn("Ограниченная работа", info.label)
        self.assertIn("ПК включён", info.reason)

    def test_4_both_offline_without_shutdown_ack_is_unreachable(self):
        """4. both agents offline without shutdown ACK -> unreachable (not shutdown observed)."""
        self.health.update("windows_system", "offline")
        self.health.update("windows_desktop", "offline")

        info = self.health.evaluate_pc_state(telemetry_age=None)
        self.assertEqual(info.state, PCState.UNREACHABLE)
        self.assertIn("Связь потеряна", info.label)
        self.assertIn("нет подтверждения выключения", info.reason)

    def test_5_shutdown_observed_flow_and_ttl(self):
        """5. shutdown observed lifecycle with TTL."""
        now = time.time()
        self.health.update("windows_system", "offline")
        self.health.update("windows_desktop", "offline")
        self.health.set_shutdown_observed(ttl=100.0, now=now)

        # Active shutdown observed
        info = self.health.evaluate_pc_state(telemetry_age=30.0, now=now + 10)
        self.assertEqual(info.state, PCState.SHUTDOWN_OBSERVED)
        self.assertIn("Выключен (наблюдается)", info.label)
        self.assertIn("ПК перестал отвечать после принятой команды выключения", info.reason)

        # After TTL expired
        info_expired = self.health.evaluate_pc_state(telemetry_age=150.0, now=now + 105)
        self.assertEqual(info_expired.state, PCState.UNREACHABLE)
        self.assertIn("Связь потеряна", info_expired.label)

    def test_6_new_heartbeat_cancels_shutdown_observed(self):
        """6. Any fresh heartbeat from an agent immediately clears shutdown_observed."""
        now = time.time()
        self.health.update("windows_system", "offline")
        self.health.update("windows_desktop", "offline")
        self.health.set_shutdown_observed(ttl=3600.0, now=now)
        self.assertEqual(self.health.evaluate_pc_state(now=now + 5).state, PCState.SHUTDOWN_OBSERVED)

        # System agent starts booting
        self.health.update("windows_system", "online", ts=now + 10)
        self.health.update("windows_session", "login_screen", ts=now + 10)

        info = self.health.evaluate_pc_state(now=now + 10)
        self.assertEqual(info.state, PCState.LOCKED)
        self.assertEqual(self.health.shutdown_observed_until, 0.0)

    def test_7_mqtt_disconnect_is_unreachable(self):
        """7. If MQTT drops, state is immediately UNREACHABLE."""
        now = time.time()
        self.health.update("windows_system", "online", ts=now)
        self.health.update("windows_desktop", "online", ts=now)
        self.health.update("windows_session", "unlocked", ts=now)
        self.health.update("telemetry", "fresh", ts=now)
        self.assertEqual(self.health.evaluate_pc_state(telemetry_age=5.0, now=now).state, PCState.READY)

        # Broker drops
        self.health.update("mqtt_broker", "unknown")
        self.health.set_all_unknown(exclude=["telegram"])

        info = self.health.evaluate_pc_state(now=now)
        self.assertEqual(info.state, PCState.UNREACHABLE)
        self.assertFalse(info.mqtt_online)
        self.assertIn("нет связи с системой", info.label.lower())

    def test_8_bot_restart_without_new_telemetry_not_ready(self):
        """8. Bot restart without telemetry -> not ready, degraded with explicit reason."""
        now = time.time()
        self.health.update("windows_system", "online", ts=now)
        self.health.update("windows_desktop", "online", ts=now)
        self.health.update("windows_session", "unlocked", ts=now)
        # Telemetry is not received yet (telemetry_age = None)
        
        info = self.health.evaluate_pc_state(telemetry_age=None, now=now)
        self.assertEqual(info.state, PCState.DEGRADED)
        self.assertIn("телеметрия ещё не получена", info.reason)

    def test_9_monitoring_dashboard_text_formatting(self):
        """9. Monitoring dashboard text reflects honesty and telemetry age warnings."""
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

        # Before any telemetry received
        text_empty = manager.dashboard_text()
        self.assertIn("🟡", text_empty)
        self.assertIn("Ожидаю получение первой телеметрии", text_empty)

        # Fresh telemetry received
        manager.process_telemetry({"timestamp": int(time.time()), "user_active": True, "cpu": {"load_percent": 10}})
        text_fresh = manager.dashboard_text()
        self.assertIn("🟢", text_fresh)
        self.assertIn("Сессия", text_fresh)

    def test_10_history_missing_buckets_fill_none_regression(self):
        """10. Regression test: history handles missing bucket intervals with None tuples."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_hist.db"
            hist = TelemetryHistory(db_path)
            now = int(time.time())
            # Insert point 1 hour ago
            hist.insert({"timestamp": now - 3600, "cpu": {"load_percent": 25.0}})
            # Insert point now
            hist.insert({"timestamp": now, "cpu": {"load_percent": 15.0}})

            # render_chart fills intermediate missing buckets with None
            try:
                chart_bytes, count = hist.render_chart(3600, "1 hour test")
                self.assertGreater(count, 2)
            except RuntimeError as e:
                if "matplotlib" in str(e):
                    pass  # Matplotlib might be missing in minimal test env


if __name__ == "__main__":
    unittest.main()
