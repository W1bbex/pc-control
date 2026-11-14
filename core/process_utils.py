"""Shared process inspection utilities for Windows agents."""

import os
import subprocess
import time
from collections.abc import Collection
import psutil


def get_top_processes(limit: int = 10, sample_delay: float = 0.7) -> list[dict]:
    """Return top processes sorted by CPU percentage and memory usage."""
    tracked = {}
    for process in psutil.process_iter(["pid", "name"]):
        try:
            process.cpu_percent(None)
            tracked[process.pid] = process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    time.sleep(sample_delay)

    processes = []
    cpu_count = max(1, psutil.cpu_count() or 1)
    for pid, process in tracked.items():
        if pid == 0:
            continue
        try:
            cpu_percent = process.cpu_percent(None) / cpu_count
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
    return processes[:limit]


def run_shell_command(
    cmd_text: str,
    use_ps: bool = False,
    timeout_s: int = 60,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    """Execute a CMD or PowerShell command headlessly without CMD quote corruption."""
    if use_ps:
        exec_cmd: str | list[str] = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; {cmd_text}",
        ]
    else:
        exec_cmd = f'cmd.exe /s /c "chcp 65001 >nul & {cmd_text}"'

    try:
        p = subprocess.run(
            exec_cmd,
            capture_output=True,
            timeout=timeout_s,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout = p.stdout
        stderr = p.stderr
        code = p.returncode
    except subprocess.TimeoutExpired:
        stdout = ""
        stderr = f"Command timed out after {timeout_s}s"
        code = 124
    except Exception as e:
        stdout = ""
        stderr = f"Execution error: {e}"
        code = -1

    max_len = 8000
    if len(stdout) > max_len:
        stdout = stdout[:max_len] + f"\n... [обрезано {len(stdout) - max_len} симв.]"
    if len(stderr) > 4000:
        stderr = stderr[:4000] + "\n... [обрезано]"

    return code, stdout, stderr


def kill_process_by_name(
    process_name: str,
    protected_processes: Collection[str],
    allow_desktop_agent_alias: bool = False,
) -> dict:
    """Terminate matching Windows processes unless they belong to protected processes."""
    if not process_name:
        raise ValueError("Process name is required")
    normalized = str(process_name).lower().strip()
    if allow_desktop_agent_alias and normalized in {"desktop_agent", "agent.py"}:
        killed = []
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                pname = (process.info["name"] or "").lower()
                cmd = " ".join(process.info.get("cmdline") or [])
                if (
                    pname in {"python.exe", "pythonw.exe"}
                    and "agent.py" in cmd
                    and "boot_agent.py" not in cmd
                    and process.info["pid"] != os.getpid()
                ):
                    process.terminate()
                    killed.append(process.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {"name": "desktop_agent", "terminated_pids": killed}

    if normalized in protected_processes:
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


def kill_process_by_pid(pid: int | str, protected_processes: Collection[str]) -> dict:
    """Terminate a single Windows process by PID unless it is self or protected."""
    process = psutil.Process(int(pid))
    name = (process.name() or "").lower()
    if process.pid == os.getpid() or name in protected_processes:
        raise PermissionError("Protected Windows process")
    original_name = process.name()
    process.terminate()
    return {"name": original_name, "terminated_pid": int(pid)}


def restart_windows_service(service_key: str, services_config: dict | None) -> dict:
    """Stop, wait for STOPPED state, and start a configured Windows service."""
    service_name = (services_config or {}).get(service_key)
    if not service_name:
        raise ValueError("Service is not configured")

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["sc.exe", "stop", service_name],
        capture_output=True,
        timeout=20,
        creationflags=creationflags,
    )
    for _ in range(10):
        time.sleep(1)
        query = subprocess.run(
            ["sc.exe", "query", service_name],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=creationflags,
        )
        if "STOPPED" in (query.stdout or ""):
            break
    completed = subprocess.run(
        ["sc.exe", "start", service_name],
        capture_output=True,
        text=True,
        timeout=20,
        creationflags=creationflags,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout or completed.stderr)
    return {"service": service_key}


def shell_execute_runas(
    executable: str,
    params: str = "",
    cwd: str | None = None,
    show_cmd: int = 1,
) -> int:
    """Elevate an executable via ShellExecuteW 'runas' with correct 64-bit HINSTANCE handling."""
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE
    ret = shell32.ShellExecuteW(
        None,
        "runas",
        str(executable),
        params,
        str(cwd) if cwd else None,
        show_cmd,
    )
    return int(ret or 0)


