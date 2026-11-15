"""Stealth mode, window minimize and display power control mixin."""

import ctypes
from ctypes import wintypes
import json
import logging
import threading
import time

from desktop.constants import TOPIC_STATUS

LOG = logging.getLogger("pc-control-agent")
_STEALTH_LOCK_INIT_GUARD = threading.Lock()


class StealthMixin:
    """Provides blackout/stealth mode, display sleep/wake, and window minimization."""

    def _get_stealth_lock(self):
        lock = getattr(self, "_stealth_lock", None)
        if lock is not None:
            return lock
        with _STEALTH_LOCK_INIT_GUARD:
            lock = getattr(self, "_stealth_lock", None)
            if lock is None:
                lock = threading.RLock()
                self._stealth_lock = lock
            return lock

    @staticmethod
    def minimize_all_windows():
        """Minimize every app window with safe retries for windows opening late."""
        com_initialized = False
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            com_initialized = True
            shell = win32com.client.Dispatch("Shell.Application")

            LOG.info("Minimizing windows (Pass 1/3)...")
            shell.MinimizeAll()

            time.sleep(2.0)
            LOG.info("Minimizing windows (Pass 2/3)...")
            shell.MinimizeAll()

            time.sleep(2.0)
            LOG.info("Minimizing windows (Pass 3/3)...")
            shell.MinimizeAll()
        except Exception as error:
            LOG.warning("Shell minimize failed, using Win+M fallback: %s", error)
            import pyautogui

            prev_failsafe = getattr(pyautogui, "FAILSAFE", True)
            try:
                pyautogui.FAILSAFE = False
                LOG.info("Minimizing windows via Win+M fallback (Pass 1)...")
                pyautogui.hotkey("win", "m")
                time.sleep(2.0)
                LOG.info("Minimizing windows via Win+M fallback (Pass 2)...")
                pyautogui.hotkey("win", "m")
                time.sleep(2.0)
                LOG.info("Minimizing windows via Win+M fallback (Pass 3)...")
                pyautogui.hotkey("win", "m")
            finally:
                pyautogui.FAILSAFE = prev_failsafe
        finally:
            shell = None
            if com_initialized:
                pythoncom.CoUninitialize()
        LOG.info("Minimized all windows after workspace launch (completed 3 passes)")

    @staticmethod
    def _send_monitor_command(lparam_val: int, timeout_ms: int = 2000) -> bool:
        hwnd_broadcast = 0xFFFF
        wm_syscommand = 0x0112
        sc_monitorpower = 0xF170
        user32 = ctypes.windll.user32
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.SendMessageTimeoutW.restype = wintypes.LPARAM
        ret = user32.SendMessageTimeoutW(
            hwnd_broadcast,
            wm_syscommand,
            sc_monitorpower,
            wintypes.LPARAM(lparam_val),
            0x0002,  # SMTO_ABORTIFHUNG
            timeout_ms,
            None,
        )
        return bool(ret)

    @staticmethod
    def turn_off_displays():
        """Request that Windows powers down every attached display only."""
        result = StealthMixin._send_monitor_command(2, 2000)
        if not result:
            raise RuntimeError("Windows did not accept the display power command")
        LOG.info("Requested display power-off")
        return {"displays": "off"}

    @staticmethod
    def wake_displays():
        """Request that Windows powers up displays and wake up GPU swapchain."""
        win32_ok = False
        mouse_ok = False
        try:
            win32_ok = StealthMixin._send_monitor_command(-1, 1000)
            if not win32_ok:
                LOG.warning("SendMessageTimeoutW display wake-up returned 0")
        except Exception as err:
            LOG.warning("SendMessageTimeoutW display wake-up failed: %s", err)
        try:
            import pyautogui

            pyautogui.moveRel(1, 0, duration=0)
            pyautogui.moveRel(-1, 0, duration=0)
            mouse_ok = True
        except Exception as err:
            LOG.warning("Mouse jitter display wake-up failed: %s", err)
        if win32_ok or mouse_ok:
            LOG.info("Requested display wake-up")
            return {"displays": "on"}
        LOG.warning("Both display wake-up methods failed")
        return {"displays": "unknown", "error": "wake_failed"}

    def handle_stealth_action(self, state: str = "toggle", hotkey=None, timeout=None) -> dict:
        """Handle stealth / blackout mode activation or deactivation."""
        stealth_lock = self._get_stealth_lock()
        with stealth_lock:
            if state == "toggle":
                target = "off" if self.stealth_mgr.is_active() else "on"
            else:
                target = str(state).lower()

            if target in ("on", "activate", "true", "1"):
                self._stealth_activating = True
                prev_mute_snapshot = getattr(self, "_prev_mute_state", None)
            else:
                self._stealth_activating = False
                self._stealth_is_active = False
                self._stealth_offline_since = None
                prev_mute_snapshot = getattr(self, "_prev_mute_state", None)
                self._prev_mute_state = None

        if target in ("on", "activate", "true", "1"):
            muted_here = False
            prev_mute = prev_mute_snapshot
            try:
                if self.stealth_mgr.mute_audio:
                    try:
                        if prev_mute is None:
                            vol = self.get_volume()
                            prev_mute = vol.get("muted") if isinstance(vol, dict) else False
                        self.set_volume(mute=True)
                        muted_here = True
                    except Exception as err:
                        LOG.warning("Failed to mute audio for stealth mode: %s", err)

                activated = bool(self.stealth_mgr.activate(hotkey=hotkey, timeout_minutes=timeout))
            except Exception as err:
                LOG.warning("Stealth manager activation raised an error: %s", err)
                activated = False
            finally:
                if not activated and muted_here and prev_mute is not None:
                    try:
                        self.set_volume(mute=prev_mute)
                    except Exception as err:
                        LOG.warning("Failed to restore audio after failed stealth activation: %s", err)

                with stealth_lock:
                    self._stealth_activating = False
                    if activated:
                        self._stealth_is_active = True
                        self._prev_mute_state = prev_mute
                        self._stealth_offline_since = None
                    else:
                        self._stealth_is_active = False
                        self._prev_mute_state = None

            if not activated:
                return {"stealth": "failed", "active": False, "error": "Failed to activate stealth mode"}

            time.sleep(0.3)

            if self.stealth_mgr.turn_off_displays:
                try:
                    self.turn_off_displays()
                except Exception as err:
                    LOG.warning("Failed to power off displays for stealth mode: %s", err)

            return {"stealth": "active", "active": True}

        self.stealth_mgr.deactivate()

        if self.stealth_mgr.mute_audio and prev_mute_snapshot is not None:
            try:
                self.set_volume(mute=prev_mute_snapshot)
            except Exception as err:
                LOG.warning("Failed to restore audio mute state: %s", err)

        self.wake_displays()
        return {"stealth": "inactive", "active": False}

    def check_stealth_process_exit(self):
        """
        Detects if stealth overlay exited locally (via secret hotkey Win+W+1 or 5x Escape).
        Automatically restores audio mute state, wakes displays, and publishes stealth_exit event to MQTT.
        """
        stealth_lock = self._get_stealth_lock()
        with stealth_lock:
            if getattr(self, "_stealth_activating", False):
                return
            prev_mute = getattr(self, "_prev_mute_state", None)
            was_active = getattr(self, "_stealth_is_active", False) or (prev_mute is not None)
            if not was_active or self.stealth_mgr.is_active():
                return
            self._stealth_is_active = False
            self._prev_mute_state = None
            self._stealth_offline_since = None

        LOG.info("Stealth process exited locally (via hotkey/watchdog). Restoring audio, display, and notifying Telegram...")
        if self.stealth_mgr.mute_audio and prev_mute is not None:
            try:
                self.set_volume(mute=prev_mute)
            except Exception as err:
                LOG.warning("Failed to restore audio mute state: %s", err)

        reason = "local_hotkey"
        if hasattr(self.stealth_mgr, "get_last_exit_reason"):
            try:
                r = self.stealth_mgr.get_last_exit_reason()
                if isinstance(r, str) and r:
                    reason = r
            except Exception:
                pass

        if reason != "network_offline":
            self.wake_displays()

        try:
            now = time.time()

            payload = {
                "state": "online",
                "event": "stealth_exit",
                "reason": reason,
                "stealth_mode": False,
                "ts": now,
                "session": self.get_session_seconds(),
                "boot_id": getattr(self, "boot_id", None),
            }
            self.mqtt.publish(TOPIC_STATUS, json.dumps(payload), qos=1)
            self.publish_telemetry()
            LOG.info("Published stealth_exit event (reason: %s) and updated telemetry to MQTT.", reason)
        except Exception as err:
            LOG.warning("Failed to publish stealth_exit event: %s", err)

    def check_stealth_offline_watchdog(self):
        """
        If stealth/blackout mode is active and the agent is offline from MQTT/network
        continuously for longer than configured offline timeout (default 10 min),
        emergency deactivates the overlay and unblocks mouse/keyboard input,
        while keeping displays powered off (DPMS black screen).
        """
        stealth_lock = self._get_stealth_lock()
        triggered = False
        prev_mute = None

        with stealth_lock:
            if getattr(self, "_stealth_activating", False) or not self.stealth_mgr.is_active():
                self._stealth_offline_since = None
                return

            is_offline = not getattr(self, "mqtt_online", False)
            if is_offline:
                offline_timeout_min = int(self.config.get("stealth_mode", {}).get("offline_timeout_minutes", 10))
                if offline_timeout_min <= 0:
                    self._stealth_offline_since = None
                    return
                now = time.time()
                if getattr(self, "_stealth_offline_since", None) is None:
                    self._stealth_offline_since = now
                    LOG.warning("Stealth mode: network/MQTT offline detected. Starting emergency offline watchdog.")
                else:
                    elapsed = now - self._stealth_offline_since
                    offline_timeout_sec = max(60, offline_timeout_min * 60)
                    if elapsed >= offline_timeout_sec:
                        LOG.warning(
                            "Stealth mode offline watchdog triggered (%.1f minutes continuously offline). "
                            "Emergency exit: dismissing overlay, unblocking input, keeping displays dark.",
                            elapsed / 60.0,
                        )
                        self._stealth_is_active = False
                        prev_mute = getattr(self, "_prev_mute_state", None)
                        self._prev_mute_state = None
                        self._stealth_offline_since = None
                        triggered = True
            else:
                if getattr(self, "_stealth_offline_since", None) is not None:
                    LOG.info("Stealth mode: network/MQTT connection restored. Resetting emergency watchdog.")
                    self._stealth_offline_since = None

        if not triggered:
            return

        self.stealth_mgr.deactivate()

        if self.stealth_mgr.mute_audio and prev_mute is not None:
            try:
                self.set_volume(mute=prev_mute)
            except Exception as err:
                LOG.warning("Failed to restore audio mute state: %s", err)

        if self.stealth_mgr.turn_off_displays:
            try:
                self.turn_off_displays()
            except Exception as err:
                LOG.warning("Failed to power off displays on emergency stealth exit: %s", err)
