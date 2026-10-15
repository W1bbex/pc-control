"""Action execution and command validation mixin for desktop agent."""

import ctypes
import logging
import os
import subprocess
import threading
import time

LOG = logging.getLogger("pc-control-agent")


class ActionDispatcherMixin:
    """Dispatches incoming remote MQTT commands to corresponding handlers."""

    def execute_action(self, command_id, action, args):
        try:
            if action == "telemetry":
                data = self.collect_telemetry()
                self.acknowledge(command_id, "executed", data)
                return

            if action == "top_processes":
                self.acknowledge(
                    command_id,
                    "executed",
                    {"processes": self.top_processes()},
                )
                return

            if action == "screenshot":
                data = self.make_screenshot(command_id)
                self.acknowledge(command_id, "executed", data)
                return

            if action == "lock":
                self.acknowledge(command_id, "executed")
                ctypes.windll.user32.LockWorkStation()
                return

            if action == "sleep":
                try:
                    self.mqtt.publish("pc/main/command", f"LED_OFF|agent-sleep-{int(time.time())}|{int(time.time())+30}", qos=1)
                except Exception:
                    pass
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                ctypes.windll.powrprof.SetSuspendState(False, False, False)
                return

            if action == "hibernate":
                try:
                    self.mqtt.publish("pc/main/command", f"LED_OFF|agent-hib-{int(time.time())}|{int(time.time())+30}", qos=1)
                except Exception:
                    pass
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                subprocess.Popen(["shutdown.exe", "/h"])
                return

            if action == "shutdown":
                try:
                    self.mqtt.publish("pc/main/command", f"LED_OFF|agent-shut-{int(time.time())}|{int(time.time())+30}", qos=1)
                except Exception:
                    pass
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                subprocess.Popen(
                    [
                        "shutdown.exe",
                        "/s",
                        "/t",
                        "5",
                        "/c",
                        "PC CONTROL: удалённое выключение",
                    ]
                )
                return

            if action == "restart":
                try:
                    self.mqtt.publish("pc/main/command", f"LED_OFF|agent-rst-{int(time.time())}|{int(time.time())+30}", qos=1)
                except Exception:
                    pass
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                subprocess.Popen(
                    [
                        "shutdown.exe",
                        "/r",
                        "/t",
                        "5",
                        "/c",
                        "PC CONTROL: удалённая перезагрузка",
                    ]
                )
                return

            if action == "launch_app":
                def report_progress(data):
                    self.acknowledge(command_id, "progress", data)

                ws_id = str(args.get("workspace_id", "")) or command_id
                res = self.launch_app(
                    args.get("app"),
                    workspace_id=ws_id,
                    progress_callback=report_progress,
                    vpn=args.get("vpn"),
                )
                if isinstance(res, dict) and (res.get("cancelled") or res.get("success") is False):
                    status = "cancelled" if res.get("cancelled") else "failed"
                    self.acknowledge(command_id, status, res)
                else:
                    self.acknowledge(command_id, "executed", res)
                return

            if action == "cancel_workspace":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.cancel_workspace(str(args.get("workspace_id", ""))),
                )
                return

            if action == "minimize_all":
                try:
                    self.minimize_all_windows()
                    self.acknowledge(command_id, "executed")
                except Exception as error:
                    LOG.exception("minimize_all failed")
                    self.acknowledge(command_id, "failed", {"error": str(error)})
                return

            if action == "screen_off":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.turn_off_displays(),
                )
                return

            if action in ("stealth_mode", "blackout_mode"):
                state = args.get("state", "toggle")
                res = self.handle_stealth_action(
                    state=state,
                    hotkey=args.get("hotkey"),
                    timeout=args.get("timeout"),
                )
                self.acknowledge(command_id, "executed", res)
                return

            if action == "kill_process":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.kill_process(args.get("name", "")),
                )
                return

            if action == "kill_pid":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.kill_process_by_pid(args.get("pid")),
                )
                return

            if action == "restart_service":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.restart_service(args.get("service")),
                )
                return

            if action == "volume":
                data = self.set_volume(
                    percent=args.get("percent"),
                    mute=args.get("mute"),
                    toggle=bool(args.get("toggle", False)),
                )
                self.acknowledge(command_id, "executed", data)
                return

            if action in {"press_esc", "send_key_esc", "send_key"}:
                key = str(args.get("key", "esc")).lower()
                count = int(args.get("count", 1))
                delay = float(args.get("delay", 0.3))
                if key in {"esc", "escape"}:
                    res = self.send_key_esc(count=count, delay=delay)
                    self.acknowledge(command_id, "executed", res)
                else:
                    self.acknowledge(command_id, "failed", {"error": f"unsupported key: {key}"})
                return

            if action in ("cmd", "exec", "powershell"):
                cmd_text = args.get("command") or args.get("cmd") or ""
                use_ps = action == "powershell" or args.get("shell") == "powershell"
                timeout_s = max(1, min(int(args.get("timeout", 60)), 300))
                cwd = args.get("cwd") or os.environ.get("USERPROFILE") or "C:\\"

                if use_ps:
                    exec_args = [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; {cmd_text}",
                    ]
                else:
                    exec_args = ["cmd.exe", "/c", f"chcp 65001 >nul & {cmd_text}"]

                try:
                    p = subprocess.run(
                        exec_args,
                        capture_output=True,
                        timeout=timeout_s,
                        cwd=cwd,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    stdout = p.stdout
                    stderr = p.stderr
                    code = p.returncode
                except subprocess.TimeoutExpired:
                    stdout = ""
                    stderr = f"Command timed out after {timeout_s}s"
                    code = 124
                except Exception as e:
                    stdout = ""
                    stderr = f"Execution error: {e}"
                    code = -1

                max_len = 8000
                if len(stdout) > max_len:
                    stdout = stdout[:max_len] + f"\n... [обрезано {len(stdout) - max_len} симв.]"
                if len(stderr) > 4000:
                    stderr = stderr[:4000] + "\n... [обрезано]"

                self.acknowledge(
                    command_id,
                    "executed" if code == 0 else "failed",
                    {
                        "stdout": stdout,
                        "stderr": stderr,
                        "exit_code": code,
                        "command": cmd_text,
                        "shell": "powershell" if use_ps else "cmd",
                    },
                )
                return

            if action == "antigravity_list":
                accounts = self.antigravity_manager.list_accounts()
                active = self.antigravity_manager.get_active_account()
                self.acknowledge(
                    command_id,
                    "executed",
                    {
                        "accounts": accounts,
                        "active": active,
                        "is_running": self.antigravity_manager.is_ide_running(),
                    },
                )
                return

            if action == "antigravity_switch":
                acc_id = args.get("account_id")
                restart = bool(args.get("restart", True))
                if acc_id == "auto":
                    best = self.antigravity_manager.find_best_account()
                    if not best:
                        self.acknowledge(command_id, "failed", {"error": "Нет доступных аккаунтов с остатком квоты"})
                        return
                    acc_id = best["id"]

                res = self.antigravity_manager.switch_account(acc_id, restart_ide=restart)
                self.acknowledge(command_id, "executed", res)
                return

            if action == "antigravity_restart":
                was_running = self.antigravity_manager.is_ide_running()
                if was_running:
                    self.antigravity_manager.kill_ide()
                restarted = self.antigravity_manager.launch_ide()
                self.acknowledge(
                    command_id,
                    "executed",
                    {"success": True, "restarted": restarted},
                )
                return

            if action == "antigravity_export":
                bundle = self.antigravity_manager.export_accounts()
                self.acknowledge(
                    command_id,
                    "executed",
                    {"accounts_count": len(bundle.get("accounts", [])), "exported_at": bundle.get("exported_at")},
                )
                return

            self.acknowledge(command_id, "unknown_action")

        except PermissionError as error:
            LOG.warning("Permission denied for %s: %s", action, error)
            self.acknowledge(command_id, "permission_denied", {"error": str(error)})
        except Exception as error:
            LOG.exception("Action failed: %s", action)
            self.acknowledge(command_id, "failed", {"error": str(error)})

    def process_command(self, command):
        if not isinstance(command, dict):
            LOG.warning("Ignoring non-object command payload")
            return
        command_id = str(command.get("id", ""))
        action = str(command.get("action", ""))
        try:
            expires = int(command.get("expires", 0))
        except (TypeError, ValueError):
            LOG.warning("Ignoring command with invalid expiry: %s", command_id)
            if command_id and len(command_id) <= 80:
                self.acknowledge(command_id, "invalid_command")
            return
        args = command.get("args") or {}
        if not isinstance(args, dict):
            LOG.warning("Ignoring command with invalid args: %s", command_id)
            if command_id and len(command_id) <= 80:
                self.acknowledge(command_id, "invalid_command")
            return
        now = int(time.time())

        if expires < now - 30 or expires > now + 300:
            self.acknowledge(command_id, "expired")
            return
        if not self.remember_command(command_id):
            self.acknowledge(command_id, "duplicate_ignored")
            return

        LOG.info("Executing action=%s id=%s", action, command_id)
        threading.Thread(
            target=self.execute_action,
            args=(command_id, action, args),
            name=f"action-{action}",
            daemon=True,
        ).start()
