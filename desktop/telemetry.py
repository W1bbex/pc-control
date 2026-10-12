"""Telemetry collection and system metrics mixin for desktop agent."""

import ctypes
from ctypes import wintypes
import json
import logging
import os
import platform
import socket
import subprocess
import threading
import time

import psutil

from desktop.constants import (
    AGENT_START_TIME,
    ICONS,
    REAL_BOOT_TIME,
    TOPIC_TELEMETRY,
)

LOG = logging.getLogger("pc-control-agent")


class TelemetryMixin:
    """Provides system hardware metrics, network rates, audio control, and telemetry publishing."""

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
    def get_session_seconds():
        try:
            for p in psutil.process_iter(["name", "create_time"]):
                if p.info["name"] and p.info["name"].lower() == "explorer.exe":
                    ct = p.info.get("create_time")
                    if ct:
                        return max(0, int(time.time() - ct))
        except Exception:
            pass
        return max(0, int(time.time() - AGENT_START_TIME))

    @staticmethod
    def get_idle_seconds():
        class LastInputInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("dwTime", ctypes.c_uint),
            ]

        info = LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        elapsed_ms = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return max(0, int(elapsed_ms / 1000))

    @staticmethod
    def get_foreground_window_safe():
        try:
            return ctypes.windll.user32.GetForegroundWindow() or None
        except Exception:
            return None

    def get_active_application_info(self):
        desktop_emoji = ICONS.get("screen") or ICONS.get("apps", "5330015235644368681")
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hwnd = self.get_foreground_window_safe()
            if not hwnd:
                return "Рабочий стол", desktop_emoji, None
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return "Рабочий стол", desktop_emoji, None
            kernel32.OpenProcess.restype = wintypes.HANDLE
            hproc = kernel32.OpenProcess(0x1000, False, pid.value)
            name = None
            if hproc:
                try:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                        name = os.path.basename(buf.value)
                finally:
                    kernel32.CloseHandle(hproc)

            title = None
            tlen = user32.GetWindowTextLengthW(hwnd)
            if tlen:
                tbuf = ctypes.create_unicode_buffer(tlen + 1)
                user32.GetWindowTextW(hwnd, tbuf, tlen + 1)
                title = tbuf.value or None

            if not name or name.lower() == "explorer.exe":
                cls_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls_buf, 256)
                cls_name = cls_buf.value
                if cls_name in ("Progman", "WorkerW", "Shell_TrayWnd") or not name:
                    return "Рабочий стол", desktop_emoji, title

            emoji_id = None
            if name and hasattr(self, "emoji_manager"):
                key = name.lower()
                cached = self.emoji_manager.cache.get(key)
                if cached:
                    emoji_id = cached.get("emoji_id")
            final_app = name or "Рабочий стол"
            final_emoji = emoji_id or (desktop_emoji if not name or name.lower() == "explorer.exe" else None)
            return final_app, final_emoji, title
        except Exception:
            return "Рабочий стол", desktop_emoji, None

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
            values = [
                item.strip()
                for item in completed.stdout.splitlines()[0].split(",")
            ]

            def optional_number(value):
                if value.upper() in {"N/A", "[N/A]", "NOT SUPPORTED"}:
                    return None
                return float(value)

            return {
                "name": values[0],
                "temp_c": float(values[1]),
                "load_percent": float(values[2]),
                "vram_used_mb": float(values[3]),
                "vram_total_mb": float(values[4]),
                "fan_percent": optional_number(values[5]),
                "power_w": optional_number(values[6]),
                "clock_mhz": optional_number(values[7]),
                "memory_clock_mhz": optional_number(values[8]),
            }
        except Exception:
            LOG.exception("Could not query NVIDIA GPU")
            return None

    @staticmethod
    def get_volume():
        import pythoncom

        pythoncom.CoInitialize()
        try:
            from pycaw.pycaw import AudioUtilities

            endpoint = AudioUtilities.GetSpeakers().EndpointVolume
            return {
                "percent": round(endpoint.GetMasterVolumeLevelScalar() * 100),
                "muted": bool(endpoint.GetMute()),
            }
        except Exception:
            return None
        finally:
            pythoncom.CoUninitialize()

    def set_volume(self, percent=None, mute=None, toggle=False):
        import pythoncom

        pythoncom.CoInitialize()
        try:
            from pycaw.pycaw import AudioUtilities

            endpoint = AudioUtilities.GetSpeakers().EndpointVolume

            if toggle:
                endpoint.SetMute(not bool(endpoint.GetMute()), None)
            elif mute is not None:
                endpoint.SetMute(bool(mute), None)

            if percent is not None:
                value = max(0, min(100, int(percent))) / 100.0
                endpoint.SetMasterVolumeLevelScalar(value, None)

            return {
                "percent": round(
                    endpoint.GetMasterVolumeLevelScalar() * 100
                ),
                "muted": bool(endpoint.GetMute()),
            }
        finally:
            pythoncom.CoUninitialize()

    def network_rates(self):
        now = time.time()
        current = psutil.net_io_counters()
        if current is None or self.previous_net is None:
            return {
                "download_mbps": 0.0,
                "upload_mbps": 0.0,
                "received_gb": 0.0,
                "sent_gb": 0.0,
            }
        elapsed = max(0.1, now - self.previous_net_time)

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

        disks = []
        seen_devices = set()
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        drive_fixed = 3
        for partition in psutil.disk_partitions(all=False):
            if partition.device in seen_devices:
                continue
            if get_drive_type(partition.mountpoint) != drive_fixed:
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

        sensors = self.sensor_helper.snapshot()
        temperatures = [
            sensor
            for sensor in sensors
            if (
                sensor.get("type") in ("Temperature", "Fan", "Power")
                and float(sensor.get("value", 0)) != 0
            )
        ][:60]

        cpu_temperature = next(
            (
                float(sensor["value"])
                for sensor in temperatures
                if (
                    sensor.get("hardware_type") == "Cpu"
                    and sensor.get("type") == "Temperature"
                    and float(sensor.get("value", 0)) > 5
                )
            ),
            None,
        )
        idle_seconds = self.get_idle_seconds()
        active_app, active_emoji, active_title = self.get_active_application_info()

        is_pc_control = False
        if active_app:
            app_lower = active_app.lower()
            if any(client in app_lower for client in ("telegram", "ayugram", "kotatogram", "64gram", "unigram")):
                title_lower = (active_title or "").lower()
                if "pc control" in title_lower or "your_pc_bot" in title_lower:
                    is_pc_control = True
        self.in_pc_control_chat = is_pc_control

        telemetry = {
            "timestamp": int(now),
            "ts": now,
            "agent_mode": "desktop",
            "host": socket.gethostname(),
            "os": platform.system().lower(),
            "windows": platform.platform(),
            "local_ip": self.get_local_ip(),
            "uptime_seconds": max(0, int(time.time() - REAL_BOOT_TIME)),
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
            "volume": self.get_volume(),
            "idle_seconds": idle_seconds,
            "session_seconds": self.get_session_seconds(),
            "active_application": active_app,
            "active_application_emoji_id": active_emoji,
            "active_window_title": active_title,
            "in_pc_control_chat": is_pc_control,
            "user_active": (
                idle_seconds is not None and idle_seconds < 300
            ),
            "sensors": temperatures,
            "stealth_mode": self.stealth_mgr.is_active(),
        }
        self.last_telemetry = telemetry
        return telemetry

    @staticmethod
    def top_processes():
        tracked = {}
        for process in psutil.process_iter(["pid", "name"]):
            try:
                process.cpu_percent(None)
                tracked[process.pid] = process
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        time.sleep(0.7)

        processes = []
        for pid, process in tracked.items():
            if pid == 0:
                continue
            try:
                cpu_percent = process.cpu_percent(None)
                cpu_percent /= max(1, psutil.cpu_count() or 1)
                mem = process.info.get("memory_info") if hasattr(process, "info") and process.info else None
                if mem is None:
                    try:
                        mem = process.memory_info()
                    except Exception:
                        mem = None
                rss_bytes = getattr(mem, "rss", 0) if mem else 0
                name = process.info.get("name") if hasattr(process, "info") and process.info else None
                if not name:
                    try:
                        name = process.name()
                    except Exception:
                        name = "unknown"
                processes.append(
                    {
                        "pid": pid,
                        "name": name or "unknown",
                        "cpu_percent": round(cpu_percent, 1),
                        "memory_mb": round(rss_bytes / 1_000_000, 1),
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError, TypeError):
                continue

        processes.sort(
            key=lambda item: (item["cpu_percent"], item["memory_mb"]),
            reverse=True,
        )
        return processes[:10]

    def publish_telemetry(self):
        with self.state_lock:
            telemetry = self.collect_telemetry()
            payload = json.dumps(telemetry, ensure_ascii=False)
        self.mqtt.publish(
            TOPIC_TELEMETRY,
            payload=payload,
            qos=0,
            retain=True,
        )

    def publish_active_app_update(self, proc_name, emoji_id, title=None):
        if not getattr(self, "mqtt_online", False):
            return
        try:
            with self.state_lock:
                if not self.last_telemetry:
                    self.last_telemetry = self.collect_telemetry()
                self.last_telemetry["active_application"] = proc_name
                self.last_telemetry["active_application_emoji_id"] = emoji_id
                self.last_telemetry["active_window_title"] = title

                is_pc_control = False
                if proc_name:
                    app_lower = proc_name.lower()
                    telegram_clients = ("telegram", "ayugram", "kotatogram", "64gram", "unigram")
                    if any(client in app_lower for client in telegram_clients):
                        title_lower = (title or "").lower()
                        if "pc control" in title_lower or "your_pc_bot" in title_lower:
                            is_pc_control = True
                self.last_telemetry["in_pc_control_chat"] = is_pc_control
                self.in_pc_control_chat = is_pc_control

                now = time.time()
                self.last_telemetry["timestamp"] = int(now)
                self.last_telemetry["ts"] = now
                payload = json.dumps(self.last_telemetry, ensure_ascii=False)

            self.mqtt.publish(TOPIC_TELEMETRY, payload=payload, qos=0, retain=True)
            LOG.info("Instant active app telemetry published: %s (title: %r, emoji: %s)", proc_name, title, emoji_id)
        except Exception:
            LOG.exception("Failed to publish instant active app telemetry")

    def _handle_active_app_change(self, exe_path, exe_name, title):
        from core.app_emoji_manager import IGNORED_PROCESSES
        from core.activity_db import get_activity_db

        key = exe_name.lower()
        if key in IGNORED_PROCESSES:
            return

        with self.state_lock:
            if self.current_session_app and key == self.current_session_app.lower():
                self.current_session_title = title
                return

            now = time.time()
            if self.current_session_app and self.current_session_start:
                try:
                    get_activity_db().record_session(
                        app_name=self.current_session_app,
                        window_title=self.current_session_title,
                        exe_path=self.current_session_exe,
                        started_at=self.current_session_start,
                        ended_at=now,
                        emoji_id=self.current_session_emoji_id,
                    )
                except Exception:
                    LOG.exception("Failed to record session to SQLite")

            emoji_id = self.emoji_manager.cache.get(key, {}).get("emoji_id")
            self.current_session_app = exe_name
            self.current_session_title = title
            self.current_session_exe = exe_path
            self.current_session_start = now
            self.current_session_emoji_id = emoji_id

        self.publish_active_app_update(exe_name, emoji_id, title)

        if not emoji_id and exe_path:
            def _bg_notify():
                try:
                    new_emoji_id = self.emoji_manager.notify_app_opened(
                        exe_path, exe_name, title
                    )
                    if new_emoji_id:
                        self.current_session_emoji_id = new_emoji_id
                        if new_emoji_id != emoji_id:
                            self.publish_active_app_update(exe_name, new_emoji_id, title)
                except Exception:
                    LOG.exception("Error in _bg_notify")

            threading.Thread(
                target=_bg_notify,
                name="emoji-notify-fast",
                daemon=True,
            ).start()
