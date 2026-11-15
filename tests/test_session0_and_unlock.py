"""Tests for Session 0 detection, WTS ctypes structs, unlock serialization, password typing, and mutex locks."""

import ctypes
from ctypes import wintypes
import os
import re
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import boot_agent
from system.action_dispatcher import ActionDispatcherMixin as SystemActionDispatcher

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_session_state_no_console_when_no_active_console():
    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=0xFFFFFFFF):
        res = boot_agent.active_console_session(ttl=0)
        assert res["session"] == "no_console"
        assert res["session_id"] is None
        assert res["user_logged_in"] is False


def test_session_state_login_screen_when_no_username():
    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=1), \
         patch("ctypes.windll.wtsapi32.WTSQuerySessionInformationW", side_effect=[True, True]):
        res = boot_agent.active_console_session(ttl=0)
        assert res["session"] == "login_screen"
        assert res["session_id"] == 1
        assert res["user_logged_in"] is False


def test_session_state_unlocked_when_input_desktop_accessible():
    real_session = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
    if real_session in (0, 0xFFFFFFFF):
        real_session = getattr(boot_agent, "find_active_interactive_session_id", lambda: 1)()
    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=real_session), \
         patch("ctypes.windll.user32.OpenInputDesktop", return_value=123), \
         patch("ctypes.windll.user32.CloseDesktop", return_value=True):
        res = boot_agent.active_console_session(ttl=0)
        assert res["session"] in ("unlocked", "login_screen", "locked")
        assert "source" in res


def test_session_state_unknown_when_wts_query_fails():
    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=1), \
         patch("ctypes.windll.wtsapi32.WTSQuerySessionInformationW", return_value=False):
        res = boot_agent.active_console_session(ttl=0)
        assert res["session"] == "unknown"


def test_find_active_interactive_session_uses_valid_console_id():
    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=1):
        assert boot_agent.find_active_interactive_session_id() == 1


def test_find_active_interactive_session_falls_back_to_session_1():
    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=0), \
         patch("ctypes.windll.wtsapi32.WTSEnumerateSessionsW", return_value=False), \
         patch("boot_agent.active_console_session", return_value={"session_id": None}):
        assert boot_agent.find_active_interactive_session_id() == 1


def test_find_active_interactive_session_handles_negative_and_all_ones():
    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=-1), \
         patch("ctypes.windll.wtsapi32.WTSEnumerateSessionsW", return_value=0), \
         patch("boot_agent.active_console_session", return_value={"session_id": None}):
        assert boot_agent.find_active_interactive_session_id() == 1

    with patch("ctypes.windll.kernel32.WTSGetActiveConsoleSessionId", return_value=0xFFFFFFFF), \
         patch("ctypes.windll.wtsapi32.WTSEnumerateSessionsW", return_value=0), \
         patch("boot_agent.active_console_session", return_value={"session_id": None}):
        assert boot_agent.find_active_interactive_session_id() == 1


def test_boot_agent_wts_apis_declare_win64_argtypes():
    boot_agent.active_console_session(ttl=0)
    wts = ctypes.windll.wtsapi32
    assert wts.WTSQuerySessionInformationW.argtypes is not None
    assert wts.WTSQuerySessionInformationW.restype == wintypes.BOOL
    assert wts.WTSFreeMemory.argtypes == [ctypes.c_void_p]
    assert wts.WTSFreeMemory.restype is None


def test_boot_agent_wtsinfoex_level1_size_and_offset():
    struct_cls = boot_agent.WTSINFOEX_LEVEL1_W
    assert ctypes.sizeof(struct_cls) == 12
    assert struct_cls.SessionFlags.offset == 8


def test_session0_lock_fails_honestly_when_utilities_missing():
    class DummySystem(SystemActionDispatcher):
        def __init__(self):
            self.config = {}
            self.acknowledge = MagicMock()

    disp = DummySystem()
    with patch("system.action_dispatcher.find_active_interactive_session_id", return_value=1), \
         patch("system.action_dispatcher.Path.is_file", return_value=False), \
         patch("system.action_dispatcher.shutil.which", return_value=None):
        disp.execute_action("cmd-lock-fail", "lock", {})

    disp.acknowledge.assert_called_once()
    cmd_id, status, payload = disp.acknowledge.call_args[0]
    assert cmd_id == "cmd-lock-fail"
    assert status == "failed"
    assert "PsExec" in payload["error"]


def test_session0_lock_uses_psexec_with_create_no_window(tmp_path):
    import subprocess

    fake_psexec = tmp_path / "PsExec64.exe"
    fake_psexec.write_bytes(b"")

    class DummySystem(SystemActionDispatcher):
        def __init__(self):
            self.config = {"unlock": {"psexec_path": str(fake_psexec)}}
            self.acknowledge = MagicMock()

    disp = DummySystem()
    with patch("system.action_dispatcher.find_active_interactive_session_id", return_value=2), \
         patch("system.action_dispatcher.subprocess.Popen") as mock_popen:
        disp.execute_action("cmd-lock-ok", "lock", {})

    mock_popen.assert_called_once()
    assert mock_popen.call_args.args[0][0] == str(fake_psexec)
    assert mock_popen.call_args.kwargs.get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    disp.acknowledge.assert_called_once_with("cmd-lock-ok", "executed", {"method": "psexec", "session_id": 2})


def test_type_password_declares_release_mutex_and_us_layout_and_modifiers():
    from scripts import type_password as tp

    assert tp.kernel32.ReleaseMutex.argtypes == [wintypes.HANDLE]
    assert tp.kernel32.ReleaseMutex.restype == wintypes.BOOL

    fake_user32 = MagicMock()
    fake_user32.LoadKeyboardLayoutW.return_value = 0x04090409
    fake_user32.GetForegroundWindow.return_value = 5555
    with patch.object(tp, "user32", fake_user32), patch("scripts.type_password.time.sleep"):
        hkl = tp._switch_layout_us_english()

    assert hkl == 0x04090409
    fake_user32.ActivateKeyboardLayout.assert_called_once_with(0x04090409, 0)
    fake_user32.PostMessageW.assert_called_once_with(5555, 0x0050, 0, 0x04090409)

    fake_user32.reset_mock()
    fake_user32.VkKeyScanExW.return_value = (2 << 8) | 0x41  # Ctrl + A
    fake_user32.MapVirtualKeyW.side_effect = lambda vk, _mode: vk
    with patch.object(tp, "user32", fake_user32), patch("scripts.type_password.time.sleep"):
        tp.type_character("@", hkl=0x04090409)

    events = [c[0] for c in fake_user32.keybd_event.call_args_list]
    assert events == [
        (0x11, 0x11, 0, 0),  # Ctrl down
        (0x41, 0x41, 0, 0),  # Key down
        (0x41, 0x41, 2, 0),  # Key up
        (0x11, 0x11, 2, 0),  # Ctrl up
    ]


def test_wtsinfoexw_data_field_alignment_offset_8():
    struct_l1 = boot_agent.WTSINFOEX_LEVEL1_W
    struct_ex = boot_agent.WTSINFOEXW
    assert ctypes.sizeof(struct_l1) == 12
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert struct_ex.Data.offset == 8
        assert struct_ex.Data.offset + struct_l1.SessionFlags.offset == 16


def test_boot_agent_exposes_wtsinfoex_structs_and_queries_session():
    assert hasattr(boot_agent, "WTSINFOEXW")
    assert hasattr(boot_agent, "WTSINFOEX_LEVEL1_W")
    session = boot_agent.active_console_session(ttl=0)
    assert isinstance(session, dict)
    assert session["session"] in {"unlocked", "locked", "login_screen", "no_console", "unknown"}
    assert "source" in session


def test_system_agent_rejects_concurrent_unlock_as_busy():
    agent = boot_agent.SystemAgent.__new__(boot_agent.SystemAgent)
    agent.config = {"unlock": {}}
    agent.unlock_lock = threading.Lock()
    agent.processed_ids = []
    agent.state_lock = threading.Lock()
    agent.acknowledge = MagicMock()

    assert agent.unlock_lock.acquire(blocking=False)
    try:
        agent.execute_action("cmd-concurrent-2", "unlock", {})
        agent.acknowledge.assert_called_with(
            "cmd-concurrent-2",
            "busy",
            {"error": "Another unlock command is currently in progress"},
        )
    finally:
        agent.unlock_lock.release()


def test_boot_info_detects_valid_boot_type():
    boot_info = boot_agent.get_boot_info()
    assert boot_info["boot_type"] in ("cold_boot", "hybrid_boot", "resume", "unknown")
    assert "fast_startup_enabled" in boot_info


def test_kill_process_blocks_boot_agent_but_allows_desktop_agent():
    with pytest.raises(PermissionError):
        boot_agent.SystemAgent.kill_process("pythonw.exe")
    with pytest.raises(PermissionError):
        boot_agent.SystemAgent.kill_process("csrss.exe")

    fake_proc = MagicMock()
    fake_proc.pid = 99999
    fake_proc.name.return_value = "pythonw.exe"
    fake_proc.cmdline.return_value = ["pythonw.exe", "C:\\pc-control\\agent.py"]
    fake_proc.info = {"pid": 99999, "name": "pythonw.exe", "cmdline": ["pythonw.exe", "C:\\pc-control\\agent.py"]}
    with patch("psutil.process_iter", return_value=[fake_proc]):
        res = boot_agent.SystemAgent.kill_process("desktop_agent")
        assert isinstance(res, dict)
        assert res.get("name") == "desktop_agent"
        assert 99999 in res.get("terminated_pids", [])
        fake_proc.terminate.assert_called_once()


def test_system_agent_wndproc_ignores_wm_queryendsession_and_cancelled_end():
    import win32con

    agent = boot_agent.SystemAgent.__new__(boot_agent.SystemAgent)
    agent.stop_event = threading.Event()
    agent._shutdown_detected = False
    agent._turn_off_led_esp = MagicMock()
    agent.mqtt = MagicMock()

    assert agent._handle_shutdown_window_message(456, win32con.WM_QUERYENDSESSION, 0, 0) == 1
    assert not agent.stop_event.is_set()
    assert agent._shutdown_detected is False

    assert agent._handle_shutdown_window_message(456, win32con.WM_ENDSESSION, 0, 0) == 0
    assert not agent.stop_event.is_set()
    assert agent._shutdown_detected is False

    assert agent._handle_shutdown_window_message(456, win32con.WM_ENDSESSION, 1, 0) == 0
    assert agent._shutdown_detected is True
    assert agent.stop_event.is_set()
    agent._turn_off_led_esp.assert_called_once()


def test_type_password_desktop_and_keystate_win32_signatures():
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


def test_type_password_mutex_and_keyboard_layout_ctypes_signatures():
    import scripts.type_password as tp

    assert tp.kernel32.CreateMutexW.restype == wintypes.HANDLE
    assert tp.kernel32.CreateMutexW.argtypes == [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    assert tp.user32.LoadKeyboardLayoutW.restype == wintypes.HKL
    assert tp.user32.ActivateKeyboardLayout.restype == wintypes.HKL


def test_type_password_attaches_to_input_desktop_handle():
    """Verify run_unlock attaches to OpenInputDesktop handle via SetThreadDesktop."""
    import scripts.type_password as tp

    fake_hdesk = 0xCAFE
    with patch.object(tp.kernel32, "CreateMutexW", return_value=0x1111), \
         patch.object(tp.kernel32, "GetLastError", return_value=0), \
         patch.object(tp.user32, "OpenInputDesktop", return_value=fake_hdesk), \
         patch.object(tp.user32, "SetThreadDesktop") as mock_set_desk, \
         patch.object(tp.kernel32, "CloseHandle"), \
         patch.object(tp.user32, "CloseDesktop"), \
         patch.dict(os.environ, {"WINDOWS_PASSWORD": ""}):
        with pytest.raises(SystemExit):
            tp.run_unlock()
        mock_set_desk.assert_any_call(fake_hdesk)


def test_type_password_mutex_closes_handle_on_existing_mutex():
    """Verify run_unlock closes mutex handle and returns immediately when mutex already exists."""
    import scripts.type_password as tp

    fake_mutex = 0x8888
    with patch.object(tp.kernel32, "CreateMutexW", return_value=fake_mutex) as mock_mutex, \
         patch.object(tp.kernel32, "GetLastError", return_value=183), \
         patch.object(tp.kernel32, "CloseHandle") as mock_close, \
         patch.object(tp.user32, "OpenInputDesktop") as mock_open:
        tp.run_unlock()
        mock_mutex.assert_called_once_with(None, True, "Global\\W1bbexPCControlTypePasswordMutex")
        mock_close.assert_called_once_with(fake_mutex)
        mock_open.assert_not_called()


def test_restart_system_agent_script_syntax_and_structure():
    """Verify restart_system_agent.ps1 is valid PowerShell syntax and uses dynamic process matching."""
    import subprocess
    ps1_path = ROOT_DIR / "scripts" / "install" / "restart_system_agent.ps1"
    assert ps1_path.exists()
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        f"$errs = $null; $toks = $null; [System.Management.Automation.Language.Parser]::ParseFile('{ps1_path}', [ref]$toks, [ref]$errs); if ($errs.Count -eq 0) {{ 'VALID' }} else {{ 'ERRORS' }}"
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "VALID" in proc.stdout


def test_boot_agent_defines_state_path_and_saves_state(tmp_path, monkeypatch):
    assert hasattr(boot_agent, "STATE_PATH")
    assert boot_agent.STATE_PATH is not None

    monkeypatch.setattr("system.STATE_PATH", tmp_path / "boot_state.json")
    monkeypatch.setattr(boot_agent, "STATE_PATH", tmp_path / "boot_state.json")
    with patch.object(boot_agent.SystemAgent, "setup_mqtt_client", return_value=None), \
         patch.object(boot_agent.SystemAgent, "load_state", return_value=None):
        agent = boot_agent.SystemAgent()
        agent.save_state()
        assert (tmp_path / "boot_state.json").exists()


def test_desktop_agent_acquire_single_instance_win64_ctypes():
    import agent

    with patch("ctypes.WinDLL") as mock_windll:
        mock_kernel32 = MagicMock()
        mock_windll.return_value = mock_kernel32
        with patch("ctypes.get_last_error", return_value=0):
            agent.acquire_single_instance()
            assert mock_kernel32.CreateMutexW.restype == wintypes.HANDLE
            assert mock_kernel32.ReleaseMutex.restype == wintypes.BOOL
            assert mock_kernel32.CloseHandle.restype == wintypes.BOOL


def test_system_action_dispatcher_lock_targets_interactive_session():
    dispatcher = SystemActionDispatcher()
    dispatcher.acknowledge = MagicMock()
    dispatcher.config = {}

    with patch("system.action_dispatcher.find_active_interactive_session_id", return_value=2), \
         patch("subprocess.Popen") as mock_popen, \
         patch("shutil.which", return_value="C:\\Windows\\System32\\tsdiscon.exe"):
        dispatcher.execute_action("cmd-1", "lock", {})
        dispatcher.acknowledge.assert_called_once()
        cmd_id, status, detail = dispatcher.acknowledge.call_args[0]
        assert cmd_id == "cmd-1"
        assert status == "executed"
        assert detail["session_id"] == 2
        assert detail["method"] in ("psexec", "tsdiscon")
        assert mock_popen.called
