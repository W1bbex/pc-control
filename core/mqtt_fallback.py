"""MQTT connection fallback that retains hostname-based TLS validation."""

import ipaddress
import logging
import socket

import paho.mqtt.client as mqtt

LOG = logging.getLogger("pc-control.mqtt-fallback")


def patch_paho_sockpair(logger=None):
    """Protect paho-mqtt against Windows socketpair crashes (WinError 10054 / 1236) on sleep/reset."""
    if getattr(mqtt.Client, "_sockpair_patched", False):
        return
    active_log = logger or LOG
    orig_loop = mqtt.Client._loop

    def safe_loop(self, timeout=1.0):
        try:
            return orig_loop(self, timeout)
        except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
            active_log.warning("MQTT _loop caught Windows socket error: %s (triggering reconnect)", e)
            conn_state_cls = getattr(mqtt, "_ConnectionState", None)
            if conn_state_cls is not None:
                disconnecting = (
                    conn_state_cls.MQTT_CS_DISCONNECTING,
                    conn_state_cls.MQTT_CS_DISCONNECTED,
                )
                if getattr(self, "_state", None) not in disconnecting:
                    self._state = conn_state_cls.MQTT_CS_CONNECTION_LOST
            try:
                if hasattr(self, "_reset_sockets"):
                    self._reset_sockets()
                elif hasattr(self, "_sock_close"):
                    self._sock_close()
            except Exception:
                pass
            return mqtt.MQTT_ERR_CONN_LOST

    mqtt.Client._loop = safe_loop

    orig_packet_queue = mqtt.Client._packet_queue

    def safe_packet_queue(self, command, packet, mid=0, qos=0, info=None):
        out_queue = getattr(self, "_out_packet", None)
        prev_len = len(out_queue) if out_queue is not None else 0
        try:
            return orig_packet_queue(self, command, packet, mid, qos, info)
        except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
            active_log.warning("MQTT _packet_queue caught Windows socketpair error: %s", e)
            if out_queue is not None and len(out_queue) > prev_len:
                last_pkt = out_queue[-1]
                if isinstance(last_pkt, dict) and last_pkt.get("mid") == mid and last_pkt.get("command") == command:
                    out_queue.pop()
            if info is not None and hasattr(info, "rc"):
                info.rc = mqtt.MQTT_ERR_NO_CONN
            try:
                if hasattr(self, "_reset_sockets"):
                    self._reset_sockets(sockpair_only=True)
            except Exception:
                pass
            return mqtt.MQTT_ERR_NO_CONN

    mqtt.Client._packet_queue = safe_packet_queue
    mqtt.Client._sockpair_patched = True


class MqttClientWithIpFallback(mqtt.Client):
    def __init__(self, *args, fallback_ip, **kwargs):
        self.fallback_ip = str(ipaddress.ip_address(fallback_ip))
        super().__init__(*args, **kwargs)

    def _create_socket(self):
        try:
            return super()._create_socket()
        except OSError as error:
            if self._transport != "tcp" or not self._ssl or self._get_proxy():
                raise
            LOG.warning("MQTT connection to %s failed (%s); trying configured IP", self._host, error)
            tcp_socket = socket.create_connection(
                (self.fallback_ip, self._port),
                timeout=self._connect_timeout,
                source_address=(self._bind_address, self._bind_port),
            )
            try:
                # Paho uses self._host for SNI and certificate hostname checks.
                return self._ssl_wrap_socket(tcp_socket)
            except Exception:
                tcp_socket.close()
                raise
