"""Tests for shared runtime config, hardware telemetry, and process helpers in core."""

import os
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.hardware_utils import compute_network_rates, query_nvidia_gpu
from core.process_utils import kill_process_by_name, kill_process_by_pid, restart_windows_service
from core.runtime_config import PROTECTED_PROCESSES, load_env_file, resolve_state_path
from desktop.constants import PROTECTED_PROCESSES as DESKTOP_PROTECTED
from desktop.telemetry import TelemetryMixin as DesktopTelemetry
from system.constants import PROTECTED_PROCESSES as SYSTEM_PROTECTED
from system.hardware_monitor import HardwareMonitorMixin as SystemHardwareMonitor


def test_shared_protected_processes_and_load_env_file(tmp_path, monkeypatch):
    assert DESKTOP_PROTECTED is PROTECTED_PROCESSES
    assert SYSTEM_PROTECTED is PROTECTED_PROCESSES

    env_file = tmp_path / ".env"
    env_file.write_text('TEST_SHARED_NEW="abc"\nTEST_SHARED_KEEP=override\n', encoding="utf-8")
    monkeypatch.setenv("TEST_SHARED_KEEP", "existing")
    monkeypatch.delenv("TEST_SHARED_NEW", raising=False)

    load_env_file(env_file)
    assert os.environ["TEST_SHARED_NEW"] == "abc"
    assert os.environ["TEST_SHARED_KEEP"] == "existing"


def test_resolve_state_path_behavior(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr("core.runtime_config.BASE_DIR", tmp_path)
    monkeypatch.setattr("core.runtime_config.DATA_DIR", data_dir)

    root_legacy = tmp_path / "state.json"
    root_legacy.write_text("{}", encoding="utf-8")
    assert resolve_state_path("state.json", require_data_dir=False) == root_legacy
    assert resolve_state_path("state.json", require_data_dir=True) == root_legacy

    data_dir.mkdir()
    assert resolve_state_path("state.json", require_data_dir=True) == data_dir / "state.json"
    assert resolve_state_path("state.json", require_data_dir=False) == root_legacy

    (data_dir / "state.json").write_text("{}", encoding="utf-8")
    assert resolve_state_path("state.json", require_data_dir=False) == data_dir / "state.json"


def test_query_gpu_handles_malformed_csv_and_valid_values():
    bad_csv = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=0, stdout="RTX 4070, 50\n", stderr="")
    with patch("subprocess.run", return_value=bad_csv):
        assert query_nvidia_gpu() is None
        assert DesktopTelemetry.query_gpu() is None
        assert SystemHardwareMonitor.query_gpu() is None

    good_csv = subprocess.CompletedProcess(
        args=["nvidia-smi"],
        returncode=0,
        stdout="RTX 4070, 52, 34, 2048, 12288, N/A, 115.5, 2100, 7000\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=good_csv):
        gpu = DesktopTelemetry.query_gpu()
        assert gpu is not None
        assert gpu["name"] == "RTX 4070"
        assert gpu["temp_c"] == 52.0
        assert gpu["fan_percent"] is None
        assert gpu["power_w"] == 115.5


def test_compute_network_rates_calculates_delta():
    holder = SimpleNamespace(
        previous_net=SimpleNamespace(bytes_recv=1_000_000, bytes_sent=500_000),
        previous_net_time=100.0,
    )
    lock = threading.Lock()
    current = SimpleNamespace(bytes_recv=2_250_000, bytes_sent=750_000)

    with patch("psutil.net_io_counters", return_value=current), patch("time.time", return_value=101.0):
        rates = compute_network_rates(holder, lock)

    assert rates["download_mbps"] == 10.0
    assert rates["upload_mbps"] == 2.0
    assert rates["received_gb"] == 0.0
    assert rates["sent_gb"] == 0.0


def test_process_utils_guards_and_service_restart():
    with pytest.raises(ValueError):
        kill_process_by_name("", PROTECTED_PROCESSES)

    with pytest.raises(PermissionError):
        kill_process_by_name("lsass.exe", PROTECTED_PROCESSES)

    self_proc = MagicMock()
    self_proc.pid = os.getpid()
    self_proc.name.return_value = "custom.exe"
    with patch("psutil.Process", return_value=self_proc), pytest.raises(PermissionError):
        kill_process_by_pid(os.getpid(), PROTECTED_PROCESSES)

    with pytest.raises(ValueError):
        restart_windows_service("unknown", {"other": "Svc"})
