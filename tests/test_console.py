import html
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure vds_bot is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "vds_bot"))

_ORIG_ENV = {
    k: os.environ.get(k)
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_ID")
}
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test_token_123")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "123456789")
try:
    from vds_bot.telegram_ui import TelegramBotApi
    from vds_bot.bot import PcControlBotApp, ALLOWED_USER_ID
finally:
    for _k, _v in _ORIG_ENV.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v


class TestConsoleUI(unittest.TestCase):
    def setUp(self):
        self.ui = TelegramBotApi("test_token")

    def test_console_card_text_idle(self):
        text = self.ui.console_card_text(target="desktop", shell="powershell", is_executing=False)
        self.assertIn("Консоль Windows", text)
        self.assertIn("Desktop (User)", text)
        self.assertIn("PowerShell", text)
        self.assertIn("Готова к вводу", text)
        self.assertIn("/cmd", text)
        self.assertIn("/ps", text)
        self.assertIn("/exit", text)

    def test_console_card_text_executing(self):
        text = self.ui.console_card_text(
            target="system", shell="cmd", last_cmd="taskkill /F /IM test.exe", is_executing=True
        )
        self.assertIn("SYSTEM (Admin)", text)
        self.assertIn("CMD", text)
        self.assertIn("Выполняется...", text)
        self.assertIn("taskkill /F /IM test.exe", text)

    def test_console_menu_markup(self):
        markup = self.ui.console_menu_markup(target="desktop", shell="powershell")
        buttons = markup["inline_keyboard"]
        self.assertEqual(len(buttons), 4)

        # Row 1: Toggles
        r1 = buttons[0]
        self.assertEqual(r1[0]["callback_data"], "console_toggle_shell")
        self.assertIn("PowerShell", r1[0]["text"])
        self.assertEqual(r1[1]["callback_data"], "console_toggle_target")
        self.assertIn("Desktop", r1[1]["text"])

        # Row 2 & 3: Quick commands
        quick_cmds = [b["callback_data"] for row in (buttons[1], buttons[2]) for b in row]
        self.assertIn("console_quick:whoami", quick_cmds)
        self.assertIn("console_quick:ipconfig", quick_cmds)
        self.assertIn("console_quick:tasklist", quick_cmds)
        self.assertIn("console_quick:dir", quick_cmds)

        # Row 4: Close button
        r4 = buttons[3]
        self.assertEqual(r4[0]["callback_data"], "console_close")
        self.assertIn("icon_custom_emoji_id", r4[0])

    def test_apps_menu_markup_contains_console(self):
        markup = self.ui.apps_menu_markup()
        console_btns = [
            btn
            for row in markup["inline_keyboard"]
            for btn in row
            if btn["callback_data"] == "console_open"
        ]
        self.assertEqual(len(console_btns), 1)
        self.assertIn("icon_custom_emoji_id", console_btns[0])

    def test_settings_menu_markup_contains_console(self):
        markup = self.ui.settings_menu_markup()
        all_cbs = [
            btn["callback_data"]
            for row in markup["inline_keyboard"]
            for btn in row
        ]
        self.assertIn("console_open", all_cbs)


class TestConsoleApp(unittest.TestCase):
    def setUp(self):
        with patch.object(PcControlBotApp, "load_state"):
            self.app = PcControlBotApp()
        self.app.ui = MagicMock()
        self.app.mqtt = MagicMock()
        self.app.mqtt.desktop_status = "online"
        self.app.mqtt.system_status = "online"

    def test_security_unauthorized_user_blocked(self):
        # Non-allowed user message
        msg = {
            "chat": {"id": 999999999, "type": "private"},
            "from": {"id": 999999999},
            "text": "/cmd whoami",
        }
        self.app.handle_message(msg)
        self.app.ui.send_message.assert_called_with(999999999, "Доступ запрещён.")
        self.app.mqtt.publish_desktop_action.assert_not_called()

        # Non-allowed callback
        cb = {
            "id": "cb1",
            "message": {"chat": {"id": 999999999}, "message_id": 10},
            "from": {"id": 999999999},
            "data": "console_open",
        }
        self.app.handle_callback(cb)
        self.app.ui.answer_callback.assert_called_with("cb1", "Отказано", alert=True)

    def test_cmd_one_shot(self):
        self.app.mqtt.publish_desktop_action.return_value = "cmd-123"
        self.app.ui.send_message.return_value = {"message_id": 42}

        msg = {
            "chat": {"id": ALLOWED_USER_ID, "type": "private"},
            "from": {"id": ALLOWED_USER_ID},
            "text": "/cmd ipconfig /all",
        }
        self.app.handle_message(msg)

        self.app.mqtt.publish_desktop_action.assert_called_once_with(
            "cmd",
            args={"command": "ipconfig /all", "timeout": 60, "shell": "cmd"},
            timeout=60,
        )
        self.assertIn("cmd-123", self.app.pending_console_cmds)
        pending = self.app.pending_console_cmds["cmd-123"]
        self.assertEqual(pending["command"], "ipconfig /all")
        self.assertEqual(pending["shell"], "cmd")
        self.assertEqual(pending["target"], "desktop")

    def test_ps_one_shot(self):
        self.app.console_target = "system"
        self.app.mqtt.publish_system_action.return_value = "sys-456"
        self.app.ui.send_message.return_value = {"message_id": 43}

        msg = {
            "chat": {"id": ALLOWED_USER_ID, "type": "private"},
            "from": {"id": ALLOWED_USER_ID},
            "text": "/ps Get-Process | Select-Object -First 5",
        }
        self.app.handle_message(msg)

        self.app.mqtt.publish_system_action.assert_called_once_with(
            "powershell",
            args={"command": "Get-Process | Select-Object -First 5", "timeout": 60, "shell": "powershell"},
            timeout=60,
        )
        self.assertIn("sys-456", self.app.pending_console_cmds)
        pending = self.app.pending_console_cmds["sys-456"]
        self.assertEqual(pending["shell"], "powershell")
        self.assertEqual(pending["target"], "system")

    def test_interactive_console_mode_and_exit(self):
        # Open console via command
        msg = {
            "chat": {"id": ALLOWED_USER_ID, "type": "private"},
            "from": {"id": ALLOWED_USER_ID},
            "text": "/console",
        }
        self.app.handle_message(msg)
        self.assertEqual(self.app.console_active_chat_id, ALLOWED_USER_ID)
        self.assertEqual(self.app.current_menu_view, "console")

        # In interactive mode, send plain text command without slash
        self.app.mqtt.publish_desktop_action.return_value = "cmd-789"
        self.app.ui.send_message.return_value = {"message_id": 44}
        msg_cmd = {
            "chat": {"id": ALLOWED_USER_ID, "type": "private"},
            "from": {"id": ALLOWED_USER_ID},
            "text": "dir C:\\",
        }
        self.app.handle_message(msg_cmd)
        self.app.mqtt.publish_desktop_action.assert_called_once()
        self.assertIn("cmd-789", self.app.pending_console_cmds)

        # Exit interactive mode
        msg_exit = {
            "chat": {"id": ALLOWED_USER_ID, "type": "private"},
            "from": {"id": ALLOWED_USER_ID},
            "text": "/exit",
        }
        self.app.handle_message(msg_exit)
        self.assertIsNone(self.app.console_active_chat_id)

    def test_toggles_and_callbacks(self):
        # Open console via callback
        cb_open = {
            "id": "cb2",
            "message": {"chat": {"id": ALLOWED_USER_ID}, "message_id": 50},
            "from": {"id": ALLOWED_USER_ID},
            "data": "console_open",
        }
        self.app.handle_callback(cb_open)
        self.assertEqual(self.app.console_active_chat_id, ALLOWED_USER_ID)

        # Toggle shell
        self.assertEqual(self.app.console_shell, "powershell")
        cb_shell = {
            "id": "cb3",
            "message": {"chat": {"id": ALLOWED_USER_ID}, "message_id": 50},
            "from": {"id": ALLOWED_USER_ID},
            "data": "console_toggle_shell",
        }
        self.app.handle_callback(cb_shell)
        self.assertEqual(self.app.console_shell, "cmd")

        # Toggle target
        self.assertEqual(self.app.console_target, "desktop")
        cb_target = {
            "id": "cb4",
            "message": {"chat": {"id": ALLOWED_USER_ID}, "message_id": 50},
            "from": {"id": ALLOWED_USER_ID},
            "data": "console_toggle_target",
        }
        self.app.handle_callback(cb_target)
        self.assertEqual(self.app.console_target, "system")

        # Quick command callback
        self.app.mqtt.publish_system_action.return_value = "cmd-quick"
        cb_quick = {
            "id": "cb5",
            "message": {"chat": {"id": ALLOWED_USER_ID}, "message_id": 50},
            "from": {"id": ALLOWED_USER_ID},
            "data": "console_quick:whoami",
        }
        self.app.handle_callback(cb_quick)
        self.app.mqtt.publish_system_action.assert_called_with(
            "cmd",
            args={"command": "whoami", "timeout": 60, "shell": "cmd"},
            timeout=60,
        )

        # Close console callback
        cb_close = {
            "id": "cb6",
            "message": {"chat": {"id": ALLOWED_USER_ID}, "message_id": 50},
            "from": {"id": ALLOWED_USER_ID},
            "data": "console_close",
        }
        self.app.handle_callback(cb_close)
        self.assertIsNone(self.app.console_active_chat_id)

    def test_offline_warning(self):
        self.app.mqtt.desktop_status = "offline"
        msg = {
            "chat": {"id": ALLOWED_USER_ID, "type": "private"},
            "from": {"id": ALLOWED_USER_ID},
            "text": "/cmd echo hello",
        }
        self.app.handle_message(msg)
        self.app.mqtt.publish_desktop_action.assert_not_called()
        args, _ = self.app.ui.send_message.call_args
        self.assertIn("Desktop-агент offline", args[1])

    def test_ack_handling_inline_short_output(self):
        pending = {
            "chat_id": ALLOWED_USER_ID,
            "message_id": 100,
            "command": "echo test",
            "target": "desktop",
            "shell": "cmd",
            "started_at": time.time() - 1.5,
        }
        ack_data = {
            "id": "cmd-test-1",
            "result": "executed",
            "data": {
                "exit_code": 0,
                "stdout": "test\n",
                "stderr": "",
            },
        }
        self.app._handle_console_ack(pending, ack_data)
        # Should call send_message with message_id=100
        self.app.ui.send_message.assert_called()
        call_args = self.app.ui.send_message.call_args
        text = call_args[0][1]
        self.assertIn("echo test", text)
        self.assertIn("test", text)
        self.assertIn("✅", text)
        self.app.ui.send_document.assert_not_called()

    def test_ack_handling_long_output_sends_document(self):
        pending = {
            "chat_id": ALLOWED_USER_ID,
            "message_id": 101,
            "command": "dir C:\\Windows",
            "target": "desktop",
            "shell": "powershell",
            "started_at": time.time() - 2.0,
        }
        long_stdout = "A" * 4000
        ack_data = {
            "id": "cmd-test-2",
            "result": "executed",
            "data": {
                "exit_code": 0,
                "stdout": long_stdout,
                "stderr": "",
            },
        }
        self.app._handle_console_ack(pending, ack_data)
        # Should call send_document because output > 3500 chars
        self.app.ui.send_document.assert_called_once()
        doc_args = self.app.ui.send_document.call_args
        self.assertEqual(doc_args[0][0], ALLOWED_USER_ID)
        self.assertEqual(doc_args[1]["filename"], "console_output.txt")
        self.assertEqual(len(doc_args[0][1]), 4000)

    def test_command_timeout_event(self):
        cmd_id = "cmd-timeout-1"
        self.app.pending_console_cmds[cmd_id] = {
            "chat_id": ALLOWED_USER_ID,
            "message_id": 102,
            "command": "sleep 100",
            "target": "desktop",
            "shell": "cmd",
            "started_at": time.time() - 60.0,
        }
        self.app._on_mqtt_event("command_timeout", {"id": cmd_id})
        self.assertNotIn(cmd_id, self.app.pending_console_cmds)
        args, _ = self.app.ui.send_message.call_args
        self.assertIn("Таймаут выполнения команды", args[1])

    def test_handle_console_ack_string_data_graceful(self):
        pending = {
            "chat_id": ALLOWED_USER_ID,
            "message_id": 105,
            "command": "dir",
            "target": "desktop",
            "shell": "cmd",
            "started_at": time.time(),
        }
        ack_data = {
            "id": "cmd-str-1",
            "result": "failed",
            "data": "Process execution failed",
        }
        self.app._handle_console_ack(pending, ack_data)
        self.app.ui.send_message.assert_called()
        args, _ = self.app.ui.send_message.call_args
        self.assertIn("Process execution failed", args[1])


if __name__ == "__main__":
    unittest.main()
