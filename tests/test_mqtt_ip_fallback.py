from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt
import pytest

from core.mqtt_fallback import MqttClientWithIpFallback


def client():
    result = MqttClientWithIpFallback(mqtt.CallbackAPIVersion.VERSION2, fallback_ip="192.0.2.10")
    result.connect_async("broker.example", 8883, 20)
    result.tls_set()
    return result


def test_primary_connection_is_preferred():
    instance = client()
    primary = object()
    with patch.object(mqtt.Client, "_create_socket", return_value=primary), \
         patch("core.mqtt_fallback.socket.create_connection") as connect:
        assert instance._create_socket() is primary
        connect.assert_not_called()


def test_fallback_connects_to_ip_but_validates_original_hostname():
    instance = client()
    raw = MagicMock()
    secured = MagicMock()
    with patch.object(mqtt.Client, "_create_socket", side_effect=OSError("DNS failed")), \
         patch("core.mqtt_fallback.socket.create_connection", return_value=raw) as connect, \
         patch.object(instance._ssl_context, "wrap_socket", return_value=secured) as wrap:
        result = instance._create_socket()
    assert result is secured
    assert connect.call_args.args[0] == ("192.0.2.10", 8883)
    assert wrap.call_args.kwargs["server_hostname"] == "broker.example"
    assert instance._ssl_context.check_hostname is True
    assert instance._host == "broker.example"


def test_fallback_closes_socket_when_tls_validation_fails():
    instance = client()
    raw = MagicMock()
    with patch.object(mqtt.Client, "_create_socket", side_effect=OSError("DNS failed")), \
         patch("core.mqtt_fallback.socket.create_connection", return_value=raw), \
         patch.object(instance, "_ssl_wrap_socket", side_effect=OSError("TLS failed")), \
         pytest.raises(OSError, match="TLS failed"):
        instance._create_socket()
    raw.close.assert_called_once()
