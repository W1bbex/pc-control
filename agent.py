#!/usr/bin/env python3
"""Desktop agent for Windows PC Control.

Thin modular coordinator delegating to desktop mixins and services.
"""

import ctypes
from ctypes import wintypes
import io
import json
import logging
import logging.handlers
import os
import queue
import signal
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

import certifi
import paho.mqtt.client as mqtt
from PIL import ImageGrab
import psutil

from desktop.constants import (
    AGENT_BOOT_ID,
    AGENT_VERSION,
    BASE_DIR,
    CONFIG_PATH,
    DATA_DIR,
    ICONS,
    LOG_DIR,
    PROTECTED_PROCESSES,
    REAL_BOOT_TIME,
    STATE_PATH,
    TOPIC_ACK,
    TOPIC_ACTIVITY_QUERY,
    TOPIC_ACTIVITY_RESPONSE,
    TOPIC_COMMAND,
    TOPIC_MEDIA_COMMAND,
    TOPIC_MEDIA_STATUS,
    TOPIC_SCREENSHOT,
    TOPIC_STATUS,
    TOPIC_TELEMETRY,
    get_real_boot_time,
)
from desktop.sensors import SensorHelper
from desktop.telemetry import TelemetryMixin
from desktop.vpn_manager import VpnManagerMixin
from desktop.stealth import StealthMixin
from desktop.launcher import LauncherMixin
from desktop.action_dispatcher import ActionDispatcherMixin

from core.activity_logger import ActivityTracker
from core.antigravity_switcher import AntigravityAccountManager
from core.app_emoji_manager import AppEmojiManager
from core.stealth_guard import StealthManager


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
    """Protect paho-mqtt against Windows socketpair crashes on sleep/reset."""
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


class PCControlAgent(
    TelemetryMixin,
    VpnManagerMixin,
    StealthMixin,
    LauncherMixin,
    ActionDispatcherMixin,
):
    """Main Windows desktop agent coordinator."""

    def __init__(self):
        self.config = load_agent_config(CONFIG_PATH)
        self.stop_event = threading.Event()
        self.boot_id = AGENT_BOOT_ID

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
        self.stealth_mgr = StealthManager(self.config)
        self._prev_mute_state = None
        self._stealth_offline_since = None
        self._stealth_is_active = False
        self._shutdown_detected = False
        self.antigravity_manager = AntigravityAccountManager()

        self.load_state()

        helper_candidates = [
            BASE_DIR / "hardware" / "sensors" / "SensorHelper.exe",
            BASE_DIR / "hardware" / "SensorHelper.exe",
        ]
        helper_path = next((p for p in helper_candidates if p.exists()), helper_candidates[0])
        self.sensor_helper = SensorHelper(helper_path)

        self._last_reconnect_attempt = 0.0
        self.current_session_app = None
        self.current_session_title = None
        self.current_session_exe = None
        self.current_session_start = None
        self.current_session_emoji_id = None
        self.in_pc_control_chat = False

        from core.media_manager import MediaManager

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
        if not getattr(self, "mqtt_online", False):
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
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = STATE_PATH.with_suffix(f".tmp.{os.getpid()}")
            temporary.write_text(
                json.dumps({"processed_ids": self.processed_ids[-50:]}),
                encoding="utf-8",
            )
            os.replace(temporary, STATE_PATH)
        except Exception:
            LOG.exception("Could not save state")

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
            def _run_media_cmd():
                try:
                    cmd = json.loads(payload_str)
                    action = cmd.get("action")
                    kwargs = {k: v for k, v in cmd.items() if k != "action"}
                    LOG.info("Received media command: %s (args: %s)", action, kwargs)
                    self.media_manager.execute_command(action, **kwargs)
                except Exception:
                    LOG.exception("Failed executing media command")

            threading.Thread(target=_run_media_cmd, name="mqtt-media-cmd", daemon=True).start()
            return

        if topic == TOPIC_ACTIVITY_QUERY:
            def _run_activity_query():
                try:
                    req = json.loads(payload_str)
                    qtype = req.get("type", "summary")
                    qval = req.get("val", "")
                    from core.activity_db import get_activity_db

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

            threading.Thread(target=_run_activity_query, name="mqtt-activity-query", daemon=True).start()
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
        if not getattr(self, "mqtt_online", False):
            return
        now = time.time()
        if not force and now - getattr(self, "last_status_publish", 0) < 5:
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

    def _fast_foreground_watcher_loop(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        last_hwnd = None
        last_exe_key = None
        last_title = None
        telegram_clients = ("telegram", "ayugram", "kotatogram", "64gram", "unigram")

        while not self.stop_event.is_set():
            try:
                hwnd = self.get_foreground_window_safe()

                if hwnd != last_hwnd:
                    last_hwnd = hwnd
                    if not hwnd:
                        last_exe_key = None
                        last_title = None
                        self.in_pc_control_chat = False
                        self.stop_event.wait(0.5)
                        continue

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
                            if getattr(self, "mqtt_online", False) and last_exe_key:
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
                    if msg == win32con.WM_QUERYENDSESSION:
                        return 1
                    if msg == win32con.WM_ENDSESSION:
                        if not wparam:
                            return 0
                        LOG.info("Windows shutdown/logoff confirmed via WM_ENDSESSION (msg=0x%x, lparam=0x%x)", msg, lparam)
                        self._shutdown_detected = True
                        try:
                            now_ts = int(time.time())
                            self.mqtt.publish(
                                "pc/main/command",
                                f"LED_OFF|agent-shutdown-{now_ts}|{now_ts+30}",
                                qos=1,
                            )
                            status_payload = {
                                "state": "offline",
                                "event": "graceful_shutdown",
                                "reason": "windows_shutdown",
                                "agent": "desktop",
                                "ts": time.time(),
                                "boot_id": AGENT_BOOT_ID,
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

    def run(self):
        self.sensor_helper.start()
        self.media_manager.start()
        if self.activity_tracker:
            self.activity_tracker.start()

        if hasattr(self, "emoji_manager"):
            self.fast_watcher_thread = threading.Thread(
                target=self._fast_foreground_watcher_loop,
                name="fast-foreground-watcher",
                daemon=True,
            )
            self.fast_watcher_thread.start()

        self._start_shutdown_watcher()

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
                    self.check_stealth_offline_watchdog()
                    self.check_stealth_process_exit()

                    try:
                        command = self.command_queue.get(timeout=0.5)
                        self.process_command(command)
                    except queue.Empty:
                        pass

                    current_interval = 5 if getattr(self, "in_pc_control_chat", False) else interval
                    if getattr(self, "mqtt_online", False) and time.time() >= next_telemetry:
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
            if self.current_session_app and self.current_session_start:
                try:
                    from core.activity_db import get_activity_db

                    get_activity_db().record_session(
                        app_name=self.current_session_app,
                        window_title=self.current_session_title,
                        exe_path=self.current_session_exe,
                        started_at=self.current_session_start,
                        ended_at=time.time(),
                        emoji_id=self.current_session_emoji_id,
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
                status_payload = {
                    "state": "offline",
                    "agent": "desktop",
                    "ts": time.time(),
                    "boot_id": AGENT_BOOT_ID,
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
        try:
            ctypes.windll.kernel32.ReleaseMutex(mutex)
            ctypes.windll.kernel32.CloseHandle(mutex)
        except Exception:
            pass


if __name__ == "__main__":
    main()
