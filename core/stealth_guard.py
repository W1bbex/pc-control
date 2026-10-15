#!/usr/bin/env python3
"""Stealth and Blackout Guard for PCControlAgent.
Provides a fullscreen black TopMost overlay that absorbs 100% of mouse and keyboard inputs,
keeping games in background without crash, blanking displays, and providing emergency local unlock
via secret hotkey (or remote unlock via Telegram bot).
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Optional, Set, Tuple

LOG = logging.getLogger("stealth_guard")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

try:
    user32.ClipCursor.argtypes = [ctypes.POINTER(wintypes.RECT)]
    user32.ClipCursor.restype = wintypes.BOOL
except Exception:
    pass

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

try:
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
except Exception:
    pass


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


MODIFIER_VK_MAP = {
    0x5B: "win",
    0x5C: "win",
    0x11: "ctrl",
    0xA2: "ctrl",
    0xA3: "ctrl",
    0x12: "alt",
    0xA4: "alt",
    0xA5: "alt",
    0x10: "shift",
    0xA0: "shift",
    0xA1: "shift",
}

RUSSIAN_KEY_MAP = {
    "й": ord("Q"), "ц": ord("W"), "у": ord("E"), "к": ord("R"), "е": ord("T"),
    "н": ord("Y"), "г": ord("U"), "ш": ord("I"), "щ": ord("O"), "з": ord("P"),
    "х": 0xDB,     "ъ": 0xDD,     "ф": ord("A"), "ы": ord("S"), "в": ord("D"),
    "а": ord("F"), "п": ord("G"), "р": ord("H"), "о": ord("J"), "л": ord("K"),
    "д": ord("L"), "ж": 0xBA,     "э": 0xDE,     "я": ord("Z"), "ч": ord("X"),
    "с": ord("C"), "м": ord("V"), "и": ord("B"), "т": ord("N"), "ь": ord("M"),
    "б": 0xBC,     "ю": 0xBE,
}

VK_MAP = {
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "pause": 0x13,
}

# Add F1-F12
for i in range(1, 13):
    VK_MAP[f"f{i}"] = 0x6F + i

# Add Numpad 0-9
for i in range(10):
    VK_MAP[f"num{i}"] = 0x60 + i
    VK_MAP[f"numpad{i}"] = 0x60 + i


def normalize_vk(vk: int) -> int:
    """Normalize numpad digit keys (0x60-0x69) to standard top-row digit keys (0x30-0x39)."""
    if 0x60 <= vk <= 0x69:
        return vk - 0x60 + 0x30
    return vk


def vk_matches(candidate: int, target: int) -> bool:
    """Check if candidate VK code matches target VK code (accounting for numpad digits)."""
    if candidate == target:
        return True
    return normalize_vk(candidate) == normalize_vk(target)


def check_network_connectivity(timeout: float = 2.0) -> bool:
    """Check whether network is reachable via socket probe."""
    probes = [
        ("1.1.1.1", 443),
        ("8.8.8.8", 443),
        ("77.88.8.8", 443),
    ]
    for host, port in probes:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            return True
        except Exception:
            continue
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    return False


EXIT_REASON_FILE = Path(__file__).resolve().parent.parent / "data" / "stealth_exit_reason.json"


def parse_hotkey(hotkey_str: str) -> Tuple[Set[str], Set[int]]:
    """
    Parse hotkey string like 'Win+W+1' or 'Ctrl+Alt+Shift+Escape' into:
    (required_modifiers_set, required_vk_codes_set)
    """
    parts = [p.strip().lower() for p in hotkey_str.split("+") if p.strip()]
    modifiers: Set[str] = set()
    vks: Set[int] = set()

    for part in parts:
        if part in ("ctrl", "control"):
            modifiers.add("ctrl")
        elif part in ("alt", "menu"):
            modifiers.add("alt")
        elif part in ("shift",):
            modifiers.add("shift")
        elif part in ("win", "windows", "super"):
            modifiers.add("win")
        elif part in VK_MAP:
            vks.add(normalize_vk(VK_MAP[part]))
        elif part in RUSSIAN_KEY_MAP:
            vks.add(normalize_vk(RUSSIAN_KEY_MAP[part]))
        elif len(part) == 1 and ("a" <= part <= "z" or "0" <= part <= "9"):
            vks.add(normalize_vk(ord(part.upper())))
        else:
            LOG.warning("Unknown key in hotkey: %s", part)

    if not vks:
        vks.add(0x1B)

    return modifiers, vks


def parse_hotkey_sequence(hotkey_str: str) -> list[int]:
    """Extract ordered list of normalized non-modifier VK codes from hotkey string."""
    parts = [p.strip().lower() for p in hotkey_str.split("+") if p.strip()]
    seq: list[int] = []
    for part in parts:
        if part in ("ctrl", "control", "alt", "menu", "shift", "win", "windows", "super"):
            continue
        if part in VK_MAP:
            seq.append(normalize_vk(VK_MAP[part]))
        elif part in RUSSIAN_KEY_MAP:
            seq.append(normalize_vk(RUSSIAN_KEY_MAP[part]))
        elif len(part) == 1 and ("a" <= part <= "z" or "0" <= part <= "9"):
            seq.append(normalize_vk(ord(part.upper())))
    return seq


class StealthOverlay:
    """Fullscreen TopMost overlay window that absorbs mouse and keyboard events."""

    def __init__(
        self,
        hotkey: str = "Win+W+1",
        timeout_seconds: float = 14400.0,
        offline_timeout_seconds: float = 600.0,
    ):
        self.hotkey_str = hotkey
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.offline_timeout_seconds = max(0.0, float(offline_timeout_seconds))
        self.required_mods, self.required_vks = parse_hotkey(hotkey)
        self.required_seq = parse_hotkey_sequence(hotkey)

        self._mouse_hook = None
        self._keyboard_hook = None
        self._mouse_proc = None
        self._keyboard_proc = None
        self._root = None
        self._stopped = threading.Event()
        self.exit_reason = "unknown"

        # Keyboard state tracking
        self._down_keys: Set[int] = set()
        self._seq_idx: int = 0
        self._last_seq_time: float = 0.0
        self._recent_escapes: list[float] = []

        # Offline network watchdog tracking
        self._offline_start_time: Optional[float] = None
        self._network_thread: Optional[threading.Thread] = None
        self._prev_foreground_hwnd = None

    def _mouse_callback(self, nCode: int, wParam: int, lParam: int) -> int:
        if nCode >= 0:
            # Swallow all mouse movement, clicks, and wheel events completely
            return 1
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _are_modifiers_matching(self, allow_recent_win: bool = False) -> bool:
        """Check whether current modifier states match self.required_mods."""
        try:
            ctrl_hw = bool(user32.GetAsyncKeyState(0x11) & 0x8000)
            alt_hw = bool(user32.GetAsyncKeyState(0x12) & 0x8000)
            shift_hw = bool(user32.GetAsyncKeyState(0x10) & 0x8000)
            win_hw = bool((user32.GetAsyncKeyState(0x5B) | user32.GetAsyncKeyState(0x5C)) & 0x8000)
        except Exception:
            ctrl_hw = alt_hw = shift_hw = win_hw = False

        ctrl_down = ctrl_hw or any(k in self._down_keys for k in (0x11, 0xA2, 0xA3))
        alt_down = alt_hw or any(k in self._down_keys for k in (0x12, 0xA4, 0xA5))
        shift_down = shift_hw or any(k in self._down_keys for k in (0x10, 0xA0, 0xA1))
        win_down = win_hw or any(k in self._down_keys for k in (0x5B, 0x5C))

        if "ctrl" in self.required_mods and not ctrl_down:
            return False
        if "alt" in self.required_mods and not alt_down:
            return False
        if "shift" in self.required_mods and not shift_down:
            return False
        if "win" in self.required_mods and not win_down:
            return False
        return True

    def _keyboard_callback(self, nCode: int, wParam: int, lParam: int) -> int:
        if nCode >= 0:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = kb.vkCode
            flags = kb.flags
            is_key_up = bool(flags & 0x80) or (wParam in (WM_KEYUP, WM_SYSKEYUP))

            if is_key_up:
                self._down_keys.discard(vk)
                return 1

            # Key down event
            self._down_keys.add(vk)
            now = time.time()
            norm_vk = normalize_vk(vk)

            # 1. Emergency unlock: 5x Escape within 2.0 seconds
            if norm_vk == 0x1B:
                self._recent_escapes.append(now)
                self._recent_escapes = [t for t in self._recent_escapes if now - t <= 2.0]
                if len(self._recent_escapes) >= 5:
                    LOG.info("Emergency unlock triggered (5x Escape pressed). Exiting stealth mode...")
                    self.stop(reason="emergency_escape")
                    return 1

            # 2. Check modifiers (Win MUST be physically down)
            mods_ok = self._are_modifiers_matching(allow_recent_win=False)

            # 3. Check chord match (all sequence keys pressed simultaneously while modifiers held)
            chord_matched = False
            if mods_ok and self.required_vks:
                chord_matched = True
                for req_vk in self.required_vks:
                    req_norm = normalize_vk(req_vk)
                    is_down = (
                        (req_norm == norm_vk)
                        or any(normalize_vk(k) == req_norm for k in self._down_keys)
                        or bool(user32.GetAsyncKeyState(req_vk) & 0x8000)
                    )
                    if not is_down:
                        chord_matched = False
                        break

            if chord_matched:
                LOG.info("Secret unlock hotkey chord pressed (%s). Exiting stealth mode...", self.hotkey_str)
                self.stop(reason="local_hotkey")
                return 1

            # 4. Check strict sequential match (e.g. Win held down, then W, then 1, within 1.5s per tap)
            if mods_ok and self.required_seq:
                expected_vk = self.required_seq[self._seq_idx]
                if norm_vk == expected_vk:
                    if self._seq_idx == 0:
                        self._seq_idx = 1
                        self._last_seq_time = now
                    else:
                        if now - self._last_seq_time <= 1.5:
                            self._seq_idx += 1
                            self._last_seq_time = now
                        else:
                            self._seq_idx = 1 if norm_vk == self.required_seq[0] else 0
                            self._last_seq_time = now

                    if self._seq_idx >= len(self.required_seq):
                        LOG.info("Secret unlock hotkey sequence completed (%s). Exiting stealth mode...", self.hotkey_str)
                        self.stop(reason="local_hotkey")
                        return 1
                else:
                    # Extraneous key breaks sequential matching!
                    self._seq_idx = 0
            else:
                self._seq_idx = 0

            # Swallow all other keystrokes
            return 1

        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _lock_cursor_and_topmost(self, set_pos: bool = False):
        """Confine cursor to 1x1 pixel at (0, 0) and enforce TopMost z-order."""
        try:
            rect = wintypes.RECT(0, 0, 1, 1)
            user32.ClipCursor(ctypes.byref(rect))
            if set_pos:
                user32.SetCursorPos(0, 0)
        except Exception as err:
            LOG.debug("ClipCursor error: %s", err)

        if self._root:
            try:
                hwnd = self._root.winfo_id()
                if hwnd:
                    # HWND_TOPMOST = -1, SWP_NOMOVE = 0x0002, SWP_NOSIZE = 0x0001, SWP_NOACTIVATE = 0x0010
                    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010)
            except Exception:
                pass

    def _periodic_guard(self):
        if self._stopped.is_set():
            return
        self._lock_cursor_and_topmost(set_pos=False)
        if self._root and not self._stopped.is_set():
            self._root.after(500, self._periodic_guard)

    def _listen_stdin(self):
        """Listen on stdin for 'stop' command from parent process."""
        try:
            while True:
                line = sys.stdin.readline()
                if not line:
                    # EOF reached (parent closed stdin or died)
                    LOG.info("Stdin EOF reached. Exiting stealth mode...")
                    self.stop(reason="stdin_eof")
                    break
                line_str = line.strip().lower()
                if line_str in ("stop", "exit", "quit"):
                    LOG.info("Received stop command from stdin. Exiting stealth mode...")
                    self.stop(reason="remote_command")
                    break
        except Exception:
            pass

    def _network_probe_loop(self):
        """Background probe loop to detect prolonged offline status."""
        while not self._stopped.is_set():
            if self.offline_timeout_seconds > 0:
                online = check_network_connectivity(timeout=2.0)
                now = time.time()
                if not online:
                    if self._offline_start_time is None:
                        self._offline_start_time = now
                        LOG.warning(
                            "StealthOverlay: Network offline detected. Emergency watchdog started (%ds timeout)",
                            int(self.offline_timeout_seconds),
                        )
                    else:
                        elapsed = now - self._offline_start_time
                        if elapsed >= self.offline_timeout_seconds:
                            LOG.warning(
                                "StealthOverlay: Network offline for %.1f minutes. Emergency watchdog triggered: blanking display and exiting.",
                                elapsed / 60.0,
                            )
                            # Re-send DPMS monitor power off command
                            try:
                                user32.SendMessageTimeoutW(0xFFFF, 0x0112, 0xF170, 2, 0x0002, 1000, None)
                            except Exception:
                                pass
                            self.stop(reason="network_offline")
                            break
                else:
                    if self._offline_start_time is not None:
                        LOG.info("StealthOverlay: Network connection restored. Resetting watchdog.")
                        self._offline_start_time = None
            self._stopped.wait(10.0)

    def run(self):
        """Initialize hooks, create Tkinter overlay and enter main event loop."""
        import tkinter as tk

        # Setup low-level hooks
        self._mouse_proc = HOOKPROC(self._mouse_callback)
        self._keyboard_proc = HOOKPROC(self._keyboard_callback)

        self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, None, 0)
        self._keyboard_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._keyboard_proc, None, 0)

        if not self._mouse_hook or not self._keyboard_hook:
            LOG.error("Failed to install Windows low-level input hooks!")

        # Create Tkinter TopMost black window
        self._root = tk.Tk()
        self._root.title("BlackoutStealthOverlay")
        self._root.configure(background="black")
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.config(cursor="none")

        # Geometry spanning all virtual monitors
        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        self._root.geometry(f"{w}x{h}+{x}+{y}")

        # Start stdin listener thread
        stdin_thread = threading.Thread(target=self._listen_stdin, daemon=True)
        stdin_thread.start()

        # Start offline network watchdog thread
        if self.offline_timeout_seconds > 0:
            self._network_thread = threading.Thread(
                target=self._network_probe_loop,
                name="stealth-network-probe",
                daemon=True,
            )
            self._network_thread.start()

        self._lock_cursor_and_topmost(set_pos=True)
        self._root.after(500, self._periodic_guard)

        # Watchdog timeout
        timeout_ms = int(self.timeout_seconds * 1000)
        self._root.after(timeout_ms, self._on_watchdog_timeout)

        # Force focus to overlay window so games release input capture
        try:
            self._prev_foreground_hwnd = user32.GetForegroundWindow()
            self._root.focus_force()
            hwnd = self._root.winfo_id()
            if hwnd:
                user32.SetForegroundWindow(hwnd)
        except Exception as err:
            LOG.debug("Focus force exception: %s", err)

        LOG.info(
            "Stealth blackout overlay started. Virtual screen: %dx%d at (%d,%d). Hotkey: %s. Watchdog: %ds. Offline watchdog: %ds",
            w, h, x, y, self.hotkey_str, int(self.timeout_seconds), int(self.offline_timeout_seconds)
        )

        try:
            self._root.mainloop()
        finally:
            self._cleanup()

    def _on_watchdog_timeout(self):
        LOG.warning("Watchdog timer expired (%ds). Auto-deactivating stealth mode.", int(self.timeout_seconds))
        self.stop(reason="watchdog_timeout")

    def stop(self, reason: str = "remote_command"):
        """Signal overlay to close cleanly and persist exit reason."""
        if self._stopped.is_set():
            return
        self.exit_reason = reason
        self._stopped.set()

        try:
            EXIT_REASON_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "reason": reason,
                "timestamp": time.time(),
                "hotkey": self.hotkey_str,
            }
            tmp = EXIT_REASON_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(EXIT_REASON_FILE)
        except Exception as err:
            LOG.debug("Failed saving stealth exit reason: %s", err)

        if self._root:
            try:
                self._root.after(0, self._root.quit)
            except Exception:
                pass

    def _cleanup(self):
        """Unhook low-level hooks, destroy window, restore foreground window and unfreeze cursor."""
        if self._mouse_hook:
            try:
                user32.UnhookWindowsHookEx(self._mouse_hook)
            except Exception:
                pass
            self._mouse_hook = None
        if self._keyboard_hook:
            try:
                user32.UnhookWindowsHookEx(self._keyboard_hook)
            except Exception:
                pass
            self._keyboard_hook = None

        if self._root:
            try:
                self._root.destroy()
            except Exception:
                pass
            self._root = None

        # Restore cursor clipping and visibility cleanly
        try:
            user32.ClipCursor(None)
        except Exception:
            pass

        # Reactivate foreground window to avoid window orphanage & mouse freeze
        try:
            prev_hwnd = self._prev_foreground_hwnd
            if prev_hwnd and user32.IsWindow(prev_hwnd):
                user32.SetForegroundWindow(prev_hwnd)
            else:
                shell_hwnd = user32.GetShellWindow()
                if shell_hwnd and user32.IsWindow(shell_hwnd):
                    user32.SetForegroundWindow(shell_hwnd)
        except Exception as err:
            LOG.debug("Failed restoring foreground window: %s", err)

        # Center cursor and nudge hit-testing
        try:
            sw = user32.GetSystemMetrics(0)  # SM_CXSCREEN
            sh = user32.GetSystemMetrics(1)  # SM_CYSCREEN
            cx = sw // 2 if sw > 0 else 500
            cy = sh // 2 if sh > 0 else 500
            user32.SetCursorPos(cx, cy)
            # MOUSEEVENTF_MOVE = 0x0001
            user32.mouse_event(0x0001, 1, 1, 0, 0)
            user32.mouse_event(0x0001, -1, -1, 0, 0)
        except Exception as err:
            LOG.debug("Failed centering/nudging cursor: %s", err)

        try:
            # IDC_ARROW = 32512
            arrow = user32.LoadCursorW(None, 32512)
            if arrow:
                user32.SetCursor(arrow)
            user32.ShowCursor(True)
        except Exception:
            pass

        LOG.info("Stealth blackout overlay terminated cleanly.")


class StealthManager:
    """Manages the background stealth overlay process and lifecycle."""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        stealth_cfg = self.config.get("stealth_mode", {})
        self.default_hotkey = stealth_cfg.get("secret_hotkey", "Win+W+1")
        self.default_timeout_minutes = stealth_cfg.get("watchdog_timeout_minutes", 240)
        self.default_offline_timeout_minutes = stealth_cfg.get("offline_timeout_minutes", 10)
        self.mute_audio = stealth_cfg.get("mute_audio", True)
        self.turn_off_displays = stealth_cfg.get("turn_off_displays", True)

        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()

    def is_active(self) -> bool:
        """Check if stealth overlay process is currently alive."""
        with self._lock:
            if self._process is None:
                return False
            poll = self._process.poll()
            if poll is not None:
                self._process = None
                return False
            return True

    def activate(
        self,
        hotkey: Optional[str] = None,
        timeout_minutes: Optional[int] = None,
        offline_timeout_minutes: Optional[int] = None,
    ) -> bool:
        """Spawn the background stealth overlay process."""
        with self._lock:
            if self.is_active():
                LOG.info("Stealth mode already active (PID %s)", self._process.pid)
                return True

            hk = hotkey or self.default_hotkey
            tm = int(timeout_minutes or self.default_timeout_minutes)
            timeout_sec = tm * 60
            otm = int(offline_timeout_minutes or self.default_offline_timeout_minutes)
            offline_sec = otm * 60

            cmd = [
                sys.executable,
                "-u",
                "-m",
                "core.stealth_guard",
                "--hotkey",
                hk,
                "--timeout",
                str(timeout_sec),
                "--offline-timeout",
                str(offline_sec),
            ]

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=str(Path(__file__).resolve().parent.parent),
                )
                LOG.info(
                    "Stealth mode process launched (PID %s, hotkey: %s, offline_timeout: %dm)",
                    self._process.pid, hk, otm
                )
                return True
            except Exception as err:
                LOG.exception("Failed to launch stealth guard process: %s", err)
                self._process = None
                return False

    def deactivate(self) -> bool:
        """Gracefully stop the background stealth overlay process."""
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._process = None
                return True

            try:
                # Send 'stop\n' to stdin and close pipe
                if self._process.stdin:
                    try:
                        self._process.stdin.write(b"stop\n")
                        self._process.stdin.flush()
                    except Exception:
                        pass
                    try:
                        self._process.stdin.close()
                    except Exception:
                        pass
                self._process.wait(timeout=2.0)
            except Exception:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=1.0)
                except Exception:
                    try:
                        self._process.kill()
                        self._process.wait(timeout=1.0)
                    except Exception:
                        pass

            self._process = None
            LOG.info("Stealth mode process stopped.")
            return True

    def toggle(
        self,
        hotkey: Optional[str] = None,
        timeout_minutes: Optional[int] = None,
        offline_timeout_minutes: Optional[int] = None,
    ) -> bool:
        """Toggle stealth mode state."""
        if self.is_active():
            return not self.deactivate()
        else:
            return self.activate(
                hotkey=hotkey,
                timeout_minutes=timeout_minutes,
                offline_timeout_minutes=offline_timeout_minutes,
            )

    def get_status(self) -> dict:
        """Return current status dict for telemetry."""
        active = self.is_active()
        return {
            "active": active,
            "pid": self._process.pid if active and self._process else None,
            "hotkey": self.default_hotkey,
            "timeout_minutes": self.default_timeout_minutes,
            "offline_timeout_minutes": self.default_offline_timeout_minutes,
        }

    def get_last_exit_reason(self) -> str:
        """Read the persistent exit reason recorded by the stealth overlay process."""
        try:
            if EXIT_REASON_FILE.exists():
                data = json.loads(EXIT_REASON_FILE.read_text(encoding="utf-8"))
                ts = float(data.get("timestamp", 0))
                # Only trust if recorded recently (within last 60s)
                if time.time() - ts < 60.0:
                    return data.get("reason", "local_hotkey")
        except Exception as err:
            LOG.debug("Error reading stealth exit reason: %s", err)
        return "local_hotkey"


def main():
    parser = argparse.ArgumentParser(description="Stealth Blackout Guard")
    parser.add_argument("--hotkey", default="Ctrl+Alt+Shift+Escape", help="Secret exit hotkey")
    parser.add_argument("--timeout", type=float, default=14400.0, help="Watchdog timeout in seconds")
    parser.add_argument("--offline-timeout", type=float, default=600.0, help="Offline network emergency timeout in seconds")
    args = parser.parse_args()

    # Logging to stdout
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    overlay = StealthOverlay(
        hotkey=args.hotkey,
        timeout_seconds=args.timeout,
        offline_timeout_seconds=args.offline_timeout,
    )

    def sig_handler(signum, frame):
        overlay.stop(reason="signal")

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    overlay.run()


if __name__ == "__main__":
    main()
