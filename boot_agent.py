#!/usr/bin/env python3

import ctypes
from ctypes import wintypes
import json
import logging
import logging.handlers
import os
import platform
import queue
import signal
import socket
import shutil
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import certifi
import paho.mqtt.client as mqtt
import psutil
import uuid

AGENT_VERSION = "2.0"
AGENT_BOOT_ID = f"sys-{uuid.uuid4().hex[:12]}"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "config.json"
_data_state = DATA_DIR / "boot-state.json"
_root_state = BASE_DIR / "boot-state.json"
STATE_PATH = _data_state if (DATA_DIR.exists() or not _root_state.exists()) else _root_state
_data_snapshot = DATA_DIR / "admin-sensors.json"
_hw_sensors_snapshot = BASE_DIR / "hardware" / "sensors" / "admin-sensors.json"
_hw_snapshot = BASE_DIR / "hardware" / "admin-sensors.json"
SENSOR_SNAPSHOT = _data_snapshot if _data_snapshot.exists() else (_hw_sensors_snapshot if _hw_sensors_snapshot.exists() else _hw_snapshot)
LOG_DIR = BASE_DIR / "logs"

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

_load_env_file(BASE_DIR / ".env")

TOPIC_COMMAND = "pc/main/windows/system/command"
TOPIC_ACK = "pc/main/windows/system/ack"
TOPIC_STATUS = "pc/main/windows/system/status"
TOPIC_TELEMETRY = "pc/main/windows/system/telemetry"


def get_real_boot_time():
    """Returns actual boot timestamp using Windows Kernel-Boot event log or psutil fallback."""
    try:
        import win32evtlog
        import xml.etree.ElementTree as ET
        from datetime import datetime
        query_str = '*[System[Provider[@Name="Microsoft-Windows-Kernel-Boot"] and (EventID=27 or EventID=20)]]'
        query = win32evtlog.EvtQuery(
            'System',
            win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection,
            query_str
        )
        try:
            events = win32evtlog.EvtNext(query, 1)
            if events:
                try:
                    xml_data = win32evtlog.EvtRender(events[0], win32evtlog.EvtRenderEventXml)
                    root = ET.fromstring(xml_data)
                    for elem in root.iter():
                        if elem.tag.endswith('TimeCreated') and 'SystemTime' in elem.attrib:
                            st = elem.attrib['SystemTime']
                            dt = datetime.fromisoformat(st.replace('Z', '+00:00'))
                            return dt.timestamp()
                finally:
                    try:
                        win32evtlog.EvtClose(events[0])
                    except Exception:
                        pass
        finally:
            try:
                win32evtlog.EvtClose(query)
            except Exception:
                pass
    except Exception:
        pass
    try:
        return psutil.boot_time()
    except Exception:
        return time.time()


REAL_BOOT_TIME = get_real_boot_time()

PROTECTED_PROCESSES = {
    "csrss.exe",
    "dwm.exe",
    "lsass.exe",
    "services.exe",
    "smss.exe",
    "system",
    "wininit.exe",
    "winlogon.exe",
    "python.exe",
    "pythonw.exe",
    "svchost.exe",
    "spoolsv.exe",
}


def configure_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pc-control-system-agent")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(message)s"
    )
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "boot-agent.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


LOG = configure_logging()


def patch_paho_sockpair():
    """Protect paho-mqtt against Windows socketpair crashes (WinError 10054 / 1236) on sleep/reset."""
    orig_loop = mqtt.Client._loop

    def safe_loop(self, timeout=1.0):
        try:
            return orig_loop(self, timeout)
        except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
            LOG.warning("MQTT _loop caught Windows socket error: %s (triggering reconnect)", e)
            try:
                self.disconnect()
            except Exception:
                pass
            return mqtt.MQTT_ERR_CONN_LOST

    mqtt.Client._loop = safe_loop

    orig_packet_queue = mqtt.Client._packet_queue

    def safe_packet_queue(self, command, packet, mid=0, qos=0, info=None):
        try:
            return orig_packet_queue(self, command, packet, mid, qos, info)
        except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
            LOG.warning("MQTT _packet_queue caught Windows socketpair error: %s", e)
            return mqtt.MQTT_ERR_NO_CONN

    mqtt.Client._packet_queue = safe_packet_queue


patch_paho_sockpair()


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
        ("State", wintypes.LONG),
        ("SessionFlags", wintypes.LONG),
    ]


class WTSINFOEXW(ctypes.Structure):
    _fields_ = [
        ("Level", wintypes.DWORD),
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

    session_id = int(kernel32.WTSGetActiveConsoleSessionId())
    if session_id == 0xFFFFFFFF:
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
    logonui_found = None
    if not username:
        state = "login_screen"
        source = f"no_user(connect_state={connect_state})"
    else:
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

    # 1. Primary: WTSGetActiveConsoleSessionId
    sess_id = int(kernel32.WTSGetActiveConsoleSessionId())
    if sess_id != 0xFFFFFFFF and sess_id != 0:
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

    # 3. Tertiary: query cached active_console_session
    try:
        info = active_console_session(ttl=0)
        sid = info.get("session_id")
        if sid and sid != 0xFFFFFFFF and sid != 0:
            return int(sid)
    except Exception:
        pass

    if fallback_sid is not None:
        return fallback_sid

    return 1 if (sess_id == 0xFFFFFFFF or sess_id == 0) else sess_id


class SystemAgent:
    def __init__(self):
        self.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        mqtt_pw = os.getenv("MQTT_PASSWORD")
        if mqtt_pw:
            self.config.setdefault("mqtt", {})["password"] = mqtt_pw
        win_pw = os.getenv("WINDOWS_PASSWORD")
        if win_pw:
            self.config.setdefault("unlock", {})["password"] = win_pw
        self.stop_event = threading.Event()
        self.command_queue = queue.Queue()
        self.mqtt_online = False
        self.state_lock = threading.Lock()
        self.unlock_lock = threading.Lock()
        self.processed_ids = []
        self.last_session = None
        self.last_status_publish = 0
        self.previous_net = psutil.net_io_counters()
        self.previous_net_time = time.time()
        self._last_reconnect_attempt = 0.0
        self._shutdown_detected = False
        self.load_state()
        self.setup_mqtt_client()

    def setup_mqtt_client(self):
        if hasattr(self, "mqtt") and self.mqtt:
            try:
                self.mqtt.loop_stop(force=True)
            except Exception:
                pass
            try:
                self.mqtt.disconnect()
            except Exception:
                pass

        self.mqtt = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"windows-system-{socket.gethostname().lower()}",
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        self.mqtt.username_pw_set(
            self.config["mqtt"]["username"],
            self.config["mqtt"]["password"],
        )
        self.mqtt.tls_set(
            ca_certs=certifi.where(),
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        self.mqtt.reconnect_delay_set(1, 30)
        self.mqtt.will_set(
            TOPIC_STATUS,
            payload='{"state":"offline"}',
            qos=1,
            retain=True,
        )
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_disconnect = self.on_disconnect
        self.mqtt.on_message = self.on_message

    def ensure_mqtt_connection(self):
        thread_alive = (
            getattr(self, "mqtt", None) is not None
            and getattr(self.mqtt, "_thread", None) is not None
            and self.mqtt._thread.is_alive()
        )

        if thread_alive:
            return

        now = time.time()
        last_attempt = getattr(self, "_last_reconnect_attempt", 0.0)
        if now - last_attempt < 10.0:
            return

        self._last_reconnect_attempt = now
        LOG.warning("System MQTT loop thread died! Resurrecting MQTT client...")
        try:
            self.setup_mqtt_client()
            if self.connect_with_retry(max_attempts=2, timeout_per_attempt=20):
                self.mqtt.loop_start()
                LOG.info("System MQTT loop restarted successfully")
        except Exception:
            LOG.exception("Failed to re-establish System MQTT connection in watchdog")

    def load_state(self):
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            self.processed_ids = list(data.get("processed_ids", []))[-50:]
        except FileNotFoundError:
            self.processed_ids = []
        except Exception:
            LOG.exception("Could not load system-agent state")

    def save_state(self):
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = STATE_PATH.with_suffix(f".tmp.{os.getpid()}")
            temporary.write_text(
                json.dumps({"processed_ids": self.processed_ids[-50:]}),
                encoding="utf-8",
            )
            os.replace(temporary, STATE_PATH)
        except Exception:
            LOG.exception("Could not save system-agent state")

    def remember_command(self, command_id):
        with self.state_lock:
            if command_id in self.processed_ids:
                return False
            self.processed_ids.append(command_id)
            self.processed_ids = self.processed_ids[-50:]
            self.save_state()
            return True

    def status_payload(self):
        session = active_console_session()
        boot_info = get_boot_info()
        return {
            "state": "online",
            "session": session["session"],
            "session_id": session["session_id"],
            "user_logged_in": session["user_logged_in"],
            "session_source": session.get("source"),
            "boot_type": boot_info.get("boot_type"),
            "boot_label": boot_info.get("boot_label"),
            "fast_startup_enabled": boot_info.get("fast_startup_enabled"),
            "host": socket.gethostname(),
            "timestamp": int(time.time()),
            "ts": time.time(),
            "boot_id": AGENT_BOOT_ID,
            "version": AGENT_VERSION,
        }

    def _turn_off_led_esp(self):
        try:
            now = int(time.time())
            self.mqtt.publish("pc/main/command", f"LED_OFF|sys-led-off-{now}|{now+30}", qos=1)
        except Exception:
            pass

    def publish_status(self, force=False):
        if not self.mqtt_online:
            return
        try:
            status = self.status_payload()
            session_key = (
                status["session"],
                status["session_id"],
                status["user_logged_in"],
            )
            if (
                force
                or session_key != self.last_session
                or time.time() - self.last_status_publish >= 5
            ):
                self.mqtt.publish(
                    TOPIC_STATUS,
                    json.dumps(status, ensure_ascii=False),
                    qos=1,
                    retain=True,
                )
                self.last_session = session_key
                self.last_status_publish = time.time()
        except Exception:
            LOG.exception("System status publication failed")

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.error("MQTT rejected connection: %s", reason_code)
            return
        self.mqtt_online = True
        client.subscribe(TOPIC_COMMAND, qos=1)
        self.publish_status(force=True)
        LOG.info("System MQTT connected")

    def on_disconnect(
        self,
        client,
        userdata,
        disconnect_flags,
        reason_code,
        properties,
    ):
        self.mqtt_online = False
        LOG.warning("System MQTT disconnected: %s", reason_code)

    def on_message(self, client, userdata, message):
        try:
            command = json.loads(message.payload.decode("utf-8"))
        except Exception:
            LOG.warning("Invalid system command payload")
            return
        self.command_queue.put(command)

    def acknowledge(self, command_id, result, data=None):
        payload = {
            "id": command_id,
            "result": result,
            "host": socket.gethostname(),
            "timestamp": int(time.time()),
        }
        if data is not None:
            payload["data"] = data
        self.mqtt.publish(
            TOPIC_ACK,
            json.dumps(payload, ensure_ascii=False),
            qos=1,
            retain=False,
        )

    @staticmethod
    def get_cpu_name():
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except Exception:
            return platform.processor() or "Unknown CPU"

    @staticmethod
    def get_local_ip():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("1.1.1.1", 53))
            address = sock.getsockname()[0]
            sock.close()
            return address
        except OSError:
            return "unknown"

    @staticmethod
    def _find_sensor_snapshot() -> Path:
        for candidate in (
            DATA_DIR / "admin-sensors.json",
            BASE_DIR / "hardware" / "sensors" / "admin-sensors.json",
            BASE_DIR / "hardware" / "admin-sensors.json",
        ):
            if candidate.exists():
                return candidate
        return DATA_DIR / "admin-sensors.json"

    @classmethod
    def read_sensors(cls):
        target = cls._find_sensor_snapshot()
        for attempt in range(3):
            try:
                if time.time() - target.stat().st_mtime > 20:
                    return []
                sensors = json.loads(target.read_text(encoding="utf-8"))
                return sensors if isinstance(sensors, list) else []
            except FileNotFoundError:
                return []
            except PermissionError:
                # The SYSTEM collector can briefly hold the file while it
                # replaces a snapshot. Treat that as a short retry, not an error.
                if attempt < 2:
                    time.sleep(0.1)
                    continue
                LOG.warning("System sensor snapshot remained locked after retries")
                return []
            except Exception:
                LOG.exception("Could not read system sensor snapshot")
                return []
        return []

    @staticmethod
    def query_gpu():
        command = [
            "nvidia-smi",
            "--query-gpu=name,temperature.gpu,utilization.gpu,"
            "memory.used,memory.total,fan.speed,power.draw,"
            "clocks.gr,clocks.mem",
            "--format=csv,noheader,nounits",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=True,
            )
            values = [
                item.strip()
                for item in completed.stdout.splitlines()[0].split(",")
            ]

            def optional_number(value):
                if value.upper() in {"N/A", "[N/A]", "NOT SUPPORTED"}:
                    return None
                return float(value)

            return {
                "name": values[0],
                "temp_c": float(values[1]),
                "load_percent": float(values[2]),
                "vram_used_mb": float(values[3]),
                "vram_total_mb": float(values[4]),
                "fan_percent": optional_number(values[5]),
                "power_w": optional_number(values[6]),
                "clock_mhz": optional_number(values[7]),
                "memory_clock_mhz": optional_number(values[8]),
            }
        except Exception:
            LOG.exception("Could not query NVIDIA GPU from system agent")
            return None

    def network_rates(self):
        now = time.time()
        current = psutil.net_io_counters()
        if current is None or self.previous_net is None:
            if current is not None:
                self.previous_net = current
                self.previous_net_time = now
            return {
                "download_mbps": 0.0,
                "upload_mbps": 0.0,
                "received_gb": 0.0,
                "sent_gb": 0.0,
            }
        elapsed = max(0.1, now - self.previous_net_time)
        received = max(0, current.bytes_recv - self.previous_net.bytes_recv)
        sent = max(0, current.bytes_sent - self.previous_net.bytes_sent)
        self.previous_net = current
        self.previous_net_time = now
        return {
            "download_mbps": round(received * 8 / elapsed / 1_000_000, 2),
            "upload_mbps": round(sent * 8 / elapsed / 1_000_000, 2),
            "received_gb": round(current.bytes_recv / 1_000_000_000, 2),
            "sent_gb": round(current.bytes_sent / 1_000_000_000, 2),
        }

    def collect_telemetry(self):
        now = time.time()
        memory = psutil.virtual_memory()
        frequency = psutil.cpu_freq()
        session = active_console_session()
        disks = []
        seen_devices = set()
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        for partition in psutil.disk_partitions(all=False):
            if partition.device in seen_devices:
                continue
            if get_drive_type(partition.mountpoint) != 3:
                continue
            seen_devices.add(partition.device)
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                disks.append(
                    {
                        "drive": partition.device.rstrip("\\"),
                        "total_gb": round(usage.total / 1_000_000_000, 1),
                        "free_gb": round(usage.free / 1_000_000_000, 1),
                        "used_percent": round(usage.percent, 1),
                    }
                )
            except OSError:
                continue

        sensors = self.read_sensors()
        useful_sensors = [
            sensor
            for sensor in sensors
            if (
                sensor.get("type") in ("Temperature", "Fan", "Power")
                and float(sensor.get("value", 0)) != 0
            )
        ][:60]
        cpu_temperature = next(
            (
                float(sensor["value"])
                for sensor in useful_sensors
                if (
                    sensor.get("hardware_type") == "Cpu"
                    and sensor.get("type") == "Temperature"
                    and float(sensor.get("value", 0)) > 5
                )
            ),
            None,
        )

        return {
            "timestamp": int(now),
            "ts": now,
            "host": socket.gethostname(),
            "os": platform.system().lower(),
            "windows": platform.platform(),
            "local_ip": self.get_local_ip(),
            "uptime_seconds": max(0, int(time.time() - REAL_BOOT_TIME)),
            "agent_mode": "system",
            "session": session,
            "cpu": {
                "name": self.get_cpu_name(),
                "load_percent": round(psutil.cpu_percent(interval=None), 1),
                "temp_c": cpu_temperature,
                "logical_cores": psutil.cpu_count(),
                "frequency_mhz": (
                    round(frequency.current)
                    if frequency and frequency.current
                    else None
                ),
            },
            "memory": {
                "used_gb": round(memory.used / 1_000_000_000, 1),
                "total_gb": round(memory.total / 1_000_000_000, 1),
                "used_percent": round(memory.percent, 1),
            },
            "gpu": self.query_gpu(),
            "disks": disks,
            "network": self.network_rates(),
            "volume": None,
            "idle_seconds": None,
            "user_active": False,
            "sensors": useful_sensors,
        }

    @staticmethod
    def top_processes():
        for process in psutil.process_iter(["pid"]):
            try:
                process.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        time.sleep(0.7)

        processes = []
        for process in psutil.process_iter(["pid", "name", "memory_info"]):
            if process.info["pid"] == 0:
                continue
            try:
                cpu_percent = process.cpu_percent(None)
                cpu_percent /= max(1, psutil.cpu_count() or 1)
                processes.append(
                    {
                        "pid": process.info["pid"],
                        "name": process.info["name"] or "unknown",
                        "cpu_percent": round(cpu_percent, 1),
                        "memory_mb": round(
                            process.info["memory_info"].rss / 1_000_000,
                            1,
                        ),
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        processes.sort(
            key=lambda item: (item["cpu_percent"], item["memory_mb"]),
            reverse=True,
        )
        return processes[:10]

    @staticmethod
    def kill_process(process_name):
        normalized = str(process_name).lower()
        if normalized in {"desktop_agent", "agent.py"}:
            killed = []
            for process in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    pname = (process.info["name"] or "").lower()
                    cmd = " ".join(process.info.get("cmdline") or [])
                    if pname in {"python.exe", "pythonw.exe"} and "agent.py" in cmd and "boot_agent.py" not in cmd and process.info["pid"] != os.getpid():
                        process.terminate()
                        killed.append(process.info["pid"])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return {"name": "desktop_agent", "terminated_pids": killed}

        if normalized in PROTECTED_PROCESSES:
            raise PermissionError("Protected Windows process")
        killed = []
        for process in psutil.process_iter(["pid", "name"]):
            try:
                if (process.info["name"] or "").lower() == normalized:
                    process.terminate()
                    killed.append(process.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {"name": process_name, "terminated_pids": killed}

    @staticmethod
    def kill_process_by_pid(pid):
        process = psutil.Process(int(pid))
        name = (process.name() or "").lower()
        if process.pid == os.getpid() or name in PROTECTED_PROCESSES:
            raise PermissionError("Protected Windows process")
        original_name = process.name()
        process.terminate()
        return {"name": original_name, "terminated_pid": int(pid)}

    def restart_service(self, service_key):
        service_name = self.config.get("services", {}).get(service_key)
        if not service_name:
            raise ValueError("Service is not configured")
        subprocess.run(
            ["sc.exe", "stop", service_name],
            capture_output=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        time.sleep(2)
        completed = subprocess.run(
            ["sc.exe", "start", service_name],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stdout or completed.stderr)
        return {"service": service_key}

    @staticmethod
    def restart_desktop_agent():
        task_name = "PC Control Desktop Agent"
        subprocess.run(
            ["schtasks.exe", "/End", "/TN", task_name],
            capture_output=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        completed = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", task_name],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stdout or completed.stderr)
        return {"task": task_name}

    def execute_action(self, command_id, action, args):
        try:
            if action == "telemetry":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.collect_telemetry(),
                )
                return
            if action == "top_processes":
                self.acknowledge(
                    command_id,
                    "executed",
                    {"processes": self.top_processes()},
                )
                return
            if action == "lock":
                self.acknowledge(command_id, "executed")
                subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
                return
            if action == "shutdown":
                self._turn_off_led_esp()
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                subprocess.Popen(
                    [
                        "shutdown.exe",
                        "/s",
                        "/t",
                        "5",
                        "/c",
                        "PC CONTROL: remote system shutdown",
                    ]
                )
                return
            if action == "restart":
                self._turn_off_led_esp()
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                subprocess.Popen(
                    [
                        "shutdown.exe",
                        "/r",
                        "/t",
                        "5",
                        "/c",
                        "PC CONTROL: remote system restart",
                    ]
                )
                return
            if action == "sleep":
                self._turn_off_led_esp()
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                ctypes.windll.powrprof.SetSuspendState(False, False, False)
                return
            if action == "hibernate":
                self._turn_off_led_esp()
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                subprocess.Popen(["shutdown.exe", "/h"])
                return
            if action == "kill_process":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.kill_process(args.get("name", "")),
                )
                return
            if action == "kill_pid":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.kill_process_by_pid(args.get("pid")),
                )
                return
            if action == "restart_service":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.restart_service(args.get("service")),
                )
                return
            if action == "restart_desktop_agent":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.restart_desktop_agent(),
                )
                return
            if action in ("cmd", "exec", "powershell"):
                cmd_text = args.get("command") or args.get("cmd") or ""
                use_ps = action == "powershell" or args.get("shell") == "powershell"
                timeout_s = min(int(args.get("timeout", 60)), 300)
                cwd = args.get("cwd") or os.environ.get("USERPROFILE") or "C:\\"

                if use_ps:
                    exec_args = [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; {cmd_text}",
                    ]
                else:
                    exec_args = ["cmd.exe", "/c", f"chcp 65001 >nul & {cmd_text}"]

                try:
                    p = subprocess.run(
                        exec_args,
                        capture_output=True,
                        timeout=timeout_s,
                        cwd=cwd,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    stdout = p.stdout
                    stderr = p.stderr
                    code = p.returncode
                except subprocess.TimeoutExpired:
                    stdout = ""
                    stderr = f"Command timed out after {timeout_s}s"
                    code = 124
                except Exception as e:
                    stdout = ""
                    stderr = f"Execution error: {e}"
                    code = -1

                max_len = 8000
                if len(stdout) > max_len:
                    stdout = stdout[:max_len] + f"\n... [обрезано {len(stdout) - max_len} симв.]"
                if len(stderr) > 4000:
                    stderr = stderr[:4000] + f"\n... [обрезано]"

                self.acknowledge(
                    command_id,
                    "executed" if code == 0 else "failed",
                    {
                        "stdout": stdout,
                        "stderr": stderr,
                        "exit_code": code,
                        "command": cmd_text,
                        "shell": "powershell" if use_ps else "cmd",
                    },
                )
                return
            if action == "unlock":
                if not self.unlock_lock.acquire(blocking=False):
                    LOG.warning("Rejecting concurrent unlock command %s (unlock_lock is busy)", command_id)
                    self.acknowledge(
                        command_id,
                        "busy",
                        {"error": "Another unlock command is currently in progress"},
                    )
                    return

                try:
                    unlock_cfg = self.config.get("unlock", {})
                    psexec_candidates = [
                        unlock_cfg.get("psexec_path"),
                        str(BASE_DIR / "PsExec64.exe"),
                        str(BASE_DIR / "hardware" / "PsExec64.exe"),
                        shutil.which("PsExec64.exe"),
                        shutil.which("PsExec.exe"),
                    ]
                    psexec = next((p for p in psexec_candidates if p and Path(p).is_file()), None)
                    if not psexec:
                        LOG.error("PsExec64.exe not found in candidates: %s", psexec_candidates)
                        raise FileNotFoundError(
                            f"PsExec64.exe executable not found in local system (checked: {[c for c in psexec_candidates if c]})"
                        )

                    pythonw_candidates = [
                        unlock_cfg.get("pythonw_path"),
                        str(BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"),
                        str(Path(sys.executable).with_name("pythonw.exe")),
                        shutil.which("pythonw.exe"),
                        sys.executable,
                    ]
                    pythonw = next((p for p in pythonw_candidates if p and Path(p).is_file()), None)
                    if not pythonw:
                        LOG.error("pythonw.exe not found in candidates: %s", pythonw_candidates)
                        raise FileNotFoundError(
                            f"pythonw.exe not found in local system (checked: {[c for c in pythonw_candidates if c]})"
                        )

                    script_candidates = [
                        unlock_cfg.get("script_path"),
                        str(BASE_DIR / "scripts" / "type_password.py"),
                        str(BASE_DIR / "type_password.py"),
                    ]
                    script = next((p for p in script_candidates if p and Path(p).is_file()), None)
                    if not script:
                        LOG.error("type_password.py not found in candidates: %s", script_candidates)
                        raise FileNotFoundError("type_password.py script not found")

                    session = active_console_session(ttl=0)
                    if session["session"] == "unlocked":
                        self.acknowledge(
                            command_id,
                            "executed",
                            {"session": "unlocked", "already_unlocked": True},
                        )
                        return
                    if session["session"] not in {"login_screen", "locked"}:
                        self.acknowledge(
                            command_id,
                            "not_ready",
                            {"session": session["session"]},
                        )
                        return

                    target_session = str(find_active_interactive_session_id())

                    completed = subprocess.run(
                        [
                            psexec,
                            "-accepteula",
                            "-nobanner",
                            "-s",
                            "-x",
                            "-i",
                            target_session,
                            pythonw,
                            script
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    if completed.returncode != 0:
                        LOG.error(
                            "Password input process failed (rc=%s): stdout=%r stderr=%r",
                            completed.returncode,
                            completed.stdout,
                            completed.stderr,
                        )
                        raise RuntimeError(
                            f"Password input process failed (exit code {completed.returncode}): "
                            f"{completed.stderr.strip() or completed.stdout.strip()}"
                        )

                    deadline = time.time() + 15
                    while time.time() < deadline:
                        session = active_console_session(ttl=0)
                        if session["session"] == "unlocked":
                            self.publish_status(force=True)
                            self.acknowledge(
                                command_id,
                                "executed",
                                {"session": "unlocked"},
                            )
                            return
                        time.sleep(0.3)

                    self.publish_status(force=True)
                    final_session = active_console_session(ttl=0)
                    self.acknowledge(
                        command_id,
                        "unlock_not_confirmed",
                        {"session": final_session["session"]},
                    )
                    return
                finally:
                    self.unlock_lock.release()
            self.acknowledge(command_id, "unsupported_before_login")
        except PermissionError as error:
            self.acknowledge(
                command_id,
                "permission_denied",
                {"error": str(error)},
            )
        except Exception as error:
            LOG.exception("System action failed: %s", action)
            self.acknowledge(
                command_id,
                "failed",
                {"error": str(error)},
            )

    def process_command(self, command):
        if not isinstance(command, dict):
            LOG.warning("Ignoring non-object system command payload")
            return
        command_id = str(command.get("id", ""))
        action = str(command.get("action", ""))
        try:
            expires = int(command.get("expires", 0))
        except (TypeError, ValueError):
            LOG.warning("Ignoring system command with invalid expiry: %s", command_id)
            if command_id and len(command_id) <= 80:
                self.acknowledge(command_id, "invalid_command")
            return
        args = command.get("args") or {}
        if not isinstance(args, dict):
            LOG.warning("Ignoring system command with invalid args: %s", command_id)
            if command_id and len(command_id) <= 80:
                self.acknowledge(command_id, "invalid_command")
            return
        now = int(time.time())
        if not command_id or len(command_id) > 80:
            return
        if expires < now - 30 or expires > now + 300:
            self.acknowledge(command_id, "expired")
            return
        if not self.remember_command(command_id):
            self.acknowledge(command_id, "duplicate_ignored")
            return
        LOG.info("Executing system action=%s id=%s", action, command_id)
        threading.Thread(
            target=self.execute_action,
            args=(command_id, action, args),
            name=f"system-action-{action}",
            daemon=True,
        ).start()

    def connect_with_retry(self, max_attempts=None, timeout_per_attempt=20):
        retry_delay = 1
        attempts = 0
        while not self.stop_event.is_set():
            attempts += 1
            try:
                self.mqtt.connect(
                    self.config["mqtt"]["host"],
                    int(self.config["mqtt"]["port"]),
                    keepalive=timeout_per_attempt,
                )
                return True
            except Exception:
                LOG.warning("System MQTT connection attempt %d failed", attempts)
                if max_attempts and attempts >= max_attempts:
                    return False
                self.stop_event.wait(retry_delay)
                retry_delay = min(5, retry_delay * 2)
        return False

    def _start_shutdown_watcher(self):
        def _watcher():
            try:
                import win32gui
                import win32con

                def wndproc(hwnd, msg, wparam, lparam):
                    if msg in (win32con.WM_QUERYENDSESSION, win32con.WM_ENDSESSION):
                        LOG.info("Windows shutdown/logoff detected in SystemAgent via WM_ENDSESSION (msg=0x%x, lparam=0x%x)", msg, lparam)
                        self._shutdown_detected = True
                        self._turn_off_led_esp()
                        try:
                            status_payload = {
                                "state": "offline",
                                "event": "graceful_shutdown",
                                "reason": "windows_shutdown",
                                "agent": "system",
                                "session": "unknown",
                                "ts": time.time(),
                                "boot_id": self.boot_id,
                            }
                            pub_info = self.mqtt.publish(
                                TOPIC_STATUS,
                                json.dumps(status_payload, ensure_ascii=False),
                                qos=1,
                                retain=True,
                            )
                            try:
                                pub_info.wait_for_publish(timeout=1.5)
                            except Exception:
                                pass
                            time.sleep(0.3)
                        except Exception:
                            pass
                        self.stop_event.set()
                        return 1
                    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

                wc = win32gui.WNDCLASS()
                wc.lpfnWndProc = wndproc
                wc.lpszClassName = f"PCControlSystemShutdownWatcher_{os.getpid()}"
                hinst = wc.hInstance = win32gui.GetModuleHandle(None)
                class_atom = win32gui.RegisterClass(wc)
                hwnd = win32gui.CreateWindow(
                    class_atom, "PCControlSystemShutdownWatcher", 0, 0, 0, 0, 0, 0, 0, hinst, None
                )
                LOG.info("System shutdown watcher window initialized (hwnd=%s)", hwnd)
                win32gui.PumpMessages()
            except Exception:
                LOG.exception("System shutdown watcher window failed to initialize")

        threading.Thread(target=_watcher, name="system-shutdown-watcher", daemon=True).start()

    def _ensure_hosts_mapping(self):
        try:
            hosts_path = Path(r"C:\Windows\System32\drivers\etc\hosts")
            if not hosts_path.exists():
                return
            content = hosts_path.read_text(encoding="utf-8", errors="replace")
            target_ip = "203.0.113.10"
            target_domain = "mqtt.example.com"

            lines = content.splitlines()
            found = False
            updated = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split()
                if len(parts) >= 2 and parts[1] == target_domain:
                    found = True
                    if parts[0] != target_ip:
                        lines[i] = f"{target_ip} {target_domain} # pc-control direct"
                        updated = True
                    break

            if not found:
                lines.append(f"{target_ip} {target_domain} # pc-control direct")
                updated = True

            if updated:
                hosts_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                LOG.info("Updated hosts file: %s -> %s", target_domain, target_ip)
        except Exception as e:
            LOG.warning("Could not verify/update hosts file: %s", e)

    def run(self):
        self._ensure_hosts_mapping()
        self._start_shutdown_watcher()
        psutil.cpu_percent(interval=None)
        if not self.connect_with_retry():
            return
        self.mqtt.loop_start()
        interval = max(
            5,
            int(self.config.get("telemetry_interval_seconds", 15)),
        )
        next_telemetry = 0
        next_session_check = 0

        try:
            while not self.stop_event.is_set():
                try:
                    self.ensure_mqtt_connection()

                    try:
                        command = self.command_queue.get(timeout=0.2)
                        self.process_command(command)
                    except queue.Empty:
                        pass

                    now = time.time()
                    if self.mqtt_online and now >= next_session_check:
                        try:
                            self.publish_status()
                        except Exception:
                            LOG.exception("System status publication failed")
                        next_session_check = now + 2
                    if self.mqtt_online and now >= next_telemetry:
                        try:
                            telemetry = self.collect_telemetry()
                            self.mqtt.publish(
                                TOPIC_TELEMETRY,
                                json.dumps(telemetry, ensure_ascii=False),
                                qos=0,
                                retain=True,
                            )
                        except Exception:
                            LOG.exception("System telemetry collection failed")
                        next_telemetry = now + interval
                except Exception:
                    LOG.exception("System agent loop iteration failed")
                    self.stop_event.wait(2)
        finally:
            self._turn_off_led_esp()
            try:
                status_payload = {
                    "state": "offline",
                    "agent": "system",
                    "session": "unknown",
                    "ts": time.time(),
                    "boot_id": self.boot_id,
                }
                if getattr(self, "_shutdown_detected", False):
                    status_payload["event"] = "graceful_shutdown"
                    status_payload["reason"] = "windows_shutdown"

                pub_info = self.mqtt.publish(
                    TOPIC_STATUS,
                    json.dumps(status_payload, ensure_ascii=False),
                    qos=1,
                    retain=True,
                )
                try:
                    pub_info.wait_for_publish(timeout=1.5)
                except Exception:
                    pass
                time.sleep(0.2)
            except Exception:
                pass
            try:
                self.mqtt.loop_stop()
            except Exception:
                pass
            try:
                self.mqtt.disconnect()
            except Exception:
                pass


def main():
    mutex = acquire_single_instance()
    agent = SystemAgent()

    def stop_agent(signum, frame):
        agent.stop_event.set()

    signal.signal(signal.SIGINT, stop_agent)
    signal.signal(signal.SIGTERM, stop_agent)
    try:
        agent.run()
    finally:
        if mutex:
            try:
                ctypes.windll.kernel32.ReleaseMutex(mutex)
            except Exception:
                pass
            try:
                ctypes.windll.kernel32.CloseHandle(mutex)
            except Exception:
                pass


if __name__ == "__main__":
    main()
