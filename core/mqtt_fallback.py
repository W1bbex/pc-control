"""MQTT connection fallback that retains hostname-based TLS validation."""

import ipaddress
import logging
import socket

import paho.mqtt.client as mqtt

LOG = logging.getLogger("pc-control.mqtt-fallback")


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
