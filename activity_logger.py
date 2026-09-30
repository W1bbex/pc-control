#!/usr/bin/env python3

"""Local, privacy-bounded desktop activity timeline for PCControlAgent."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import logging.handlers
import os
from pathlib import Path
import socket
import threading
import time

import psutil


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int | None
    name: str
    exe: str | None
    create_time: float | None


class ActivityRecorder:
    """Write structured events to a daily rotating JSON Lines file."""

    def __init__(self, path: Path, retention_days: int = 30):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(f"pc-control-activity-{path}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)

        handler = logging.handlers.TimedRotatingFileHandler(
            path,
            when="midnight",
            interval=1,
            backupCount=max(1, int(retention_days)),
            encoding="utf-8",
            delay=True,
        )
        handler.suffix = "%Y-%m-%d"
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(handler)
        self._lock = threading.Lock()

    def emit(self, event: str, **fields):
        now = datetime.now().astimezone()
        payload = {
            "ts": now.isoformat(timespec="milliseconds"),
            "epoch": round(now.timestamp(), 3),
            "event": event,
            **fields,
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.logger.info(line)

    def close(self):
        with self._lock:
            for handler in list(self.logger.handlers):
                handler.flush()
                handler.close()
                self.logger.removeHandler(handler)


class ActivityTracker:
    """Poll inexpensive Windows signals and turn changes into a timeline."""

    def __init__(self, config, log_dir: Path, stop_event: threading.Event, on_foreground_change=None):
        self.config = config or {}
        self.stop_event = stop_event
        self.on_foreground_change = on_foreground_change
        self.poll_seconds = max(0.25, float(self.config.get("poll_seconds", 1)))
        self.process_poll_seconds = max(
            10.0, float(self.config.get("process_poll_seconds", 30.0))
        )
        self.heartbeat_seconds = max(
            10, float(self.config.get("heartbeat_seconds", 60))
        )
        self.network_poll_seconds = max(
            2, float(self.config.get("network_poll_seconds", 5))
        )
        self.idle_threshold_seconds = max(
            30, int(self.config.get("idle_threshold_seconds", 300))
        )
        self.include_window_titles = bool(
            self.config.get("include_window_titles", True)
        )
        self.include_process_paths = bool(
            self.config.get("include_process_paths", True)
        )
        self.record_existing_processes = bool(
            self.config.get("record_existing_processes", True)
        )
        retention_days = int(self.config.get("retention_days", 30))
        self.recorder = ActivityRecorder(
            log_dir / "activity.jsonl",
            retention_days=retention_days,
        )
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(
            target=self.run,
            name="activity-tracker",
            daemon=True,
        )
        self.thread.start()

    def stop(self):
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=max(2, self.poll_seconds * 2))
        self.recorder.close()

    def _processes(self, previous=None):
        result = {}
        previous = previous or {}
        for process in psutil.process_iter(["pid", "name"]):
            try:
                item = process.info
                cached = previous.get(int(item["pid"]))
                if cached is not None and cached.name == (item.get("name") or "unknown"):
                    result[cached.pid] = cached
                    continue
                details = process.as_dict(attrs=["ppid", "exe", "create_time"])
                info = ProcessInfo(
                    pid=int(item["pid"]),
                    ppid=details.get("ppid"),
                    name=item.get("name") or "unknown",
                    exe=details.get("exe") if self.include_process_paths else None,
                    create_time=details.get("create_time"),
                )
                result[info.pid] = info
            except (psutil.Error, OSError, TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _idle_seconds():
        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        # LASTINPUTINFO uses the low 32 bits of the system tick counter.
        elapsed = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return max(0, round(elapsed / 1000, 1))

    def _foreground(self, processes):
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {"hwnd": 0, "pid": 0, "process": None, "title": None}

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = None
        if self.include_window_titles:
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value or None

        info = processes.get(pid.value)
        if info is None and pid.value:
            try:
                process = psutil.Process(pid.value)
                info = ProcessInfo(
                    pid=pid.value,
                    ppid=process.ppid(),
                    name=process.name() or "unknown",
                    exe=process.exe() if self.include_process_paths else None,
                    create_time=process.create_time(),
                )
                processes[pid.value] = info
            except (psutil.Error, OSError):
                pass

        return {
            "hwnd": int(hwnd),
            "pid": int(pid.value),
            "process": info.name if info else None,
            "exe": info.exe if info else None,
            "title": title,
        }

    @staticmethod
    def _network_state():
        state = []
        stats = psutil.net_if_stats()
        for name, addresses in psutil.net_if_addrs().items():
            if not stats.get(name) or not stats[name].isup:
                continue
            ips = sorted(
                address.address
                for address in addresses
                if address.family in (socket.AF_INET, socket.AF_INET6)
                and not address.address.startswith(("127.", "::1", "fe80:"))
            )
            if ips:
                state.append({"interface": name, "addresses": ips})
        return sorted(state, key=lambda item: item["interface"].lower())

    def _process_fields(self, info):
        fields = {
            "pid": info.pid,
            "ppid": info.ppid,
            "process": info.name,
        }
        if info.exe:
            fields["exe"] = info.exe
        if info.create_time:
            fields["process_started"] = datetime.fromtimestamp(
                info.create_time
            ).astimezone().isoformat(timespec="milliseconds")
        return fields

    def run(self):
        known = {}
        foreground_signature = None
        last_foreground = None
        last_heartbeat = 0.0
        last_network_poll = 0.0
        last_process_poll = 0.0
        network_state = None
        was_idle = False

        self.recorder.emit(
            "tracker_start",
            schema=SCHEMA_VERSION,
            host=socket.gethostname(),
            tracker_pid=os.getpid(),
            timezone=datetime.now().astimezone().tzname(),
        )

        try:
            known = self._processes()
            last_process_poll = time.monotonic()
            if self.record_existing_processes:
                for info in sorted(known.values(), key=lambda item: item.pid):
                    self.recorder.emit(
                        "process_existing", **self._process_fields(info)
                    )

            while not self.stop_event.is_set():
                loop_started = time.monotonic()
                if loop_started - last_process_poll >= self.process_poll_seconds:
                    current = self._processes(known)
                    for pid, info in current.items():
                        old = known.get(pid)
                        if old is None or old.create_time != info.create_time:
                            self.recorder.emit(
                                "process_start", **self._process_fields(info)
                            )
                    for pid, info in known.items():
                        if pid not in current:
                            fields = self._process_fields(info)
                            if info.create_time:
                                fields["lifetime_seconds"] = max(
                                    0, round(time.time() - info.create_time, 1)
                                )
                            self.recorder.emit("process_exit", **fields)
                    known = current
                    last_process_poll = loop_started

                foreground = self._foreground(known)
                signature = (
                    foreground.get("hwnd"),
                    foreground.get("pid"),
                    foreground.get("title"),
                )
                if signature != foreground_signature:
                    self.recorder.emit("foreground", **foreground)
                    foreground_signature = signature
                    last_foreground = foreground
                    last_heartbeat = time.monotonic()
                    if self.on_foreground_change and foreground.get("exe") and foreground.get("process"):
                        try:
                            self.on_foreground_change(foreground["exe"], foreground["process"], foreground.get("title"))
                        except Exception:
                            pass

                idle_seconds = self._idle_seconds()
                is_idle = (
                    idle_seconds is not None
                    and idle_seconds >= self.idle_threshold_seconds
                )
                if is_idle and not was_idle:
                    self.recorder.emit(
                        "idle_start",
                        idle_seconds=idle_seconds,
                        foreground=last_foreground,
                    )
                elif was_idle and not is_idle:
                    self.recorder.emit("idle_end", idle_seconds=idle_seconds)
                was_idle = is_idle

                now_monotonic = time.monotonic()
                if now_monotonic - last_heartbeat >= self.heartbeat_seconds:
                    self.recorder.emit(
                        "heartbeat",
                        idle_seconds=idle_seconds,
                        foreground=last_foreground,
                    )
                    last_heartbeat = now_monotonic

                if now_monotonic - last_network_poll >= self.network_poll_seconds:
                    current_network = self._network_state()
                    if network_state is None:
                        self.recorder.emit("network_snapshot", interfaces=current_network)
                    elif current_network != network_state:
                        self.recorder.emit(
                            "network_change",
                            before=network_state,
                            after=current_network,
                        )
                    network_state = current_network
                    last_network_poll = now_monotonic

                elapsed = time.monotonic() - loop_started
                self.stop_event.wait(max(0.05, self.poll_seconds - elapsed))
        except Exception as error:
            self.recorder.emit(
                "tracker_error",
                error_type=type(error).__name__,
                error=str(error),
            )
        finally:
            self.recorder.emit("tracker_stop")
