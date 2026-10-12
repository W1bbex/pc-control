"""Stealth mode, window minimize and display power control mixin."""

import ctypes
import json
import logging
import time

from desktop.constants import TOPIC_STATUS

LOG = logging.getLogger("pc-control-agent")


class StealthMixin:
    """Provides blackout/stealth mode, display sleep/wake, and window minimization."""

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

            LOG.info("Minimizing windows via Win+M fallback (Pass 1)...")
            pyautogui.hotkey("win", "m")
            time.sleep(2.0)
            LOG.info("Minimizing windows via Win+M fallback (Pass 2)...")
            pyautogui.hotkey("win", "m")
            time.sleep(2.0)
            LOG.info("Minimizing windows via Win+M fallback (Pass 3)...")
            pyautogui.hotkey("win", "m")
        finally:
            if com_initialized:
                pythoncom.CoUninitialize()
        LOG.info("Minimized all windows after workspace launch (completed 3 passes)")

    @staticmethod
    def turn_off_displays():
        """Request that Windows powers down every attached display only."""
        hwnd_broadcast = 0xFFFF
        wm_syscommand = 0x0112
        sc_monitorpower = 0xF170
        monitor_off = 2
        result = ctypes.windll.user32.SendMessageTimeoutW(
            hwnd_broadcast,
            wm_syscommand,
            sc_monitorpower,
            monitor_off,
            0x0002,  # SMTO_ABORTIFHUNG
            2000,
            None,
        )
        if not result:
            raise RuntimeError("Windows did not accept the display power command")
        LOG.info("Requested display power-off")
        return {"displays": "off"}

    @staticmethod
    def wake_displays():
        """Request that Windows powers up displays and wake up GPU swapchain."""
        hwnd_broadcast = 0xFFFF
        wm_syscommand = 0x0112
        sc_monitorpower = 0xF170
        monitor_on = -1
        try:
            ctypes.windll.user32.SendMessageTimeoutW(
                hwnd_broadcast,
                wm_syscommand,
                sc_monitorpower,
                monitor_on,
                0x0002,
                1000,
                None,
            )
        except Exception:
            pass
        try:
            import pyautogui

            pyautogui.moveRel(1, 0, duration=0)
            pyautogui.moveRel(-1, 0, duration=0)
        except Exception:
            pass
        LOG.info("Requested display wake-up")
        return {"displays": "on"}

    def handle_stealth_action(self, state: str = "toggle", hotkey=None, timeout=None) -> dict:
        """Handle stealth / blackout mode activation or deactivation."""
        if state == "toggle":
            target = "off" if self.stealth_mgr.is_active() else "on"
        else:
            target = str(state).lower()

        if target in ("on", "activate", "true", "1"):
            if self.stealth_mgr.mute_audio:
                try:
                    vol = self.get_volume()
                    self._prev_mute_state = vol.get("muted") if isinstance(vol, dict) else False
                    self.set_volume(mute=True)
                except Exception as err:
                    LOG.warning("Failed to mute audio for stealth mode: %s", err)

            self._stealth_is_active = True
            self.stealth_mgr.activate(hotkey=hotkey, timeout_minutes=timeout)
            time.sleep(0.3)

            if self.stealth_mgr.turn_off_displays:
                try:
                    self.turn_off_displays()
                except Exception as err:
                    LOG.warning("Failed to power off displays for stealth mode: %s", err)

            return {"stealth": "active", "active": True}
        else:
            self._stealth_is_active = False
            self._stealth_offline_since = None
            self.stealth_mgr.deactivate()

            if self.stealth_mgr.mute_audio and self._prev_mute_state is not None:
                try:
                    self.set_volume(mute=self._prev_mute_state)
                except Exception as err:
                    LOG.warning("Failed to restore audio mute state: %s", err)
                self._prev_mute_state = None

            self.wake_displays()
            return {"stealth": "inactive", "active": False}

    def check_stealth_process_exit(self):
        """
        Detects if stealth overlay exited locally (via secret hotkey Win+W+1 or 5x Escape).
        Automatically restores audio mute state, wakes displays, and publishes stealth_exit event to MQTT.
        """
        was_active = self._stealth_is_active or (self._prev_mute_state is not None)
        if was_active and not self.stealth_mgr.is_active():
            LOG.info("Stealth process exited locally (via hotkey/watchdog). Restoring audio, display, and notifying Telegram...")
            self._stealth_is_active = False
            if self.stealth_mgr.mute_audio and self._prev_mute_state is not None:
                try:
                    self.set_volume(mute=self._prev_mute_state)
                except Exception as err:
                    LOG.warning("Failed to restore audio mute state: %s", err)
            self._prev_mute_state = None
            self._stealth_offline_since = None
            self.wake_displays()

            try:
                now = time.time()
                payload = {
                    "state": "online",
                    "event": "stealth_exit",
                    "reason": "local_hotkey",
                    "stealth_mode": False,
                    "ts": now,
                    "session": self.get_session_seconds(),
                    "boot_id": getattr(self, "boot_id", None),
                }
                self.mqtt.publish(TOPIC_STATUS, json.dumps(payload), qos=1)
                self.publish_telemetry()
                LOG.info("Published stealth_exit event and updated telemetry to MQTT.")
            except Exception as err:
                LOG.warning("Failed to publish stealth_exit event: %s", err)

    def check_stealth_offline_watchdog(self):
        """
        If stealth/blackout mode is active and the agent is offline from MQTT/network
        continuously for longer than configured offline timeout (default 10 min),
        emergency deactivates the overlay and unblocks mouse/keyboard input,
        while keeping displays powered off (DPMS black screen).
        """
        if not self.stealth_mgr.is_active():
            self._stealth_offline_since = None
            return

        is_offline = not getattr(self, "mqtt_online", False)

        if is_offline:
            now = time.time()
            if self._stealth_offline_since is None:
                self._stealth_offline_since = now
                LOG.warning("Stealth mode: network/MQTT offline detected. Starting emergency offline watchdog.")
            else:
                elapsed = now - self._stealth_offline_since
                offline_timeout_min = self.config.get("stealth_mode", {}).get("offline_timeout_minutes", 10)
                offline_timeout_sec = max(60, int(offline_timeout_min) * 60)
                if elapsed >= offline_timeout_sec:
                    LOG.warning(
                        "Stealth mode offline watchdog triggered (%.1f minutes continuously offline). "
                        "Emergency exit: dismissing overlay, unblocking input, keeping displays dark.",
                        elapsed / 60.0,
                    )
                    self._stealth_is_active = False
                    self.stealth_mgr.deactivate()

                    if self.stealth_mgr.mute_audio and self._prev_mute_state is not None:
                        try:
                            self.set_volume(mute=self._prev_mute_state)
                        except Exception as err:
                            LOG.warning("Failed to restore audio mute state: %s", err)
                        self._prev_mute_state = None

                    if self.stealth_mgr.turn_off_displays:
                        try:
                            self.turn_off_displays()
                        except Exception as err:
                            LOG.warning("Failed to power off displays on emergency stealth exit: %s", err)

                    self._stealth_offline_since = None
        else:
            if self._stealth_offline_since is not None:
                LOG.info("Stealth mode: network/MQTT connection restored. Resetting emergency watchdog.")
                self._stealth_offline_since = None
