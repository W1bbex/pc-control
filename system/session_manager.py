import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import threading
import time

import psutil

from system.constants import LOG


def acquire_single_instance():
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL

    mutex = kernel32.CreateMutexW(
        None,
        True,
        "Global\\W1bbexPCControlSystemAgent",
    )
    err = kernel32.GetLastError()
    if err == 183:
        if mutex:
            kernel32.CloseHandle(mutex)
        raise SystemExit("PC Control System Agent is already running")
    if not mutex and err == 5:
        mutex = kernel32.CreateMutexW(
            None,
            True,
            "Local\\W1bbexPCControlSystemAgent",
        )
        if kernel32.GetLastError() == 183:
            if mutex:
                kernel32.CloseHandle(mutex)
            raise SystemExit("PC Control System Agent is already running")
    return mutex


_console_session_cache = None
_console_session_cache_time = 0.0
_console_session_cache_lock = threading.Lock()

_cached_boot_info = None


def get_boot_info():
    """Detect boot type (cold_boot, hybrid_boot, resume) from Windows Event Log and registry (cached)."""
    global _cached_boot_info
    if _cached_boot_info is not None:
        return _cached_boot_info

    boot_type = "unknown"
    boot_code = None
    boot_label = "Неизвестный тип запуска"
    fast_startup_enabled = None

    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Power"
        ) as key:
            fast_startup_enabled = bool(winreg.QueryValueEx(key, "HiberbootEnabled")[0])
    except Exception:
        pass

    try:
        out = subprocess.run(
            [
                "wevtutil", "qe", "System",
                "/q:*[System[Provider[@Name='Microsoft-Windows-Kernel-Boot'] and (EventID=27 or EventID=20)]]",
                "/rd:true", "/c:1", "/f:RenderedXml"
            ],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if out.returncode == 0 and out.stdout.strip():
            import xml.etree.ElementTree as ET
            root = ET.fromstring(out.stdout)
            data = {}
            for el in root.iter("{http://schemas.microsoft.com/win/2004/08/events/event}Data"):
                name = el.attrib.get("Name")
                if name:
                    data[name] = el.text

            if "BootType" in data:
                boot_code = int(data["BootType"])
                if boot_code == 0:
                    boot_type = "cold_boot"
                    boot_label = "Холодный запуск"
                elif boot_code == 1:
                    boot_type = "hybrid_boot"
                    boot_label = "Windows восстановлена через быстрый запуск"
                elif boot_code == 2:
                    boot_type = "resume"
                    boot_label = "Выход из гибернации"
    except Exception as e:
        LOG.warning("Could not read boot type from event log: %s", e)

    _cached_boot_info = {
        "boot_type": boot_type,
        "boot_code": boot_code,
        "boot_label": boot_label,
        "fast_startup_enabled": fast_startup_enabled,
    }
    return _cached_boot_info


class WTSINFOEX_LEVEL1_W(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.ULONG),
        ("SessionState", wintypes.ULONG),
        ("SessionFlags", wintypes.LONG),
    ]


class WTSINFOEXW(ctypes.Structure):
    _fields_ = [
        ("Level", wintypes.DWORD),
    ] + ([("_pad", wintypes.DWORD)] if ctypes.sizeof(ctypes.c_void_p) == 8 else []) + [
        ("Data", WTSINFOEX_LEVEL1_W),
    ]


def active_console_session(ttl=0.5):
    """
    Query real state of active console session using Windows API (WTS + LogonUI + Input Desktop).
    Returns honest states: no_console, login_screen, locked, unlocked, unknown.
    Compatible with Session 0 service isolation.
    """
    global _console_session_cache, _console_session_cache_time
    now = time.time()
    with _console_session_cache_lock:
        if _console_session_cache is not None and (now - _console_session_cache_time) < ttl:
            return _console_session_cache

    kernel32 = ctypes.windll.kernel32
    wtsapi32 = ctypes.windll.wtsapi32
    user32 = ctypes.windll.user32

    wtsapi32.WTSQuerySessionInformationW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
    wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    wtsapi32.WTSFreeMemory.restype = None

    kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    kernel32.WTSGetActiveConsoleSessionId.argtypes = []
    session_id = int(kernel32.WTSGetActiveConsoleSessionId())
    if session_id in (0xFFFFFFFF, -1):
        res = {
            "session": "no_console",
            "session_id": None,
            "user_logged_in": False,
            "source": "WTSGetActiveConsoleSessionId=0xFFFFFFFF",
        }
        with _console_session_cache_lock:
            _console_session_cache = res
            _console_session_cache_time = now
        return res

    # 1. Query Username
    username_buf = ctypes.c_void_p()
    returned = wintypes.DWORD()
    username = ""
    user_query_ok = bool(wtsapi32.WTSQuerySessionInformationW(
        None,
        session_id,
        5,  # WTSUserName
        ctypes.byref(username_buf),
        ctypes.byref(returned),
    ))
    if user_query_ok and username_buf.value:
        try:
            username = ctypes.wstring_at(username_buf.value) or ""
        finally:
            wtsapi32.WTSFreeMemory(username_buf)

    # 2. Query Connect State
    cstate_buf = ctypes.c_void_p()
    connect_state = None
    cstate_ok = bool(wtsapi32.WTSQuerySessionInformationW(
        None,
        session_id,
        8,  # WTSConnectState
        ctypes.byref(cstate_buf),
        ctypes.byref(returned),
    ))
    if cstate_ok and cstate_buf.value:
        try:
            connect_state = ctypes.cast(cstate_buf, ctypes.POINTER(ctypes.c_int)).contents.value
        finally:
            wtsapi32.WTSFreeMemory(cstate_buf)

    # 3. Query Input Desktop accessibility with explicit 64-bit handle types
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL

    hdesk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    err_desk = ctypes.GetLastError()
    input_desktop_accessible = bool(hdesk)
    input_desk_name = ""
    if hdesk:
        try:
            name_buf = ctypes.create_unicode_buffer(256)
            if user32.GetUserObjectInformationW(hdesk, 2, name_buf, 256, None):
                input_desk_name = name_buf.value
        except Exception:
            pass
        finally:
            user32.CloseDesktop(hdesk)

    # Conflict / error check
    if not user_query_ok:
        res = {
            "session": "unknown",
            "session_id": session_id,
            "user_logged_in": False,
            "source": "WTSQuerySessionInformation_failed",
        }
        with _console_session_cache_lock:
            _console_session_cache = res
            _console_session_cache_time = now
        return res

    # If no user is logged in, this is definitively the logon screen
    if not username:
        res = {
            "session": "login_screen",
            "session_id": session_id,
            "user_logged_in": False,
            "source": f"no_user(connect_state={connect_state})",
        }
        with _console_session_cache_lock:
            _console_session_cache = res
            _console_session_cache_time = now
        return res

    # 4. Query SessionFlags via WTSSessionInfoEx (25) - works across session boundaries / Session 0!
    session_flags = None
    infoex_buf = ctypes.c_void_p()
    infoex_bytes = wintypes.DWORD()
    infoex_ok = bool(wtsapi32.WTSQuerySessionInformationW(
        None,
        session_id,
        25,  # WTSSessionInfoEx
        ctypes.byref(infoex_buf),
        ctypes.byref(infoex_bytes),
    ))
    if infoex_ok and infoex_buf.value:
        try:
            info = ctypes.cast(infoex_buf, ctypes.POINTER(WTSINFOEXW)).contents
            session_flags = info.Data.SessionFlags
        finally:
            wtsapi32.WTSFreeMemory(infoex_buf)

    # 5. Determine state based on cross-validated signals
    if connect_state is not None and connect_state != 0:
        state = "locked"
        source = f"wts_disconnected(connect_state={connect_state})"
    elif session_flags == 1:  # WTS_SESSIONSTATE_UNLOCK
        state = "unlocked"
        source = "wts_session_flags_unlocked"
    elif session_flags == 0:  # WTS_SESSIONSTATE_LOCK
        state = "locked"
        source = "wts_session_flags_locked"
    elif input_desktop_accessible:
        state = "unlocked"
        source = "input_desktop_accessible"
    else:
        # Fallback only when flags and input desktop are inconclusive
        logonui_found = False
        try:
            for p in psutil.process_iter(['name', 'pid']):
                try:
                    pname = (p.info.get('name') or '').lower()
                    if pname == 'logonui.exe':
                        psess = ctypes.c_uint32()
                        if kernel32.ProcessIdToSessionId(p.info['pid'], ctypes.byref(psess)):
                            if psess.value == session_id:
                                logonui_found = True
                                break
                        else:
                            logonui_found = True
                            break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass

        if not logonui_found:
            state = "unlocked"
            source = "logonui_not_running"
        else:
            state = "locked"
            source = f"input_desktop_denied(err={err_desk})"

    res = {
        "session": state,
        "session_id": session_id,
        "user_logged_in": bool(username),
        "source": source,
    }
    with _console_session_cache_lock:
        _console_session_cache = res
        _console_session_cache_time = now
    return res


def find_active_interactive_session_id():
    kernel32 = ctypes.windll.kernel32
    wtsapi32 = ctypes.windll.wtsapi32
    kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    kernel32.WTSGetActiveConsoleSessionId.argtypes = []

    # 1. Primary: WTSGetActiveConsoleSessionId
    sess_id = int(kernel32.WTSGetActiveConsoleSessionId())
    if sess_id not in (0xFFFFFFFF, 0, -1):
        return sess_id

    # 2. Secondary: enumerate sessions for state 0 (WTSActive) or any non-zero user session
    class _WTS_SESSION_INFOW(ctypes.Structure):
        _fields_ = [
            ("SessionId", wintypes.DWORD),
            ("pWinStationName", wintypes.LPWSTR),
            ("State", ctypes.c_int),
        ]

    p_info = ctypes.POINTER(_WTS_SESSION_INFOW)()
    count = wintypes.DWORD()
    fallback_sid = None
    if wtsapi32.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(p_info), ctypes.byref(count)):
        try:
            for i in range(count.value):
                entry = p_info[i]
                sid_val = int(entry.SessionId)
                if sid_val != 0:
                    if entry.State == 0:  # 0 = WTSActive
                        return sid_val
                    if fallback_sid is None:
                        fallback_sid = sid_val
        finally:
            wtsapi32.WTSFreeMemory(p_info)

    # 3. Tertiary: query cached active_console_session (check if patched in boot_agent or local)
    try:
        boot_mod = sys.modules.get("boot_agent")
        acs = getattr(boot_mod, "active_console_session", active_console_session) if boot_mod else active_console_session
        info = acs(ttl=0)
        sid = info.get("session_id")
        if sid and sid not in (0xFFFFFFFF, 0, -1):
            return int(sid)
    except Exception:
        pass

    if fallback_sid is not None:
        return fallback_sid

    return 1 if (sess_id in (0xFFFFFFFF, 0, -1)) else sess_id
