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
        try:
            for line in self.process.stdout:
                try:
                    sensors = json.loads(line)
                    if isinstance(sensors, list):
                        with self.lock:
                            self.latest = sensors
                except json.JSONDecodeError:
                    LOG.warning("Invalid sensor helper output")
        except (ValueError, OSError):
            pass

    def _read_stderr(self):
        try:
            for line in self.process.stderr:
                LOG.warning("Sensor helper: %s", line.rstrip())
        except (ValueError, OSError):
            pass

    def _find_admin_snapshot(self) -> Path:
        candidates = (
            BASE_DIR / "data" / "admin-sensors.json",
            self.executable.parent / "admin-sensors.json",
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
        return BASE_DIR / "data" / "admin-sensors.json"

    def snapshot(self):
        target = self._find_admin_snapshot()
        for attempt in range(3):
            try:
                age = time.time() - target.stat().st_mtime
                if age < 15:
                    data = json.loads(target.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        return data
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if attempt < 2:
                    time.sleep(0.1)
                    continue
                break
            except OSError:
                break
            except Exception:
                LOG.exception("Could not read elevated sensor snapshot")
                break

        with self.lock:
            if self.process and self.process.poll() is not None and not self.latest:
                LOG.debug("SensorHelper process exited with code %s", self.process.poll())
            return list(self.latest)

    def stop(self):
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        except Exception:
            pass
        finally:
            for stream in (getattr(self.process, "stdout", None), getattr(self.process, "stderr", None)):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
