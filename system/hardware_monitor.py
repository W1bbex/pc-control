import ctypes
import json
import platform
import socket
import subprocess
import time
from pathlib import Path

import psutil

from system.constants import BASE_DIR, DATA_DIR, LOG, REAL_BOOT_TIME
from system.session_manager import active_console_session


class HardwareMonitorMixin:
    """Mixin for hardware telemetry, sensor reading, and GPU/CPU stats."""

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
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("1.1.1.1", 53))
                return sock.getsockname()[0]
        except OSError:
            return "unknown"

    @staticmethod
    def _find_sensor_snapshot() -> Path:
        for candidate in (
            DATA_DIR / "admin-sensors.json",
            BASE_DIR / "hardware" / "sensors" / "admin-sensors.json",
            BASE_DIR / "hardware" / "admin-sensors.json",
        ):
            if candidate.exists():
                return candidate
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
            lines = [l for l in completed.stdout.splitlines() if l.strip()]
            if not lines:
                return None
            values = [
                item.strip()
                for item in lines[0].split(",")
            ]

            def optional_number(value):
                if value.upper() in {"N/A", "[N/A]", "NOT SUPPORTED"}:
                    return None
                return float(value)

            return {
                "name": values[0],
                "temp_c": optional_number(values[1]),
                "load_percent": optional_number(values[2]),
                "vram_used_mb": optional_number(values[3]),
                "vram_total_mb": optional_number(values[4]),
                "fan_percent": optional_number(values[5]),
                "power_w": optional_number(values[6]),
                "clock_mhz": optional_number(values[7]),
                "memory_clock_mhz": optional_number(values[8]),
            }
        except Exception:
            LOG.exception("Could not query NVIDIA GPU from system agent")
            return None

    def network_rates(self):
        now = time.time()
        current = psutil.net_io_counters()
        if current is None or getattr(self, "previous_net", None) is None:
            if current is not None:
                self.previous_net = current
                self.previous_net_time = now
            return {
                "download_mbps": 0.0,
                "upload_mbps": 0.0,
                "received_gb": 0.0,
                "sent_gb": 0.0,
            }
        elapsed = max(0.1, now - getattr(self, "previous_net_time", now))
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
                and _safe_sensor_val(sensor) != 0
            )
        ][:60]
        cpu_temperature = next(
            (
                _safe_sensor_val(sensor)
                for sensor in useful_sensors
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
