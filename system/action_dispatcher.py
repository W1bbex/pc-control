import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from system.constants import BASE_DIR, LOG
from system.session_manager import (
    active_console_session,
    find_active_interactive_session_id,
)


class ActionDispatcherMixin:
    """Mixin for executing remote commands (power, process, service, exec, unlock)."""

    def execute_action(self, command_id, action, args):
        try:
            if action == "telemetry":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.collect_telemetry(),
                )
                return
            if action == "top_processes":
                self.acknowledge(
                    command_id,
                    "executed",
                    {"processes": self.top_processes()},
                )
                return
            if action == "lock":
                self.acknowledge(command_id, "executed")
                subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
                return
            if action == "shutdown":
                self._turn_off_led_esp()
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                subprocess.Popen(
                    [
                        "shutdown.exe",
                        "/s",
                        "/t",
                        "5",
                        "/c",
                        "PC CONTROL: remote system shutdown",
                    ]
                )
                return
            if action == "restart":
                self._turn_off_led_esp()
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                subprocess.Popen(
                    [
                        "shutdown.exe",
                        "/r",
                        "/t",
                        "5",
                        "/c",
                        "PC CONTROL: remote system restart",
                    ]
                )
                return
            if action == "sleep":
                self._turn_off_led_esp()
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                ctypes.windll.powrprof.SetSuspendState(False, False, False)
                return
            if action == "hibernate":
                self._turn_off_led_esp()
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                subprocess.Popen(["shutdown.exe", "/h"])
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
            if action == "restart_desktop_agent":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.restart_desktop_agent(),
                )
                return
            if action in ("cmd", "exec", "powershell"):
                cmd_text = args.get("command") or args.get("cmd") or ""
                use_ps = action == "powershell" or args.get("shell") == "powershell"
                timeout_s = min(int(args.get("timeout", 60)), 300)
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
                    stderr = stderr[:4000] + f"\n... [обрезано]"

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
            if action == "unlock":
                if not self.unlock_lock.acquire(blocking=False):
                    LOG.warning("Rejecting concurrent unlock command %s (unlock_lock is busy)", command_id)
                    self.acknowledge(
                        command_id,
                        "busy",
                        {"error": "Another unlock command is currently in progress"},
                    )
                    return

                try:
                    unlock_cfg = self.config.get("unlock", {})
                    psexec_candidates = [
                        unlock_cfg.get("psexec_path"),
                        str(BASE_DIR / "PsExec64.exe"),
                        str(BASE_DIR / "hardware" / "PsExec64.exe"),
                        shutil.which("PsExec64.exe"),
                        shutil.which("PsExec.exe"),
                    ]
                    psexec = next((p for p in psexec_candidates if p and Path(p).is_file()), None)
                    if not psexec:
                        LOG.error("PsExec64.exe not found in candidates: %s", psexec_candidates)
                        raise FileNotFoundError(
                            f"PsExec64.exe executable not found in local system (checked: {[c for c in psexec_candidates if c]})"
                        )

                    pythonw_candidates = [
                        unlock_cfg.get("pythonw_path"),
                        str(BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"),
                        str(Path(sys.executable).with_name("pythonw.exe")),
                        shutil.which("pythonw.exe"),
                        sys.executable,
                    ]
                    pythonw = next((p for p in pythonw_candidates if p and Path(p).is_file()), None)
                    if not pythonw:
                        LOG.error("pythonw.exe not found in candidates: %s", pythonw_candidates)
                        raise FileNotFoundError(
                            f"pythonw.exe not found in local system (checked: {[c for c in pythonw_candidates if c]})"
                        )

                    script_candidates = [
                        unlock_cfg.get("script_path"),
                        str(BASE_DIR / "scripts" / "type_password.py"),
                        str(BASE_DIR / "type_password.py"),
                    ]
                    script = next((p for p in script_candidates if p and Path(p).is_file()), None)
                    if not script:
                        LOG.error("type_password.py not found in candidates: %s", script_candidates)
                        raise FileNotFoundError("type_password.py script not found")

                    session = active_console_session(ttl=0)
                    if session["session"] == "unlocked":
                        self.acknowledge(
                            command_id,
                            "executed",
                            {"session": "unlocked", "already_unlocked": True},
                        )
                        return
                    if session["session"] not in {"login_screen", "locked"}:
                        self.acknowledge(
                            command_id,
                            "not_ready",
                            {"session": session["session"]},
                        )
                        return

                    sid = find_active_interactive_session_id()
                    if not sid or sid in (0xFFFFFFFF, -1, 0):
                        sid = 1
                    target_session = str(sid)

                    completed = subprocess.run(
                        [
                            psexec,
                            "-accepteula",
                            "-nobanner",
                            "-s",
                            "-x",
                            "-i",
                            target_session,
                            pythonw,
                            script
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    if completed.returncode != 0:
                        LOG.error(
                            "Password input process failed (rc=%s): stdout=%r stderr=%r",
                            completed.returncode,
                            completed.stdout,
                            completed.stderr,
                        )
                        raise RuntimeError(
                            f"Password input process failed (exit code {completed.returncode}): "
                            f"{completed.stderr.strip() or completed.stdout.strip()}"
                        )

                    deadline = time.time() + 15
                    while time.time() < deadline:
                        session = active_console_session(ttl=0)
                        if session["session"] == "unlocked":
                            self.publish_status(force=True)
                            self.acknowledge(
                                command_id,
                                "executed",
                                {"session": "unlocked"},
                            )
                            return
                        time.sleep(0.3)

                    self.publish_status(force=True)
                    final_session = active_console_session(ttl=0)
                    self.acknowledge(
                        command_id,
                        "unlock_not_confirmed",
                        {"session": final_session["session"]},
                    )
                    return
                finally:
                    self.unlock_lock.release()
            self.acknowledge(command_id, "unsupported_before_login")
        except PermissionError as error:
            self.acknowledge(
                command_id,
                "permission_denied",
                {"error": str(error)},
            )
        except Exception as error:
            LOG.exception("System action failed: %s", action)
            self.acknowledge(
                command_id,
                "failed",
                {"error": str(error)},
            )
