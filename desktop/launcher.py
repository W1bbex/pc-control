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

from desktop.constants import PROTECTED_PROCESSES

LOG = logging.getLogger("pc-control-agent")


class LauncherMixin:
    """Provides application launch, workspace orchestration, keystroke simulation and process management."""

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

        for i in range(count):
            try:
                user32.keybd_event(VK_ESCAPE, SCAN_ESCAPE, 0, 0)
                time.sleep(0.05)
                user32.keybd_event(VK_ESCAPE, SCAN_ESCAPE, KEYEVENTF_KEYUP, 0)
            except Exception as e:
                LOG.warning("keybd_event failed: %s, falling back to pyautogui", e)
                try:
                    import pyautogui

                    pyautogui.press("esc")
                except Exception:
                    pass
            if i < count - 1:
                time.sleep(delay)

        LOG.info("Simulated ESC key press (count=%d)", count)
        return {"action": "press_esc", "count": count, "success": True}

    def launch_app(self, app_key, workspace_id=None, progress_callback=None, vpn=None):
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
        selected_vpn = str(vpn or self.config.get("default_vpn", "incy")).lower().strip()

        cancel_event = None
        if scenario and workspace_id:
            cancel_event = threading.Event()
            with self.workspace_lock:
                self.workspace_cancellations[workspace_id] = cancel_event

        normalized_steps = []
        if isinstance(raw_steps, list):
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
                "label": exe_name,
                "command": [raw_steps],
                "required": True,
                "verification": {"type": "process_name", "targets": [Path(raw_steps).name.lower()]},
            })

        launched = []
        verified_steps = []
        try:
            for idx, step in enumerate(normalized_steps):
                if cancel_event and cancel_event.is_set():
                    LOG.info("Workspace launch cancelled before step %s (%s)", idx + 1, step.get("id"))
                    return {
                        "app": app_key,
                        "launched": launched,
                        "verified_steps": verified_steps,
                        "cancelled": True,
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

                is_vpn = (
                    step_id in ("happ_vpn", "incy_vpn", "vpn")
                    or ver_type in ("happ_vpn", "incy_vpn", "vpn")
                    or any(k in str(executable).lower() for k in ("happ.exe", "incy.exe"))
                )

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
                    is_happ = (ver_type == "happ_vpn") or ("happ.exe" in str(executable).lower())
                    is_incy = (ver_type == "incy_vpn") or ("incy.exe" in str(executable).lower())

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
                    app_already_running = any(
                        (p.info["name"] or "").lower() in target_names
                        for p in psutil.process_iter(["name"])
                    )
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
                        LOG.warning("WinError 740 for %s, falling back to shell=True", executable)
                        process = subprocess.Popen(
                            cmd,
                            cwd=str(Path(executable).parent),
                            shell=True,
                            creationflags=subprocess.DETACHED_PROCESS
                            | subprocess.CREATE_NEW_PROCESS_GROUP,
                        )
                    else:
                        raise

                time.sleep(0.3)
                if process.poll() not in (None, 0):
                    raise RuntimeError(f"Application exited immediately: {executable}")
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
                    while True:
                        process_verified = (process.poll() is None) or any(
                            (p.info["name"] or "").lower() in target_names
                            for p in psutil.process_iter(["name"])
                        )
                        if process_verified or time.time() >= ver_deadline:
                            break
                        if cancel_event and cancel_event.is_set():
                            break
                        time.sleep(0.5)
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

            if minimize_after:
                self.minimize_all_windows()

            return {
                "app": app_key,
                "launched": launched,
                "verified_steps": verified_steps,
                "success": True,
                "desktop_minimized": minimize_after,
            }
        finally:
            if cancel_event and workspace_id:
                with self.workspace_lock:
                    self.workspace_cancellations.pop(workspace_id, None)

    def cancel_workspace(self, workspace_id):
        with self.workspace_lock:
            cancel_event = self.workspace_cancellations.get(workspace_id)
        if not cancel_event:
            return {"workspace_id": workspace_id, "state": "not_running"}
        cancel_event.set()
        return {"workspace_id": workspace_id, "state": "cancelling"}

    @staticmethod
    def kill_process(process_name):
        if not process_name:
            raise ValueError("Process name is required")
        normalized = str(process_name).lower().strip()
        if normalized in PROTECTED_PROCESSES:
            raise PermissionError("Protected Windows process")
        killed = []
        for process in psutil.process_iter(["pid", "name"]):
            try:
                if (process.info["name"] or "").lower() == normalized:
                    process.terminate()
                    killed.append(process.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {"name": process_name, "terminated_pids": killed}

    @staticmethod
    def kill_process_by_pid(pid):
        process = psutil.Process(int(pid))
        name = (process.name() or "").lower()
        if process.pid == os.getpid() or name in PROTECTED_PROCESSES:
            raise PermissionError("Protected Windows process")
        original_name = process.name()
        process.terminate()
        return {"name": original_name, "terminated_pid": int(pid)}

    def restart_service(self, service_key):
        service_name = self.config.get("services", {}).get(service_key)
        if not service_name:
            raise ValueError("Service is not configured")
        subprocess.run(
            ["sc.exe", "stop", service_name],
            capture_output=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        time.sleep(2)
        completed = subprocess.run(
            ["sc.exe", "start", service_name],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stdout or completed.stderr)
        return {"service": service_key}
