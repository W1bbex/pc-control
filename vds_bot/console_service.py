#!/usr/bin/env python3
"""
Remote Console Execution Service for PC Control Bot.
Handles dispatching PowerShell / CMD commands to Desktop and SYSTEM agents, and formatting streaming outputs.
"""

import html
import logging
import threading
import time

LOG = logging.getLogger("pc-control-bot.console")


class ConsoleServiceMixin:
    """
    Mixin for PcControlBotApp providing remote terminal execution and command acknowledgements.
    """

    def execute_console_command(self, chat_id, cmd_text, target=None, shell=None):
        if not cmd_text or not cmd_text.strip():
            self.ui.send_message(chat_id, "⚠️ Команда не может быть пустой.")
            return

        cmd_text = cmd_text.strip()
        target = (target or getattr(self, "console_target", "desktop") or "desktop").lower()
        shell = (shell or getattr(self, "console_shell", "powershell") or "powershell").lower()

        # Check agent status
        if target == "desktop" and self.mqtt.desktop_status != "online":
            self.ui.send_message(
                chat_id,
                "❌ <b>Desktop-агент offline.</b>\nПереключитесь на <b>SYSTEM</b> или дождитесь разблокировки ПК.",
            )
            return
        elif target == "system" and self.mqtt.system_status != "online":
            self.ui.send_message(
                chat_id,
                "❌ <b>System-агент offline.</b>\nВключите ПК или проверьте службу boot_agent.",
            )
            return

        timeout_s = 60
        action = "powershell" if shell == "powershell" else "cmd"
        args = {"command": cmd_text, "timeout": timeout_s, "shell": shell}

        safe_cmd = html.escape(cmd_text)
        shell_badge = "PowerShell" if shell == "powershell" else "CMD"
        target_badge = "Desktop" if target == "desktop" else "SYSTEM"
        status_msg = self.ui.send_message(
            chat_id,
            f"⏳ [<b>{target_badge}</b> | <b>{shell_badge}</b>] Выполняю:\n<code>{safe_cmd}</code>",
        )
        status_msg_id = status_msg.get("message_id") if isinstance(status_msg, dict) else None

        pending_info = {
            "chat_id": chat_id,
            "message_id": status_msg_id,
            "command": cmd_text,
            "target": target,
            "shell": shell,
            "started_at": time.time(),
        }

        def _send_publish_failed() -> None:
            msg_fail = "❌ Не удалось отправить команду в MQTT (брокер недоступен)."
            if status_msg_id:
                self.ui.send_message(chat_id, msg_fail, message_id=status_msg_id)
            else:
                self.ui.send_message(chat_id, msg_fail)

        gen_id = getattr(type(self.mqtt), "generate_command_id", None)
        if callable(gen_id):
            cmd_id = self.mqtt.generate_command_id("sys" if target == "system" else "desk")
            with getattr(self, "state_lock", threading.RLock()):
                self.pending_console_cmds[cmd_id] = pending_info
            if target == "system":
                sent_id = self.mqtt.publish_system_action(action, args=args, timeout=timeout_s, command_id=cmd_id)
            else:
                sent_id = self.mqtt.publish_desktop_action(action, args=args, timeout=timeout_s, command_id=cmd_id)
            if not sent_id:
                with getattr(self, "state_lock", threading.RLock()):
                    self.pending_console_cmds.pop(cmd_id, None)
                _send_publish_failed()
            return

        if target == "system":
            cmd_id = self.mqtt.publish_system_action(action, args=args, timeout=timeout_s)
        else:
            cmd_id = self.mqtt.publish_desktop_action(action, args=args, timeout=timeout_s)

        if not cmd_id:
            _send_publish_failed()
            return

        early_ack_data = None
        with getattr(self, "state_lock", threading.RLock()):
            early_acks = getattr(self, "early_console_acks", None)
            if early_acks is not None and cmd_id in early_acks:
                early_ack_data = early_acks.pop(cmd_id)
            else:
                self.pending_console_cmds[cmd_id] = pending_info

        if early_ack_data is not None:
            self._handle_console_ack(pending_info, early_ack_data)

    def _handle_console_ack(self, pending_cmd, data):
        chat_id = pending_cmd["chat_id"]
        status_msg_id = pending_cmd.get("message_id")
        cmd_text = pending_cmd["command"]
        target = pending_cmd["target"]
        shell = pending_cmd["shell"]
        started_at = pending_cmd.get("started_at", time.time())
        elapsed = max(0.0, time.time() - started_at)

        ack_data = data.get("data")
        if not isinstance(ack_data, dict):
            ack_data = {"stderr": str(ack_data)} if ack_data else {}
        exit_code = ack_data.get("exit_code")
        if exit_code is None:
            exit_code = 0 if data.get("result") == "executed" else 1
        stdout = (ack_data.get("stdout") or "").strip()
        stderr = (ack_data.get("stderr") or "").strip()

        status_badge = "✅" if exit_code == 0 else f"❌ ({exit_code})"
        shell_badge = "PowerShell" if shell == "powershell" else "CMD"
        target_badge = "Desktop" if target == "desktop" else "SYSTEM"
        safe_cmd = cmd_text if len(cmd_text) <= 250 else cmd_text[:247] + "..."
        header = (
            f"💻 <b>Консоль [{target_badge} | {shell_badge}]</b>\n"
            f"Команда: <code>{html.escape(safe_cmd)}</code>\n"
            f"Статус: {status_badge} | Время: <code>{elapsed:.2f}s</code>\n"
        )

        full_output = ""
        if stdout:
            full_output += stdout
        if stderr:
            if full_output:
                full_output += "\n--- STDERR ---\n"
            full_output += stderr

        if not full_output:
            full_output = "(нет вывода)"

        escaped_output = html.escape(full_output)
        combined_len = len(header) + len(escaped_output) + 30
        if combined_len <= 3800:
            formatted_text = f"{header}\n<pre><code>{escaped_output}</code></pre>"
            res = None
            if status_msg_id:
                res = self.ui.send_message(chat_id, formatted_text, message_id=status_msg_id)
            if not res:
                self.ui.send_message(chat_id, formatted_text)
        else:
            max_preview_escaped = max(200, 3500 - len(header))
            raw_limit = min(1000, max_preview_escaped)
            preview = full_output[:raw_limit]
            while preview and len(html.escape(preview)) > max_preview_escaped:
                raw_limit = max(50, raw_limit - 50)
                preview = full_output[:raw_limit]
            escaped_preview = html.escape(preview)
            summary_text = (
                f"{header}\n"
                f"⚠️ <i>Вывод слишком большой ({len(full_output)} симв.). Полный вывод прикреплён файлом.</i>\n\n"
                f"<pre><code>{escaped_preview}\n... [см. вложенный файл]</code></pre>"
            )
            res = None
            if status_msg_id:
                res = self.ui.send_message(chat_id, summary_text, message_id=status_msg_id)
            if not res:
                self.ui.send_message(chat_id, summary_text)
            self.ui.send_document(
                chat_id,
                full_output.encode("utf-8"),
                filename="console_output.txt",
                caption=f"📄 Вывод: {html.escape(cmd_text[:40])}",
            )
