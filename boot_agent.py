#!/usr/bin/env python3

import ctypes
from ctypes import wintypes
import json
import logging
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

# Re-export all symbols from system package for 100% backward compatibility
from system.constants import (
    AGENT_BOOT_ID,
    AGENT_VERSION,
    BASE_DIR,
    CONFIG_PATH,
    DATA_DIR,
    LOG,
    LOG_DIR,
    PROTECTED_PROCESSES,
    REAL_BOOT_TIME,
    STATE_PATH,
    TOPIC_ACK,
    TOPIC_COMMAND,
    TOPIC_STATUS,
    TOPIC_TELEMETRY,
    configure_logging,
    get_real_boot_time,
)
from system.session_manager import (
    WTSINFOEX_LEVEL1_W,
    WTSINFOEXW,
    acquire_single_instance,
    active_console_session,
    find_active_interactive_session_id,
    get_boot_info,
)
from system.hardware_monitor import HardwareMonitorMixin
from system.process_manager import ProcessManagerMixin
from system.action_dispatcher import ActionDispatcherMixin


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


class SystemAgent(HardwareMonitorMixin, ProcessManagerMixin, ActionDispatcherMixin):
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
        self.boot_id = AGENT_BOOT_ID
        self.load_state()
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
                    if msg == win32con.WM_QUERYENDSESSION:
                        LOG.info("Windows queried session end (WM_QUERYENDSESSION, lparam=0x%x). Acknowledging readiness.", lparam)
                        return 1

                    if msg == win32con.WM_ENDSESSION:
                        if not wparam:
                            LOG.info("Windows shutdown cancelled (WM_ENDSESSION with wparam=0, lparam=0x%x)", lparam)
                            return 0
                        LOG.info("Windows shutdown confirmed in SystemAgent via WM_ENDSESSION (wparam=%s, lparam=0x%x)", wparam, lparam)
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
                                "boot_id": getattr(self, "boot_id", AGENT_BOOT_ID),
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
                    "boot_id": getattr(self, "boot_id", AGENT_BOOT_ID),
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
