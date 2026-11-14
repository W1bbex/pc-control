"""Tests for the ESP8266 firmware static timeout verification helper."""

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "hardware" / "esp8266"))

from check_esp_timeouts import check_timeouts


def test_check_esp_timeouts_accepts_valid_connect_mqtt(tmp_path):
    ino = tmp_path / "valid.ino"
    ino.write_text(
        """
void helper() {
  secureClient.stop();
}

void connectMqtt() {
  secureClient.stop();
  secureClient.setTimeout(3000);
  mqtt.connect("esp");
}
""",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        check_timeouts(str(ino))
    assert exc.value.code == 0


def test_check_esp_timeouts_rejects_stop_outside_connect_mqtt(tmp_path):
    ino = tmp_path / "invalid_scope.ino"
    ino.write_text(
        """
void serviceNetworkRecovery() {
  secureClient.stop();
}

void connectMqtt() {
  secureClient.setTimeout(3000);
  mqtt.connect("esp");
}
""",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        check_timeouts(str(ino))
    assert exc.value.code == 1


def test_check_esp_timeouts_rejects_wrong_order_inside_connect_mqtt(tmp_path):
    ino = tmp_path / "wrong_order.ino"
    ino.write_text(
        """
void connectMqtt() {
  secureClient.setTimeout(3000);
  secureClient.stop();
  mqtt.connect("esp");
}
""",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        check_timeouts(str(ino))
    assert exc.value.code == 1
