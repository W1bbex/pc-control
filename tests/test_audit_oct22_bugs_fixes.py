import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch

from vds_bot.power_flow import PowerFlowManager, PowerFlowStage
from vds_bot.routers.workspace_router import route_workspace_callback
from vds_bot.routers.physical_router import route_physical_callback
from vds_bot.routers.monitoring_router import route_monitoring_callback
from vds_bot.routers.command_router import route_message
from vds_bot.physical_button import PhysicalButtonManager


class TestOct22BugFixes(unittest.TestCase):
    def setUp(self):
        self.ui = MagicMock()
        self.mqtt = MagicMock()
        self.mqtt.esp_status = "online"
        self.chat_id = 123456
        self.message_id = 999

    # --- BUG 1: Unlock + Setup card kickback fix ---
    def test_bug1_selecting_mode_prevents_periodic_card_overwrite(self):
        flow = PowerFlowManager(self.ui, self.mqtt)
        flow.chat_id = self.chat_id
        flow.message_id = self.message_id
        flow.stage = PowerFlowStage.POWERING_ON
        flow.last_card_update_at = time.time() - 10

        app = MagicMock()
        app.power_flow = flow
        app.ui = self.ui
        app.chosen_vpn = "incy"

        # 1. User clicks "Разблок + Сетап"
        handled = route_workspace_callback(app, "flow_unlock_and_workspace", self.chat_id, self.message_id, "cb_1")
        self.assertTrue(handled)
        self.assertTrue(flow.selecting_mode)

        # 2. Background tick arrives (force=False) while user is viewing VPN buttons
        self.ui.send_message.reset_mock()
        flow._update_launch_card(force=False)
        # MUST NOT update/overwrite card while selecting_mode is active!
        self.ui.send_message.assert_not_called()

        # 3. User chooses VPN
        handled = route_workspace_callback(app, "flow_workspace_vpn:incy", self.chat_id, self.message_id, "cb_2")
        self.assertTrue(handled)
        self.assertFalse(flow.selecting_mode)
        self.assertTrue(flow.auto_launch_workspace)
        self.assertEqual(flow.target_workspace_vpn, "incy")

    def test_bug1_game_selecting_mode_lifecycle(self):
        flow = PowerFlowManager(self.ui, self.mqtt)
        app = MagicMock()
        app.power_flow = flow
        app.ui = self.ui

        # Click unlock and game
        route_workspace_callback(app, "flow_unlock_and_game", self.chat_id, self.message_id, "cb_g1")
        self.assertTrue(flow.selecting_mode)

        # Pick game
        route_workspace_callback(app, "flow_game:minecraft", self.chat_id, self.message_id, "cb_g2")
        self.assertTrue(flow.selecting_mode)

        # Pick VPN -> clears selecting_mode
        route_workspace_callback(app, "flow_game_vpn:minecraft:happ", self.chat_id, self.message_id, "cb_g3")
        self.assertFalse(flow.selecting_mode)
        self.assertTrue(flow.auto_launch_game)
        self.assertEqual(flow.target_game, "minecraft")
        self.assertEqual(flow.target_game_vpn, "happ")

    # --- BUG 2: Button diagnostics vs Morse code password ---
    def test_bug2_physical_test_notifies_esp_and_stops_on_exit(self):
        app = MagicMock()
        app.ui = self.ui
        app.mqtt = self.mqtt
        phys_btn = PhysicalButtonManager(self.ui, self.mqtt)
        app.physical_btn = phys_btn

        # 1. Enter diagnostics
        handled = route_physical_callback(app, "physical_test", self.chat_id, self.message_id, "cb_test")
        self.assertTrue(handled)
        self.assertEqual(phys_btn.mode, "test")
        self.mqtt.publish_esp_action.assert_called_with("START_PHYSICAL_TEST")

        # 2. Exit diagnostics back to physical menu
        self.mqtt.publish_esp_action.reset_mock()
        handled = route_physical_callback(app, "physical_button", self.chat_id, self.message_id, "cb_back")
        self.assertTrue(handled)
        self.assertIsNone(phys_btn.mode)
        self.mqtt.publish_esp_action.assert_called_with("STOP_PHYSICAL_TEST")

    def test_bug2_physical_button_suppresses_failed_and_locked_in_test_mode(self):
        phys_btn = PhysicalButtonManager(self.ui, self.mqtt)
        phys_btn.mode = "test"
        phys_btn.esp_locked = False

        # Simulate ESP sending 'failed' during test mode
        phys_btn.handle_event({"event": "failed", "failed_attempts": 2}, self.chat_id)
        self.assertFalse(phys_btn.esp_locked)
        self.mqtt.publish_esp_action.assert_called_with("UNLOCK_PHYSICAL_INPUT")

        # Simulate ESP sending 'locked' during test mode
        phys_btn.handle_event({"event": "locked"}, self.chat_id)
        self.assertFalse(phys_btn.esp_locked)
        # Did not display lock message to user
        self.ui.send_message.assert_not_called()

    # --- BUG 3: Cooler ARGB Backlight Persistence & Zero Overwrite ---
    def test_bug3_cooler_led_off_persists_and_suppresses_start_flow_led_on(self):
        app = MagicMock()
        app.ui = self.ui
        app.mqtt = self.mqtt
        app.user_led_state = "on"
        app.user_led_color = "255,150,125"
        app.save_state = MagicMock()

        # 1. User turns off cooler backlight in UI
        handled = route_monitoring_callback(app, "led_action:off", self.chat_id, self.message_id, "cb_off")
        self.assertTrue(handled)
        self.assertEqual(app.user_led_state, "off")
        app.save_state.assert_called()
        self.mqtt.publish_esp_action.assert_called_with("LED_OFF")

        # 2. User powers on PC -> PowerFlowManager.start_power_on()
        flow = PowerFlowManager(self.ui, self.mqtt)
        flow.app = app
        self.mqtt.publish_esp_action.reset_mock()

        flow.start_power_on(self.chat_id)
        # MUST NOT send LED_ON because user_led_state is 'off'!
        for call_args in self.mqtt.publish_esp_action.call_args_list:
            self.assertNotEqual(call_args[0][0], "LED_ON")
            self.assertNotEqual(call_args[0][0], "LED_COLOR")

    def test_bug3_cooler_led_custom_color_persists_and_restores_on_start(self):
        app = MagicMock()
        app.ui = self.ui
        app.mqtt = self.mqtt
        app.user_led_state = "off"
        app.user_led_color = None
        app.save_state = MagicMock()

        # 1. User picks preset #3 ("255,130,155")
        handled = route_monitoring_callback(app, "led_action:preset:3", self.chat_id, self.message_id, "cb_color")
        self.assertTrue(handled)
        self.assertEqual(app.user_led_state, "on")
        self.assertEqual(app.user_led_color, "255,130,155")
        app.save_state.assert_called()
        self.mqtt.publish_esp_action.assert_called_with("LED_COLOR", value="255,130,155")

        # 2. User starts flow -> should restore custom color
        flow = PowerFlowManager(self.ui, self.mqtt)
        flow.app = app
        self.mqtt.publish_esp_action.reset_mock()

        flow.start_power_on(self.chat_id)
        self.mqtt.publish_esp_action.assert_called_with("LED_COLOR", value="255,130,155")

    def test_bug3_slash_commands_update_user_led_state(self):
        app = MagicMock()
        app.ui = self.ui
        app.mqtt = self.mqtt
        app.mqtt.esp_status = "online"
        app.user_led_state = "on"
        app.allowed_user_id = self.chat_id
        app.save_state = MagicMock()

        # /led_off
        msg_off = {"chat": {"id": self.chat_id}, "from": {"id": self.chat_id}, "text": "/led_off"}
        route_message(app, msg_off)
        self.assertEqual(app.user_led_state, "off")
        app.save_state.assert_called()
        self.mqtt.publish_esp_action.assert_called_with("LED_OFF")

        # /led_on
        app.save_state.reset_mock()
        self.mqtt.publish_esp_action.reset_mock()
        msg_on = {"chat": {"id": self.chat_id}, "from": {"id": self.chat_id}, "text": "/led_on"}
        route_message(app, msg_on)
        self.assertEqual(app.user_led_state, "on")
        app.save_state.assert_called()


if __name__ == '__main__':
    unittest.main()
