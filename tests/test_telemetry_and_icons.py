"""Tests for telemetry collection, GPU queries, process sampling, activity tracking/DB, icon extraction, and emoji caching."""

from collections import namedtuple
import ctypes
from ctypes import wintypes
import json
import logging
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import psutil
import requests

import boot_agent
from core import icon_extractor
from core.activity_db import ActivityDB, get_activity_db
from core.activity_logger import ActivityTracker
from core.app_emoji_manager import AppEmojiManager
from core.icon_extractor import extract_app_icon_png
from core.premium_checker import PremiumChecker
from core.process_utils import get_top_processes
from desktop.sensors import SensorHelper
from desktop.telemetry import TelemetryMixin as DesktopTelemetry
from system.hardware_monitor import HardwareMonitorMixin as SystemHardwareMonitor
from vds_bot.monitoring import MonitoringManager

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_icon_extractor_gdi_and_shell_win64_signatures():
    assert icon_extractor.user32.GetDC.argtypes == [wintypes.HWND]
    assert icon_extractor.user32.GetDC.restype == wintypes.HDC
    assert icon_extractor.user32.ReleaseDC.argtypes == [wintypes.HWND, wintypes.HDC]
    assert icon_extractor.user32.ReleaseDC.restype == ctypes.c_int
    assert icon_extractor.gdi32.CreateCompatibleDC.argtypes == [wintypes.HDC]
    assert icon_extractor.gdi32.CreateCompatibleDC.restype == wintypes.HDC
    assert icon_extractor.gdi32.DeleteDC.argtypes == [wintypes.HDC]
    assert icon_extractor.gdi32.DeleteDC.restype == wintypes.BOOL
    assert icon_extractor.shell32.SHGetFileInfoW.argtypes == [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(icon_extractor.SHFILEINFOW),
        wintypes.UINT,
        wintypes.UINT,
    ]
    assert icon_extractor.shell32.SHGetFileInfoW.restype == ctypes.c_void_p


def test_icon_extractor_defines_bitmap_struct_and_getobject_signature():
    assert hasattr(icon_extractor, "BITMAP")
    assert hasattr(icon_extractor.gdi32, "GetObjectW")
    assert icon_extractor.gdi32.GetObjectW.argtypes is not None


def test_icon_extractor_handles_invalid_private_extract_icons_code(tmp_path):
    dummy_exe = tmp_path / "dummy.exe"
    dummy_exe.write_bytes(b"dummy")

    with patch("core.icon_extractor.user32.PrivateExtractIconsW", return_value=0xFFFFFFFF):
        res = extract_app_icon_png(str(dummy_exe))
        assert res is None or isinstance(res, bytes)


def test_app_emoji_manager_and_icon_extractor_handle_none_path():
    assert extract_app_icon_png(None) is None

    mgr = AppEmojiManager.__new__(AppEmojiManager)
    mgr.upload_lock = MagicMock()
    mgr.cache = {}
    assert mgr.get_or_create_emoji_id(None, "dummy.exe") is None


def test_app_emoji_manager_redacts_bot_token_in_error_logs():
    mgr = AppEmojiManager(token="SECRET_BOT_TOKEN_12345", user_id=100)
    with patch(
        "requests.post",
        side_effect=requests.exceptions.ConnectionError(
            "Failed connecting to https://api.telegram.org/botSECRET_BOT_TOKEN_12345/addStickerToSet"
        ),
    ):
        with patch.object(logging.Logger, "warning") as mock_warn:
            emoji_id = mgr.get_or_create_emoji_id("C:\\test.exe", "test.exe")
            assert emoji_id is None
            for call in mock_warn.call_args_list:
                assert "SECRET_BOT_TOKEN_12345" not in str(call)


def test_core_package_init_has_no_utf8_bom():
    init_path = ROOT_DIR / "core" / "__init__.py"
    assert not init_path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_desktop_agent_initializes_state_lock():
    from agent import PCControlAgent

    dummy_config = {
        "mqtt": {"host": "127.0.0.1", "port": 1883, "username": "test_user", "password": "test_password"},
        "activity_logging": {"enabled": False},
    }

    with patch("agent.load_agent_config", return_value=dummy_config), \
         patch.object(PCControlAgent, "setup_mqtt_client", return_value=None), \
         patch.object(PCControlAgent, "connect_with_retry", return_value=True), \
         patch.object(PCControlAgent, "load_state", return_value=None):
        agent = PCControlAgent()
        assert hasattr(agent, "state_lock")
        assert isinstance(agent.state_lock, type(threading.Lock()))


def test_desktop_agent_foreground_callback_deduplicates_when_watcher_alive():
    from agent import PCControlAgent

    agent = PCControlAgent.__new__(PCControlAgent)
    agent._handle_active_app_change = MagicMock()

    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = True
    agent.fast_watcher_thread = mock_thread

    agent._on_foreground_change("C:\\app.exe", "app.exe", "Title")
    agent._handle_active_app_change.assert_not_called()

    mock_thread.is_alive.return_value = False
    agent._on_foreground_change("C:\\app.exe", "app.exe", "Title")
    agent._handle_active_app_change.assert_called_once_with("C:\\app.exe", "app.exe", "Title")


def test_desktop_agent_acknowledge_publishes_without_blocking():
    import agent

    app = agent.PCControlAgent.__new__(agent.PCControlAgent)
    app.mqtt = MagicMock()
    mock_publish_info = MagicMock()
    app.mqtt.publish.return_value = mock_publish_info

    app.acknowledge("test-cmd", "executed", {"data": 123})
    assert app.mqtt.publish.called
    assert not mock_publish_info.wait_for_publish.called


def test_desktop_agent_network_rates_returns_zero_when_counters_none():
    import agent

    mock_agent = MagicMock()
    mock_agent.previous_net = MagicMock(bytes_recv=1000, bytes_sent=1000)
    mock_agent.previous_net_time = 100.0

    with patch("psutil.net_io_counters", return_value=None):
        rates = agent.PCControlAgent.network_rates(mock_agent)
        assert isinstance(rates, dict)
        assert rates["download_mbps"] == 0.0
        assert rates["upload_mbps"] == 0.0


def test_boot_agent_network_rates_returns_zero_when_counters_none():
    agent = boot_agent.SystemAgent.__new__(boot_agent.SystemAgent)
    agent.previous_net = None
    agent.previous_net_time = 0.0

    with patch("boot_agent.psutil.net_io_counters", return_value=None):
        rates = agent.network_rates()
        assert rates["download_mbps"] == 0.0
        assert rates["upload_mbps"] == 0.0


def test_boot_agent_collect_telemetry_ignores_null_sensor_values():
    agent = boot_agent.SystemAgent.__new__(boot_agent.SystemAgent)
    agent.read_sensors = MagicMock(return_value=[
        {"name": "Fan 1", "type": "Fan", "value": None, "hardware_type": "Motherboard"},
        {"name": "CPU Package", "type": "Temperature", "value": None, "hardware_type": "Cpu"},
        {"name": "CPU Core #1", "type": "Temperature", "value": "45.5", "hardware_type": "Cpu"},
    ])
    agent.get_local_ip = MagicMock(return_value="127.0.0.1")
    agent.get_cpu_name = MagicMock(return_value="Intel CPU")
    agent.get_volume = MagicMock(return_value=50)
    agent.network_rates = MagicMock(return_value={"rx": 0, "tx": 0})
    agent.query_gpu = MagicMock(return_value={})

    data = agent.collect_telemetry()
    assert isinstance(data, dict)
    assert data.get("cpu", {}).get("temp_c") == 45.5


def test_desktop_telemetry_collect_ignores_null_sensor_values():
    mixin = DesktopTelemetry()
    mixin.sensor_helper = MagicMock()
    mixin.sensor_helper.snapshot.return_value = [
        {"name": "Fan 1", "type": "Fan", "value": None, "hardware_type": "Motherboard"},
        {"name": "CPU Package", "type": "Temperature", "value": None, "hardware_type": "Cpu"},
        {"name": "GPU Hotspot", "type": "Temperature", "value": "52.0", "hardware_type": "Gpu"},
    ]
    mixin.get_idle_seconds = MagicMock(return_value=10)
    mixin.get_active_application_info = MagicMock(return_value=("Code.exe", None, "test.py"))
    mixin.antigravity_manager = MagicMock()
    mixin.antigravity_manager.get_active_account.return_value = None
    mixin.previous_net = psutil.net_io_counters()
    mixin.previous_net_time = time.time()
    mixin.stealth_mgr = MagicMock()
    mixin.stealth_mgr.is_active.return_value = False
    mixin.get_local_ip = MagicMock(return_value="127.0.0.1")
    mixin.get_cpu_name = MagicMock(return_value="Intel CPU")
    mixin.get_volume = MagicMock(return_value=50)
    mixin.get_session_seconds = MagicMock(return_value=100)
    mixin.query_gpu = MagicMock(return_value={})
    mixin.network_rates = MagicMock(return_value={"rx": 0, "tx": 0})

    data = mixin.collect_telemetry()
    assert isinstance(data, dict)
    assert data.get("cpu", {}).get("temp_c") is None
    assert len(data.get("sensors", [])) == 1


def test_hardware_monitor_and_telemetry_query_gpu_handle_empty_stdout():
    mock_res = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=0, stdout="\n  \n", stderr="")
    with patch("subprocess.run", return_value=mock_res):
        assert SystemHardwareMonitor.query_gpu() is None
        assert DesktopTelemetry.query_gpu() is None


def test_desktop_telemetry_query_gpu_handles_missing_nvidia_smi():
    with patch("subprocess.run", side_effect=FileNotFoundError("nvidia-smi not found")):
        assert DesktopTelemetry.query_gpu() is None


def test_sensor_helper_omits_obsolete_admin_snapshot(tmp_path):
    helper = SensorHelper(tmp_path / "dummy.exe")
    assert not hasattr(helper, "admin_snapshot")


def test_top_processes_dispatched_through_desktop_dispatcher():
    from desktop.action_dispatcher import ActionDispatcherMixin

    dispatcher = ActionDispatcherMixin()
    dispatcher.acknowledge = MagicMock()
    dispatcher.top_processes = MagicMock(return_value=[{"pid": 1, "name": "app.exe"}])
    dispatcher.execute_action("cmd-top-1", "top_processes", {})
    dispatcher.top_processes.assert_called_once_with()
    dispatcher.acknowledge.assert_called_once_with(
        "cmd-top-1",
        "executed",
        {"processes": [{"pid": 1, "name": "app.exe"}]},
    )


def test_shared_get_top_processes_sorts_and_limits_processes():
    proc_idle = MagicMock()
    proc_idle.pid = 0
    proc_idle.info = {"pid": 0, "name": "System Idle Process"}

    proc_low = MagicMock()
    proc_low.pid = 101
    proc_low.info = {"pid": 101, "name": "low.exe"}
    proc_low.memory_info.return_value = MagicMock(rss=50_000_000)
    proc_low.cpu_percent.side_effect = [0.0, 4.0]

    proc_high = MagicMock()
    proc_high.pid = 202
    proc_high.info = {"pid": 202, "name": "high.exe"}
    proc_high.memory_info.return_value = MagicMock(rss=200_000_000)
    proc_high.cpu_percent.side_effect = [0.0, 40.0]

    with patch("core.process_utils.psutil.process_iter", return_value=[proc_idle, proc_low, proc_high]), \
         patch("core.process_utils.psutil.cpu_count", return_value=4), \
         patch("core.process_utils.time.sleep"):
        top = get_top_processes(limit=2, sample_delay=0.0)

    assert [p["name"] for p in top] == ["high.exe", "low.exe"]
    assert top[0]["cpu_percent"] == 10.0
    assert top[0]["memory_mb"] == 200.0


def test_activity_logger_skips_interfaces_with_none_address():
    class MockAddr:
        family = __import__("socket").AF_INET
        address = None

    class MockStat:
        isup = True

    with patch("core.activity_logger.psutil.net_if_stats", return_value={"eth0": MockStat()}), \
         patch("core.activity_logger.psutil.net_if_addrs", return_value={"eth0": [MockAddr()]}):
        tracker = ActivityTracker.__new__(ActivityTracker)
        assert tracker._network_state() == []


def test_activity_logger_ignores_out_of_range_create_time():
    tracker = ActivityTracker(config={}, log_dir=MagicMock(), stop_event=threading.Event())
    ProcMock = namedtuple("ProcMock", ["pid", "ppid", "name", "exe", "create_time"])

    p_neg = ProcMock(pid=100, ppid=1, name="test.exe", exe="C:\\test.exe", create_time=-9999999999)
    fields = tracker._process_fields(p_neg)
    assert "process_started" not in fields
    assert fields["pid"] == 100

    p_overflow = ProcMock(pid=101, ppid=1, name="test2.exe", exe="C:\\test2.exe", create_time=1e15)
    fields2 = tracker._process_fields(p_overflow)
    assert "process_started" not in fields2
    assert fields2["pid"] == 101


def test_activity_logger_foreground_sets_user32_argtypes():
    from core import activity_logger

    assert activity_logger._USER32.GetWindowThreadProcessId.argtypes is not None
    assert activity_logger._USER32.GetWindowTextLengthW.argtypes is not None
    assert activity_logger._USER32.GetWindowTextW.argtypes is not None

    tracker = ActivityTracker(config={}, log_dir=MagicMock(), stop_event=threading.Event())
    fake_user32 = MagicMock()
    fake_user32.GetForegroundWindow.return_value = 0
    with patch.object(activity_logger, "_USER32", fake_user32):
        tracker._foreground({})
        fake_user32.GetForegroundWindow.assert_called_once()


def test_handle_active_app_change_writes_sqlite_outside_state_lock():
    from agent import PCControlAgent

    with patch.object(PCControlAgent, "__init__", lambda self: None):
        agent = PCControlAgent()
        agent.state_lock = threading.Lock()
        agent.emoji_manager = MagicMock()
        agent.emoji_manager.upload_lock = threading.Lock()
        agent.emoji_manager.get_cached_emoji_id.return_value = "999"
        agent.current_session_app = "old.exe"
        agent.current_session_title = "Old"
        agent.current_session_exe = r"C:\old.exe"
        agent.current_session_start = 100.0
        agent.current_session_emoji_id = "111"
        agent.publish_active_app_update = MagicMock()

        lock_was_free = []
        fake_db = MagicMock()

        def _check_lock_free(**kwargs):
            acquired = agent.state_lock.acquire(blocking=False)
            lock_was_free.append(acquired)
            if acquired:
                agent.state_lock.release()

        fake_db.record_session.side_effect = _check_lock_free
        with patch("core.activity_db.get_activity_db", return_value=fake_db):
            agent._handle_active_app_change(r"C:\new.exe", "new.exe", "New Window")

        assert lock_was_free == [True]
        fake_db.record_session.assert_called_once()
        assert agent.current_session_app == "new.exe"


def test_app_emoji_manager_releases_upload_lock_and_discards_cleared_epoch(tmp_path):
    from core.app_emoji_manager import AppEmojiManager

    dummy_exe = tmp_path / "dummy.exe"
    dummy_exe.write_bytes(b"MZ")
    cache_file = tmp_path / "icon_cache.json"

    with patch("core.app_emoji_manager.CACHE_FILE", cache_file):
        mgr = AppEmojiManager(token="tok", user_id=123, pack_name="pack_by_bot")
        mgr.cache = {}

    lock_free_in_extract = []

    def _extract(_path):
        free = mgr.upload_lock.acquire(blocking=False)
        lock_free_in_extract.append(free)
        if free:
            mgr.upload_lock.release()
        return b"png-bytes"

    def _post(*args, **kwargs):
        free = mgr.upload_lock.acquire(blocking=False)
        lock_free_in_extract.append(free)
        if free:
            mgr.upload_lock.release()
        mgr.clear_all_emojis()
        res = MagicMock()
        res.json.return_value = {"ok": True}
        return res

    fake_get = MagicMock()
    fake_get.json.return_value = {
        "ok": True,
        "result": {"stickers": [{"custom_emoji_id": "999888777"}]},
    }

    with patch("core.app_emoji_manager.CACHE_FILE", cache_file), \
         patch("core.app_emoji_manager.extract_app_icon_png", side_effect=_extract), \
         patch("requests.post", side_effect=_post), \
         patch("requests.get", return_value=fake_get):
        emoji_id = mgr.get_or_create_emoji_id(str(dummy_exe), "dummy.exe")

    assert lock_free_in_extract == [True, True]
    assert emoji_id == "999888777"
    assert "dummy.exe" not in mgr.cache


def test_fast_foreground_watcher_does_not_label_inaccessible_window_as_explorer():
    import ctypes
    import psutil
    from agent import PCControlAgent

    with patch.object(PCControlAgent, "__init__", lambda self: None):
        agent = PCControlAgent()
        agent.stop_event = threading.Event()
        agent.state_lock = threading.Lock()
        agent.mqtt_online = False
        agent.in_pc_control_chat = False
        agent.emoji_manager = MagicMock()
        agent.get_foreground_window_safe = MagicMock(return_value=9876)
        agent._handle_active_app_change = MagicMock()
        agent.publish_active_app_update = MagicMock()

        orig_wait = agent.stop_event.wait

        def _stop_after_one(timeout=0.0):
            agent.stop_event.set()
            return orig_wait(0)

        agent.stop_event.wait = _stop_after_one

        fake_user32 = MagicMock()

        def _set_pid(_hwnd, pid_ref):
            if hasattr(pid_ref, "_obj"):
                pid_ref._obj.value = 4321
            return 1

        def _set_cls(_hwnd, cls_buf, _size):
            cls_buf.value = "ConsoleWindowClass"
            return len("ConsoleWindowClass")

        fake_user32.GetWindowThreadProcessId.side_effect = _set_pid
        fake_user32.GetClassNameW.side_effect = _set_cls

        fake_kernel32 = MagicMock()
        fake_kernel32.OpenProcess.return_value = 0

        with patch("desktop.os_events._WIN32_SIGS_INITIALIZED", False), \
             patch("agent.ctypes.windll") as mock_windll, \
             patch("agent.psutil.Process", side_effect=psutil.AccessDenied(4321)):
            mock_windll.user32 = fake_user32
            mock_windll.kernel32 = fake_kernel32
            agent._fast_foreground_watcher_loop()

        agent._handle_active_app_change.assert_not_called()


def test_activity_tracker_stop_unblocks_wait_loop_promptly(tmp_path):
    stop_ev = threading.Event()
    tracker = ActivityTracker(
        config={"poll_seconds": 5.0, "record_existing_processes": False},
        log_dir=tmp_path,
        stop_event=stop_ev,
    )
    assert not tracker.stop_event.is_set()

    with patch.object(tracker, "_processes", return_value={}), \
         patch.object(tracker, "_foreground", return_value={}):
        tracker.start()
        assert tracker.thread is not None
        time.sleep(0.05)

        t0 = time.monotonic()
        tracker.stop()
        elapsed = time.monotonic() - t0

        assert tracker.stop_event.is_set()
        assert not tracker.thread.is_alive()
        assert elapsed < 0.5


def test_activity_db_migrates_legacy_schema_adding_emoji_id(tmp_path):
    test_db = tmp_path / "legacy_activity.db"
    conn = sqlite3.connect(str(test_db))
    conn.execute(
        """
        CREATE TABLE app_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_name TEXT NOT NULL,
            window_title TEXT,
            exe_path TEXT,
            started_at REAL NOT NULL,
            ended_at REAL NOT NULL,
            duration_sec REAL NOT NULL,
            started_iso TEXT NOT NULL,
            ended_iso TEXT NOT NULL,
            date_str TEXT NOT NULL,
            time_str TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    now = time.time()
    db = ActivityDB(test_db)
    rec_id = db.record_session(
        app_name="Code.exe",
        window_title="test",
        exe_path="C:/Code.exe",
        started_at=now - 10.0,
        ended_at=now,
        emoji_id="12345",
    )
    assert rec_id is not None
    db.close()


def test_get_activity_db_returns_thread_safe_singleton(tmp_path):
    import core.activity_db as adb

    orig_path = adb.DB_PATH
    orig_global = adb._GLOBAL_DB
    adb.DB_PATH = tmp_path / "singleton_activity.db"
    adb._GLOBAL_DB = None
    instances = []
    try:
        def _worker():
            instances.append(get_activity_db())

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(instances) == 10
        assert all(inst is instances[0] for inst in instances)
    finally:
        if adb._GLOBAL_DB is not None:
            adb._GLOBAL_DB.close()
        adb._GLOBAL_DB = orig_global
        adb.DB_PATH = orig_path


def test_premium_checker_saves_cache_atomically(tmp_path):
    cache_file = tmp_path / "premium_cache.json"
    checker = PremiumChecker(cache_path=cache_file)
    checker._memory_cache = {12345: {"is_premium": True, "cached_at": 1000.0}}
    checker._save_cache()

    assert cache_file.exists()
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert data["12345"]["is_premium"] is True


def test_premium_checker_allow_stale_returns_expired_entry():
    checker = PremiumChecker()
    with checker._lock:
        checker._memory_cache[999] = {
            "is_premium": True,
            "expires_at": time.time() - 100,
            "source": "test",
        }

    assert checker.get_cached_status(999, allow_stale=False) is False
    assert checker.get_cached_status(999, allow_stale=True) is True


def test_monitoring_dashboard_renders_paused_media_without_name_error():
    mgr = MonitoringManager.__new__(MonitoringManager)
    mgr.mqtt = MagicMock()
    mgr.mqtt.health.evaluate_pc_state.return_value = MagicMock(state="READY", label="В сети")
    mgr.latest_telemetry = {"cpu": {"load": 10}, "timestamp": 12345}
    mgr.latest_received_at = 12345.0
    mgr.is_shutting_down = lambda: False
    mgr.get_os_type = lambda t=None: "windows"
    mgr.header_os_tag = lambda t=None: "🪟"

    media_state = {
        "active": True,
        "title": "Test Track",
        "artist": "Test Artist",
        "is_playing": False,
        "is_liked": True,
    }
    text = mgr.dashboard_text(media_state)
    assert "Test Track" in text
    assert "⏸️" in text


def test_telemetry_concurrent_publish_and_active_app_update_without_deadlock():
    from agent import PCControlAgent

    ag = PCControlAgent.__new__(PCControlAgent)
    ag.state_lock = threading.Lock()
    ag.mqtt_online = True
    ag.mqtt = MagicMock()
    ag.last_telemetry = {}
    ag.in_pc_control_chat = False
    ag.previous_net = psutil.net_io_counters()
    ag.previous_net_time = time.time() - 1.0
    ag.sensor_helper = MagicMock()
    ag.sensor_helper.snapshot.return_value = []
    ag.stealth_mgr = MagicMock()
    ag.stealth_mgr.is_active.return_value = False
    ag.get_idle_seconds = MagicMock(return_value=5)
    ag.get_active_application_info = MagicMock(return_value=("Code.exe", "123", "main.py"))
    ag.get_local_ip = MagicMock(return_value="127.0.0.1")
    ag.get_cpu_name = MagicMock(return_value="Intel CPU")
    ag.get_volume = MagicMock(return_value={"percent": 50, "muted": False})
    ag.get_session_seconds = MagicMock(return_value=120)
    ag.query_gpu = MagicMock(return_value=None)

    barrier = threading.Barrier(4)
    errors = []

    def _run_publish():
        try:
            barrier.wait(timeout=2.0)
            ag.publish_telemetry()
        except Exception as exc:
            errors.append(exc)

    def _run_active_app():
        try:
            barrier.wait(timeout=2.0)
            ag.publish_active_app_update("Telegram.exe", "999", "PC Control")
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_run_publish),
        threading.Thread(target=_run_active_app),
        threading.Thread(target=_run_publish),
        threading.Thread(target=_run_active_app),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=3.0)
        assert not th.is_alive()

    assert not errors
    assert ag.mqtt.publish.call_count == 4
    for call in ag.mqtt.publish.call_args_list:
        payload = json.loads(call.kwargs["payload"])
        assert "timestamp" in payload
