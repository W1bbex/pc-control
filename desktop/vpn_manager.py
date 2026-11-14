"""VPN management mixin for Happ VPN and INCY VPN automation."""

import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request

import psutil

from core.process_utils import shell_execute_runas

LOG = logging.getLogger("pc-control-agent")

if hasattr(ctypes, "windll") and hasattr(ctypes.windll, "user32"):
    try:
        user32 = ctypes.windll.user32
        user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
        user32.GetThreadDesktop.restype = wintypes.HANDLE
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
        user32.SetThreadDesktop.restype = wintypes.BOOL
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.CloseDesktop.restype = wintypes.BOOL
    except Exception as _e:
        LOG.warning("Failed to initialize Win32 desktop types: %s", _e)


class VpnManagerMixin:
    """Provides methods to check and connect Happ and INCY VPN services."""

    def _get_incy_lock(self):
        lock = getattr(self, "_incy_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._incy_lock = lock
        return lock

    @staticmethod
    def _stop_incy_processes():
        incy_procs = [
            p for p in psutil.process_iter(["name", "pid"])
            if (p.info["name"] or "").lower() == "incy.exe"
        ]
        for p in incy_procs:
            try:
                p.terminate()
            except Exception:
                pass
        if incy_procs:
            try:
                psutil.wait_procs(incy_procs, timeout=3.0)
            except Exception:
                pass

    @staticmethod
    def _load_incy_preferences(pref_path: Path) -> dict:
        if not pref_path.exists():
            return {}
        try:
            with open(pref_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            raise ValueError(f"Corrupted INCY preferences at {pref_path}: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"Invalid INCY preferences structure at {pref_path}")
        return data

    @staticmethod
    def _write_incy_preferences_atomic(pref_path: Path, prefs: dict):
        pref_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = pref_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, pref_path)

    def _finalize_incy_connection(self, verify_google: bool, already_connected: bool = False) -> dict:
        if verify_google and not self.probe_google_https(timeout=3.0):
            LOG.warning("INCY connected, but Google HTTPS probe failed! Auto-switching server...")
            heal_res = self.switch_next_incy_server()
            if heal_res.get("success") and self.probe_google_https(timeout=3.5):
                return {"verified": True, "server": heal_res.get("server")}
            return {
                "verified": False,
                "reason": "google_https_probe_failed",
                "error": heal_res.get("error") or "Google HTTPS probe failed after server switch",
            }
        result = {"verified": True}
        if already_connected:
            result["already_connected"] = True
        return result

    @staticmethod
    def _run_network_probe(timeout=2.0):
        probe_targets = [
            ("1.1.1.1", 53),
            ("8.8.8.8", 53),
            ("77.88.8.8", 53),
        ]
        for host, port in probe_targets:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(min(1.5, timeout))
                    s.connect((host, port))
                    return True
            except Exception:
                pass
        try:
            req = urllib.request.Request(
                "http://www.google.com/generate_204",
                headers={"User-Agent": "PCControlAgent/2.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.getcode() in (200, 204):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def probe_google_https(timeout=3.5):
        """
        Verify TLS / HTTPS connectivity to Google endpoints required by Antigravity IDE.
        Accepts HTTP 200, 204 or API HTTP responses (400, 401, 403, 404) that confirm TLS
        handshake with Google succeeded and was not blocked by DPI/censorship/dead tunnel.
        """
        probe_urls = [
            "https://www.google.com/generate_204",
            "https://oauth2.googleapis.com",
        ]
        for url in probe_urls:
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "PCControlAgent/2.0"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.getcode() in (200, 204):
                        return True
            except urllib.error.HTTPError as e:
                # Reaching Google server with status code proves TLS handshake completed
                if e.code in (200, 204, 400, 401, 403, 404):
                    return True
            except Exception:
                pass
        return False

    @classmethod
    def check_happ_vpn_active(cls):
        """
        Verify that Happ VPN is actively running and connected:
        1. Process happ.exe is running.
        2. Interface 'happ-tun' or 'Happ' exists, is up, and has an IPv4 address.
        3. Network probe succeeds.
        """
        try:
            has_proc = any(
                (p.info["name"] or "").lower() in ("happ.exe", "flyfrogllc.happ.exe")
                for p in psutil.process_iter(["name"])
            )
            if not has_proc:
                return False

            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
            has_tun = False
            for iface, if_addrs in addrs.items():
                if "happ" in iface.lower():
                    if_stat = stats.get(iface)
                    if if_stat and not if_stat.isup:
                        continue
                    if any(
                        a.family == socket.AF_INET
                        and a.address
                        and not a.address.startswith("169.254")
                        for a in if_addrs
                    ):
                        has_tun = True
                        break
            if not has_tun:
                return False

            return cls._run_network_probe(timeout=2.5)
        except Exception as e:
            LOG.warning("Error checking Happ VPN active status: %s", e)
            return False

    @classmethod
    def check_incy_vpn_active(cls):
        """
        Verify that INCY VPN is actively running and connected:
        1. Process incy.exe is running.
        2. Interface 'wwan99' / 'wintun' exists, is up, and has an IPv4 address.
        3. Network probe succeeds.
        """
        try:
            has_proc = any(
                (p.info["name"] or "").lower() == "incy.exe"
                for p in psutil.process_iter(["name"])
            )
            if not has_proc:
                return False

            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
            has_tun = False
            for iface, if_addrs in addrs.items():
                if any(k in iface.lower() for k in ("wwan99", "wintun", "incy")):
                    if_stat = stats.get(iface)
                    if if_stat and not if_stat.isup:
                        continue
                    if any(
                        a.family == socket.AF_INET
                        and a.address
                        and not a.address.startswith("169.254")
                        for a in if_addrs
                    ):
                        has_tun = True
                        break
            if not has_tun:
                return False

            return cls._run_network_probe(timeout=2.5)
        except Exception as e:
            LOG.warning("Error checking INCY VPN active status: %s", e)
            return False

    def connect_incy_vpn(self, cancel_event=None, timeout=None, verify_google=True):
        """
        Ensure INCY VPN is connected and verified.
        Ensures autoConnect is set in preferences.json, launches incy.exe if not running,
        and polls up to timeout for network readiness and Google HTTPS accessibility.
        """
        with self._get_incy_lock():
            LOG.info("Verifying INCY VPN connection")
            if self.check_incy_vpn_active():
                LOG.info("INCY VPN is already active and verified")
                return self._finalize_incy_connection(verify_google, already_connected=True)

            try:
                pref_path = Path(os.path.expandvars(r"%APPDATA%\incy\preferences.json"))
                if pref_path.exists():
                    prefs = self._load_incy_preferences(pref_path)
                    if not prefs.get("autoConnect"):
                        prefs["autoConnect"] = True
                        self._write_incy_preferences_atomic(pref_path, prefs)
                        LOG.info("Enabled autoConnect in INCY preferences.json")
            except Exception as e:
                LOG.warning("Could not update INCY preferences: %s", e)

            is_running = any(
                (p.info["name"] or "").lower() == "incy.exe"
                for p in psutil.process_iter(["name"])
            )
            if not is_running:
                incy_cmd = self.config.get("apps", {}).get("INCY", r"C:\Program Files\INCY\incy.exe")
                resolved_exe = os.path.expandvars(str(incy_cmd))
                if Path(resolved_exe).exists():
                    LOG.info("Launching INCY: %s", resolved_exe)
                    try:
                        subprocess.Popen(
                            [resolved_exe],
                            cwd=str(Path(resolved_exe).parent),
                            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                        )
                    except OSError as e:
                        if getattr(e, "winerror", None) == 740:
                            LOG.warning("WinError 740 for %s, elevating via ShellExecuteW 'runas'", resolved_exe)
                            ret = shell_execute_runas(str(resolved_exe), "", Path(resolved_exe).parent)
                            if ret <= 32:
                                LOG.warning("ShellExecuteW 'runas' for %s failed or cancelled (code=%s)", resolved_exe, ret)
                                return {"verified": False, "reason": "uac_denied_or_failed", "code": ret}
                        else:
                            raise

        if timeout is None:
            timeout = max(45, int(self.config.get("vpn_ready_delay_seconds", self.config.get("happ_ready_delay_seconds", 45))))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                return {"cancelled": True}
            if self.check_incy_vpn_active():
                LOG.info("INCY VPN connection successfully verified via network probe")
                return self._finalize_incy_connection(verify_google)
            time.sleep(0.5)

        if self.check_incy_vpn_active():
            LOG.info("INCY VPN connection verified at deadline")
            return self._finalize_incy_connection(verify_google)

        LOG.warning("INCY VPN verification timed out after %ds", timeout)
        return {"verified": False, "reason": "vpn_verification_timeout"}

    @classmethod
    def get_incy_servers(cls):
        """
        Reads server list from %LOCALAPPDATA%\\incy\\incy.db.
        Excludes Russia and dead servers.
        Returns list of dicts sorted by latency ascending.
        """
        db_path = Path(os.path.expandvars(r"%LOCALAPPDATA%\incy\incy.db"))
        if not db_path.exists():
            return []
        conn = None
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("SELECT id, name, latency FROM servers WHERE name NOT LIKE '%Россия%'")
            rows = cur.fetchall()
            servers = []
            for s_id, s_name, s_lat in rows:
                lat = int(s_lat) if s_lat is not None and s_lat >= 0 else 9999
                servers.append({
                    "id": str(s_id),
                    "name": str(s_name),
                    "latency": lat,
                })
            servers.sort(key=lambda s: s["latency"])
            return servers
        except Exception as e:
            LOG.warning("Failed to query INCY servers from db: %s", e)
            return []
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def switch_next_incy_server(self, target_server_id=None):
        """
        Switches INCY VPN to the specified server or the next best healthy server from incy.db.
        Stops incy.exe before updating preferences.json atomically, relaunches, and awaits tunnel readiness.
        """
        with self._get_incy_lock():
            servers = self.get_incy_servers()
            if not servers:
                return {"success": False, "error": "No available servers in INCY database"}

            # Stop running incy.exe BEFORE mutating preferences.json so clean exit cannot overwrite changes
            self._stop_incy_processes()

            pref_path = Path(os.path.expandvars(r"%APPDATA%\incy\preferences.json"))
            try:
                prefs = self._load_incy_preferences(pref_path)
            except Exception as e:
                LOG.warning("Refusing to overwrite unreadable INCY preferences: %s", e)
                return {"success": False, "error": str(e)}
            current_id = prefs.get("selectedServerId")

            target = None
            if target_server_id:
                for s in servers:
                    if str(s["id"]) == str(target_server_id):
                        target = s
                        break

            if not target:
                current_idx = -1
                for idx, s in enumerate(servers):
                    if str(s["id"]) == str(current_id):
                        current_idx = idx
                        break
                next_idx = (current_idx + 1) % len(servers)
                target = servers[next_idx]

            LOG.info("Switching INCY VPN server to: %s (%s, %dms)", target["name"], target["id"], target["latency"])

            prefs["selectedServerId"] = target["id"]
            prefs["lastConnectedServerId"] = target["id"]
            prefs["autoConnect"] = True
            try:
                self._write_incy_preferences_atomic(pref_path, prefs)
            except Exception as e:
                LOG.warning("Failed to write INCY preferences: %s", e)
                return {"success": False, "error": f"Failed to write preferences: {e}"}

            # Launch incy.exe
            incy_cmd = self.config.get("apps", {}).get("INCY", r"C:\Program Files\INCY\incy.exe")
            resolved_exe = os.path.expandvars(str(incy_cmd))
            if Path(resolved_exe).exists():
                try:
                    subprocess.Popen(
                        [resolved_exe],
                        cwd=str(Path(resolved_exe).parent),
                        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
                except OSError as e:
                    if getattr(e, "winerror", None) == 740:
                        ret = shell_execute_runas(str(resolved_exe), "", Path(resolved_exe).parent)
                        if ret <= 32:
                            LOG.warning("ShellExecuteW 'runas' for %s failed or cancelled (code=%s)", resolved_exe, ret)
                            return {
                                "success": False,
                                "error": f"Elevation failed or was cancelled (code {ret})",
                                "server": target["name"],
                                "server_id": target["id"],
                            }
                    else:
                        raise
            else:
                if self.check_incy_vpn_active():
                    return {
                        "success": True,
                        "server": target["name"],
                        "server_id": target["id"],
                        "latency": target.get("latency", 0),
                    }
                LOG.warning("INCY executable not found at %s", resolved_exe)
                return {
                    "success": False,
                    "error": f"INCY executable not found at {resolved_exe}",
                    "server": target["name"],
                    "server_id": target["id"],
                }

        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            if self.check_incy_vpn_active():
                LOG.info("INCY VPN reconnected on server %s", target["name"])
                return {
                    "success": True,
                    "server": target["name"],
                    "server_id": target["id"],
                    "latency": target["latency"],
                }
            time.sleep(0.5)

        return {
            "success": False,
            "error": "Timeout waiting for INCY tunnel after server switch",
            "server": target["name"],
            "server_id": target["id"],
        }

    def heal_vpn_and_antigravity(self, attempts=3):
        """
        Auto-recovery routine for Antigravity blank window / bad VPN connection:
        1. Kills stuck Antigravity IDE processes.
        2. Cycles INCY server to a working country that successfully passes Google HTTPS probe.
        3. Restarts Antigravity IDE.
        """
        LOG.info("Starting Antigravity & VPN healing routine (attempts=%d)", attempts)

        # 1. Kill Antigravity IDE
        try:
            from core.antigravity_switcher import AntigravityAccountManager
            AntigravityAccountManager.kill_ide()
        except Exception:
            for p in psutil.process_iter(["name", "pid"]):
                if "antigravity" in (p.info["name"] or "").lower():
                    try:
                        p.kill()
                    except Exception:
                        pass

        # 2. Rotate servers until Google HTTPS passes
        working_server = None
        for attempt in range(1, attempts + 1):
            LOG.info("Healing attempt %d/%d: switching INCY server", attempt, attempts)
            switch_res = self.switch_next_incy_server()
            if not switch_res.get("success"):
                LOG.warning("Server switch attempt %d failed: %s", attempt, switch_res.get("error"))
                time.sleep(1.0)
                continue

            time.sleep(1.5)
            if self.probe_google_https(timeout=4.0):
                LOG.info("Google HTTPS probe succeeded on server %s", switch_res.get("server"))
                working_server = switch_res
                break
            else:
                LOG.warning("Google HTTPS probe failed on server %s; trying next", switch_res.get("server"))

        if not working_server:
            if self.probe_google_https(timeout=4.0):
                working_server = {"server": "Текущий сервер INCY", "latency": 0}
            else:
                return {
                    "success": False,
                    "error": "Не удалось найти сервер INCY с рабочим доступом к Google HTTPS",
                }

        # 3. Relaunch Antigravity IDE
        restarted = False
        launch_error = None
        try:
            from core.antigravity_switcher import AntigravityAccountManager
            restarted = bool(AntigravityAccountManager.launch_ide())
        except Exception as e:
            launch_error = str(e)
            LOG.error("Failed to launch Antigravity via switcher: %s", e)

        if not restarted:
            ag_cmd = self.config.get("apps", {}).get("Antigravity", r"%LOCALAPPDATA%\Programs\antigravity\Antigravity.exe")
            ag_exe = os.path.expandvars(str(ag_cmd))
            if Path(ag_exe).exists():
                try:
                    subprocess.Popen([ag_exe], cwd=str(Path(ag_exe).parent), creationflags=subprocess.DETACHED_PROCESS)
                    restarted = True
                except Exception as e:
                    launch_error = str(e)
                    LOG.error("Failed to launch Antigravity executable %s: %s", ag_exe, e)

        if not restarted:
            return {
                "success": False,
                "server": working_server.get("server"),
                "latency": working_server.get("latency", 0),
                "google_https_ok": True,
                "restarted_ide": False,
                "error": launch_error or "Не удалось запустить Antigravity IDE",
            }

        return {
            "success": True,
            "server": working_server.get("server"),
            "latency": working_server.get("latency", 0),
            "google_https_ok": True,
            "restarted_ide": True,
        }

    def connect_happ_vpn(self, cancel_event=None, timeout=None):
        """
        Verify Happ VPN connection.
        If already connected -> skips clicking and returns immediately.
        Otherwise restores window, clicks connect button, and awaits real verification.
        """
        LOG.info("Automating HAPP VPN connection")
        if self.check_happ_vpn_active():
            LOG.info("HAPP VPN is already active and verified; skipping button click")
            return {"verified": True, "already_connected": True}

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
        user32.GetThreadDesktop.restype = wintypes.HANDLE
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
        user32.SetThreadDesktop.restype = wintypes.BOOL
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.CloseDesktop.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL

        hdesk = None
        hdesk_orig = None
        try:
            hdesk_orig = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
            hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
            if hdesk:
                user32.SetThreadDesktop(hdesk)
        except Exception:
            pass

        try:
            started_at = time.monotonic()
            window_wait = min(15.0, float(timeout)) if timeout is not None else 15.0
            deadline = started_at + window_wait
            clicked = False
            window_title = None

            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
            user32.EnumWindows.restype = wintypes.BOOL

            while time.monotonic() < deadline:
                if cancel_event and cancel_event.is_set():
                    return {"cancelled": True}
                if self.check_happ_vpn_active():
                    LOG.info("HAPP VPN became active during window check; skipping click")
                    return {"verified": True, "already_connected": True}

                happ_windows = []

                def _enum_cb(hwnd, _):
                    if not user32.IsWindowVisible(hwnd):
                        return True
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buff = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buff, length + 1)
                        if "happ" in buff.value.lower():
                            rect = wintypes.RECT()
                            user32.GetWindowRect(hwnd, ctypes.byref(rect))
                            happ_windows.append((hwnd, buff.value, rect))
                    return True

                proc_cb = EnumWindowsProc(_enum_cb)
                user32.EnumWindows(proc_cb, 0)

                if not happ_windows:
                    time.sleep(0.5)
                    continue

                for hwnd, title, rect in happ_windows:
                    try:
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                        user32.SetForegroundWindow(hwnd)
                    except Exception:
                        pass

                time.sleep(0.4)

                happ_windows.clear()
                proc_cb2 = EnumWindowsProc(_enum_cb)
                user32.EnumWindows(proc_cb2, 0)
                if not happ_windows:
                    time.sleep(0.5)
                    continue

                target_hwnd, target_title, target_rect = max(
                    happ_windows,
                    key=lambda item: (item[2].right - item[2].left) * (item[2].bottom - item[2].top),
                )
                w_width = target_rect.right - target_rect.left
                w_height = target_rect.bottom - target_rect.top
                if w_width < 300 or w_height < 200:
                    time.sleep(0.5)
                    continue

                button_x = target_rect.left + round(w_width * 0.77)
                button_y = target_rect.top + round(w_height * 0.35)

                try:
                    import pyautogui
                    pyautogui.click(button_x, button_y)
                    clicked = True
                    window_title = target_title
                    LOG.info("Clicked HAPP connect button at (%s, %s) in window %r", button_x, button_y, target_title)
                    break
                except Exception as click_err:
                    LOG.warning("Failed clicking HAPP button with pyautogui: %s", click_err)
                    user32.SetCursorPos(button_x, button_y)
                    time.sleep(0.1)
                    user32.mouse_event(2, 0, 0, 0, 0)  # LEFTDOWN
                    time.sleep(0.05)
                    user32.mouse_event(4, 0, 0, 0, 0)  # LEFTUP
                    clicked = True
                    window_title = target_title
                    LOG.info("Clicked HAPP connect button via mouse_event at (%s, %s)", button_x, button_y)
                    break

            if not clicked:
                if self.check_happ_vpn_active():
                    LOG.info("HAPP VPN active despite window click skipped; verified")
                    return {"verified": True, "already_connected": True}
                LOG.warning("HAPP window was not ready after 15s")
                return {"verified": False, "reason": "window_not_found"}

            if timeout is not None:
                remaining = max(0.0, float(timeout) - (time.monotonic() - started_at))
                verify_timeout = remaining
            else:
                verify_timeout = max(35, int(self.config.get("happ_ready_delay_seconds", self.config.get("vpn_ready_delay_seconds", 35))))
            verify_deadline = time.monotonic() + verify_timeout
            while time.monotonic() < verify_deadline:
                if cancel_event and cancel_event.is_set():
                    return {"cancelled": True}
                if self.check_happ_vpn_active():
                    LOG.info("HAPP VPN connection successfully verified via network probe")
                    return {"verified": True, "window": window_title}
                time.sleep(0.5)

            if self.check_happ_vpn_active():
                LOG.info("HAPP VPN connection verified at deadline")
                return {"verified": True, "window": window_title}

            LOG.warning("HAPP VPN connection verification timed out after %ds", verify_timeout)
            return {"verified": False, "reason": "vpn_verification_timeout"}
        finally:
            if hdesk:
                try:
                    if hdesk_orig:
                        user32.SetThreadDesktop(hdesk_orig)
                    user32.CloseDesktop(hdesk)
                except Exception:
                    pass
