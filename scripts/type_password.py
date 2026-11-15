import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "scripts" else SCRIPT_DIR
LOG_DIR = BASE_DIR / "logs"
LOG_PATH = LOG_DIR / "type_password.log"
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32

user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
user32.GetThreadDesktop.restype = wintypes.HANDLE

user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenInputDesktop.restype = wintypes.HANDLE

user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
user32.SetThreadDesktop.restype = wintypes.BOOL

user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL

user32.GetUserObjectInformationW.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
user32.GetUserObjectInformationW.restype = wintypes.BOOL

user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short

kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

user32.LoadKeyboardLayoutW.argtypes = [wintypes.LPCWSTR, wintypes.UINT]
user32.LoadKeyboardLayoutW.restype = wintypes.HKL

user32.ActivateKeyboardLayout.argtypes = [wintypes.HKL, wintypes.UINT]
user32.ActivateKeyboardLayout.restype = wintypes.HKL

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND

user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
user32.GetKeyboardLayout.restype = wintypes.HKL

user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL

user32.VkKeyScanExW.argtypes = [wintypes.WCHAR, wintypes.HKL]
user32.VkKeyScanExW.restype = ctypes.c_short

user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short

user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT

user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ctypes.c_void_p]
user32.keybd_event.restype = None

kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = wintypes.DWORD

kernel32.GetCurrentProcessId.argtypes = []
kernel32.GetCurrentProcessId.restype = wintypes.DWORD

kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
kernel32.ProcessIdToSessionId.restype = wintypes.BOOL

kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
kernel32.ReleaseMutex.restype = wintypes.BOOL


def _switch_layout_us_english():
    """Load English (US) layout and request the foreground window (e.g. LogonUI) to adopt it."""
    hkl = user32.LoadKeyboardLayoutW("00000409", 1)
    if hkl:
        user32.ActivateKeyboardLayout(hkl, 0)
        fg_hwnd = user32.GetForegroundWindow()
        if fg_hwnd:
            user32.PostMessageW(fg_hwnd, 0x0050, 0, hkl)  # WM_INPUTLANGCHANGEREQUEST
            time.sleep(0.05)
            try:
                thread_id = user32.GetWindowThreadProcessId(fg_hwnd, None)
                if thread_id:
                    curr_hkl = user32.GetKeyboardLayout(thread_id)
                    hkl_val = 0
                    if curr_hkl:
                        if isinstance(curr_hkl, int):
                            hkl_val = curr_hkl
                        elif hasattr(curr_hkl, "value"):
                            hkl_val = int(curr_hkl.value or 0)
                        else:
                            try:
                                hkl_val = int(ctypes.cast(curr_hkl, ctypes.c_void_p).value or 0)
                            except Exception:
                                hkl_val = 0
                    if (hkl_val & 0xFFFF) != 0x0409:
                        VK_MENU = 0x12
                        VK_SHIFT = 0x10
                        user32.keybd_event(VK_MENU, user32.MapVirtualKeyW(VK_MENU, 0), 0, None)
                        user32.keybd_event(VK_SHIFT, user32.MapVirtualKeyW(VK_SHIFT, 0), 0, None)
                        time.sleep(0.03)
                        user32.keybd_event(VK_SHIFT, user32.MapVirtualKeyW(VK_SHIFT, 0), 2, None)
                        user32.keybd_event(VK_MENU, user32.MapVirtualKeyW(VK_MENU, 0), 2, None)
                        time.sleep(0.05)
            except Exception as e:
                log(f"Failed querying/switching keyboard layout: {e}")
    return hkl


def _send_vk_with_modifiers(vk: int, mod_mask: int, hold_delay: float = 0.06):
    """Send a virtual key wrapped with Shift (1), Ctrl (2), and/or Alt (4) modifiers."""
    active_mods = []
    for bit, mod_vk in ((1, 0x10), (2, 0x11), (4, 0x12)):
        if mod_mask & bit:
            mod_scan = user32.MapVirtualKeyW(mod_vk, 0)
            active_mods.append((mod_vk, mod_scan))
            user32.keybd_event(mod_vk, mod_scan, 0, 0)
            time.sleep(0.02)

    vk_scan = user32.MapVirtualKeyW(vk, 0)
    user32.keybd_event(vk, vk_scan, 0, 0)
    time.sleep(hold_delay)
    user32.keybd_event(vk, vk_scan, 2, 0)
    time.sleep(hold_delay)

    for mod_vk, mod_scan in reversed(active_mods):
        user32.keybd_event(mod_vk, mod_scan, 2, 0)
        time.sleep(0.02)


def type_character(ch: str, hkl=None):
    """Map and type a single password character using the target keyboard layout."""
    if '0' <= ch <= '9':
        _send_vk_with_modifiers(0x30 + int(ch), 0)
        return

    scan = user32.VkKeyScanExW(ch, hkl) if hkl else user32.VkKeyScanW(ch)
    if scan == -1 or (scan & 0xFFFF) == 0xFFFF:
        raise ValueError(f"Unsupported character in password: ord={ord(ch)}")

    vk = scan & 0xFF
    mod_mask = (scan >> 8) & 0xFF
    _send_vk_with_modifiers(vk, mod_mask)

def _load_env_file(path: Path):
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
    except Exception:
        pass
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass

_load_env_file(ENV_PATH)

def log(msg):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.ctime()} - {msg}\n")
    except Exception:
        pass

def run_unlock():
    hdesk_in = None
    hdesk_orig = None
    mutex = None
    try:
        try:
            hdesk_orig = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        except Exception:
            pass

        pid = kernel32.GetCurrentProcessId()
        session_id = ctypes.c_uint32()
        kernel32.ProcessIdToSessionId(pid, ctypes.byref(session_id))
        
        # OS-level global mutex to prevent concurrent type_password execution
        mutex = kernel32.CreateMutexW(None, True, "Global\\W1bbexPCControlTypePasswordMutex")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            log(f"Another type_password process is already active. PID {pid} exiting immediately.")
            if mutex:
                kernel32.CloseHandle(mutex)
                mutex = None
            return

        def send_key(vk, delay=0.08):
            scan = user32.MapVirtualKeyW(vk, 0)
            user32.keybd_event(vk, scan, 0, 0)
            time.sleep(delay)
            user32.keybd_event(vk, scan, 2, 0)
            time.sleep(delay)

        def get_input_desktop_name():
            hdesk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_READOBJECTS
            if not hdesk:
                hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
            desk_name = "unknown"
            if hdesk:
                try:
                    name_buf = ctypes.create_unicode_buffer(256)
                    needed = wintypes.DWORD()
                    if user32.GetUserObjectInformationW(hdesk, 2, name_buf, ctypes.sizeof(name_buf), ctypes.byref(needed)):
                        desk_name = name_buf.value
                finally:
                    user32.CloseDesktop(hdesk)
            return desk_name

        # Attach thread to current active input desktop immediately
        hdesk_in = user32.OpenInputDesktop(0, False, 0x01FF)
        if not hdesk_in:
            hdesk_in = user32.OpenInputDesktop(0, False, 0x0100)
        attached = False
        if hdesk_in:
            attached = bool(user32.SetThreadDesktop(hdesk_in))

        desk_name = get_input_desktop_name()
        log(f"Started typing password. PID: {pid}, Session ID: {session_id.value}, Input Desktop: {desk_name}, Attached: {attached}")

        password = os.getenv("WINDOWS_PASSWORD")
        if not password and CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                password = cfg.get("unlock", {}).get("password")
            except Exception as e:
                log(f"Failed to read config.json: {e}")

        if not password:
            raise RuntimeError("Unlock password is not configured")

        def wake_display_and_dismiss_curtain():
            # Mouse jitter to wake physical monitor from DPMS sleep
            user32.mouse_event(0x0001, 2, 2, 0, 0)
            time.sleep(0.03)
            user32.mouse_event(0x0001, -2, -2, 0, 0)
            time.sleep(0.05)
            # Dismiss lock screen curtain (space / click)
            send_key(0x20, 0.08)  # SPACE
            time.sleep(0.05)
            user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
            time.sleep(0.03)
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP

        new_desk = desk_name
        if desk_name == "Winlogon":
            # Unlock window is ALREADY OPEN on Winlogon!
            log("Winlogon credential window is already active. Skipping wake-up key.")
            time.sleep(0.2)
            new_desk = "Winlogon"
        else:
            log("Waking screen and dismissing lock curtain via mouse move + click + SPACE...")
            wake_display_and_dismiss_curtain()

            # Fast poll for Winlogon desktop to appear as soon as LogonUI opens (up to 5s)
            deadline = time.time() + 5.0
            last_wake_action = time.time()
            while time.time() < deadline:
                time.sleep(0.15)
                cur_desk = get_input_desktop_name()
                if cur_desk == "Winlogon":
                    new_desk = "Winlogon"
                    break
                # If still on lock curtain after 1s, retry wake-up
                if time.time() - last_wake_action >= 1.0:
                    last_wake_action = time.time()
                    wake_display_and_dismiss_curtain()

            log(f"Desktop after wait: {new_desk}")

        if new_desk != "Winlogon":
            log(f"Active desktop is {new_desk} (not Winlogon). Failed to reach LogonUI window. Aborting to prevent typing into active user window!")
            sys.exit(2)

        # Switch thread to Winlogon desktop for credential entry
        hdesk_win = user32.OpenInputDesktop(0, False, 0x01FF)
        if not hdesk_win:
            hdesk_win = user32.OpenInputDesktop(0, False, 0x0100)
        if hdesk_win:
            attached_win = bool(user32.SetThreadDesktop(hdesk_win))
            log(f"Attached to Winlogon desktop: {attached_win}")
            if hdesk_in and hdesk_in != hdesk_win:
                try:
                    user32.CloseDesktop(hdesk_in)
                except Exception:
                    pass
            hdesk_in = hdesk_win

        # Clear any leftover characters in the PIN/password box cleanly
        log("Clearing input field via Ctrl+A and Backspace...")
        VK_CONTROL = 0x11
        ctrl_scan = user32.MapVirtualKeyW(VK_CONTROL, 0)
        user32.keybd_event(VK_CONTROL, ctrl_scan, 0, 0)
        time.sleep(0.02)
        send_key(0x41, 0.04)  # 'A'
        user32.keybd_event(VK_CONTROL, ctrl_scan, 2, 0)
        time.sleep(0.04)
        for _ in range(15):
            send_key(0x08, 0.02)

        # Ensure English layout for reliable key mapping on LogonUI / active window
        hkl = None
        try:
            hkl = _switch_layout_us_english()
        except Exception:
            pass

        # Ensure CapsLock is off to avoid password case inversion and account lockout
        VK_CAPITAL = 0x14
        if user32.GetKeyState(VK_CAPITAL) & 1:
            log("CapsLock is active! Disabling CapsLock to prevent password case inversion...")
            send_key(VK_CAPITAL, 0.04)
            time.sleep(0.05)

        time.sleep(0.1)
        log("Typing password...")

        for ch in str(password):
            try:
                type_character(ch, hkl=hkl)
            except ValueError as err:
                log(f"{err}. Aborting typing to prevent lockout!")
                sys.exit(1)

        time.sleep(0.2)
        log("Pressing ENTER")
        send_key(0x0D, 0.06)  # VK_RETURN
        log("Finished typing successfully")

    except SystemExit:
        raise
    except Exception as e:
        log(f"Error in type_password: {e}")
        sys.exit(1)
    finally:
        try:
            if hdesk_orig:
                user32.SetThreadDesktop(hdesk_orig)
            if hdesk_in:
                user32.CloseDesktop(hdesk_in)
        except Exception as e:
            log(f"Error closing desktop handle: {e}")
        try:
            if mutex:
                kernel32.ReleaseMutex(mutex)
                kernel32.CloseHandle(mutex)
        except Exception as e:
            log(f"Error releasing mutex: {e}")

if __name__ == "__main__":
    run_unlock()
