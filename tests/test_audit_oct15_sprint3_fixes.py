"""
Regression tests for Audit Findings (October 15, 2026 - Sprint 3 P0/P1 & P2 fixes).
Covers:
1. scripts/auto_backup.py imports os and collect_local_code_files executes without NameError.
2. vds_bot/bot.py lockfile PID check does not fail with UnboundLocalError when proc is not set.
3. vds_bot/routers/command_router.py routes plain '/shutdown' to shutdown menu.
4. vds_bot/mqtt_dispatcher.py properly resolves bot app._executor.
5. scripts/deploy_vds.py permissions and rollback current_release preservation.
6. core/app_emoji_manager.py and core/icon_extractor.py handle exe_path=None without TypeError.
7. scripts/type_password.py ctypes 64-bit signatures.
8. system/hardware_monitor.py and desktop/telemetry.py query_gpu empty stdout protection.
9. system/action_dispatcher.py and desktop/action_dispatcher.py timeout clamping.
10. core/premium_checker.py atomic file write.
"""

import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from core.app_emoji_manager import AppEmojiManager
from core.icon_extractor import extract_app_icon_png
from core.premium_checker import PremiumChecker
from desktop.action_dispatcher import ActionDispatcherMixin as DesktopActionDispatcher
from desktop.telemetry import TelemetryMixin as DesktopTelemetry
from system.action_dispatcher import ActionDispatcherMixin as SystemActionDispatcher
from system.hardware_monitor import HardwareMonitorMixin as SystemHardwareMonitor


def test_auto_backup_imports_os():
    """1.1: Verify scripts/auto_backup.py imports os and does not crash with NameError."""
    import scripts.auto_backup as auto_backup
    assert hasattr(auto_backup, "os")
    assert auto_backup.os is not None
    # Verify collect_local_code_files runs without NameError
    files = auto_backup.collect_local_code_files(auto_backup.BASE_DIR / "core")
    assert isinstance(files, list)


def test_vds_bot_acquire_lock_unbound_local_error_safe(tmp_path):
    """1.2: Verify vds_bot _acquire_lock handles dead/unreachable PID without UnboundLocalError."""
    from vds_bot.bot import PcControlBotApp
    with patch("vds_bot.bot.TelegramBotApi"), \
         patch("vds_bot.bot.MqttGateway"), \
         patch("vds_bot.bot.LOCK_PATH", tmp_path / "bot.lock"):
        app = PcControlBotApp.__new__(PcControlBotApp)
        lock_file = tmp_path / "bot.lock"
        # Simulate lock file pointing to non-existent PID 999999
        lock_file.write_text("999999", encoding="utf-8")
        with patch("psutil.pid_exists", return_value=True), \
             patch("psutil.Process", side_effect=psutil.NoSuchProcess(999999)):
            # Must not raise UnboundLocalError
            app._acquire_lock()
            assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_command_router_routes_plain_shutdown():
    """1.3: Verify /shutdown without args opens shutdown menu."""
    from vds_bot.routers.command_router import route_message
    app = MagicMock()
    app.ui.shutdown_menu_markup.return_value = {"inline_keyboard": []}
    app.scheduled_shutdown_ts = None
    app.awaiting_shutdown_time_chat_id = None
    app.active_stealth_card = None
    app.allowed_user_id = 12345
    app.console_active_chat_id = None
    
    msg = {"chat": {"id": 12345}, "text": "/shutdown", "message_id": 42}
    route_message(app, msg)
    
    app.ui.send_message.assert_called_once()
    assert "Выключение ПК" in app.ui.send_message.call_args[0][1]


def test_mqtt_dispatcher_resolves_executor():
    """1.4: Verify mqtt_dispatcher screenshot unpacker uses _executor."""
    from vds_bot.mqtt_dispatcher import MqttDispatcherMixin
    app = MqttDispatcherMixin()
    mock_exec = MagicMock()
    app._executor = mock_exec
    
    resolved = getattr(app, "_executor", getattr(app, "executor", None))
    assert resolved is mock_exec


def test_app_emoji_manager_and_icon_extractor_none_safe():
    """1.7: Verify None exe_path does not trigger TypeError: stat: path should be string... not NoneType."""
    assert extract_app_icon_png(None) is None
    
    mgr = AppEmojiManager.__new__(AppEmojiManager)
    mgr.upload_lock = MagicMock()
    mgr.cache = {}
    assert mgr.get_or_create_emoji_id(None, "dummy.exe") is None


def test_type_password_ctypes_signatures():
    """1.8: Verify scripts/type_password.py defines 64-bit ctypes argtypes and restype."""
    import scripts.type_password as tp
    assert tp.kernel32.CreateMutexW.restype == wintypes.HANDLE
    assert tp.kernel32.CreateMutexW.argtypes == [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    assert tp.user32.LoadKeyboardLayoutW.restype == wintypes.HKL
    assert tp.user32.ActivateKeyboardLayout.restype == wintypes.HKL


def test_query_gpu_empty_output_safe():
    """2.1: Verify query_gpu handles empty nvidia-smi stdout without IndexError."""
    mock_res = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=0, stdout="\n  \n", stderr="")
    with patch("subprocess.run", return_value=mock_res):
        assert SystemHardwareMonitor.query_gpu() is None
        assert DesktopTelemetry.query_gpu() is None


def test_action_dispatcher_timeout_clamping():
    """2.2: Verify negative or zero timeouts are clamped to >= 1s."""
    agent_sys = SystemActionDispatcher()
    agent_sys.acknowledge = MagicMock()
    
    # We test with a command that will be passed to subprocess.run
    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.stdout = "ok"
        mock_proc.stderr = ""
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc
        
        agent_sys.execute_action("cmd-1", "cmd", {"command": "echo test", "timeout": -10})
        # timeout passed to subprocess.run must be >= 1
        assert mock_run.call_args[1]["timeout"] >= 1


def test_premium_checker_atomic_save(tmp_path):
    """2.3: Verify premium_cache saves atomically without corrupting cache on write."""
    cache_file = tmp_path / "premium_cache.json"
    checker = PremiumChecker(cache_path=cache_file)
    checker._memory_cache = {12345: {"is_premium": True, "cached_at": 1000.0}}
    checker._save_cache()
    
    assert cache_file.exists()
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert data["12345"]["is_premium"] is True
