"""Hardware sensor helper for reading elevated sensor snapshots."""

import json
import logging
import subprocess
import threading
import time
from pathlib import Path

from desktop.constants import BASE_DIR

LOG = logging.getLogger("pc-control-agent")


class SensorHelper:
    def __init__(self, executable: Path):
        self.executable = executable
        self.process = None
        self.latest = []
        self.lock = threading.Lock()

    def start(self):
        if not self.executable.exists():
            LOG.warning("Sensor helper is not installed")
            return

        try:
            self.process = subprocess.Popen(
                [str(self.executable)],
                cwd=str(self.executable.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(
                target=self._read_stdout,
                name="sensor-reader",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._read_stderr,
                name="sensor-errors",
                daemon=True,
            ).start()
            LOG.info("Hardware sensor helper started")
        except Exception:
            LOG.exception("Could not start hardware sensor helper")

    def _read_stdout(self):
        for line in self.process.stdout:
            try:
                sensors = json.loads(line)
                if isinstance(sensors, list):
                    with self.lock:
                        self.latest = sensors
            except json.JSONDecodeError:
                LOG.warning("Invalid sensor helper output")

    def _read_stderr(self):
        for line in self.process.stderr:
            LOG.warning("Sensor helper: %s", line.rstrip())

    def _find_admin_snapshot(self) -> Path:
        for candidate in (
            BASE_DIR / "data" / "admin-sensors.json",
            self.executable.parent / "admin-sensors.json",
            BASE_DIR / "hardware" / "admin-sensors.json",
        ):
            if candidate.exists():
                return candidate
        return BASE_DIR / "data" / "admin-sensors.json"

    def snapshot(self):
        target = self._find_admin_snapshot()
        try:
            age = time.time() - target.stat().st_mtime
            if age < 15:
                data = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
        except (FileNotFoundError, PermissionError, OSError):
            pass
        except Exception:
            LOG.exception("Could not read elevated sensor snapshot")

        with self.lock:
            return list(self.latest)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
