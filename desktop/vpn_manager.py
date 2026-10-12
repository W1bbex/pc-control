"""VPN management mixin for Happ VPN and INCY VPN automation."""

import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request

import psutil

LOG = logging.getLogger("pc-control-agent")


class VpnManagerMixin:
    """Provides methods to check and connect Happ and INCY VPN services."""

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

    def connect_incy_vpn(self, cancel_event=None, timeout=None):
        """
        Ensure INCY VPN is connected and verified.
        Ensures autoConnect is set in preferences.json, launches incy.exe if not running,
        and polls up to timeout for network readiness.
        """
        LOG.info("Verifying INCY VPN connection")
        if self.check_incy_vpn_active():
            LOG.info("INCY VPN is already active and verified")
            return {"verified": True, "already_connected": True}

        try:
            pref_path = Path(os.path.expandvars(r"%APPDATA%\incy\preferences.json"))
            if pref_path.exists():
                with open(pref_path, "r", encoding="utf-8") as f:
                    prefs = json.load(f)
                if not prefs.get("autoConnect"):
                    prefs["autoConnect"] = True
                    with open(pref_path, "w", encoding="utf-8") as f:
                        json.dump(prefs, f, indent=2)
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
                subprocess.Popen(
                    [resolved_exe],
                    cwd=str(Path(resolved_exe).parent),
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                )

        if timeout is None:
            timeout = max(45, int(self.config.get("vpn_ready_delay_seconds", self.config.get("happ_ready_delay_seconds", 45))))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                return {"cancelled": True}
            if self.check_incy_vpn_active():
                LOG.info("INCY VPN connection successfully verified via network probe")
                return {"verified": True}
            time.sleep(0.5)

        if self.check_incy_vpn_active():
            LOG.info("INCY VPN connection verified at deadline")
            return {"verified": True}

        LOG.warning("INCY VPN verification timed out after %ds", timeout)
        return {"verified": False, "reason": "vpn_verification_timeout"}

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
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.CloseDesktop.restype = wintypes.BOOL

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
            deadline = time.monotonic() + 15.0
            clicked = False
            window_title = None

            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

            while time.monotonic() < deadline:
                if cancel_event and cancel_event.is_set():
                    return {"cancelled": True}
                if self.check_happ_vpn_active():
                    LOG.info("HAPP VPN became active during window check; skipping click")
                    return {"verified": True, "already_connected": True}

                happ_windows = []

                def _enum_cb(hwnd, _):
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
                user32.EnumWindows(EnumWindowsProc(_enum_cb), 0)
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
