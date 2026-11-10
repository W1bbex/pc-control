#!/usr/bin/env python3

import json
import logging
import queue
import time
import secrets
import threading
import paho.mqtt.client as mqtt

from health_engine import HealthEngine

LOG = logging.getLogger("pc-control-bot.mqtt_gateway")

# ESP Topics
TOPIC_ESP_COMMAND = "pc/main/command"
TOPIC_ESP_STATUS = "pc/main/esp/status"
TOPIC_PHYSICAL_INPUT = "pc/main/physical_input"

# System Agent (boot_agent.py) Topics
TOPIC_SYSTEM_COMMAND = "pc/main/windows/system/command"
TOPIC_SYSTEM_STATUS = "pc/main/windows/system/status"
TOPIC_SYSTEM_ACK = "pc/main/windows/system/ack"

# Desktop Agent (agent.py) Topics
TOPIC_DESKTOP_COMMAND = "pc/main/windows/command"
TOPIC_DESKTOP_STATUS = "pc/main/windows/status"
TOPIC_DESKTOP_ACK = "pc/main/windows/ack"

# General ACK, Telemetry & Screenshot Topics
TOPIC_GENERAL_ACK = "pc/main/ack"
TOPIC_TELEMETRY = "pc/main/windows/telemetry"
TOPIC_SYSTEM_TELEMETRY = "pc/main/windows/system/telemetry"
TOPIC_TELEMETRY_LEGACY = "pc/main/telemetry"
TOPIC_SCREENSHOT = "pc/main/windows/screenshot"
TOPIC_MEDIA_STATUS = "pc/main/windows/media/status"
TOPIC_MEDIA_COMMAND = "pc/main/windows/media/command"
TOPIC_ACTIVITY_QUERY = "pc/main/windows/activity/query"
TOPIC_ACTIVITY_RESPONSE = "pc/main/windows/activity/response"


class MqttGateway:
    def __init__(self, username, password, host="127.0.0.1", port=1883, on_event_callback=None):
        self.username = username
        self.password = password
        self.host = host
        self.port = port
        self.on_event_callback = on_event_callback

        self.mqtt_online = False
        self.health = HealthEngine()
        self.health.update("telegram", "unknown")
        
        self.pending_commands = {}
        self.pending_lock = threading.Lock()
        self.last_media_state = {}
        self.health_thread = threading.Thread(target=self._health_loop, daemon=True)

        self._event_queue = queue.Queue(maxsize=1000)
        self._dispatch_thread = threading.Thread(target=self._dispatch_loop, daemon=True, name="mqtt-event-dispatcher")

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"pc-control-bot-{secrets.token_hex(4)}",
            clean_session=True,
        )
        self.client.reconnect_delay_set(min_delay=1, max_delay=120)
        self.client.username_pw_set(self.username, self.password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def start(self):
        try:
            self.client.connect_async(self.host, self.port, keepalive=30)
            self.client.loop_start()
            self.health_thread.start()
            self._dispatch_thread.start()
            LOG.info("MQTT gateway thread started")
        except Exception:
            LOG.exception("MQTT gateway connect failed")

    @property
    def system_status(self):
        return self.health.get_status("windows_system")

    @property
    def system_session(self):
        return self.health.get_status("windows_session")

    @property
    def desktop_status(self):
        return self.health.get_status("windows_desktop")

    @property
    def esp_status(self):
        return self.health.get_status("esp")

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            LOG.info("MQTT connected successfully")
            self.mqtt_online = True
            self.health.update("mqtt_broker", "online")
            
            topics = [
                (TOPIC_SYSTEM_STATUS, 1),
                (TOPIC_DESKTOP_STATUS, 1),
                (TOPIC_ESP_STATUS, 1),
                (TOPIC_PHYSICAL_INPUT, 1),
                (TOPIC_SYSTEM_ACK, 1),
                (TOPIC_DESKTOP_ACK, 1),
                (TOPIC_GENERAL_ACK, 1),
                (TOPIC_TELEMETRY, 1),
                (TOPIC_SYSTEM_TELEMETRY, 1),
                (TOPIC_TELEMETRY_LEGACY, 1),
                (TOPIC_SCREENSHOT, 1),
                (TOPIC_MEDIA_STATUS, 1),
                (TOPIC_ACTIVITY_RESPONSE, 1),
            ]
            self.client.subscribe(topics)
        else:
            LOG.warning("MQTT connection failed with code: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        LOG.warning("MQTT disconnected: %s", reason_code)
        self.mqtt_online = False
        self.health.update("mqtt_broker", "unknown")
        
        # When broker drops, MQTT statuses become unknown
        self.health.set_all_unknown(exclude=["telegram"])
        self._emit_health_state()

    def _on_message(self, client, userdata, message):
        topic = message.topic
        if topic == TOPIC_SCREENSHOT:
            # Binary image payload
            self._emit("screenshot_image", message.payload)
            return

        payload = message.payload.decode("utf-8", errors="replace").strip()
        now = time.time()

        if topic == TOPIC_MEDIA_STATUS:
            try:
                self.last_media_state = json.loads(payload)
                self._emit("media_status", self.last_media_state)
            except Exception as e:
                LOG.warning("Failed parsing media status: %s", e)
            return

        if topic == TOPIC_ACTIVITY_RESPONSE:
            try:
                data = json.loads(payload)
                self._emit("activity_response", data)
            except Exception as e:
                LOG.warning("Failed parsing activity response: %s", e)
            return

        if topic == TOPIC_ESP_STATUS:
            try:
                data = json.loads(payload)
                if isinstance(data, dict) and "event" in data and "state" not in data:
                    LOG.info("ESP diagnostic retained=%s data=%s", message.retain, payload[:1024])
                    return
                current = str(data.get("state", "unknown"))
                boot_id = data.get("boot_id")
                ts = data.get("ts")
                LOG.info("ESP status retained=%s data=%s", message.retain, payload[:1024])
            except Exception:
                current, boot_id, ts = payload, None, None
                
            try:
                self.health.update("esp", current, boot_id=boot_id, ts=ts, is_retained=message.retain)
                self._emit_health_state()
            except Exception as e:
                LOG.warning("Failed to update esp health: %s", e)
            return

        if topic == TOPIC_SYSTEM_STATUS:
            try:
                data = json.loads(payload)
                current = str(data.get("state", "unknown"))
                session = str(data.get("session", "unknown"))
                boot_id = data.get("boot_id")
                ts = data.get("ts")
            except Exception:
                data = {}
                current, session, boot_id, ts = payload, "unknown", None, None
                
            try:
                if isinstance(data, dict):
                    if data.get("event") == "graceful_shutdown" or data.get("state") in ("shutdown", "graceful_shutdown"):
                        self.health.set_graceful_shutdown(ts=ts, reason=data.get("reason", "windows_shutdown"))
                        self._emit("graceful_shutdown", data)
                    elif data.get("event") == "stealth_exit":
                        self._emit("stealth_exit", data)
                self.health.update("windows_system", current, boot_id=boot_id, ts=ts, is_retained=message.retain)
                self.health.update("windows_session", session, ts=ts, is_retained=message.retain)
                self._emit_health_state()
            except Exception as e:
                LOG.warning("Failed to update windows_system/session health: %s", e)
            return

        if topic == TOPIC_DESKTOP_STATUS:
            try:
                data = json.loads(payload)
                current = str(data.get("state", "unknown"))
                boot_id = data.get("boot_id")
                ts = data.get("ts")
            except Exception:
                data = {}
                current, boot_id, ts = payload, None, None
                
            try:
                if isinstance(data, dict):
                    if data.get("event") == "graceful_shutdown" or data.get("state") in ("shutdown", "graceful_shutdown"):
                        self.health.set_graceful_shutdown(ts=ts, reason=data.get("reason", "windows_shutdown"))
                        self._emit("graceful_shutdown", data)
                    elif data.get("event") == "stealth_exit":
                        self._emit("stealth_exit", data)
                self.health.update("windows_desktop", current, boot_id=boot_id, ts=ts, is_retained=message.retain)
                self._emit_health_state()
            except Exception as e:
                LOG.warning("Failed to update windows_desktop health: %s", e)
            return

        if topic == TOPIC_PHYSICAL_INPUT:
            try:
                event_data = json.loads(payload)
                self._emit("physical_input", event_data)
            except Exception:
                pass
            return

        if topic in {TOPIC_SYSTEM_ACK, TOPIC_DESKTOP_ACK, TOPIC_GENERAL_ACK}:
            try:
                ack_data = json.loads(payload)
                self._handle_ack(ack_data)
                self._emit("ack", ack_data)
            except Exception:
                pass
            return

        if topic in {TOPIC_TELEMETRY, TOPIC_TELEMETRY_LEGACY, TOPIC_SYSTEM_TELEMETRY}:
            try:
                telemetry_data = json.loads(payload)
                # Older agents only sent ``timestamp``. Accept both fields so
                # valid telemetry is not downgraded to ``unknown`` while
                # Windows agents are being rolled out.
                ts = telemetry_data.get("ts", telemetry_data.get("timestamp"))
                boot_id = telemetry_data.get("boot_id")
                self.health.update("telemetry", "fresh", ts=ts, boot_id=boot_id, is_retained=message.retain)
                self._emit_health_state()
                self._emit("telemetry", telemetry_data)
            except Exception:
                pass
            return

    def _handle_ack(self, ack_data):
        cmd_id = ack_data.get("id")
        if not cmd_id:
            return
        result = ack_data.get("result")
        with self.pending_lock:
            if cmd_id in self.pending_commands:
                if result == "progress":
                    entry = self.pending_commands[cmd_id]
                    entry["expires"] = int(time.time()) + int(entry.get("timeout", 30))
                else:
                    del self.pending_commands[cmd_id]

    def _health_loop(self):
        while True:
            time.sleep(5.0)
            now = time.time()
            if not self.mqtt_online:
                continue
            
            # Check for stale components
            changed = self.health.check_all_stale()
            if changed:
                self._emit_health_state()

            # Check for timed out commands
            expired = []
            with self.pending_lock:
                for cmd_id, data in list(self.pending_commands.items()):
                    if now > data["expires"]:
                        expired.append((cmd_id, data))
                        del self.pending_commands[cmd_id]
            
            for cmd_id, data in expired:
                LOG.warning("Command %s timed out", cmd_id)
                self._emit("command_timeout", {"id": cmd_id, "action": data.get("action")})

    def _emit_health_state(self):
        self._emit("health_update", {
            "esp_status": self.health.get_status("esp"),
            "system_status": self.health.get_status("windows_system"),
            "system_session": self.health.get_status("windows_session"),
            "desktop_status": self.health.get_status("windows_desktop"),
            "telemetry": self.health.get_status("telemetry"),
            "mqtt_broker": self.health.get_status("mqtt_broker"),
        })

    def _emit(self, event_name, data):
        if not self.on_event_callback:
            return
        q = getattr(self, "_event_queue", None)
        if q is not None:
            try:
                q.put_nowait((event_name, data))
            except queue.Full:
                LOG.warning("Event queue full, dropping event: %s", event_name)
        else:
            try:
                self.on_event_callback(event_name, data)
            except Exception:
                LOG.exception("Error in event callback for %s", event_name)

    def _dispatch_loop(self):
        while True:
            try:
                event_name, data = self._event_queue.get()
                try:
                    if self.on_event_callback:
                        self.on_event_callback(event_name, data)
                except Exception:
                    LOG.exception("Error in event callback for %s", event_name)
                finally:
                    self._event_queue.task_done()
            except Exception:
                LOG.exception("Unhandled error in mqtt dispatch loop")

    def publish(self, topic, payload, qos=1):
        if not self.mqtt_online:
            LOG.warning("Cannot publish to %s: MQTT offline", topic)
            return False
        try:
            msg_info = self.client.publish(topic, payload, qos=qos)
            return msg_info.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception:
            LOG.exception("Publish to %s failed", topic)
            return False

    def publish_esp_action(self, action, value=None):
        action_id = f"esp-{int(time.time()*1000)}-{secrets.token_hex(3)}"
        expires = int(time.time()) + 30
        payload = f"{action}|{action_id}|{expires}"
        if value:
            payload += f"|{value}"
            
        with self.pending_lock:
            self.pending_commands[action_id] = {"action": action, "expires": expires, "timeout": 30, "target": "esp"}
            
        success = self.publish(TOPIC_ESP_COMMAND, payload)
        if not success:
            with self.pending_lock:
                self.pending_commands.pop(action_id, None)
            return None
            
        return action_id

    def publish_system_action(self, action, args=None, timeout=30):
        """
        Publishes to pc/main/windows/system/command (boot_agent.py).
        Actions: "unlock", "lock", "sleep", "restart", "shutdown"
        Payload schema: {"id": "...", "action": "...", "expires": <unix_ts>, "args": {...}}
        """
        action_id = f"sys-{int(time.time()*1000)}-{secrets.token_hex(3)}"
        expires = int(time.time()) + timeout
        payload_data = {
            "id": action_id,
            "action": action.lower(),
            "expires": expires,
            "args": args or {},
        }
        
        with self.pending_lock:
            self.pending_commands[action_id] = {"action": action, "expires": expires, "timeout": timeout, "target": "system"}
            
        success = self.publish(TOPIC_SYSTEM_COMMAND, json.dumps(payload_data))
        if not success:
            with self.pending_lock:
                self.pending_commands.pop(action_id, None)
            return None
            
        return action_id

    def publish_desktop_action(self, action, args=None, timeout=30):
        """
        Publishes to pc/main/windows/command (agent.py).
        Actions: "launch_app", "cancel_workspace", "screen_off", "lock", "sleep", "restart", "shutdown", "volume"
        Payload schema: {"id": "...", "action": "...", "expires": <unix_ts>, "args": {...}}
        """
        action_id = f"desk-{int(time.time()*1000)}-{secrets.token_hex(3)}"
        expires = int(time.time()) + timeout
        payload_data = {
            "id": action_id,
            "action": action.lower(),
            "expires": expires,
            "args": args or {},
        }
        
        with self.pending_lock:
            self.pending_commands[action_id] = {"action": action, "expires": expires, "timeout": timeout, "target": "desktop"}
            
        success = self.publish(TOPIC_DESKTOP_COMMAND, json.dumps(payload_data))
        if not success:
            with self.pending_lock:
                self.pending_commands.pop(action_id, None)
            return None
            
        return action_id
 
    def send_media_command(self, action: str, **kwargs):
        payload = json.dumps({"action": action, **kwargs}, ensure_ascii=False)
        return self.publish(TOPIC_MEDIA_COMMAND, payload, qos=1)

    def query_activity(self, qtype: str, val: str):
        payload = json.dumps({"type": qtype, "val": val}, ensure_ascii=False)
        return self.publish(TOPIC_ACTIVITY_QUERY, payload, qos=1)
