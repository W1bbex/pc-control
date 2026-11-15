import ctypes
import json
import platform
import socket
import threading
import time
from pathlib import Path

import psutil

from core.hardware_utils import (
    compute_network_rates,
    get_cpu_name as read_cpu_name,
    get_local_ip as resolve_local_ip,
    query_nvidia_gpu,
)
from system.constants import BASE_DIR, DATA_DIR, LOG, REAL_BOOT_TIME
from system.session_manager import active_console_session


class HardwareMonitorMixin:
    """Mixin for hardware telemetry, sensor reading, and GPU/CPU stats."""

    @staticmethod
    def get_cpu_name():
        return read_cpu_name()

    @staticmethod
    def get_local_ip():
        return resolve_local_ip()

    @staticmethod
    def _find_sensor_snapshot() -> Path:
        candidates = (
            DATA_DIR / "admin-sensors.json",
            BASE_DIR / "hardware" / "sensors" / "admin-sensors.json",
            BASE_DIR / "hardware" / "admin-sensors.json",
        )
        existing = []
        for candidate in candidates:
            try:
                if candidate.exists():
                    existing.append((candidate.stat().st_mtime, candidate))
            except OSError:
                continue
        if existing:
            return max(existing, key=lambda item: item[0])[1]
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
        return query_nvidia_gpu(
            logger=LOG,
            warn_on_error=True,
            missing_flag_holder=HardwareMonitorMixin,
        )

    _init_lock = threading.Lock()

    def _get_net_state_lock(self):
        lock = getattr(self, "_net_state_lock", None)
        if lock is None:
            with HardwareMonitorMixin._init_lock:
                lock = getattr(self, "_net_state_lock", None)
                if lock is None:
                    lock = threading.Lock()
                    self._net_state_lock = lock
        return lock

    def network_rates(self):
        return compute_network_rates(self, self._get_net_state_lock())

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

        def _safe_sensor_val(s):
            val = s.get("value")
            if val is None:
                return 0.0
            try:
                return float(val)
            except (ValueError, TypeError):
                return 0.0

        sensors = self.read_sensors()
        useful_sensors = [
            sensor
            for sensor in sensors
            if (
                sensor.get("type") in ("Temperature", "Fan", "Power")
                and (sensor.get("type") == "Fan" or _safe_sensor_val(sensor) != 0)
            )
        ][:60]
        cpu_temperature = next(
            (
                _safe_sensor_val(sensor)
                for sensor in sensors
                if (
                    sensor.get("hardware_type") == "Cpu"
                    and sensor.get("type") == "Temperature"
                    and _safe_sensor_val(sensor) > 5
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
