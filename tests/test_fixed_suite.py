import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Add project root and vds_bot to sys.path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VDS_DIR = os.path.join(ROOT_DIR, "vds_bot")
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if VDS_DIR not in sys.path:
    sys.path.insert(0, VDS_DIR)


class TestFixedUnlockAndSession(unittest.TestCase):
    def test_type_password_constants_and_structures(self):
        import type_password
        self.assertTrue(hasattr(type_password, "log"))
        self.assertTrue(callable(type_password.log))

    def test_boot_agent_wtsinfoex_struct(self):
        import boot_agent
        self.assertTrue(hasattr(boot_agent, "WTSINFOEXW"))
        self.assertTrue(hasattr(boot_agent, "WTSINFOEX_LEVEL1_W"))

        # Query active console session in live environment
        session = boot_agent.active_console_session(ttl=0)
        self.assertIsInstance(session, dict)
        self.assertIn("session", session)
        self.assertIn(session["session"], {"unlocked", "locked", "login_screen", "no_console", "unknown"})
        self.assertIn("source", session)


class TestTelegramSecurity(unittest.TestCase):
    def test_token_redaction_on_requests_exception(self):
        from telegram_ui import TelegramBotApi
        fake_token = "9876543210:SECRET_TOKEN_ABCXYZ"
        api = TelegramBotApi(fake_token)

        with patch.object(api.session, "post", side_effect=Exception(f"ConnectionError to https://api.telegram.org/bot{fake_token}/getMe")):
            result = api.call_raw("getMe")
            self.assertFalse(result["ok"])
            self.assertNotIn(fake_token, result["description"])
            self.assertIn("[REDACTED_BOT_TOKEN]", result["description"])

    def test_call_logging_redacts_token(self):
        from telegram_ui import TelegramBotApi
        fake_token = "123456789:ANOTHER_SECRET_TOKEN"
        api = TelegramBotApi(fake_token)

        with patch.object(api.session, "post", side_effect=Exception(f"Timeout connecting to /bot{fake_token}/sendMessage")):
            res = api.call("sendMessage", {"chat_id": 123, "text": "test"})
            self.assertIsNone(res)


class TestHealthEngineResilience(unittest.TestCase):
    def test_string_ts_does_not_raise_typeerror(self):
        import time
        from health_engine import ComponentState
        comp = ComponentState("test_comp", 30)
        now = time.time()
        comp.last_ts = now - 5.0

        # Pass a string ts that could previously trigger TypeError: '<' not supported between str and float
        try:
            comp.update("online", ts=str(now))
            self.assertEqual(comp.status, "online")
            self.assertAlmostEqual(comp.last_ts, now, delta=0.1)
        except TypeError as e:
            self.fail(f"ComponentState.update raised TypeError on string ts: {e}")

        # Pass an unparseable ts
        comp.update("online", ts="invalid_string")
        self.assertIsNotNone(comp.status)


class TestAgentResilience(unittest.TestCase):
    def test_network_rates_handles_none_counters(self):
        import agent
        mock_agent = MagicMock()
        mock_agent.previous_net = MagicMock(bytes_recv=1000, bytes_sent=1000)
        mock_agent.previous_net_time = 100.0

        with patch("psutil.net_io_counters", return_value=None):
            rates = agent.PCControlAgent.network_rates(mock_agent)
            self.assertIsInstance(rates, dict)
            self.assertEqual(rates["download_mbps"], 0.0)
            self.assertEqual(rates["upload_mbps"], 0.0)


class TestPowerFlowRecovery(unittest.TestCase):
    def test_unlock_failed_recovers_to_waiting_workspace_decision(self):
        from power_flow import PowerFlowManager, PowerFlowStage
        mock_ui = MagicMock()
        mock_mqtt = MagicMock()

        with patch.object(PowerFlowManager, "load_state"):
            with patch.object(PowerFlowManager, "save_state"):
                flow = PowerFlowManager(mock_ui, mock_mqtt)
                flow.stage = PowerFlowStage.UNLOCK_FAILED
                flow.chat_id = 12345
                flow.on_system_session_update("unlocked", 12345)

                self.assertEqual(flow.stage, PowerFlowStage.WAITING_WORKSPACE_DECISION)


class TestDynamicMainMenu(unittest.TestCase):
    def setUp(self):
        from telegram_ui import TelegramBotApi
        self.ui = TelegramBotApi("123456:TEST_TOKEN")

    def test_unlocked_shows_shutdown_button(self):
        markup = self.ui.main_menu_markup(session="unlocked")
        bottom_btn = markup["inline_keyboard"][-1][0]
        self.assertEqual(bottom_btn["callback_data"], "shutdown_pc")
        self.assertIn("Выключить", bottom_btn["text"])

    def test_locked_shows_unlock_button(self):
        markup = self.ui.main_menu_markup(session="locked")
        bottom_btn = markup["inline_keyboard"][-1][0]
        self.assertEqual(bottom_btn["callback_data"], "unlock_windows_manual")
        self.assertIn("Разблокировать", bottom_btn["text"])

    def test_login_screen_shows_unlock_button(self):
        markup = self.ui.main_menu_markup(session="login_screen")
        bottom_btn = markup["inline_keyboard"][-1][0]
        self.assertEqual(bottom_btn["callback_data"], "unlock_windows_manual")
        self.assertIn("Разблокировать", bottom_btn["text"])

    def test_offline_system_shows_power_on_button(self):
        markup = self.ui.main_menu_markup(system_status="offline")
        bottom_btn = markup["inline_keyboard"][-1][0]
        self.assertEqual(bottom_btn["callback_data"], "power_on")
        self.assertIn("Включить", bottom_btn["text"])

    def test_workspace_button_not_in_main_menu(self):
        markup = self.ui.main_menu_markup(session="unlocked")
        callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
        self.assertNotIn("workspace_start_direct", callbacks)

    def test_apps_menu_shows_workspace_button(self):
        markup = self.ui.apps_menu_markup()
        first_btn = markup["inline_keyboard"][0][0]
        self.assertEqual(first_btn["callback_data"], "workspace_start_direct")
        self.assertIn("рабочий сетап", first_btn["text"].lower())

    def test_power_menu_offline_shows_only_power_on_and_back(self):
        markup = self.ui.power_menu_markup(system_status="offline")
        callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
        self.assertEqual(callbacks, ["power_on", "menu"])
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "Включить ПК")
        self.assertEqual(markup["inline_keyboard"][0][0]["style"], "success")

    def test_power_menu_online_unlocked_shows_shutdown_and_lock(self):
        markup = self.ui.power_menu_markup(session="unlocked", system_status="online")
        callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
        self.assertIn("shutdown_pc", callbacks)
        self.assertIn("lock_windows", callbacks)
        self.assertNotIn("power_on", callbacks)
        self.assertNotIn("unlock_windows_manual", callbacks)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "shutdown_pc")

    def test_power_menu_online_locked_shows_unlock(self):
        markup = self.ui.power_menu_markup(session="locked", system_status="online")
        callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
        self.assertIn("shutdown_pc", callbacks)
        self.assertIn("unlock_windows_manual", callbacks)
        self.assertNotIn("power_on", callbacks)
        self.assertNotIn("lock_windows", callbacks)

    def test_shutdown_observed_shows_power_on_button(self):
        from health_engine import PCState
        mock_state = MagicMock()
        mock_state.state = PCState.SHUTDOWN_OBSERVED
        mock_state.session_status = "offline"
        mock_state.system_status = "offline"
        markup = self.ui.main_menu_markup(pc_state=mock_state)
        bottom_btn = markup["inline_keyboard"][-1][0]
        self.assertEqual(bottom_btn["callback_data"], "power_on")
        self.assertIn("Включить", bottom_btn["text"])


class TestMorseUnlockGesture(unittest.TestCase):
    def setUp(self):
        from physical_button import PhysicalButtonManager
        self.mock_ui = MagicMock()
        self.mock_mqtt = MagicMock()
        self.mgr = PhysicalButtonManager(self.mock_ui, self.mock_mqtt)

    def test_ineligible_by_default(self):
        ok, reason = self.mgr.is_morse_unlock_eligible()
        self.assertFalse(ok)
        self.assertIn("не вводился", reason)

    def test_eligible_after_morse_success_within_5_minutes(self):
        import time
        now = time.time()
        self.mgr.handle_event({"event": "success"}, chat_id=123)
        ok, reason = self.mgr.is_morse_unlock_eligible(now=now + 120.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_rejected_after_5_minutes(self):
        import time
        now = time.time()
        self.mgr.handle_event({"event": "success"}, chat_id=123)
        ok, reason = self.mgr.is_morse_unlock_eligible(now=now + 301.0)
        self.assertFalse(ok)
        self.assertIn("более 5 минут", reason)

    def test_revoked_by_remote_or_bot(self):
        self.mgr.handle_event({"event": "success"}, chat_id=123)
        self.mgr.revoke_morse_eligibility()
        ok, reason = self.mgr.is_morse_unlock_eligible()
        self.assertFalse(ok)
        self.assertIn("не вводился", reason)

    def test_unlock_gesture_triggers_callback(self):
        triggered = []
        self.mgr.on_trigger_unlock_cb = lambda cid: triggered.append(cid)
        self.mgr.handle_event({"event": "success"}, chat_id=123)

        # Send 2 clicks ('C', 'C')
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.assertEqual(len(triggered), 0)
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 110}, chat_id=123)
        self.assertEqual(len(triggered), 1)
        self.assertEqual(triggered[0], 123)
        # Eligibility consumed
        self.assertFalse(self.mgr.physical_morse_unlock_eligible)

    def test_custom_unlock_pattern(self):
        triggered = []
        self.mgr.on_trigger_unlock_cb = lambda cid: triggered.append(cid)
        self.mgr.saved_unlock_pattern = "CH"  # Click then Hold
        self.mgr.handle_event({"event": "success"}, chat_id=123)

        # 2 clicks should NOT trigger
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.assertEqual(len(triggered), 0)

        # Click then Hold SHOULD trigger
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "hold", "duration_ms": 600}, chat_id=123)
        self.assertEqual(len(triggered), 1)

    def test_hold_hold_alone_does_nothing(self):
        """User requirement: 'холд холд ничего не означал' - HH alone must never trigger any callback."""
        unlock_triggered = []
        combo_triggered = []
        self.mgr.on_trigger_unlock_cb = lambda cid: unlock_triggered.append(cid)
        self.mgr.on_trigger_unlock_and_workspace_cb = lambda cid: combo_triggered.append(cid)

        # Send 2 holds ('H', 'H')
        self.mgr.handle_event({"event": "press", "kind": "hold", "duration_ms": 700}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "hold", "duration_ms": 750}, chat_id=123)

        self.assertEqual(len(unlock_triggered), 0, "HH alone must NOT trigger unlock")
        self.assertEqual(len(combo_triggered), 0, "HH alone must NOT trigger workspace combo")

    def test_unlock_and_workspace_gesture_combo(self):
        """User specification: click click + hold hold = unlock + workspace setup (CCHH)."""
        unlock_triggered = []
        combo_triggered = []
        self.mgr.on_trigger_unlock_cb = lambda cid: unlock_triggered.append(cid)
        self.mgr.on_trigger_unlock_and_workspace_cb = lambda cid: combo_triggered.append(cid)
        self.mgr.handle_event({"event": "success"}, chat_id=123)

        # 1. Click, Click -> triggers unlock immediately
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 110}, chat_id=123)
        self.assertEqual(len(unlock_triggered), 1)
        self.assertEqual(len(combo_triggered), 0)

        # 2. Hold, Hold follows within gesture window -> triggers unlock + workspace combo
        self.mgr.handle_event({"event": "press", "kind": "hold", "duration_ms": 650}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "hold", "duration_ms": 700}, chat_id=123)
        self.assertEqual(len(combo_triggered), 1)
        self.assertEqual(combo_triggered[0], 123)

    def test_single_trailing_click_after_debounce_does_not_retrigger_unlock(self):
        """Audit finding fix: after CC triggers unlock, a single C 3.2s later must NOT retrigger unlock."""
        import time
        unlock_triggered = []
        self.mgr.on_trigger_unlock_cb = lambda cid: unlock_triggered.append(cid)
        self.mgr.handle_event({"event": "success"}, chat_id=123)

        # 1. First CC combo triggers unlock
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 110}, chat_id=123)
        self.assertEqual(len(unlock_triggered), 1)

        # Re-grant eligibility for testing subsequent click sequence
        self.mgr.physical_morse_unlock_eligible = True

        # 2. Simulate 3.2s passing (debounce elapsed) and a single click occurs
        self.mgr.last_unlock_triggered_at -= 3.5
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)

        # Must still be 1 (single click must NOT trigger unlock)
        self.assertEqual(len(unlock_triggered), 1, "Single click must NOT re-trigger unlock!")

        # 3. Second click arrives -> now it should trigger a second unlock
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.assertEqual(len(unlock_triggered), 2, "Second click completes CC and triggers unlock")

    def test_press_without_morse_does_not_trigger_unlock(self):
        """When PC is off / morse not entered, CC clicks must NOT trigger unlock."""
        unlock_triggered = []
        self.mgr.on_trigger_unlock_cb = lambda cid: unlock_triggered.append(cid)
        self.mgr.physical_morse_unlock_eligible = False

        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.mgr.handle_event({"event": "press", "kind": "click", "duration_ms": 100}, chat_id=123)
        self.assertEqual(len(unlock_triggered), 0)

    def test_failed_morse_notifies_user(self):
        """When incorrect morse is typed, failed event sends Telegram alert."""
        self.mgr.handle_event({"event": "failed", "failed_attempts": 1}, chat_id=123)
        self.mock_ui.send_message.assert_called()
        last_call_text = self.mock_ui.send_message.call_args[0][1]
        self.assertIn("Неверный пароль", last_call_text)
        self.assertIn("1 из 3", last_call_text)

    def test_failed_after_recent_unlock_gesture_is_suppressed(self):
        """When failed event arrives shortly after an unlock gesture, it is suppressed."""
        import time
        self.mgr.last_unlock_gesture_at = time.time()
        self.mock_ui.send_message.reset_mock()
        self.mgr.handle_event({"event": "failed", "failed_attempts": 1}, chat_id=123)
        self.mock_ui.send_message.assert_not_called()
        self.mock_mqtt.publish_esp_action.assert_called_with("UNLOCK_PHYSICAL_INPUT")


class TestPowerFlowWorkspaceAutoLaunch(unittest.TestCase):
    def test_on_unlock_success_auto_launches_workspace_when_flag_set(self):
        from power_flow import PowerFlowManager, PowerFlowStage
        mock_ui = MagicMock()
        mock_mqtt = MagicMock()
        with patch.object(PowerFlowManager, "load_state"):
            with patch.object(PowerFlowManager, "save_state"):
                flow = PowerFlowManager(mock_ui, mock_mqtt)
                ws_started = []
                flow.on_workspace_start_cb = lambda cid, card_message_id=None, workspace_id=None, vpn="incy": ws_started.append((cid, workspace_id, vpn))
                flow.auto_launch_workspace = True
                flow.target_workspace_vpn = "happ"
                flow.stage = PowerFlowStage.UNLOCKING

                flow.on_unlock_success(12345)

                self.assertEqual(flow.stage, PowerFlowStage.WORKSPACE_RUNNING)
                self.assertFalse(flow.auto_launch_workspace)
                self.assertEqual(len(ws_started), 1)
                self.assertEqual(ws_started[0][0], 12345)
                self.assertEqual(ws_started[0][2], "happ")

    def test_on_unlock_success_normal_flow_when_flag_false(self):
        from power_flow import PowerFlowManager, PowerFlowStage
        mock_ui = MagicMock()
        mock_mqtt = MagicMock()
        with patch.object(PowerFlowManager, "load_state"):
            with patch.object(PowerFlowManager, "save_state"):
                flow = PowerFlowManager(mock_ui, mock_mqtt)
                ws_started = []
                flow.on_workspace_start_cb = lambda cid, card_message_id=None, workspace_id=None: ws_started.append(cid)
                flow.auto_launch_workspace = False
                flow.stage = PowerFlowStage.UNLOCKING

                flow.on_unlock_success(12345)

                self.assertEqual(flow.stage, PowerFlowStage.WAITING_WORKSPACE_DECISION)
                self.assertEqual(len(ws_started), 0)
                self.assertTrue(mock_ui.send_message.called)


class TestWorkspaceConcurrencyGuard(unittest.TestCase):
    def test_start_async_workspace_setup_blocks_concurrent_runs(self):
        import time
        import threading
        from bot import PcControlBotApp
        with patch.object(PcControlBotApp, "__init__", return_value=None):
            app = PcControlBotApp()
            app._workspace_start_lock = threading.Lock()
            app._workspace_running = False
            runs = []

            def fake_runner(chat_id, card_message_id=None, workspace_id=None):
                runs.append(workspace_id)
                time.sleep(0.1)

            app.run_workspace_setup_steps = fake_runner

            # First invocation launches thread and sets running
            app.start_async_workspace_setup(123, workspace_id="ws-1")
            time.sleep(0.02)
            self.assertTrue(app._workspace_running)

            # Second concurrent invocation must be rejected
            app.start_async_workspace_setup(123, workspace_id="ws-2")
            self.assertEqual(len(runs), 1)

            # Wait for thread to finish
            time.sleep(0.15)
            self.assertFalse(app._workspace_running)


if __name__ == "__main__":
    unittest.main()
