import os
import subprocess
import time
from pathlib import Path

import psutil

from system.constants import LOG, PROTECTED_PROCESSES


class ProcessManagerMixin:
    """Mixin for process inspection, process termination, service management, and hosts mapping."""

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

    @staticmethod
    def kill_process(process_name):
        normalized = str(process_name).lower()
        if normalized in {"desktop_agent", "agent.py"}:
            killed = []
            for process in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    pname = (process.info["name"] or "").lower()
                    cmd = " ".join(process.info.get("cmdline") or [])
                    if pname in {"python.exe", "pythonw.exe"} and "agent.py" in cmd and "boot_agent.py" not in cmd and process.info["pid"] != os.getpid():
                        process.terminate()
                        killed.append(process.info["pid"])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return {"name": "desktop_agent", "terminated_pids": killed}

        if normalized in PROTECTED_PROCESSES:
            raise PermissionError("Protected Windows process")
        killed = []
        for process in psutil.process_iter(["pid", "name"]):
            try:
                if (process.info["name"] or "").lower() == normalized:
                    process.terminate()
                    killed.append(process.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {"name": process_name, "terminated_pids": killed}

    @staticmethod
    def kill_process_by_pid(pid):
        process = psutil.Process(int(pid))
        name = (process.name() or "").lower()
        if process.pid == os.getpid() or name in PROTECTED_PROCESSES:
            raise PermissionError("Protected Windows process")
        original_name = process.name()
        process.terminate()
        return {"name": original_name, "terminated_pid": int(pid)}

    def restart_service(self, service_key):
        service_name = self.config.get("services", {}).get(service_key)
        if not service_name:
            raise ValueError("Service is not configured")
        subprocess.run(
            ["sc.exe", "stop", service_name],
            capture_output=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(10):
            time.sleep(1)
            q = subprocess.run(
                ["sc.exe", "query", service_name],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if "STOPPED" in (q.stdout or ""):
                break
        completed = subprocess.run(
            ["sc.exe", "start", service_name],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stdout or completed.stderr)
        return {"service": service_key}

    @staticmethod
    def restart_desktop_agent():
        task_name = "PC Control Desktop Agent"
        subprocess.run(
            ["schtasks.exe", "/End", "/TN", task_name],
            capture_output=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        completed = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", task_name],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stdout or completed.stderr)
        return {"task": task_name}

    def _ensure_hosts_mapping(self):
        try:
            target_ip = os.getenv("API_HOSTS_IP", self.config.get("api_hosts_ip", "")).strip()
            if not target_ip:
                return
            sys_root = os.environ.get("SystemRoot", r"C:\Windows")
            hosts_path = Path(sys_root) / "System32" / "drivers" / "etc" / "hosts"
            if not hosts_path.exists():
                return
            content = hosts_path.read_text(encoding="utf-8", errors="replace")
            target_domain = self.config.get("api_hosts_domain", "mqtt.example.com").strip()

            lines = content.splitlines()
            found = False
            updated = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split()
                if len(parts) >= 2 and parts[1] == target_domain:
                    found = True
                    if parts[0] != target_ip:
                        lines[i] = f"{target_ip} {target_domain} # pc-control direct"
                        updated = True
                    break

            if not found:
                lines.append(f"{target_ip} {target_domain} # pc-control direct")
                updated = True

            if updated:
                hosts_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                LOG.info("Updated hosts file: %s -> %s", target_domain, target_ip)
        except Exception as e:
            LOG.warning("Could not verify/update hosts file: %s", e)
