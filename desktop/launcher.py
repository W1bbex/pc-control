"""Application launcher, workspace coordinator and process killer mixin."""

import ctypes
import glob
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import psutil

from core.process_utils import (
    kill_process_by_name,
    kill_process_by_pid as kill_process_pid,
    restart_windows_service,
    shell_execute_runas,
)
from desktop.constants import PROTECTED_PROCESSES
from desktop.codex_launch import diagnose_codex_launch

LOG = logging.getLogger("pc-control-agent")


class LauncherMixin:
    """Provides application launch, workspace orchestration, keystroke simulation and process management."""

    def _get_workspace_lock(self):
        lock = getattr(self, "workspace_lock", None)
        if lock is None:
            lock = threading.Lock()
            self.workspace_lock = lock
        if getattr(self, "workspace_cancellations", None) is None:
            self.workspace_cancellations = {}
        return lock

    @staticmethod
    def send_key_esc(count=1, delay=0.3):
        """
        Simulate pressing the Escape (ESC) key on Windows using hardware scan codes.
        Works in desktop applications, fullscreen DirectX/DirectInput games (GTA, Roblox), etc.
        """
        count = max(1, min(int(count), 50))
        delay = max(0.05, float(delay))
        user32 = ctypes.windll.user32

        VK_ESCAPE = 0x1B
        SCAN_ESCAPE = 0x01
        KEYEVENTF_KEYUP = 0x0002

        success_count = 0
        for i in range(count):
            sent = False
            try:
                user32.keybd_event(VK_ESCAPE, SCAN_ESCAPE, 0, 0)
                time.sleep(0.05)
                user32.keybd_event(VK_ESCAPE, SCAN_ESCAPE, KEYEVENTF_KEYUP, 0)
                sent = True
            except Exception as e:
                LOG.warning("keybd_event failed: %s, falling back to pyautogui", e)
                try:
                    import pyautogui

                    pyautogui.press("esc")
                    sent = True
                except Exception as pe:
                    LOG.error("pyautogui fallback failed: %s", pe)
            if sent:
                success_count += 1
            if i < count - 1:
                time.sleep(delay)

        overall_success = (success_count == count)
        LOG.info("Simulated ESC key press (success=%s, count=%d/%d)", overall_success, success_count, count)
        return {"action": "press_esc", "count": count, "success": overall_success, "sent": success_count}

    @staticmethod
    def _has_visible_window(target_names):
        return bool(LauncherMixin._find_visible_window(target_names))

    @staticmethod
    def _find_visible_window(target_names, *, exclude_minimized=False):
        """Return a visible top-level HWND owned by one of the target processes."""
        hdesk = None
        orig_desk = None
        user32 = None
        try:
            import psutil
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            kernel32 = getattr(ctypes.windll, "kernel32", None)
            if kernel32 and hasattr(kernel32, "GetCurrentThreadId"):
                kernel32.GetCurrentThreadId.argtypes = []
                kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
            user32.GetThreadDesktop.restype = wintypes.HANDLE
            user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            user32.OpenDesktopW.restype = wintypes.HANDLE
            user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
            user32.SetThreadDesktop.restype = wintypes.BOOL
            user32.CloseDesktop.argtypes = [wintypes.HANDLE]
            user32.CloseDesktop.restype = wintypes.BOOL
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.IsIconic.argtypes = [wintypes.HWND]
            user32.IsIconic.restype = wintypes.BOOL
            user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            user32.GetWindowTextLengthW.restype = ctypes.c_int
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD

            if kernel32 and hasattr(user32, "GetThreadDesktop"):
                try:
                    orig_desk = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
                except Exception:
                    orig_desk = None
            hdesk = user32.OpenDesktopW("Default", 0, False, 0x0100)
            if hdesk:
                user32.SetThreadDesktop(hdesk)

            target_set = {t.lower() for t in target_names}
            matching_pids = set()
            for p in psutil.process_iter(["pid", "name"]):
                if (p.info["name"] or "").lower() in target_set:
                    matching_pids.add(p.info["pid"])

            if not matching_pids:
                return None

            window = None

            def enum_cb(hwnd, lparam):
                nonlocal window
                if user32.IsWindowVisible(hwnd):
                    if exclude_minimized and user32.IsIconic(hwnd):
                        return True
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        if pid.value in matching_pids:
                            window = hwnd
                            return False
                return True

            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
            user32.EnumWindows.restype = wintypes.BOOL
            user32.EnumWindows(EnumWindowsProc(enum_cb), 0)
            return window
        except Exception as err:
            if exclude_minimized:
                raise
            LOG.warning("Failed checking visible window: %s", err)
            return None
        finally:
            if hdesk and user32:
                try:
                    if orig_desk:
                        user32.SetThreadDesktop(orig_desk)
                except Exception:
                    pass
                try:
                    user32.CloseDesktop(hdesk)
                except Exception:
                    pass

    def minimize_workspace_windows(self, cancel_event=None):
        """Minimize the workspace and check for a Codex window restored during startup."""
        if cancel_event is None:
            cancel_event = threading.Event()
        if cancel_event.wait(2.0):
            return False
        self.minimize_all_windows()
        if cancel_event.wait(2.0):
            return False

        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindowAsync.restype = wintypes.BOOL
        for _ in range(4):
            hwnd = self._find_visible_window(["chatgpt.exe"], exclude_minimized=True)
            if not hwnd:
                return True
            user32.ShowWindowAsync(hwnd, 6)  # SW_MINIMIZE
            if cancel_event.wait(0.5):
                return False

        minimized = not self._find_visible_window(["chatgpt.exe"], exclude_minimized=True)
        if not minimized:
            LOG.warning("A Codex window remained open after workspace minimization")
        return minimized

    def launch_app(self, app_key, workspace_id=None, progress_callback=None, vpn=None, connect_vpn=True):
        app_config = self.config.get("apps", {}).get(app_key)
        if not app_config:
            app_config = self.config.get("scenarios", {}).get(app_key)
        if not app_config:
            raise ValueError("Application is not configured")

        if isinstance(app_config, str) and app_config in self.config.get("scenarios", {}):
            app_config = self.config["scenarios"][app_config]

        scenario = app_config if isinstance(app_config, dict) else {}
        minimize_after = bool(scenario.get("minimize_after", False))
        inter_app_delay = max(0.0, float(scenario.get("inter_app_delay_seconds", 1.5)))
        raw_steps = scenario.get("steps") if scenario else app_config
        direct_vpn = app_key.lower() if isinstance(app_key, str) and app_key.lower() in {"happ", "incy"} else None
        selected_vpn = str(vpn or direct_vpn or self.config.get("default_vpn", "incy")).lower().strip()

        cancel_event = None
        if workspace_id:
            cancel_event = threading.Event()
            with self._get_workspace_lock():
                self.workspace_cancellations[workspace_id] = cancel_event

        normalized_steps = []
        if isinstance(raw_steps, list):
            if raw_steps and all(isinstance(x, str) for x in raw_steps):
                exe_name = Path(raw_steps[0]).stem
                normalized_steps.append({
                    "id": exe_name.lower(),
                    "label": app_key if app_key else exe_name,
                    "command": raw_steps,
                    "required": True,
                    "verification": {"type": "process_name", "targets": [Path(raw_steps[0]).name.lower()]},
                })
            else:
                for item in raw_steps:
                    if isinstance(item, dict):
                        normalized_steps.append(item)
                    elif isinstance(item, list):
                        exe_name = Path(item[0]).stem if item else "app"
                        normalized_steps.append({
                            "id": exe_name.lower(),
                            "label": exe_name,
                            "command": item,
                            "required": True,
                            "verification": {"type": "process_name", "targets": [Path(item[0]).name.lower()]} if item else {},
                        })
                    elif isinstance(item, str):
                        exe_name = Path(item).stem
                        normalized_steps.append({
                            "id": exe_name.lower(),
                            "label": exe_name,
                            "command": [item],
                            "required": True,
                            "verification": {"type": "process_name", "targets": [Path(item).name.lower()]},
                        })
        elif isinstance(raw_steps, str):
            exe_name = Path(raw_steps).stem
            normalized_steps.append({
                "id": exe_name.lower(),
                "label": app_key if app_key else exe_name,
                "command": [raw_steps],
                "required": True,
                "verification": {"type": "process_name", "targets": [Path(raw_steps).name.lower()]},
            })

        launched = []
        verified_steps = []
        codex_verified = False
        try:
            for idx, step in enumerate(normalized_steps):
                if cancel_event and cancel_event.is_set():
                    LOG.info("Workspace launch cancelled before step %s (%s)", idx + 1, step.get("id"))
                    return {
                        "app": app_key,
                        "launched": launched,
                        "verified_steps": verified_steps,
                        "cancelled": True,
                        "success": False,
                        "desktop_minimized": False,
                    }

                step_id = step.get("id") or step.get("step_id", f"step_{idx}")
                label = step.get("label", step_id)
                raw_cmd = step.get("command") or ([step.get("executable")] if step.get("executable") else [])
                cmd = [os.path.expandvars(str(c)) for c in raw_cmd]
                required = bool(step.get("required", True))
                verification = step.get("verification") or {}
                if isinstance(verification, str):
                    ver_type = verification
                    targets = []
                else:
                    ver_type = verification.get("type", "process_name")
                    targets = [t.lower() for t in verification.get("targets", [])]

                if not cmd:
                    continue
                executable = cmd[0]

                if step_id.lower() in ("chatgpt", "codex") or (app_key == "ChatGPT" and len(normalized_steps) == 1):
                    if progress_callback:
                        progress_callback({"step_id": step_id, "app": label, "state": "starting"})
                    try:
                        result = diagnose_codex_launch(
                            "2", executable, self._find_visible_window,
                            timeout=float(verification.get("timeout", 20.0)) if isinstance(verification, dict) else 20.0,
                            allow_existing=True, cancel_event=cancel_event,
                        )
                    except (OSError, ValueError) as error:
                        LOG.exception("Codex launch through Explorer failed")
                        result = {"state": "error", "error": str(error)}
                    state = result.get("state")
                    if state == "cancelled":
                        return {"app": app_key, "launched": launched, "verified_steps": verified_steps,
                                "cancelled": True, "success": False, "desktop_minimized": False}
                    if state != "window_visible":
                        LOG.error("Codex launch was not verified: %s", state)
                        if progress_callback:
                            progress_callback({"step_id": step_id, "app": label, "state": "failed", "error": state})
                        if required:
                            return {"app": app_key, "launched": launched, "verified_steps": verified_steps,
                                    "failed_step": step_id, "success": False,
                                    "reason": f"step_{step_id}_failed_verification", "desktop_minimized": False}
                    else:
                        codex_verified = True
                        launched.append(label)
                        verified_steps.append(step_id)
                        if progress_callback:
                            progress_callback({"step_id": step_id, "app": label, "state": "verified"})
                    if idx < len(normalized_steps) - 1:
                        time.sleep(inter_app_delay)
                    continue

                is_vpn = (
                    step_id in ("happ_vpn", "incy_vpn", "vpn")
                    or ver_type in ("happ_vpn", "incy_vpn", "vpn")
                    or any(k in str(executable).lower() for k in ("happ.exe", "incy.exe"))
                )

                # Opening Happ or INCY without connecting must not invoke VPN automation.
                if (app_key or "").lower() in ("happ", "incy") and not connect_vpn:
                    is_vpn = False

                if is_vpn:
                    if selected_vpn == "happ":
                        step_id = "happ_vpn"
                        label = "Happ (VPN)"
                        configured_path = self.config.get("apps", {}).get("Happ", r"C:\Program Files\FlyFrogLLC\Happ\Happ.exe")
                        executable = str(configured_path)
                        cmd = [os.path.expandvars(executable)]
                        is_happ = True
                        is_incy = False
                    else:
                        step_id = "incy_vpn"
                        label = "INCY (VPN)"
                        configured_path = self.config.get("apps", {}).get("INCY", r"C:\Program Files\INCY\incy.exe")
                        executable = str(configured_path)
                        cmd = [os.path.expandvars(executable)]
                        is_happ = False
                        is_incy = True
                else:
                    is_happ = connect_vpn and ((ver_type == "happ_vpn") or ("happ.exe" in str(executable).lower()))
                    is_incy = connect_vpn and ((ver_type == "incy_vpn") or ("incy.exe" in str(executable).lower()))

                if "*" in str(executable) or "?" in str(executable):
                    matched = glob.glob(str(executable))
                    if matched:
                        executable = sorted(matched, key=os.path.getmtime, reverse=True)[0]
                        cmd[0] = executable

                resolved_exe = shutil.which(executable)
                if resolved_exe:
                    executable = resolved_exe
                elif not Path(executable).exists():
                    raise FileNotFoundError(executable)

                if is_happ or is_incy:
                    is_active = self.check_happ_vpn_active() if is_happ else self.check_incy_vpn_active()
                    if is_active:
                        LOG.info("Step %s (%s) is already running and VPN is verified", step_id, label)
                        launched.append(f"{label} (already running verified)")
                        verified_steps.append(step_id)
                        if progress_callback:
                            progress_callback({
                                "step_id": step_id,
                                "app": label,
                                "state": "already_running_verified",
                            })
                        if idx < len(normalized_steps) - 1:
                            time.sleep(inter_app_delay)
                        continue
                else:
                    target_names = targets if targets else [Path(executable).name.lower()]
                    is_browser_or_bg = any(
                        b in t for t in target_names for b in ("chrome", "msedge", "brave", "opera", "browser")
                    )
                    matching_procs = [
                        p for p in psutil.process_iter(["name"])
                        if (p.info["name"] or "").lower() in target_names
                    ]
                    if matching_procs:
                        if is_browser_or_bg:
                            # For browsers, background service processes (e.g. --no-startup-window)
                            # must not prevent launching the interactive browser window.
                            app_already_running = self._has_visible_window(target_names)
                        else:
                            app_already_running = True
                    else:
                        app_already_running = False

                    if app_already_running:
                        LOG.info("Step %s (%s) is already running verified", step_id, label)
                        launched.append(f"{label} (already running verified)")
                        verified_steps.append(step_id)
                        if progress_callback:
                            progress_callback({
                                "step_id": step_id,
                                "app": label,
                                "state": "already_running_verified",
                            })
                        if idx < len(normalized_steps) - 1:
                            time.sleep(inter_app_delay)
                        continue

                is_ag = (step_id == "antigravity") or ("antigravity.exe" in str(executable).lower())
                if is_ag and not (is_happ or is_incy):
                    # Pre-flight check: ensure Google HTTPS is reachable before starting Antigravity to prevent blank window
                    if not self.probe_google_https(timeout=2.5):
                        LOG.warning("Pre-launch check for Antigravity: Google HTTPS unreachable! Running auto-heal...")
                        heal_res = self.heal_vpn_and_antigravity(attempts=2)
                        if heal_res.get("success"):
                            LOG.info("Auto-healed VPN before Antigravity launch (server: %s)", heal_res.get("server"))
                            launched.append(label)
                            verified_steps.append(step_id)
                            if progress_callback:
                                progress_callback({
                                    "step_id": step_id,
                                    "app": label,
                                    "state": "verified",
                                })
                            if idx < len(normalized_steps) - 1:
                                time.sleep(inter_app_delay)
                            continue

                if progress_callback:
                    progress_callback({
                        "step_id": step_id,
                        "app": label,
                        "state": "starting",
                    })

                try:
                    process = subprocess.Popen(
                        cmd,
                        cwd=str(Path(executable).parent),
                        creationflags=subprocess.DETACHED_PROCESS
                        | subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
                except OSError as e:
                    if getattr(e, "winerror", None) == 740:
                        LOG.warning("WinError 740 for %s, elevating via ShellExecuteW 'runas'", executable)
                        params_str = subprocess.list2cmdline([str(p) for p in cmd[1:]]) if len(cmd) > 1 else ""
                        ret = shell_execute_runas(str(executable), params_str, Path(executable).parent)
                        if ret <= 32:
                            raise RuntimeError(f"ShellExecuteW 'runas' failed with code {ret} for {executable}")
                        process = None
                    else:
                        raise

                time.sleep(0.3)
                exit_code = process.poll() if process is not None else None
                if exit_code not in (None, 0):
                    raise RuntimeError(f"Application exited immediately with code {exit_code}: {executable}")
                launched.append(label)

                if is_happ or is_incy:
                    if progress_callback:
                        progress_callback({
                            "step_id": step_id,
                            "app": label,
                            "state": "connecting",
                        })
                    vpn_res = (
                        self.connect_happ_vpn(cancel_event=cancel_event)
                        if is_happ
                        else self.connect_incy_vpn(cancel_event=cancel_event)
                    )
                    if vpn_res.get("cancelled"):
                        return {
                            "app": app_key,
                            "launched": launched,
                            "verified_steps": verified_steps,
                            "cancelled": True,
                            "success": False,
                            "desktop_minimized": False,
                        }
                    if not vpn_res.get("verified"):
                        if progress_callback:
                            progress_callback({
                                "step_id": step_id,
                                "app": label,
                                "state": "failed",
                                "error": vpn_res.get("reason", "vpn_failed"),
                            })
                        if required:
                            LOG.error("Required VPN step %s failed; halting scenario", step_id)
                            return {
                                "app": app_key,
                                "launched": launched,
                                "verified_steps": verified_steps,
                                "failed_step": step_id,
                                "success": False,
                                "reason": "vpn_verification_failed",
                                "desktop_minimized": False,
                            }
                    else:
                        verified_steps.append(step_id)
                        if progress_callback:
                            progress_callback({
                                "step_id": step_id,
                                "app": label,
                                "state": "verified",
                            })
                else:
                    time.sleep(0.5)
                    target_names = targets if targets else [Path(executable).name.lower()]
                    ver_timeout = float(verification.get("timeout", 1.0)) if isinstance(verification, dict) else 1.0
                    ver_deadline = time.time() + max(0.5, ver_timeout)
                    process_verified = False
                    is_bootstrap_loader = (
                        bool(targets)
                        and Path(executable).name.lower() not in target_names
                    )
                    while True:
                        target_running = any(
                            (p.info["name"] or "").lower() in target_names
                            for p in psutil.process_iter(["name"])
                        )
                        if is_bootstrap_loader:
                            process_verified = target_running
                        else:
                            process_verified = target_running or (process is not None and process.poll() is None)
                        if cancel_event and cancel_event.is_set():
                            break
                        if process_verified or time.time() >= ver_deadline:
                            break
                        time.sleep(0.5)
                    if cancel_event and cancel_event.is_set():
                        LOG.info("Launch cancelled during verification of step %s", step_id)
                        return {
                            "app": app_key,
                            "launched": launched,
                            "verified_steps": verified_steps,
                            "failed_step": None,
                            "success": False,
                            "reason": "cancelled",
                            "desktop_minimized": False,
                        }
                    if process_verified:
                        verified_steps.append(step_id)
                        if progress_callback:
                            progress_callback({
                                "step_id": step_id,
                                "app": label,
                                "state": "verified",
                            })
                    else:
                        if progress_callback:
                            progress_callback({
                                "step_id": step_id,
                                "app": label,
                                "state": "failed",
                            })
                        if required:
                            LOG.error("Required app step %s failed verification; halting scenario", step_id)
                            return {
                                "app": app_key,
                                "launched": launched,
                                "verified_steps": verified_steps,
                                "failed_step": step_id,
                                "success": False,
                                "reason": f"step_{step_id}_failed_verification",
                                "desktop_minimized": False,
                            }

                if idx < len(normalized_steps) - 1:
                    time.sleep(inter_app_delay)

            desktop_minimized = False
            if minimize_after and not (cancel_event and cancel_event.is_set()):
                if codex_verified:
                    desktop_minimized = self.minimize_workspace_windows(cancel_event)
                else:
                    self.minimize_all_windows()
                    desktop_minimized = True

            if cancel_event and cancel_event.is_set():
                return {"app": app_key, "launched": launched, "verified_steps": verified_steps,
                        "cancelled": True, "success": False, "desktop_minimized": desktop_minimized}

            return {
                "app": app_key,
                "launched": launched,
                "verified_steps": verified_steps,
                "success": True,
                "desktop_minimized": desktop_minimized,
            }
        finally:
            if cancel_event and workspace_id:
                with self._get_workspace_lock():
                    self.workspace_cancellations.pop(workspace_id, None)

    def cancel_workspace(self, workspace_id):
        with self._get_workspace_lock():
            cancel_event = self.workspace_cancellations.get(workspace_id)
        if not cancel_event:
            return {"workspace_id": workspace_id, "state": "not_running"}
        cancel_event.set()
        return {"workspace_id": workspace_id, "state": "cancelling"}

    @staticmethod
    def kill_process(process_name):
        return kill_process_by_name(
            process_name,
            PROTECTED_PROCESSES,
            allow_desktop_agent_alias=False,
        )

    @staticmethod
    def kill_process_by_pid(pid):
        return kill_process_pid(pid, PROTECTED_PROCESSES)

    def restart_service(self, service_key):
        return restart_windows_service(service_key, self.config.get("services"))
