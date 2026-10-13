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

user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short

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
                    if user32.GetUserObjectInformationW(hdesk, 2, name_buf, 256, None):
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

        # Ensure English layout for reliable key mapping
        try:
            hkl = user32.LoadKeyboardLayoutW("00000409", 1)
            if hkl:
                user32.ActivateKeyboardLayout(hkl, 0)
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
        VK_SHIFT = 0x10
        shift_scan = user32.MapVirtualKeyW(VK_SHIFT, 0)

        for ch in str(password):
            if ch.isdigit():
                vk = 0x30 + int(ch)
                send_key(vk, 0.06)
                continue

            scan = user32.VkKeyScanW(ord(ch))
            if scan == -1 or (scan & 0xFFFF) == 0xFFFF:
                log(f"Unsupported character in password: ord={ord(ch)}. Aborting typing to prevent lockout!")
                sys.exit(1)

            vk = scan & 0xFF
            need_shift = bool((scan >> 8) & 1)

            if need_shift:
                user32.keybd_event(VK_SHIFT, shift_scan, 0, 0)
                time.sleep(0.02)
                send_key(vk, 0.06)
                user32.keybd_event(VK_SHIFT, shift_scan, 2, 0)
                time.sleep(0.02)
            else:
                send_key(vk, 0.06)

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
