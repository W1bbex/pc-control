#!/usr/bin/env python3

import ctypes
from ctypes import wintypes
import io
import json
import logging
import logging.handlers
import os
import platform
import queue
import signal
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import certifi
import paho.mqtt.client as mqtt
from PIL import ImageGrab
import psutil

from activity_logger import ActivityTracker
from app_emoji_manager import AppEmojiManager

AGENT_VERSION = "2.0"
AGENT_BOOT_ID = f"desktop-{uuid.uuid4().hex[:12]}"

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
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

DEFAULT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DEFAULT_USER_ID = int(os.getenv("TELEGRAM_USER_ID", "0"))

AGENT_START_TIME = time.time()


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
        events = win32evtlog.EvtNext(query, 1)
        if events:
            xml_data = win32evtlog.EvtRender(events[0], win32evtlog.EvtRenderEventXml)
            root = ET.fromstring(xml_data)
            for elem in root.iter():
                if elem.tag.endswith('TimeCreated') and 'SystemTime' in elem.attrib:
                    st = elem.attrib['SystemTime']
                    dt = datetime.fromisoformat(st.replace('Z', '+00:00'))
                    return dt.timestamp()
    except Exception:
        pass
    try:
        return psutil.boot_time()
    except Exception:
        return time.time()


REAL_BOOT_TIME = get_real_boot_time()


def load_agent_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        return {}
    config = json.loads(path.read_text(encoding="utf-8"))
    mqtt_password = os.getenv("MQTT_PASSWORD")
    if mqtt_password:
        config.setdefault("mqtt", {})["password"] = mqtt_password
    windows_password = os.getenv("WINDOWS_PASSWORD")
    if windows_password:
        config.setdefault("unlock", {})["password"] = windows_password
    return config

TOPIC_COMMAND = "pc/main/windows/command"
TOPIC_ACK = "pc/main/windows/ack"
TOPIC_STATUS = "pc/main/windows/status"
TOPIC_TELEMETRY = "pc/main/windows/telemetry"
TOPIC_SCREENSHOT = "pc/main/windows/screenshot"
TOPIC_MEDIA_STATUS = "pc/main/windows/media/status"
TOPIC_MEDIA_COMMAND = "pc/main/windows/media/command"
TOPIC_ACTIVITY_QUERY = "pc/main/windows/activity/query"
TOPIC_ACTIVITY_RESPONSE = "pc/main/windows/activity/response"

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
    logger = logging.getLogger("pc-control-agent")
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(message)s"
    )

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "agent.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(file_handler)

    logger.propagate = False
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logger.handlers):
        logger.addHandler(file_handler)

    if sys.stdout and getattr(sys.stdout, "isatty", lambda: False)():
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
            logger.addHandler(stream_handler)

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
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    mutex = kernel32.CreateMutexW(
        None,
        True,
        "Local\\W1bbexPCControlAgent",
    )
    if ctypes.get_last_error() == 183:
        raise SystemExit("PC Control Agent is already running")
    return mutex


class SensorHelper:
    def __init__(self, executable: Path):
        self.executable = executable
        self.admin_snapshot = executable.parent / "admin-sensors.json"
        self.process = None
        self.latest = []
        self.lock = threading.Lock()

    def start(self):
        if not self.executable.exists():
            LOG.warning("Sensor helper is not installed")
            return

        try:
            self.process = subprocess.Popen(
                [str(self.executable)],
                cwd=str(self.executable.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(
                target=self._read_stdout,
                name="sensor-reader",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._read_stderr,
                name="sensor-errors",
                daemon=True,
            ).start()
            LOG.info("Hardware sensor helper started")
        except Exception:
            LOG.exception("Could not start hardware sensor helper")

    def _read_stdout(self):
        for line in self.process.stdout:
            try:
                sensors = json.loads(line)
                if isinstance(sensors, list):
                    with self.lock:
                        self.latest = sensors
            except json.JSONDecodeError:
                LOG.warning("Invalid sensor helper output")

    def _read_stderr(self):
        for line in self.process.stderr:
            LOG.warning("Sensor helper: %s", line.rstrip())

    def snapshot(self):
        try:
            age = time.time() - self.admin_snapshot.stat().st_mtime
            if age < 15:
                data = json.loads(
                    self.admin_snapshot.read_text(encoding="utf-8")
                )
                if isinstance(data, list):
                    return data
        except (FileNotFoundError, PermissionError, OSError):
            pass
        except Exception:
            LOG.exception("Could not read elevated sensor snapshot")

        with self.lock:
            return list(self.latest)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()


class PCControlAgent:
    def __init__(self):
        self.config = load_agent_config(CONFIG_PATH)
        self.stop_event = threading.Event()

        tg_conf = self.config.get("telegram", {})
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN") or tg_conf.get("token") or tg_conf.get("bot_token") or ""
        raw_tg_uid = os.getenv("TELEGRAM_USER_ID") or tg_conf.get("user_id") or 0
        telegram_user_id = int(raw_tg_uid) if raw_tg_uid else 0

        self.emoji_manager = AppEmojiManager(
            token=telegram_token,
            user_id=telegram_user_id,
        )
        self.fast_watcher_thread = None

        def _on_foreground_change(exe_path, proc_name, title):
            self._handle_active_app_change(exe_path, proc_name, title)

        activity_config = self.config.get("activity_logging", {})
        self.activity_tracker = None
        if activity_config.get("enabled", True):
            self.activity_tracker = ActivityTracker(
                activity_config,
                LOG_DIR,
                self.stop_event,
                on_foreground_change=_on_foreground_change,
            )
        self.command_queue = queue.Queue()
        self.state_lock = threading.Lock()
        self.mqtt_online = False
        self.last_status_publish = 0
        self.last_telemetry = {}
        self.processed_ids = []
        self.previous_net = psutil.net_io_counters()
        self.previous_net_time = time.time()
        self.workspace_cancellations = {}
        self.workspace_lock = threading.Lock()

        self.load_state()

        helper_path = BASE_DIR / "hardware" / "SensorHelper.exe"
        self.sensor_helper = SensorHelper(helper_path)

        self._last_reconnect_attempt = 0.0
        self.current_session_app = None
        self.current_session_title = None
        self.current_session_exe = None
        self.current_session_start = None
        self.current_session_emoji_id = None

        from media_manager import MediaManager
        self.media_manager = MediaManager(
            token=telegram_token,
            user_id=telegram_user_id,
            on_status_change=self.publish_media_status,
        )

        self.setup_mqtt_client()

    def setup_mqtt_client(self):
        if hasattr(self, "mqtt") and self.mqtt:
            try:
                self.mqtt.loop_stop()
            except Exception:
                pass
            try:
                self.mqtt.disconnect()
            except Exception:
                pass

        self.mqtt = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"windows-agent-{socket.gethostname().lower()}",
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
            payload=json.dumps({"state": "offline", "agent": "desktop"}),
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
        LOG.warning("Desktop MQTT loop thread died! Resurrecting MQTT client...")
        try:
            self.setup_mqtt_client()
            if self.connect_with_retry(max_attempts=2, timeout_per_attempt=20):
                self.mqtt.loop_start()
                LOG.info("Desktop MQTT loop restarted successfully")
        except Exception:
            LOG.exception("Failed to recover MQTT in desktop ensure_mqtt_connection")

    def publish_media_status(self, state):
        if not self.mqtt_online:
            return
        try:
            payload = json.dumps(state, ensure_ascii=False)
            self.mqtt.publish(TOPIC_MEDIA_STATUS, payload=payload, qos=1, retain=True)
            LOG.info("Published media status: %s (playing=%s)", state.get("title"), state.get("is_playing"))
        except Exception:
            LOG.exception("Failed to publish media status")

    def load_state(self):
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            self.processed_ids = list(data.get("processed_ids", []))[-50:]
        except FileNotFoundError:
            self.processed_ids = []
        except Exception:
            LOG.exception("Could not load state")
            self.processed_ids = []

    def save_state(self):
        temporary = STATE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"processed_ids": self.processed_ids[-50:]}),
            encoding="utf-8",
        )
        os.replace(temporary, STATE_PATH)

    def remember_command(self, command_id):
        with self.state_lock:
            if command_id in self.processed_ids:
                return False
            self.processed_ids.append(command_id)
            self.processed_ids = self.processed_ids[-50:]
            self.save_state()
            return True

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.error("MQTT rejected connection: %s", reason_code)
            return

        self.mqtt_online = True
        client.subscribe([
            (TOPIC_COMMAND, 1),
            (TOPIC_MEDIA_COMMAND, 1),
            (TOPIC_ACTIVITY_QUERY, 1),
        ])
        self.publish_status(force=True)
        LOG.info("MQTT connected and subscribed to command, media, and activity topics")

    def on_disconnect(
        self,
        client,
        userdata,
        disconnect_flags,
        reason_code,
        properties,
    ):
        self.mqtt_online = False
        LOG.warning("MQTT disconnected: %s", reason_code)

    def on_message(self, client, userdata, message):
        topic = message.topic
        payload_str = message.payload.decode("utf-8", errors="replace")

        if topic == TOPIC_MEDIA_COMMAND:
            try:
                cmd = json.loads(payload_str)
                action = cmd.get("action")
                kwargs = {k: v for k, v in cmd.items() if k != "action"}
                LOG.info("Received media command: %s (args: %s)", action, kwargs)
                self.media_manager.execute_command(action, **kwargs)
            except Exception:
                LOG.exception("Failed executing media command")
            return

        if topic == TOPIC_ACTIVITY_QUERY:
            try:
                req = json.loads(payload_str)
                qtype = req.get("type", "summary")
                qval = req.get("val", "")
                from activity_db import get_activity_db
                db = get_activity_db()
                if qtype == "summary":
                    res = db.get_day_summary(qval)
                elif qtype == "timeline":
                    res = db.get_day_timeline(qval)
                elif qtype == "at":
                    res = db.get_activity_at(float(qval))
                else:
                    res = []
                resp_payload = json.dumps({"req": req, "result": res}, ensure_ascii=False)
                self.mqtt.publish(TOPIC_ACTIVITY_RESPONSE, payload=resp_payload, qos=1)
            except Exception:
                LOG.exception("Failed handling activity query")
            return

        try:
            command = json.loads(payload_str)
        except Exception:
            LOG.warning("Invalid command payload")
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

        info = self.mqtt.publish(
            TOPIC_ACK,
            json.dumps(payload, ensure_ascii=False),
            qos=1,
            retain=False,
        )
        try:
            info.wait_for_publish(timeout=5)
        except RuntimeError:
            pass

    def publish_status(self, force=False):
        if not self.mqtt_online:
            return
        now = time.time()
        if not force and now - self.last_status_publish < 5:
            return
        payload = {
            "state": "online",
            "host": socket.gethostname(),
            "timestamp": int(now),
            "ts": now,
            "boot_id": AGENT_BOOT_ID,
            "version": AGENT_VERSION,
            "agent": "desktop",
            "pid": os.getpid(),
            "capabilities": ["apps", "screenshot", "desktop"],
        }
        self.mqtt.publish(
            TOPIC_STATUS,
            json.dumps(payload, ensure_ascii=False),
            qos=1,
            retain=True,
        )
        self.last_status_publish = now

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
    def get_session_seconds():
        # First priority: find creation time of explorer.exe in the user session
        try:
            for p in psutil.process_iter(["name", "create_time"]):
                if p.info["name"] and p.info["name"].lower() == "explorer.exe":
                    ct = p.info.get("create_time")
                    if ct:
                        return max(0, int(time.time() - ct))
        except Exception:
            pass
        # Second priority: time since this desktop agent started
        return max(0, int(time.time() - AGENT_START_TIME))

    @staticmethod
    def get_idle_seconds():
        class LastInputInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("dwTime", ctypes.c_uint),
            ]

        info = LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        elapsed_ms = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return max(0, int(elapsed_ms / 1000))

    @staticmethod
    def get_foreground_window_safe():
        try:
            return ctypes.windll.user32.GetForegroundWindow() or None
        except Exception:
            return None

    def get_active_application_info(self):
        desktop_emoji = "5251242026447446579"
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hwnd = self.get_foreground_window_safe()
            if not hwnd:
                return "Рабочий стол", desktop_emoji, None
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return "Рабочий стол", desktop_emoji, None
            hproc = kernel32.OpenProcess(0x1000, False, pid.value)
            name = None
            if hproc:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                    name = os.path.basename(buf.value)
                kernel32.CloseHandle(hproc)

            title = None
            tlen = user32.GetWindowTextLengthW(hwnd)
            if tlen:
                tbuf = ctypes.create_unicode_buffer(tlen + 1)
                user32.GetWindowTextW(hwnd, tbuf, tlen + 1)
                title = tbuf.value or None

            # If foreground window is explorer.exe, check if it's the desktop or taskbar
            if not name or name.lower() == "explorer.exe":
                cls_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls_buf, 256)
                cls_name = cls_buf.value
                if cls_name in ("Progman", "WorkerW", "Shell_TrayWnd") or not name:
                    return "Рабочий стол", desktop_emoji, title

            emoji_id = None
            if name and hasattr(self, "emoji_manager"):
                key = name.lower()
                cached = self.emoji_manager.cache.get(key)
                if cached:
                    emoji_id = cached.get("emoji_id")
            final_app = name or "Рабочий стол"
            final_emoji = emoji_id or (desktop_emoji if not name or name.lower() == "explorer.exe" else None)
            return final_app, final_emoji, title
        except Exception:
            return "Рабочий стол", desktop_emoji, None

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
            LOG.exception("Could not query NVIDIA GPU")
            return None

    @staticmethod
    def get_volume():
        import pythoncom

        pythoncom.CoInitialize()
        try:
            from pycaw.pycaw import AudioUtilities

            endpoint = AudioUtilities.GetSpeakers().EndpointVolume
            return {
                "percent": round(endpoint.GetMasterVolumeLevelScalar() * 100),
                "muted": bool(endpoint.GetMute()),
            }
        except Exception:
            return None
        finally:
            pythoncom.CoUninitialize()

    def set_volume(self, percent=None, mute=None, toggle=False):
        import pythoncom

        pythoncom.CoInitialize()
        try:
            from pycaw.pycaw import AudioUtilities

            endpoint = AudioUtilities.GetSpeakers().EndpointVolume

            if toggle:
                endpoint.SetMute(not bool(endpoint.GetMute()), None)
            elif mute is not None:
                endpoint.SetMute(bool(mute), None)

            if percent is not None:
                value = max(0, min(100, int(percent))) / 100.0
                endpoint.SetMasterVolumeLevelScalar(value, None)

            return {
                "percent": round(
                    endpoint.GetMasterVolumeLevelScalar() * 100
                ),
                "muted": bool(endpoint.GetMute()),
            }
        finally:
            pythoncom.CoUninitialize()

    def network_rates(self):
        now = time.time()
        current = psutil.net_io_counters()
        if current is None or self.previous_net is None:
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

        disks = []
        seen_devices = set()
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        drive_fixed = 3
        for partition in psutil.disk_partitions(all=False):
            if partition.device in seen_devices:
                continue
            if get_drive_type(partition.mountpoint) != drive_fixed:
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

        sensors = self.sensor_helper.snapshot()
        temperatures = [
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
                for sensor in temperatures
                if (
                    sensor.get("hardware_type") == "Cpu"
                    and sensor.get("type") == "Temperature"
                    and float(sensor.get("value", 0)) > 5
                )
            ),
            None,
        )
        idle_seconds = self.get_idle_seconds()
        active_app, active_emoji, active_title = self.get_active_application_info()

        # Check if user is currently inside PC Control chat in Telegram/AyuGram
        is_pc_control = False
        if active_app:
            app_lower = active_app.lower()
            if any(client in app_lower for client in ("telegram", "ayugram", "kotatogram", "64gram", "unigram")):
                title_lower = (active_title or "").lower()
                if "pc control" in title_lower or "your_pc_bot" in title_lower:
                    is_pc_control = True
        self.in_pc_control_chat = is_pc_control

        telemetry = {
            "timestamp": int(now),
            "ts": now,
            "agent_mode": "desktop",
            "host": socket.gethostname(),
            "windows": platform.platform(),
            "local_ip": self.get_local_ip(),
            "uptime_seconds": max(0, int(time.time() - REAL_BOOT_TIME)),
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
            "volume": self.get_volume(),
            "idle_seconds": idle_seconds,
            "session_seconds": self.get_session_seconds(),
            "active_application": active_app,
            "active_application_emoji_id": active_emoji,
            "active_window_title": active_title,
            "in_pc_control_chat": is_pc_control,
            "user_active": (
                idle_seconds is not None and idle_seconds < 300
            ),
            "sensors": temperatures,
        }
        self.last_telemetry = telemetry
        return telemetry

    @staticmethod
    def top_processes():
        processes = []
        for process in psutil.process_iter(["pid", "name"]):
            if process.info["pid"] == 0:
                continue
            try:
                process.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        time.sleep(0.7)

        for process in psutil.process_iter(
            ["pid", "name", "memory_info"]
        ):
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

    def make_screenshot(self, command_id):
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
        if len(payload) > 950_000:
            raise RuntimeError("Screenshot exceeds MQTT safety limit")

        info = self.mqtt.publish(
            TOPIC_SCREENSHOT,
            payload=payload,
            qos=0,
            retain=False,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError("MQTT rejected screenshot")

        return {
            "bytes": len(image_data),
            "width": image.width,
            "height": image.height,
        }

    @staticmethod
    def check_happ_vpn_active():
        """
        Verify that Happ process is running in the system.
        """
        try:
            for p in psutil.process_iter(["name"]):
                name = (p.info["name"] or "").lower()
                if name in ("happ.exe", "flyfrogllc.happ.exe"):
                    return True
        except Exception:
            pass
        return False

    def connect_happ_vpn(self, cancel_event=None):
        """
        Verify that Happ process has started and is running.
        If already running -> skips and returns immediately.
        Otherwise polls up to 12s for process appearance.
        """
        LOG.info("Verifying Happ process presence")
        if self.check_happ_vpn_active():
            LOG.info("Happ process is active and running")
            return {"verified": True, "already_connected": True}

        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                return {"cancelled": True}
            if self.check_happ_vpn_active():
                LOG.info("Happ process detected and verified")
                return {"verified": True}
            time.sleep(0.5)

        LOG.warning("Happ process was not detected within timeout")
        return {"verified": False, "reason": "process_not_found"}

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

            # Pass 1: Immediate minimize
            LOG.info("Minimizing windows (Pass 1/3)...")
            shell.MinimizeAll()

            # Pass 2: Retry after ~2 seconds
            time.sleep(2.0)
            LOG.info("Minimizing windows (Pass 2/3)...")
            shell.MinimizeAll()

            # Pass 3: Final retry after ~2 more seconds (~4s total)
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

    def launch_app(self, app_key, workspace_id=None, progress_callback=None):
        app_config = self.config.get("apps", {}).get(app_key)
        if not app_config:
            raise ValueError("Application is not configured")

        scenario = app_config if isinstance(app_config, dict) else {}
        minimize_after = bool(scenario.get("minimize_after", False))
        inter_app_delay = max(0.0, float(scenario.get("inter_app_delay_seconds", 1.5)))
        raw_steps = scenario.get("steps") if scenario else app_config

        cancel_event = None
        if scenario and workspace_id:
            cancel_event = threading.Event()
            with self.workspace_lock:
                self.workspace_cancellations[workspace_id] = cancel_event

        normalized_steps = []
        if isinstance(raw_steps, list):
            for item in raw_steps:
                if isinstance(item, dict):
                    normalized_steps.append(item)
                elif isinstance(item, list):
                    exe_name = Path(item[0]).stem if item else "app"
                    normalized_steps.append({
                        "id": exe_name.lower(),
                        "label": exe_name,
                        "command": item,
                        "required": True,
                        "verification": {"type": "process_name", "targets": [Path(item[0]).name.lower()]} if item else {},
                    })
                elif isinstance(item, str):
                    exe_name = Path(item).stem
                    normalized_steps.append({
                        "id": exe_name.lower(),
                        "label": exe_name,
                        "command": [item],
                        "required": True,
                        "verification": {"type": "process_name", "targets": [Path(item).name.lower()]},
                    })
        elif isinstance(raw_steps, str):
            exe_name = Path(raw_steps).stem
            normalized_steps.append({
                "id": exe_name.lower(),
                "label": exe_name,
                "command": [raw_steps],
                "required": True,
                "verification": {"type": "process_name", "targets": [Path(raw_steps).name.lower()]},
            })

        launched = []
        verified_steps = []
        try:
            for idx, step in enumerate(normalized_steps):
                if cancel_event and cancel_event.is_set():
                    LOG.info("Workspace launch cancelled before step %s (%s)", idx + 1, step.get("id"))
                    return {
                        "app": app_key,
                        "launched": launched,
                        "verified_steps": verified_steps,
                        "cancelled": True,
                        "desktop_minimized": False,
                    }

                step_id = step.get("id", f"step_{idx}")
                label = step.get("label", step_id)
                cmd = [os.path.expandvars(str(c)) for c in (step.get("command") or [])]
                required = bool(step.get("required", True))
                verification = step.get("verification") or {}
                ver_type = verification.get("type", "process_name")
                targets = [t.lower() for t in verification.get("targets", [])]

                if not cmd:
                    continue
                executable = cmd[0]
                resolved_exe = shutil.which(executable)
                if resolved_exe:
                    executable = resolved_exe
                elif not Path(executable).exists():
                    raise FileNotFoundError(executable)

                is_happ = (ver_type == "happ_vpn") or ("happ.exe" in str(executable).lower())

                # Check if already running and verified
                if is_happ:
                    if self.check_happ_vpn_active():
                        LOG.info("Step %s (%s) is already running and VPN is verified", step_id, label)
                        launched.append(f"{label} (already running verified)")
                        verified_steps.append(step_id)
                        if progress_callback:
                            progress_callback({
                                "step_id": step_id,
                                "app": label,
                                "state": "already_running_verified",
                            })
                        if idx < len(normalized_steps) - 1:
                            time.sleep(inter_app_delay)
                        continue
                else:
                    target_names = targets if targets else [Path(executable).name.lower()]
                    app_already_running = any(
                        (p.info["name"] or "").lower() in target_names
                        for p in psutil.process_iter(["name"])
                    )
                    if app_already_running:
                        LOG.info("Step %s (%s) is already running verified", step_id, label)
                        launched.append(f"{label} (already running verified)")
                        verified_steps.append(step_id)
                        if progress_callback:
                            progress_callback({
                                "step_id": step_id,
                                "app": label,
                                "state": "already_running_verified",
                            })
                        if idx < len(normalized_steps) - 1:
                            time.sleep(inter_app_delay)
                        continue

                # Launch application
                if progress_callback:
                    progress_callback({
                        "step_id": step_id,
                        "app": label,
                        "state": "starting",
                    })

                try:
                    process = subprocess.Popen(
                        cmd,
                        cwd=str(Path(executable).parent),
                        creationflags=subprocess.DETACHED_PROCESS
                        | subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
                except OSError as e:
                    if e.winerror == 740:
                        LOG.warning("WinError 740 for %s, falling back to shell=True", executable)
                        process = subprocess.Popen(
                            cmd,
                            cwd=str(Path(executable).parent),
                            shell=True,
                            creationflags=subprocess.DETACHED_PROCESS
                            | subprocess.CREATE_NEW_PROCESS_GROUP,
                        )
                    else:
                        raise

                time.sleep(0.3)
                if process.poll() not in (None, 0):
                    raise RuntimeError(f"Application exited immediately: {executable}")
                launched.append(label)

                # Verification for HAPP VPN
                if is_happ:
                    if progress_callback:
                        progress_callback({
                            "step_id": step_id,
                            "app": label,
                            "state": "connecting",
                        })
                    vpn_res = self.connect_happ_vpn(cancel_event=cancel_event)
                    if vpn_res.get("cancelled"):
                        return {
                            "app": app_key,
                            "launched": launched,
                            "verified_steps": verified_steps,
                            "cancelled": True,
                            "desktop_minimized": False,
                        }
                    if not vpn_res.get("verified"):
                        if progress_callback:
                            progress_callback({
                                "step_id": step_id,
                                "app": label,
                                "state": "failed",
                                "error": vpn_res.get("reason", "vpn_failed"),
                            })
                        if required:
                            LOG.error("Required VPN step %s failed; halting scenario", step_id)
                            return {
                                "app": app_key,
                                "launched": launched,
                                "verified_steps": verified_steps,
                                "failed_step": step_id,
                                "success": False,
                                "reason": "vpn_verification_failed",
                                "desktop_minimized": False,
                            }
                    else:
                        verified_steps.append(step_id)
                        if progress_callback:
                            progress_callback({
                                "step_id": step_id,
                                "app": label,
                                "state": "verified",
                            })
                else:
                    # Verification for ordinary apps
                    time.sleep(0.5)
                    target_names = targets if targets else [Path(executable).name.lower()]
                    process_verified = (process.poll() is None) or any(
                        (p.info["name"] or "").lower() in target_names
                        for p in psutil.process_iter(["name"])
                    )
                    if process_verified:
                        verified_steps.append(step_id)
                        if progress_callback:
                            progress_callback({
                                "step_id": step_id,
                                "app": label,
                                "state": "verified",
                            })
                    else:
                        if progress_callback:
                            progress_callback({
                                "step_id": step_id,
                                "app": label,
                                "state": "failed",
                            })
                        if required:
                            LOG.error("Required app step %s failed verification; halting scenario", step_id)
                            return {
                                "app": app_key,
                                "launched": launched,
                                "verified_steps": verified_steps,
                                "failed_step": step_id,
                                "success": False,
                                "reason": f"step_{step_id}_failed_verification",
                                "desktop_minimized": False,
                            }

                if idx < len(normalized_steps) - 1:
                    time.sleep(inter_app_delay)

            if minimize_after:
                self.minimize_all_windows()

            return {
                "app": app_key,
                "launched": launched,
                "verified_steps": verified_steps,
                "success": True,
                "desktop_minimized": minimize_after,
            }
        finally:
            if cancel_event and workspace_id:
                with self.workspace_lock:
                    self.workspace_cancellations.pop(workspace_id, None)

    def cancel_workspace(self, workspace_id):
        with self.workspace_lock:
            cancel_event = self.workspace_cancellations.get(workspace_id)
        if not cancel_event:
            return {"workspace_id": workspace_id, "state": "not_running"}
        cancel_event.set()
        return {"workspace_id": workspace_id, "state": "cancelling"}


    @staticmethod
    def kill_process(process_name):
        if not process_name:
            raise ValueError("Process name is required")
        normalized = str(process_name).lower().strip()
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

    def execute_action(self, command_id, action, args):
        try:
            if action == "telemetry":
                data = self.collect_telemetry()
                self.acknowledge(command_id, "executed", data)
                return

            if action == "top_processes":
                self.acknowledge(
                    command_id,
                    "executed",
                    {"processes": self.top_processes()},
                )
                return


            if action == "top_processes":
                self.acknowledge(
                    command_id,
                    "executed",
                    {"processes": self.top_processes()},
                )
                return

            if action == "screenshot":
                data = self.make_screenshot(command_id)
                self.acknowledge(command_id, "executed", data)
                return

            if action == "lock":
                self.acknowledge(command_id, "executed")
                ctypes.windll.user32.LockWorkStation()
                return

            if action == "sleep":
                try:
                    self.mqtt.publish("pc/main/command", f"LED_OFF|agent-sleep-{int(time.time())}|{int(time.time())+30}", qos=1)
                except Exception:
                    pass
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                ctypes.windll.powrprof.SetSuspendState(False, False, False)
                return

            if action == "hibernate":
                try:
                    self.mqtt.publish("pc/main/command", f"LED_OFF|agent-hib-{int(time.time())}|{int(time.time())+30}", qos=1)
                except Exception:
                    pass
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                subprocess.Popen(["shutdown.exe", "/h"])
                return

            if action == "shutdown":
                try:
                    self.mqtt.publish("pc/main/command", f"LED_OFF|agent-shut-{int(time.time())}|{int(time.time())+30}", qos=1)
                except Exception:
                    pass
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                subprocess.Popen(
                    [
                        "shutdown.exe",
                        "/s",
                        "/t",
                        "5",
                        "/c",
                        "PC CONTROL: удалённое выключение",
                    ]
                )
                return

            if action == "restart":
                try:
                    self.mqtt.publish("pc/main/command", f"LED_OFF|agent-rst-{int(time.time())}|{int(time.time())+30}", qos=1)
                except Exception:
                    pass
                self.acknowledge(command_id, "executed")
                time.sleep(1)
                subprocess.Popen(
                    [
                        "shutdown.exe",
                        "/r",
                        "/t",
                        "5",
                        "/c",
                        "PC CONTROL: удалённая перезагрузка",
                    ]
                )
                return

            if action == "launch_app":
                def report_progress(data):
                    self.acknowledge(command_id, "progress", data)

                ws_id = str(args.get("workspace_id", "")) or command_id
                res = self.launch_app(
                    args.get("app"),
                    workspace_id=ws_id,
                    progress_callback=report_progress,
                )
                if isinstance(res, dict) and res.get("success") is False:
                    self.acknowledge(command_id, "failed", res)
                else:
                    self.acknowledge(command_id, "executed", res)
                return

            if action == "cancel_workspace":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.cancel_workspace(str(args.get("workspace_id", ""))),
                )
                return

            if action == "minimize_all":
                try:
                    self.minimize_all_windows()
                    self.acknowledge(command_id, "executed")
                except Exception as error:
                    LOG.exception("minimize_all failed")
                    self.acknowledge(command_id, "failed", {"error": str(error)})
                return

            if action == "screen_off":
                self.acknowledge(
                    command_id,
                    "executed",
                    self.turn_off_displays(),
                )
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

            if action == "volume":
                data = self.set_volume(
                    percent=args.get("percent"),
                    mute=args.get("mute"),
                    toggle=bool(args.get("toggle", False)),
                )
                self.acknowledge(command_id, "executed", data)
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

            self.acknowledge(command_id, "unknown_action")
        except PermissionError as error:
            LOG.warning("Permission denied for %s: %s", action, error)
            self.acknowledge(command_id, "permission_denied", {"error": str(error)})
        except Exception as error:
            LOG.exception("Action failed: %s", action)
            self.acknowledge(command_id, "failed", {"error": str(error)})

    def process_command(self, command):
        if not isinstance(command, dict):
            LOG.warning("Ignoring non-object command payload")
            return
        command_id = str(command.get("id", ""))
        action = str(command.get("action", ""))
        try:
            expires = int(command.get("expires", 0))
        except (TypeError, ValueError):
            LOG.warning("Ignoring command with invalid expiry: %s", command_id)
            if command_id and len(command_id) <= 80:
                self.acknowledge(command_id, "invalid_command")
            return
        args = command.get("args") or {}
        if not isinstance(args, dict):
            LOG.warning("Ignoring command with invalid args: %s", command_id)
            if command_id and len(command_id) <= 80:
                self.acknowledge(command_id, "invalid_command")
            return
        now = int(time.time())

        if expires < now - 30 or expires > now + 300:
            self.acknowledge(command_id, "expired")
            return
        if not self.remember_command(command_id):
            self.acknowledge(command_id, "duplicate_ignored")
            return

        LOG.info("Executing action=%s id=%s", action, command_id)
        threading.Thread(
            target=self.execute_action,
            args=(command_id, action, args),
            name=f"action-{action}",
            daemon=True,
        ).start()

    def publish_telemetry(self):
        telemetry = self.collect_telemetry()
        payload = json.dumps(telemetry, ensure_ascii=False)
        self.mqtt.publish(
            TOPIC_TELEMETRY,
            payload=payload,
            qos=0,
            retain=True,
        )

    def publish_active_app_update(self, proc_name, emoji_id, title=None):
        if not self.mqtt_online:
            return
        try:
            with self.state_lock:
                if not self.last_telemetry:
                    self.last_telemetry = self.collect_telemetry()
                self.last_telemetry["active_application"] = proc_name
                self.last_telemetry["active_application_emoji_id"] = emoji_id
                self.last_telemetry["active_window_title"] = title

                is_pc_control = False
                if proc_name:
                    app_lower = proc_name.lower()
                    if any(client in app_lower for client in ("telegram", "ayugram", "kotatogram", "64gram", "unigram")):
                        title_lower = (title or "").lower()
                        if "pc control" in title_lower or "your_pc_bot" in title_lower:
                            is_pc_control = True
                self.last_telemetry["in_pc_control_chat"] = is_pc_control
                self.in_pc_control_chat = is_pc_control

                now = time.time()
                self.last_telemetry["timestamp"] = int(now)
                self.last_telemetry["ts"] = now
                payload = json.dumps(self.last_telemetry, ensure_ascii=False)

            self.mqtt.publish(TOPIC_TELEMETRY, payload=payload, qos=0, retain=True)
            LOG.info("Instant active app telemetry published: %s (title: %r, emoji: %s)", proc_name, title, emoji_id)
        except Exception:
            LOG.exception("Failed to publish instant active app telemetry")

    def _handle_active_app_change(self, exe_path, exe_name, title):
        from app_emoji_manager import IGNORED_PROCESSES
        from activity_db import get_activity_db
        key = exe_name.lower()
        if key in IGNORED_PROCESSES:
            return

        if self.current_session_app and key == self.current_session_app.lower():
            self.current_session_title = title
            return

        now = time.time()
        # 1. Close and record previous app session to SQLite
        if self.current_session_app and self.current_session_start:
            try:
                get_activity_db().record_session(
                    app_name=self.current_session_app,
                    window_title=self.current_session_title,
                    exe_path=self.current_session_exe,
                    started_at=self.current_session_start,
                    ended_at=now,
                    emoji_id=self.current_session_emoji_id
                )
            except Exception:
                LOG.exception("Failed to record session to SQLite")

        # 2. Instant lookup from cache (0.01 ms)
        emoji_id = self.emoji_manager.cache.get(key, {}).get("emoji_id")

        # 3. Open new session in memory
        self.current_session_app = exe_name
        self.current_session_title = title
        self.current_session_exe = exe_path
        self.current_session_start = now
        self.current_session_emoji_id = emoji_id

        # 4. INSTANT MQTT push to VDS (2-5 ms) so bot dashboard updates in real time!
        self.publish_active_app_update(exe_name, emoji_id, title)

        # 5. Asynchronously extract icon to sticker pack ONLY if new / not cached
        if not emoji_id and exe_path:
            def _bg_notify():
                try:
                    new_emoji_id = self.emoji_manager.notify_app_opened(
                        exe_path, exe_name, title
                    )
                    if new_emoji_id:
                        self.current_session_emoji_id = new_emoji_id
                        if new_emoji_id != emoji_id:
                            self.publish_active_app_update(exe_name, new_emoji_id, title)
                except Exception:
                    LOG.exception("Error in _bg_notify")

            threading.Thread(
                target=_bg_notify,
                name="emoji-notify-fast",
                daemon=True
            ).start()

    def _fast_foreground_watcher_loop(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        last_hwnd = None
        last_exe_key = None
        last_title = None
        telegram_clients = ("telegram", "ayugram", "kotatogram", "64gram", "unigram")

        while not self.stop_event.is_set():
            try:
                hwnd = self.get_foreground_window_safe()

                # Fast path: if foreground HWND has not changed, check title changes (e.g. tabs)
                if hwnd != last_hwnd:
                    last_hwnd = hwnd
                    if not hwnd:
                        last_exe_key = None
                        last_title = None
                        self.in_pc_control_chat = False
                        self.stop_event.wait(0.5)
                        continue

                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    exe_path = None
                    exe_name = None

                    if pid.value:
                        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
                        if hproc:
                            buf = ctypes.create_unicode_buffer(1024)
                            size = wintypes.DWORD(1024)
                            if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                                exe_path = buf.value
                                exe_name = os.path.basename(exe_path)
                            kernel32.CloseHandle(hproc)

                        # Fallback to psutil if OpenProcess or QueryFullProcessImageNameW was restricted
                        if not exe_name:
                            try:
                                proc = psutil.Process(pid.value)
                                exe_name = proc.name()
                                exe_path = proc.exe()
                            except Exception:
                                pass

                    # Check for Desktop class names if explorer or no exe
                    if not exe_name or exe_name.lower() == "explorer.exe":
                        cls_buf = ctypes.create_unicode_buffer(256)
                        user32.GetClassNameW(hwnd, cls_buf, 256)
                        if cls_buf.value in ("Progman", "WorkerW", "Shell_TrayWnd") or not exe_name:
                            exe_name = "explorer.exe"

                    if exe_name:
                        exe_key = exe_name.lower()
                        title = None
                        tlen = user32.GetWindowTextLengthW(hwnd)
                        if tlen:
                            tbuf = ctypes.create_unicode_buffer(tlen + 1)
                            user32.GetWindowTextW(hwnd, tbuf, tlen + 1)
                            title = tbuf.value or None

                        if exe_key != last_exe_key:
                            last_exe_key = exe_key
                            last_title = title
                            if any(c in exe_key for c in telegram_clients):
                                self.in_pc_control_chat = bool(title and ("pc control" in title.lower() or "your_pc_bot" in title.lower()))
                            else:
                                self.in_pc_control_chat = False
                            self._handle_active_app_change(exe_path, exe_name, title)
                        elif title != last_title:
                            # Same exe, different window of the same app
                            last_title = title
                            self.current_session_title = title
                            if any(c in exe_key for c in telegram_clients):
                                self.in_pc_control_chat = bool(title and ("pc control" in title.lower() or "your_pc_bot" in title.lower()))
                            else:
                                self.in_pc_control_chat = False
                            emoji_id = self.emoji_manager.cache.get(exe_key, {}).get("emoji_id")
                            self.publish_active_app_update(self.current_session_app or exe_name, emoji_id, title)
                    else:
                        last_exe_key = None
                        last_title = None
                        self.in_pc_control_chat = False
                else:
                    # Same window: check if user switched tabs/documents in ANY app
                    tlen = user32.GetWindowTextLengthW(hwnd)
                    if tlen:
                        tbuf = ctypes.create_unicode_buffer(tlen + 1)
                        user32.GetWindowTextW(hwnd, tbuf, tlen + 1)
                        title = tbuf.value or None
                        if title != last_title:
                            last_title = title
                            self.current_session_title = title
                            if last_exe_key and any(c in last_exe_key for c in telegram_clients):
                                self.in_pc_control_chat = bool(title and ("pc control" in title.lower() or "your_pc_bot" in title.lower()))
                            else:
                                self.in_pc_control_chat = False
                            if self.mqtt_online and last_exe_key:
                                try:
                                    emoji_id = self.emoji_manager.cache.get(last_exe_key, {}).get("emoji_id")
                                    self.publish_active_app_update(self.current_session_app or last_exe_key, emoji_id, title)
                                except Exception:
                                    pass
            except Exception:
                pass

            self.stop_event.wait(0.5)

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
                LOG.warning("Desktop MQTT connection attempt %d failed", attempts)
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
                        LOG.info("Windows shutdown/logoff detected via WM_ENDSESSION (msg=0x%x)", msg)
                        try:
                            now_ts = int(time.time())
                            self.mqtt.publish(
                                "pc/main/command",
                                f"LED_OFF|agent-shutdown-{now_ts}|{now_ts+30}",
                                qos=1,
                            )
                            self.mqtt.publish(
                                TOPIC_STATUS,
                                json.dumps({"state": "offline", "agent": "desktop", "ts": time.time()}),
                                qos=1,
                                retain=True,
                            )
                            time.sleep(0.3)
                        except Exception:
                            pass
                        self.stop_event.set()
                        return 1
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

    def run(self):
        self.sensor_helper.start()
        self.media_manager.start()
        if self.activity_tracker:
            self.activity_tracker.start()

        if hasattr(self, "emoji_manager"):
            threading.Thread(
                target=self._fast_foreground_watcher_loop,
                name="fast-foreground-watcher",
                daemon=True,
            ).start()

        self._start_shutdown_watcher()

        # Prime cpu_percent to establish baseline
        psutil.cpu_percent(interval=None)

        if not self.connect_with_retry():
            self.sensor_helper.stop()
            self.media_manager.stop()
            if self.activity_tracker:
                self.activity_tracker.stop()
            return
        self.mqtt.loop_start()

        interval = max(5, int(self.config.get("telemetry_interval_seconds", 10)))
        next_telemetry = 0

        try:
            while not self.stop_event.is_set():
                try:
                    self.ensure_mqtt_connection()

                    try:
                        command = self.command_queue.get(timeout=0.5)
                        self.process_command(command)
                    except queue.Empty:
                        pass

                    current_interval = 5 if getattr(self, "in_pc_control_chat", False) else interval
                    if self.mqtt_online and time.time() >= next_telemetry:
                        try:
                            self.publish_telemetry()
                        except Exception:
                            LOG.exception("Telemetry collection failed")
                        next_telemetry = time.time() + current_interval
                    try:
                        self.publish_status()
                    except Exception:
                        LOG.exception("Status publication failed")
                except Exception:
                    LOG.exception("Desktop agent loop iteration failed")
                    self.stop_event.wait(2)
        finally:
            # Flush final active app session to SQLite
            if self.current_session_app and self.current_session_start:
                try:
                    from activity_db import get_activity_db
                    get_activity_db().record_session(
                        app_name=self.current_session_app,
                        window_title=self.current_session_title,
                        exe_path=self.current_session_exe,
                        started_at=self.current_session_start,
                        ended_at=time.time(),
                        emoji_id=self.current_session_emoji_id
                    )
                except Exception:
                    pass

            try:
                now_ts = int(time.time())
                self.mqtt.publish(
                    "pc/main/command",
                    f"LED_OFF|agent-exit-{now_ts}|{now_ts+30}",
                    qos=1,
                )
            except Exception:
                pass

            try:
                self.mqtt.publish(
                    TOPIC_STATUS,
                    json.dumps({"state": "offline", "agent": "desktop", "ts": time.time()}),
                    qos=1,
                    retain=True,
                )
                time.sleep(0.2)
            except Exception:
                pass
            self.media_manager.stop()
            try:
                self.mqtt.loop_stop()
            except Exception:
                pass
            try:
                self.mqtt.disconnect()
            except Exception:
                pass
            self.sensor_helper.stop()
            if self.activity_tracker:
                self.activity_tracker.stop()

def main():
    mutex = acquire_single_instance()
    agent = PCControlAgent()

    def stop_agent(signum, frame):
        agent.stop_event.set()

    signal.signal(signal.SIGINT, stop_agent)
    signal.signal(signal.SIGTERM, stop_agent)

    try:
        agent.run()
    finally:
        ctypes.windll.kernel32.ReleaseMutex(mutex)


if __name__ == "__main__":
    main()
