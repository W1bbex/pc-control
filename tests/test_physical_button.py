"""Tests for PhysicalButtonManager Morse eligibility, unlock gestures, diagnostics mode, and ACK guards."""

import time
import unittest
from unittest.mock import MagicMock

from vds_bot.physical_button import PhysicalButtonManager
from vds_bot.routers.physical_router import route_physical_callback


class TestPhysicalButtonManager(unittest.TestCase):
    def setUp(self):
        self.mock_ui = MagicMock()
        self.mock_mqtt = MagicMock()
        self.mgr = PhysicalButtonManager(self.mock_ui, self.mock_mqtt)

    def test_morse_unlock_ineligible_by_default(self):
        ok, reason = self.mgr.is_morse_unlock_eligible()
        self.assertFalse(ok)
        self.assertIn("не вводился", reason)

    def test_morse_unlock_eligible_within_five_minutes(self):
        now = time.time()
        self.mgr.handle_event({"event": "success"}, chat_id=123)
        ok, reason = self.mgr.is_morse_unlock_eligible(now=now + 120.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_morse_unlock_expires_after_five_minutes(self):
        now = time.time()
        self.mgr.handle_event({"event": "success"}, chat_id=123)
        ok, reason = self.mgr.is_morse_unlock_eligible(now=now + 301.0)
        self.assertFalse(ok)
        self.assertIn("более 5 минут", reason)

    def test_morse_eligibility_revoked_explicitly(self):
        self.mgr.handle_event({"event": "success"}, chat_id=123)
        self.mgr.revoke_morse_eligibility()
        ok, reason = self.mgr.is_morse_unlock_eligible()
        self.assertFalse(ok)
        self.assertIn("не вводился", reason)

    def test_double_click_gesture_triggers_and_consumes_unlock(self):
        triggered = []
        self.mgr.on_trigger_unlock_cb = lambda cid: triggered.append(cid)
        self.mgr.handle_event({"event": "success"}, chat_id=123)

        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.assertEqual(len(triggered), 0)
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 110}, chat_id=123)
        self.assertEqual(triggered, [123])
        self.assertFalse(self.mgr.physical_morse_unlock_eligible)

    def test_custom_unlock_pattern_matches_click_then_hold(self):
        triggered = []
        self.mgr.on_trigger_unlock_cb = lambda cid: triggered.append(cid)
        self.mgr.saved_unlock_pattern = "CH"
        self.mgr.handle_event({"event": "success"}, chat_id=123)

        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.assertEqual(len(triggered), 0)

        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "hold", "duration_ms": 600}, chat_id=123)
        self.assertEqual(len(triggered), 1)

    def test_hold_hold_alone_does_not_trigger_unlock_or_combo(self):
        unlock_triggered = []
        combo_triggered = []
        self.mgr.on_trigger_unlock_cb = lambda cid: unlock_triggered.append(cid)
        self.mgr.on_trigger_unlock_and_workspace_cb = lambda cid: combo_triggered.append(cid)

        self.mgr.handle_event({"event": "press", "kind": "hold", "duration_ms": 700}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "hold", "duration_ms": 750}, chat_id=123)

        self.assertEqual(len(unlock_triggered), 0)
        self.assertEqual(len(combo_triggered), 0)

    def test_click_click_then_hold_hold_triggers_unlock_and_workspace_combo(self):
        unlock_triggered = []
        combo_triggered = []
        self.mgr.on_trigger_unlock_cb = lambda cid: unlock_triggered.append(cid)
        self.mgr.on_trigger_unlock_and_workspace_cb = lambda cid: combo_triggered.append(cid)
        self.mgr.handle_event({"event": "success"}, chat_id=123)

        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 110}, chat_id=123)
        self.assertEqual(len(unlock_triggered), 1)
        self.assertEqual(len(combo_triggered), 0)

        self.mgr.handle_event({"event": "press", "kind": "hold", "duration_ms": 650}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "hold", "duration_ms": 700}, chat_id=123)
        self.assertEqual(combo_triggered, [123])

    def test_workspace_combo_rejected_when_morse_ineligible(self):
        combo_triggered = []
        self.mgr.on_trigger_unlock_and_workspace_cb = lambda cid: combo_triggered.append(cid)

        for kind, duration in [("click", 100), ("click", 110), ("hold", 600), ("hold", 650)]:
            self.mgr.handle_event({"event": "press", "kind": kind, "duration_ms": duration}, chat_id=123)

        self.assertEqual(len(combo_triggered), 0)
        self.assertEqual(len(self.mgr.gesture_buffer), 0)

    def test_single_click_after_debounce_does_not_retrigger_unlock(self):
        unlock_triggered = []
        self.mgr.on_trigger_unlock_cb = lambda cid: unlock_triggered.append(cid)
        self.mgr.handle_event({"event": "success"}, chat_id=123)

        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 110}, chat_id=123)
        self.assertEqual(len(unlock_triggered), 1)

        self.mgr.physical_morse_unlock_eligible = True
        self.mgr.last_unlock_triggered_at -= 3.5
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.assertEqual(len(unlock_triggered), 1)

        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.assertEqual(len(unlock_triggered), 2)

    def test_gesture_timeout_resets_consumed_count_for_subsequent_clicks(self):
        unlock_calls = []
        self.mgr.on_trigger_unlock_cb = lambda cid: unlock_calls.append(cid)

        self.mgr.handle_event({"event": "success"}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 110}, chat_id=123)
        self.assertEqual(len(unlock_calls), 1)
        self.assertEqual(self.mgr.last_unlock_consumed_count, 2)

        self.mgr.physical_morse_unlock_eligible = True
        self.mgr.gesture_buffer = [(s, t - 10.0) for (s, t) in self.mgr.gesture_buffer]
        self.mgr.last_unlock_triggered_at -= 10.0

        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.assertEqual(self.mgr.last_unlock_consumed_count, 0)
        self.assertEqual(len(self.mgr.gesture_buffer), 1)

        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.assertEqual(len(unlock_calls), 2)

    def test_clicks_without_morse_eligibility_do_not_trigger_unlock(self):
        unlock_triggered = []
        self.mgr.on_trigger_unlock_cb = lambda cid: unlock_triggered.append(cid)
        self.mgr.physical_morse_unlock_eligible = False

        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.assertEqual(len(unlock_triggered), 0)

    def test_failed_morse_notifies_user_with_attempt_counter(self):
        self.mgr.handle_event({"event": "failed", "failed_attempts": 1}, chat_id=123)
        self.mock_ui.send_message.assert_called()
        last_call_text = self.mock_ui.send_message.call_args[0][1]
        self.assertIn("Неверный пароль", last_call_text)
        self.assertIn("1 из 3", last_call_text)

    def test_failed_morse_shortly_after_unlock_gesture_is_suppressed(self):
        self.mgr.last_unlock_gesture_at = time.time()
        self.mock_ui.send_message.reset_mock()
        self.mgr.handle_event({"event": "failed", "failed_attempts": 1}, chat_id=123)
        self.mock_ui.send_message.assert_not_called()
        self.mock_mqtt.publish_esp_action.assert_called_with("UNLOCK_PHYSICAL_INPUT")

    def test_failed_morse_event_suppressed_and_unlocked_in_test_mode(self):
        self.mgr.mode = "test"
        self.mgr.handle_event({"event": "failed", "failed_attempts": 1}, 12345)
        self.assertEqual(self.mock_mqtt.publish_esp_action.call_args[0][0], "UNLOCK_PHYSICAL_INPUT")
        self.mock_ui.send_message.assert_not_called()

    def test_test_mode_suppresses_failed_and_locked_events(self):
        self.mgr.mode = "test"
        self.mgr.esp_locked = False

        self.mgr.handle_event({"event": "failed", "failed_attempts": 2}, 123456)
        self.assertFalse(self.mgr.esp_locked)
        self.mock_mqtt.publish_esp_action.assert_called_with("UNLOCK_PHYSICAL_INPUT")

        self.mgr.handle_event({"event": "locked"}, 123456)
        self.assertFalse(self.mgr.esp_locked)
        self.mock_ui.send_message.assert_not_called()

    def test_physical_test_callback_toggles_esp_diagnostics_mode(self):
        app = MagicMock()
        app.ui = self.mock_ui
        app.mqtt = self.mock_mqtt
        app.physical_btn = self.mgr

        self.assertTrue(route_physical_callback(app, "physical_test", 123456, 999, "cb_test"))
        self.assertEqual(self.mgr.mode, "test")
        self.mock_mqtt.publish_esp_action.assert_called_with("START_PHYSICAL_TEST")

        self.mock_mqtt.publish_esp_action.reset_mock()
        self.assertTrue(route_physical_callback(app, "physical_button", 123456, 999, "cb_back"))
        self.assertIsNone(self.mgr.mode)
        self.mock_mqtt.publish_esp_action.assert_called_with("STOP_PHYSICAL_TEST")

    def test_handle_ack_ignores_none_when_no_pending_command(self):
        self.mgr.pending_command_id = None
        self.mgr.saved_password = "EXISTING_PASSWORD"

        self.mgr.handle_ack({"id": None, "result": "executed"}, chat_id=123)
        self.assertEqual(self.mgr.saved_password, "EXISTING_PASSWORD")
        self.mock_ui.send_message.assert_not_called()
