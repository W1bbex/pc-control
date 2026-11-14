"""Shared hardware telemetry helpers (CPU, IP, NVIDIA GPU, network rates) for Windows agents."""

import platform
import socket
import subprocess
import time

import psutil


def get_cpu_name() -> str:
    """Read human-readable CPU brand string from Windows Registry with platform fallback."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    except Exception:
        return platform.processor() or "Unknown CPU"


def get_local_ip() -> str:
    """Determine primary outbound local IPv4 address without transmitting packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 53))
            return sock.getsockname()[0]
    except OSError:
        return "unknown"


def _optional_gpu_number(value: str) -> float | None:
    if value.upper() in {"N/A", "[N/A]", "NOT SUPPORTED"}:
        return None
    return float(value)


def query_nvidia_gpu(
    logger=None,
    *,
    warn_on_error: bool = False,
    missing_flag_holder=None,
) -> dict | None:
    """Query primary NVIDIA GPU telemetry via nvidia-smi CSV output."""
    command = [
        "nvidia-smi",
        "--query-gpu=name,temperature.gpu,utilization.gpu,"
        "memory.used,memory.total,fan.speed,power.draw,"
        "clocks.gr,clocks.mem",
        "--format=csv,noheader,nounits",
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=creationflags,
            check=True,
        )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            return None
        values = [item.strip() for item in lines[0].split(",")]
        if len(values) < 9:
            raise ValueError(f"Unexpected nvidia-smi column count: {len(values)}")

        return {
            "name": values[0],
            "temp_c": _optional_gpu_number(values[1]),
            "load_percent": _optional_gpu_number(values[2]),
            "vram_used_mb": _optional_gpu_number(values[3]),
            "vram_total_mb": _optional_gpu_number(values[4]),
            "fan_percent": _optional_gpu_number(values[5]),
            "power_w": _optional_gpu_number(values[6]),
            "clock_mhz": _optional_gpu_number(values[7]),
            "memory_clock_mhz": _optional_gpu_number(values[8]),
        }
    except FileNotFoundError as err:
        if missing_flag_holder is not None:
            if not getattr(missing_flag_holder, "_nvidia_smi_missing_logged", False):
                missing_flag_holder._nvidia_smi_missing_logged = True
                if logger is not None:
                    logger.debug("nvidia-smi not found on PATH; skipping NVIDIA GPU telemetry")
        elif logger is not None:
            logger.debug("NVIDIA GPU query not available: %s", err)
        return None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError, IndexError) as err:
        if logger is not None:
            if warn_on_error:
                logger.warning("Could not query NVIDIA GPU: %s", err)
            else:
                logger.debug("NVIDIA GPU query not available: %s", err)
        return None


def compute_network_rates(state_holder, lock) -> dict[str, float]:
    """Compute instantaneous network throughput (Mbps) and total counters under lock."""
    current = psutil.net_io_counters()
    with lock:
        now = time.time()
        prev = getattr(state_holder, "previous_net", None)
        prev_time = getattr(state_holder, "previous_net_time", 0.0)
        if current is None or prev is None:
            if current is not None:
                state_holder.previous_net = current
                state_holder.previous_net_time = now
            return {
                "download_mbps": 0.0,
                "upload_mbps": 0.0,
                "received_gb": 0.0,
                "sent_gb": 0.0,
            }
        elapsed = max(0.1, now - prev_time)
        received = max(0, current.bytes_recv - prev.bytes_recv)
        sent = max(0, current.bytes_sent - prev.bytes_sent)
        state_holder.previous_net = current
        state_holder.previous_net_time = now

    return {
        "download_mbps": round(received * 8 / elapsed / 1_000_000, 2),
        "upload_mbps": round(sent * 8 / elapsed / 1_000_000, 2),
        "received_gb": round(current.bytes_recv / 1_000_000_000, 2),
        "sent_gb": round(current.bytes_sent / 1_000_000_000, 2),
    }
