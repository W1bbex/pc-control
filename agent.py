#!/usr/bin/env python3
"""Desktop agent for Windows PC Control.

Thin modular coordinator delegating to desktop mixins and services.
"""

import ctypes
from ctypes import wintypes
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
import psutil

from desktop.constants import (
    AGENT_BOOT_ID,
    AGENT_VERSION,
    BASE_DIR,
    CONFIG_PATH,
    LOG_DIR,
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
)
from desktop.os_events import (
    capture_desktop_screenshot,
    get_window_title,
    is_telegram_control_chat,
    resolve_hwnd_app_info,
    start_shutdown_watcher,
)
from desktop.sensors import SensorHelper
from desktop.telemetry import TelemetryMixin
from desktop.vpn_manager import VpnManagerMixin
from desktop.stealth import StealthMixin
from desktop.launcher import LauncherMixin
from desktop.action_dispatcher import ActionDispatcherMixin

from core.activity_logger import ActivityTracker
from core.mqtt_fallback import MqttClientWithIpFallback, patch_paho_sockpair
from core.antigravity_switcher import AntigravityAccountManager
from core.app_emoji_manager import AppEmojiManager
from core.stealth_guard import StealthManager


def load_agent_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        return {}
    config = json.loads(path.read_text(encoding="utf-8"))
    mqtt_host = os.getenv("MQTT_HOST")
    if mqtt_host:
        config.setdefault("mqtt", {})["host"] = mqtt_host
    mqtt_fallback_ip = os.getenv("MQTT_FALLBACK_IP")
    if mqtt_fallback_ip:
        config.setdefault("mqtt", {})["fallback_ip"] = mqtt_fallback_ip
    mqtt_username = os.getenv("MQTT_USERNAME")
    if mqtt_username:
        config.setdefault("mqtt", {})["username"] = mqtt_username
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

patch_paho_sockpair(LOG)


def acquire_single_instance():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    mutex = kernel32.CreateMutexW(
        None,
        True,
        "Local\\W1bbexPCControlAgent",
    )
    if ctypes.get_last_error() == 183:
        if mutex:
            try:
                kernel32.CloseHandle(mutex)
            except Exception:
                pass
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
        mqtt_cfg = self.config.get("mqtt")
        if not isinstance(mqtt_cfg, dict) or not str(mqtt_cfg.get("host") or "").strip():
            LOG.error("Missing required 'mqtt.host' configuration")
            sys.exit(1)
        try:
            mqtt_port = int(mqtt_cfg.get("port", 8883))
            if not (1 <= mqtt_port <= 65535):
                raise ValueError("port out of range")
            mqtt_cfg["port"] = mqtt_port
        except (TypeError, ValueError):
            LOG.error("Invalid 'mqtt.port' configuration: %r", mqtt_cfg.get("port") if isinstance(mqtt_cfg, dict) else None)
            sys.exit(1)

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

        activity_config = self.config.get("activity_logging", {})
        self.activity_tracker = None
        if activity_config.get("enabled", True):
            self.activity_tracker = ActivityTracker(
                activity_config,
                LOG_DIR,
                self.stop_event,
                on_foreground_change=self._on_foreground_change,
            )
        self.command_queue = queue.Queue()
        self.state_lock = threading.Lock()
        self._incy_lock = threading.RLock()
        self._net_state_lock = threading.Lock()
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

        mqtt_cfg = self.config.get("mqtt", {})
        fallback_ip = mqtt_cfg.get("fallback_ip")
        client_class = MqttClientWithIpFallback if fallback_ip else mqtt.Client
        fallback_options = {"fallback_ip": fallback_ip} if fallback_ip else {}
        self.mqtt = client_class(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"windows-agent-{socket.gethostname().lower()}",
            clean_session=True,
            protocol=mqtt.MQTTv311,
            **fallback_options,
        )
        self.mqtt.username_pw_set(
            mqtt_cfg.get("username", ""),
            mqtt_cfg.get("password", ""),
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

        self.mqtt_online = False
        now = time.time()
        last_attempt = getattr(self, "_last_reconnect_attempt", 0.0)
        if now - last_attempt < 10.0:
            return

        self._last_reconnect_attempt = now
        LOG.warning("Desktop MQTT loop thread died! Resurrecting MQTT client...")
        try:
            self.setup_mqtt_client()
            if self.connect_with_retry(max_attempts=1, timeout_per_attempt=5):
                self.mqtt.loop_start()
                LOG.info("Desktop MQTT loop restarted successfully")
        except Exception:
            self.mqtt_online = False
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
                except Exception as e:
                    LOG.exception("Failed handling activity query")
                    try:
                        err_payload = json.dumps({"req": locals().get("req", {}), "result": [], "error": str(e)}, ensure_ascii=False)
                        self.mqtt.publish(TOPIC_ACTIVITY_RESPONSE, payload=err_payload, qos=1)
                    except Exception:
                        pass

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

        try:
            self.mqtt.publish(
                TOPIC_ACK,
                json.dumps(payload, ensure_ascii=False),
                qos=1,
                retain=False,
            )
        except Exception as err:
            LOG.warning("Failed publishing ACK for %s: %s", command_id, err)

    def _on_foreground_change(self, exe_path, proc_name, title):
        """Route ActivityTracker transitions unless fast_watcher_thread is active."""
        if not getattr(self, "fast_watcher_thread", None) or not self.fast_watcher_thread.is_alive():
            self._handle_active_app_change(exe_path, proc_name, title)

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
        payload, meta = capture_desktop_screenshot(command_id)
        info = self.mqtt.publish(
            TOPIC_SCREENSHOT,
            payload=payload,
            qos=0,
            retain=False,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError("MQTT rejected screenshot")
        return meta

    def _fast_foreground_watcher_loop(self):
        last_hwnd = None
        last_exe_key = None
        last_title = None

        while not self.stop_event.is_set():
            try:
                hwnd = self.get_foreground_window_safe()

                if hwnd != last_hwnd:
                    last_hwnd = hwnd
                    if not hwnd:
                        last_exe_key = None
                        last_title = None
                        with self.state_lock:
                            self.in_pc_control_chat = False
                        self.stop_event.wait(0.5)
                        continue

                    exe_path, exe_name, title = resolve_hwnd_app_info(hwnd)

                    if exe_name:
                        exe_key = exe_name.lower()
                        in_chat = is_telegram_control_chat(exe_key, title)

                        if exe_key != last_exe_key:
                            last_exe_key = exe_key
                            last_title = title
                            with self.state_lock:
                                self.in_pc_control_chat = in_chat
                            self._handle_active_app_change(exe_path, exe_name, title)
                        elif title != last_title:
                            last_title = title
                            with self.state_lock:
                                self.current_session_title = title
                                self.in_pc_control_chat = in_chat
                                target_app = self.current_session_app or exe_name
                            from core.app_emoji_manager import read_cached_emoji_id
                            emoji_id = read_cached_emoji_id(self.emoji_manager, exe_key)
                            self.publish_active_app_update(target_app, emoji_id, title)
                    else:
                        last_exe_key = None
                        last_title = None
                        with self.state_lock:
                            self.in_pc_control_chat = False
                else:
                    title = get_window_title(hwnd)
                    if title and title != last_title:
                        last_title = title
                        in_chat = is_telegram_control_chat(last_exe_key, title)
                        with self.state_lock:
                            self.current_session_title = title
                            self.in_pc_control_chat = in_chat
                            target_app = self.current_session_app or last_exe_key
                        if getattr(self, "mqtt_online", False) and last_exe_key:
                            try:
                                from core.app_emoji_manager import read_cached_emoji_id
                                emoji_id = read_cached_emoji_id(self.emoji_manager, last_exe_key)
                                self.publish_active_app_update(target_app, emoji_id, title)
                            except Exception:
                                pass
            except Exception as e:
                LOG.warning("Error in foreground watcher loop: %s", e)

            self.stop_event.wait(0.5)

    def connect_with_retry(self, max_attempts=None, timeout_per_attempt=20):
        retry_delay = 1
        attempts = 0
        connect_timeout = max(1.0, min(float(timeout_per_attempt), 5.0))
        keepalive_s = max(20, int(timeout_per_attempt))
        mqtt_cfg = self.config.get("mqtt", {})
        host = mqtt_cfg.get("host")
        port = int(mqtt_cfg.get("port", 8883))
        if not host:
            LOG.error("Missing mqtt.host in connect_with_retry")
            return False

        while not self.stop_event.is_set():
            attempts += 1
            try:
                if hasattr(self.mqtt, "_connect_timeout"):
                    self.mqtt._connect_timeout = connect_timeout
                self.mqtt.connect(
                    host,
                    port,
                    keepalive=keepalive_s,
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
        def _on_shutdown():
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

        start_shutdown_watcher(_on_shutdown)

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

                    with self.state_lock:
                        in_pc_control_chat = getattr(self, "in_pc_control_chat", False)
                    current_interval = 5 if in_pc_control_chat else interval
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
            self.stop_event.set()
            with self.state_lock:
                app_name = self.current_session_app
                start_time = self.current_session_start
                win_title = self.current_session_title
                exe_p = self.current_session_exe
                emoji_id = self.current_session_emoji_id

            if app_name and start_time:
                try:
                    from core.activity_db import get_activity_db

                    get_activity_db().record_session(
                        app_name=app_name,
                        window_title=win_title,
                        exe_path=exe_p,
                        started_at=start_time,
                        ended_at=time.time(),
                        emoji_id=emoji_id,
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
            fast_watcher = getattr(self, "fast_watcher_thread", None)
            if fast_watcher is not None and hasattr(fast_watcher, "is_alive") and fast_watcher.is_alive():
                try:
                    fast_watcher.join(timeout=1.0)
                except Exception:
                    pass
            try:
                from core.activity_db import _GLOBAL_DB
                if _GLOBAL_DB is not None:
                    _GLOBAL_DB.close()
            except Exception:
                pass


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
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex.restype = wintypes.BOOL
            kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.ReleaseMutex(mutex)
            kernel32.CloseHandle(mutex)
        except Exception:
            pass


if __name__ == "__main__":
    main()
