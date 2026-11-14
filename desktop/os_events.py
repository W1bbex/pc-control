#!/usr/bin/env python3
"""Desktop OS Events, Foreground Tracking and Screenshot Capture.

Provides:
- Capture and compression of multi-monitor screenshots for MQTT transmission.
- Win32 hidden window message pump for graceful Windows shutdown/logoff detection (WM_QUERYENDSESSION / WM_ENDSESSION).
- Foreground window and active process identification helpers using Win32 API.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import io
import json
import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from PIL import ImageGrab
import psutil

LOG = logging.getLogger("pc-control.desktop.os_events")

TELEGRAM_CLIENTS = ("telegram", "ayugram", "kotatogram", "64gram", "unigram")


def capture_desktop_screenshot(
    command_id: str,
    max_payload_bytes: int = 950_000,
) -> Tuple[bytes, Dict[str, Any]]:
    """Captures desktop screenshot across all monitors and packs it into MQTT payload.

    Returns:
        (payload_bytes, metadata_dict)
    """
    image = ImageGrab.grab(all_screens=True)
    image = image.convert("RGB")
    image.thumbnail((1600, 1000))

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=72, optimize=True)
    image_data = buffer.getvalue()

    header = json.dumps(
        {
            "id": command_id,
            "format": "jpeg",
            "width": image.width,
            "height": image.height,
        }
    ).encode("utf-8")

    payload = len(header).to_bytes(4, "big") + header + image_data
    if len(payload) > max_payload_bytes:
        raise RuntimeError("Screenshot exceeds MQTT safety limit")

    meta = {
        "bytes": len(image_data),
        "width": image.width,
        "height": image.height,
    }
    return payload, meta


def start_shutdown_watcher(on_shutdown_callback: Callable[[], None]) -> None:
    """Spawns a daemon thread running a hidden Win32 window message loop to capture WM_ENDSESSION."""
    def _watcher():
        try:
            import win32gui
            import win32con

            def wndproc(hwnd, msg, wparam, lparam):
                if msg == win32con.WM_QUERYENDSESSION:
                    return 1
                if msg == win32con.WM_ENDSESSION:
                    if not wparam:
                        return 0
                    LOG.info("Windows shutdown/logoff confirmed via WM_ENDSESSION (msg=0x%x, lparam=0x%x)", msg, lparam)
                    try:
                        on_shutdown_callback()
                    except Exception:
                        LOG.exception("Error in shutdown callback")
                    return 0
                return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

            wc = win32gui.WNDCLASS()
            wc.lpfnWndProc = wndproc
            wc.lpszClassName = f"PCControlDesktopShutdownWatcher_{os.getpid()}"
            hinst = wc.hInstance = win32gui.GetModuleHandle(None)
            class_atom = win32gui.RegisterClass(wc)
            hwnd = win32gui.CreateWindow(
                class_atom, "PCControlShutdownWatcher", 0, 0, 0, 0, 0, 0, 0, hinst, None
            )
            LOG.info("Shutdown watcher window initialized (hwnd=%s)", hwnd)
            win32gui.PumpMessages()
        except Exception:
            LOG.exception("Shutdown watcher window failed to initialize")

    threading.Thread(target=_watcher, name="shutdown-watcher", daemon=True).start()


def resolve_hwnd_app_info(hwnd: int) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Inspects a Windows HWND and returns (exe_path, exe_name, window_title)."""
    if not hwnd or sys.platform != "win32":
        return None, None, None

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe_path = None
    exe_name = None

    if pid.value:
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if hproc:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_ulong(1024)
                if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                    exe_path = buf.value
                    exe_name = os.path.basename(exe_path)
            finally:
                kernel32.CloseHandle(hproc)

        if not exe_name:
            try:
                proc = psutil.Process(pid.value)
                exe_name = proc.name()
                exe_path = proc.exe()
            except Exception:
                pass

    if not exe_name or exe_name.lower() == "explorer.exe":
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        if cls_buf.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
            exe_name = "explorer.exe"

    title = get_window_title(hwnd)
    return exe_path, exe_name, title


def get_window_title(hwnd: int) -> Optional[str]:
    """Retrieves window title string for a Win32 HWND."""
    if not hwnd or sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    tlen = user32.GetWindowTextLengthW(hwnd)
    if tlen:
        tbuf = ctypes.create_unicode_buffer(tlen + 1)
        user32.GetWindowTextW(hwnd, tbuf, tlen + 1)
        return tbuf.value or None
    return None


def is_telegram_control_chat(exe_key: Optional[str], title: Optional[str]) -> bool:
    """Checks whether the foreground window is Telegram focused on the PC control bot chat."""
    if not exe_key or not any(c in exe_key for c in TELEGRAM_CLIENTS):
        return False
    if not title:
        return False
    title_lower = title.lower()
    return "pc control" in title_lower or "your_pc_bot" in title_lower
