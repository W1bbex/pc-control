import os
import subprocess
import time
from pathlib import Path

import psutil

from core.process_utils import (
    get_top_processes,
    kill_process_by_name,
    kill_process_by_pid as kill_process_pid,
    restart_windows_service,
)
from system.constants import LOG, PROTECTED_PROCESSES


class ProcessManagerMixin:
    """Mixin for process inspection, process termination, service management, and hosts mapping."""

    @staticmethod
    def top_processes():
        return get_top_processes()

    @staticmethod
    def kill_process(process_name):
        return kill_process_by_name(
            process_name,
            PROTECTED_PROCESSES,
            allow_desktop_agent_alias=True,
        )

    @staticmethod
    def kill_process_by_pid(pid):
        return kill_process_pid(pid, PROTECTED_PROCESSES)

    def restart_service(self, service_key):
        return restart_windows_service(service_key, self.config.get("services"))

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
            target_domain = (
                os.getenv("API_HOSTS_DOMAIN")
                or self.config.get("api_hosts_domain")
                or os.getenv("MQTT_HOST")
                or self.config.get("mqtt", {}).get("host", "")
            ).strip()
            if not target_domain or target_domain == "mqtt.example.com":
                return

            lines = content.splitlines()
            target_domain_l = target_domain.lower()
            found = False
            updated = False
            for i, line in enumerate(lines):
                code_part = line.split("#", 1)[0].strip()
                if not code_part:
                    continue
                parts = code_part.split()
                if len(parts) < 2:
                    continue
                aliases = [alias.lower() for alias in parts[1:]]
                if target_domain_l in aliases:
                    found = True
                    if parts[0] != target_ip:
                        if len(parts) == 2:
                            lines[i] = f"{target_ip} {target_domain} # pc-control direct"
                        else:
                            lines[i] = f"{target_ip} {' '.join(parts[1:])} # pc-control direct"
                        updated = True
                    break

            if not found:
                lines.append(f"{target_ip} {target_domain} # pc-control direct")
                updated = True

            if updated:
                tmp_hosts = hosts_path.with_name(f".{hosts_path.name}.tmp")
                tmp_hosts.write_text("\n".join(lines) + "\n", encoding="utf-8")
                tmp_hosts.replace(hosts_path)
                LOG.info("Updated hosts file: %s -> %s", target_domain, target_ip)
        except Exception as e:
            LOG.warning("Could not verify/update hosts file: %s", e)
