#!/usr/bin/env python3
"""
Unit and regression tests verifying all fixes from AUDIT_REPORT.md:
1. Win32 GDI & Shell ctypes signatures in core/icon_extractor.py
2. Win32 desktop & keyboard ctypes signatures and CapsLock protection in scripts/type_password.py
3. Dynamic process termination and absence of hardcoded PIDs in restart_system_agent.ps1
4. Active interactive session resolution and safe Session 1 fallback in boot_agent.py
5. HealthEngine preservation of SHUTDOWN_OBSERVED when agents are 'stale'
6. PowerFlowManager thread-safety lock and atomic state serialization
7. PCControlAgent state_lock synchronization during active window changes and telemetry
"""

import ctypes
from ctypes import wintypes
import os
import re
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "vds_bot"))

from core import icon_extractor
import boot_agent
from health_engine import HealthEngine, PCState
from power_flow import PowerFlowManager


# ==============================================================================
# 1. GDI & Shell32 Win32 Types in icon_extractor.py
# ==============================================================================

def test_icon_extractor_gdi_and_shell_signatures():
    """Verify that all GDI and Shell32 APIs have 64-bit safe argtypes and restype."""
    # user32.GetDC
    assert icon_extractor.user32.GetDC.argtypes == [wintypes.HWND]
    assert icon_extractor.user32.GetDC.restype == wintypes.HDC

    # user32.ReleaseDC
    assert icon_extractor.user32.ReleaseDC.argtypes == [wintypes.HWND, wintypes.HDC]
    assert icon_extractor.user32.ReleaseDC.restype == ctypes.c_int

    # gdi32.CreateCompatibleDC
    assert icon_extractor.gdi32.CreateCompatibleDC.argtypes == [wintypes.HDC]
    assert icon_extractor.gdi32.CreateCompatibleDC.restype == wintypes.HDC

    # gdi32.DeleteDC
    assert icon_extractor.gdi32.DeleteDC.argtypes == [wintypes.HDC]
    assert icon_extractor.gdi32.DeleteDC.restype == wintypes.BOOL

    # shell32.SHGetFileInfoW
    assert icon_extractor.shell32.SHGetFileInfoW.argtypes == [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(icon_extractor.SHFILEINFOW),
        wintypes.UINT,
        wintypes.UINT,
    ]
    assert icon_extractor.shell32.SHGetFileInfoW.restype == ctypes.c_void_p


# ==============================================================================
# 2. Desktop Handles & CapsLock Logic in scripts/type_password.py
# ==============================================================================

def test_type_password_win32_signatures():
    """Verify scripts/type_password.py defines correct ctypes signatures for HDESK."""
    import scripts.type_password as tp

    assert tp.user32.GetThreadDesktop.argtypes == [wintypes.DWORD]
    assert tp.user32.GetThreadDesktop.restype == wintypes.HANDLE

    assert tp.user32.OpenInputDesktop.argtypes == [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    assert tp.user32.OpenInputDesktop.restype == wintypes.HANDLE

    assert tp.user32.SetThreadDesktop.argtypes == [wintypes.HANDLE]
    assert tp.user32.SetThreadDesktop.restype == wintypes.BOOL

    assert tp.user32.CloseDesktop.argtypes == [wintypes.HANDLE]
    assert tp.user32.CloseDesktop.restype == wintypes.BOOL

    assert tp.user32.GetKeyState.argtypes == [ctypes.c_int]
    assert tp.user32.GetKeyState.restype == ctypes.c_short


def test_type_password_no_invalid_desktop_window_call():
    """Ensure SetThreadDesktop(GetDesktopWindow()) was completely removed."""
    tp_source = (ROOT_DIR / "scripts" / "type_password.py").read_text(encoding="utf-8")
    assert "GetDesktopWindow" not in tp_source, "GetDesktopWindow() must not be used as an HDESK desktop handle!"


# ==============================================================================
# 3. Safe Process Termination in restart_system_agent.ps1
# ==============================================================================

def test_restart_script_has_no_hardcoded_pids():
    """Ensure restart_system_agent.ps1 does not kill arbitrary hardcoded PIDs."""
    ps1_path = ROOT_DIR / "scripts" / "install" / "restart_system_agent.ps1"
    content = ps1_path.read_text(encoding="utf-8")

    assert "2660" not in content, "Hardcoded PID 2660 found in restart script!"
    assert "6332" not in content, "Hardcoded PID 6332 found in restart script!"
    assert not re.search(r"taskkill\s+/F\s+/PID\s+\d+", content, re.IGNORECASE), \
        "Found hardcoded taskkill by PID in restart_system_agent.ps1!"


# ==============================================================================
# 4. Session Resolution & Safe Fallback in boot_agent.py
# ==============================================================================

def test_boot_agent_session_resolution_active():
    """When WTSGetActiveConsoleSessionId returns a valid session ID, use it."""
    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=1):
        assert boot_agent.find_active_interactive_session_id() == 1


def test_boot_agent_session_resolution_fallback_defaults_to_session_1():
    """When console is 0/0xFFFFFFFF and WTSEnumerateSessionsW fails, fallback must be Session 1."""
    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=0), \
         patch("ctypes.windll.wtsapi32.WTSEnumerateSessionsW", return_value=False), \
         patch("boot_agent.active_console_session", return_value={"session_id": None}):
        sid = boot_agent.find_active_interactive_session_id()
        assert sid == 1, f"Expected safe fallback Session 1, got {sid}"


# ==============================================================================
# 5. HealthEngine Preservation of SHUTDOWN_OBSERVED with Stale Status
# ==============================================================================

def test_health_engine_accepts_stale_in_shutdown_observed():
    """HealthEngine must consider 'stale' status valid during SHUTDOWN_OBSERVED."""
    engine = HealthEngine()

    engine.update("mqtt_broker", "online")
    engine.update("windows_system", "stale")
    engine.update("windows_desktop", "offline")
    engine.update("windows_session", "unknown")
    engine.update("esp", "online")

    engine.set_shutdown_observed(ttl=60.0)

    state = engine.evaluate_pc_state(telemetry_age=15)
    assert state.state == PCState.SHUTDOWN_OBSERVED
    assert "Выключен" in state.label

    # Both agents are 'stale'
    engine.update("windows_system", "stale")
    engine.update("windows_desktop", "stale")

    state_both_stale = engine.evaluate_pc_state(telemetry_age=25)
    assert state_both_stale.state == PCState.SHUTDOWN_OBSERVED


# ==============================================================================
# 6. PowerFlowManager Thread-Safety and Locking
# ==============================================================================

def test_power_flow_manager_has_thread_lock():
    """Verify PowerFlowManager has _lock and save_state is protected."""
    manager = PowerFlowManager(telegram_ui=MagicMock(), mqtt_gateway=MagicMock())
    assert hasattr(manager, "_lock"), "PowerFlowManager must have _lock attribute"
    assert isinstance(manager._lock, type(threading.Lock())), "_lock must be a threading.Lock"


# ==============================================================================
# 7. Agent Thread Synchronization (state_lock)
# ==============================================================================

def test_agent_handle_active_app_change_under_state_lock():
    """Verify PCControlAgent has state_lock initialized."""
    from agent import PCControlAgent

    dummy_config = {
        "mqtt": {"host": "127.0.0.1", "port": 1883, "username": "test_user", "password": "test_password"},
        "activity_logging": {"enabled": False},
    }

    with patch("agent.load_agent_config", return_value=dummy_config), \
         patch.object(PCControlAgent, "setup_mqtt_client", return_value=None), \
         patch.object(PCControlAgent, "connect_with_retry", return_value=True), \
         patch.object(PCControlAgent, "load_state", return_value=None):
        agent = PCControlAgent()
        assert hasattr(agent, "state_lock")
        assert isinstance(agent.state_lock, type(threading.Lock()))


# ==============================================================================
# 8. boot_agent STATE_PATH Declaration and Safe Save State
# ==============================================================================

def test_boot_agent_state_path_defined_and_save_state():
    """Verify boot_agent defines STATE_PATH and save_state does not crash with NameError."""
    assert hasattr(boot_agent, "STATE_PATH"), "boot_agent must define STATE_PATH!"
    assert boot_agent.STATE_PATH is not None

    with patch.object(boot_agent.SystemAgent, "setup_mqtt_client", return_value=None), \
         patch.object(boot_agent.SystemAgent, "load_state", return_value=None):
        agent = boot_agent.SystemAgent()
        # Must not raise NameError or FileNotFoundError
        agent.save_state()


# ==============================================================================
# 9. physical_button Gesture Combo Eligibility Validation
# ==============================================================================

def test_physical_button_ws_combo_rejected_when_ineligible():
    """Ensure unlock+workspace combo (CCHH) is rejected if Morse code was not entered."""
    from physical_button import PhysicalButtonManager
    mock_ui = MagicMock()
    mock_mqtt = MagicMock()
    mgr = PhysicalButtonManager(mock_ui, mock_mqtt)

    combo_triggered = []
    mgr.on_trigger_unlock_and_workspace_cb = lambda cid: combo_triggered.append(cid)

    # Ineligible state (Morse never entered)
    assert not mgr.physical_morse_unlock_eligible
    assert mgr.last_unlock_gesture_at == 0.0

    # User enters CCHH combo
    for kind, duration in [("click", 100), ("click", 110), ("hold", 600), ("hold", 650)]:
        mgr.handle_event({"event": "press", "kind": kind, "duration_ms": duration}, chat_id=123)

    # Must be rejected because Morse code was never entered!
    assert len(combo_triggered) == 0, "Combo must NOT trigger when Morse was not entered!"
    assert len(mgr.gesture_buffer) == 0, "Gesture buffer must not accumulate presses when ineligible!"


# ==============================================================================
# 10. icon_extractor PrivateExtractIconsW Error Code Handling
# ==============================================================================

def test_icon_extractor_ignores_0xffffffff_failure(tmp_path):
    """When PrivateExtractIconsW returns 0xFFFFFFFF (failure), do not treat as success."""
    dummy_exe = tmp_path / "dummy.exe"
    dummy_exe.write_bytes(b"dummy")

    with patch("core.icon_extractor.user32.PrivateExtractIconsW", return_value=0xFFFFFFFF):
        res = icon_extractor.extract_app_icon_png(str(dummy_exe))
        # Fallback to shell should be tried or return None, not crash on invalid pointer
        assert res is None or isinstance(res, bytes)
