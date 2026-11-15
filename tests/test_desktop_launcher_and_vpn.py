"""Tests for desktop application launcher, UAC elevation, workspace config steps, VPN rotation, and Antigravity integration."""

import ctypes
from ctypes import wintypes
import importlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import urllib.error
from unittest.mock import MagicMock, patch

from agent import PCControlAgent
from core.antigravity_crypto import ProtobufHelper
from core.antigravity_switcher import AntigravityAccountManager
from desktop.action_dispatcher import ActionDispatcherMixin as DesktopActionDispatcher
from desktop.launcher import LauncherMixin
from desktop.vpn_manager import VpnManagerMixin
from system.action_dispatcher import ActionDispatcherMixin as SystemActionDispatcher
from vds_bot.bot import WORKSPACE_STEPS, PcControlBotApp
from vds_bot.power_flow import PowerFlowManager
from vds_bot.routers.antigravity_router import (
    route_antigravity_callback,
    route_antigravity_command,
)
from vds_bot.telegram_ui import TelegramBotApi

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_kill_ide_waits_for_terminated_and_killed_processes():
    mock_proc1 = MagicMock()
    mock_proc1.info = {"name": "antigravity.exe"}
    mock_proc2 = MagicMock()
    mock_proc2.info = {"name": "antigravity.exe"}

    with patch("core.antigravity_process.psutil.process_iter", return_value=[mock_proc1, mock_proc2]), \
         patch("core.antigravity_process.psutil.wait_procs", side_effect=[
             ([mock_proc1], [mock_proc2]),
             ([mock_proc2], []),
         ]) as mock_wait:
        AntigravityAccountManager.kill_ide()
        assert mock_proc2.kill.called
        assert mock_wait.call_count == 2


def test_antigravity_client_secret_reads_env_override():
    import core.antigravity_switcher as switcher_mod

    had_prev = "ANTIGRAVITY_CLIENT_SECRET" in os.environ
    prev_val = os.environ.get("ANTIGRAVITY_CLIENT_SECRET")
    os.environ["ANTIGRAVITY_CLIENT_SECRET"] = "custom-secret-123"
    try:
        importlib.reload(switcher_mod)
        assert switcher_mod.DEFAULT_CLIENT_SECRET == "custom-secret-123"
    finally:
        if had_prev:
            os.environ["ANTIGRAVITY_CLIENT_SECRET"] = prev_val
        else:
            os.environ.pop("ANTIGRAVITY_CLIENT_SECRET", None)
        importlib.reload(switcher_mod)


def test_launcher_winerror_740_retries_with_runas_uac():
    class TestApp(LauncherMixin):
        def __init__(self):
            self.config = {"apps": {"DummyApp": r"C:\Tools\tool.exe"}}

    app = TestApp()
    err_740 = OSError()
    err_740.winerror = 740

    mock_running_proc = MagicMock()
    mock_running_proc.info = {"name": "tool.exe"}

    with patch("desktop.launcher.shutil.which", return_value=r"C:\Tools\tool.exe"), \
         patch("desktop.launcher.Path.exists", return_value=True), \
         patch("subprocess.Popen", side_effect=err_740), \
         patch("desktop.launcher.psutil.process_iter", side_effect=[[], [mock_running_proc]]), \
         patch("ctypes.windll.shell32.ShellExecuteW", return_value=42) as mock_shell:
        res = app.launch_app("DummyApp", r"C:\Tools\tool.exe")
        assert res.get("success") is True
        mock_shell.assert_called_once()
        assert mock_shell.call_args[0][1] == "runas"


def test_incy_vpn_winerror_740_retries_with_runas_uac():
    class TestVpn(VpnManagerMixin):
        def __init__(self):
            self.config = {"apps": {"INCY": r"C:\Program Files\INCY\incy.exe"}}

    vpn = TestVpn()
    err_740 = OSError()
    err_740.winerror = 740

    with patch.object(vpn, "check_incy_vpn_active", side_effect=[False, True]), \
         patch.object(vpn, "probe_google_https", return_value=True), \
         patch("desktop.vpn_manager.psutil.process_iter", return_value=[]), \
         patch("desktop.vpn_manager.Path.exists", return_value=True), \
         patch("subprocess.Popen", side_effect=err_740), \
         patch("ctypes.windll.shell32.ShellExecuteW", return_value=42) as mock_shell:
        res = vpn.connect_incy_vpn(timeout=2)
        assert res.get("verified") is True
        mock_shell.assert_called_once()
        assert mock_shell.call_args[0][1] == "runas"


def test_run_shell_command_preserves_cmd_quotes_and_hides_window():
    import subprocess
    from core.process_utils import run_shell_command

    fake_completed = MagicMock(returncode=0, stdout="ok", stderr="")
    with patch("core.process_utils.subprocess.run", return_value=fake_completed) as mock_run:
        code, stdout, stderr = run_shell_command('dir "C:\\Program Files"', use_ps=False, timeout_s=15)

    assert code == 0
    assert stdout == "ok"
    assert stderr == ""
    cmd_arg = mock_run.call_args[0][0]
    assert isinstance(cmd_arg, str)
    assert 'cmd.exe /s /c "chcp 65001 >nul & dir "C:\\Program Files""' == cmd_arg
    expected_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert mock_run.call_args.kwargs.get("creationflags") == expected_flag


def test_desktop_antigravity_restart_acknowledges_failed_on_launch_error():
    class DummyDispatcher(DesktopActionDispatcher):
        def __init__(self):
            self.antigravity_manager = MagicMock()
            self.antigravity_manager.is_ide_running.return_value = True
            self.antigravity_manager.launch_ide.side_effect = RuntimeError("missing executable")
            self.acknowledge = MagicMock()

    disp = DummyDispatcher()
    disp.execute_action("cmd-restart-1", "antigravity_restart", {})
    disp.antigravity_manager.kill_ide.assert_called_once()
    disp.acknowledge.assert_called_once()
    call_args = disp.acknowledge.call_args[0]
    assert call_args[0] == "cmd-restart-1"
    assert call_args[1] == "failed"
    assert call_args[2]["restarted"] is False
    assert "missing executable" in call_args[2]["error"]


def test_connect_incy_vpn_returns_verified_false_when_google_probe_fails():
    class TestVpn(VpnManagerMixin):
        def __init__(self):
            self.config = {}

    vpn = TestVpn()
    with patch.object(vpn, "check_incy_vpn_active", return_value=True), \
         patch.object(vpn, "probe_google_https", return_value=False), \
         patch.object(vpn, "switch_next_incy_server", return_value={"success": False, "error": "blocked"}):
        res = vpn.connect_incy_vpn(verify_google=True)

    assert res["verified"] is False
    assert res["reason"] == "google_https_probe_failed"


def test_switch_next_incy_server_rejects_corrupt_prefs_and_stops_before_write(tmp_path):
    class TestVpn(VpnManagerMixin):
        def __init__(self):
            self.config = {"apps": {"INCY": str(tmp_path / "missing_incy.exe")}}

    vpn = TestVpn()
    pref_file = tmp_path / "incy" / "preferences.json"
    pref_file.parent.mkdir(parents=True)
    corrupt_raw = "{broken json"
    pref_file.write_text(corrupt_raw, encoding="utf-8")

    servers = [{"id": "srv-1", "name": "FI-1", "latency": 20}, {"id": "srv-2", "name": "DE-2", "latency": 35}]
    with patch.object(vpn, "get_incy_servers", return_value=servers), \
         patch.object(vpn, "_stop_incy_processes") as mock_stop, \
         patch("os.path.expandvars", side_effect=lambda s: str(pref_file) if "preferences.json" in s else s):
        res = vpn.switch_next_incy_server()

    assert res["success"] is False
    assert pref_file.read_text(encoding="utf-8") == corrupt_raw
    mock_stop.assert_called_once()

    pref_file.write_text(json.dumps({"selectedServerId": "srv-1", "customFlag": 42}), encoding="utf-8")
    call_order = []

    def _record_stop():
        call_order.append("stop")

    original_atomic = VpnManagerMixin._write_incy_preferences_atomic

    def _record_write(path_arg, prefs_arg):
        call_order.append("write")
        original_atomic(path_arg, prefs_arg)

    with patch.object(vpn, "get_incy_servers", return_value=servers), \
         patch.object(vpn, "_stop_incy_processes", side_effect=_record_stop), \
         patch.object(VpnManagerMixin, "_write_incy_preferences_atomic", side_effect=_record_write), \
         patch.object(vpn, "check_incy_vpn_active", return_value=True), \
         patch("os.path.expandvars", side_effect=lambda s: str(pref_file) if "preferences.json" in s else s):
        res_ok = vpn.switch_next_incy_server()

    assert res_ok["success"] is True
    assert call_order == ["stop", "write"]
    updated = json.loads(pref_file.read_text(encoding="utf-8"))
    assert updated["selectedServerId"] == "srv-2"
    assert updated["autoConnect"] is True
    assert updated["customFlag"] == 42


def test_heal_vpn_and_antigravity_returns_failed_when_relaunch_impossible(tmp_path):
    class TestVpn(VpnManagerMixin):
        def __init__(self):
            self.config = {"apps": {"Antigravity": str(tmp_path / "missing_ag.exe")}}

    vpn = TestVpn()
    with patch("core.antigravity_switcher.AntigravityAccountManager.kill_ide"), \
         patch.object(vpn, "switch_next_incy_server", return_value={"success": True, "server": "DE-2", "latency": 30}), \
         patch.object(vpn, "probe_google_https", return_value=True), \
         patch("core.antigravity_switcher.AntigravityAccountManager.launch_ide", side_effect=RuntimeError("no binary")), \
         patch("time.sleep"):
        res = vpn.heal_vpn_and_antigravity(attempts=1)

    assert res["success"] is False
    assert res["google_https_ok"] is True
    assert res["restarted_ide"] is False
    assert "no binary" in res["error"]


def test_vpn_manager_declares_64bit_desktop_handles():
    import desktop.vpn_manager  # noqa: F401

    user32 = ctypes.windll.user32
    assert user32.GetThreadDesktop.restype == wintypes.HANDLE
    assert user32.OpenInputDesktop.restype == wintypes.HANDLE
    assert user32.SetThreadDesktop.argtypes == [wintypes.HANDLE]
    assert user32.SetThreadDesktop.restype == wintypes.BOOL


def test_launcher_cancelled_workspace_returns_success_false():
    mixin = LauncherMixin()
    cancel_event = threading.Event()
    cancel_event.set()

    mixin.config = {
        "apps": {},
        "scenarios": {
            "work": {
                "steps": [{"id": "s1", "executable": "cmd.exe"}],
            }
        },
    }
    mixin.workspace_lock = threading.Lock()
    mixin.workspace_cancellations = {}

    with patch("threading.Event", return_value=cancel_event):
        res = mixin.launch_app("work", workspace_id="ws1")

    assert res.get("cancelled") is True
    assert res.get("success") is False


def test_action_dispatcher_acknowledges_cancelled_workspace():
    dispatcher = DesktopActionDispatcher()
    dispatcher.acknowledge = MagicMock()
    dispatcher.launch_app = MagicMock(return_value={
        "app": "telegram",
        "cancelled": True,
        "success": False,
    })

    dispatcher.execute_action("cmd-123", "launch_app", {"app": "telegram"})
    dispatcher.acknowledge.assert_called_once_with(
        "cmd-123",
        "cancelled",
        {"app": "telegram", "cancelled": True, "success": False},
    )


def test_antigravity_switcher_declares_64bit_dpapi_signatures():
    import core.antigravity_crypto  # noqa: F401

    assert hasattr(ctypes.windll.crypt32.CryptUnprotectData, "argtypes")
    assert ctypes.windll.crypt32.CryptUnprotectData.restype == wintypes.BOOL
    assert hasattr(ctypes.windll.kernel32.LocalFree, "argtypes")
    assert ctypes.windll.kernel32.LocalFree.restype == ctypes.c_void_p


def test_system_action_dispatcher_clamps_negative_timeout():
    agent_sys = SystemActionDispatcher()
    agent_sys.acknowledge = MagicMock()

    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.stdout = "ok"
        mock_proc.stderr = ""
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        agent_sys.execute_action("cmd-1", "cmd", {"command": "echo test", "timeout": -10})
        assert mock_run.call_args[1]["timeout"] >= 1


def test_vpn_manager_retains_enum_windows_callback_reference():
    agent = PCControlAgent.__new__(PCControlAgent)
    agent.config = {}
    visible_hwnd = 101
    hidden_hwnd = 202
    active_calls = [False, False, False, True]

    mock_user32 = MagicMock()
    mock_kernel32 = MagicMock()
    mock_kernel32.GetCurrentThreadId.return_value = 1
    mock_user32.GetThreadDesktop.return_value = 10
    mock_user32.OpenInputDesktop.return_value = 20
    mock_user32.IsWindowVisible.side_effect = lambda hwnd: hwnd == visible_hwnd
    mock_user32.GetWindowTextLengthW.return_value = 4

    def fake_get_window_text(hwnd, buf, _max_len):
        buf.value = "Happ"
        return 4

    def fake_get_rect(hwnd, rect_ptr):
        rect_ptr._obj.left = 0
        rect_ptr._obj.top = 0
        rect_ptr._obj.right = 800
        rect_ptr._obj.bottom = 600
        return 1

    enum_callbacks = []

    def fake_enum_windows(cb, lparam):
        enum_callbacks.append(cb)
        cb(hidden_hwnd, lparam)
        cb(visible_hwnd, lparam)
        return 1

    mock_user32.GetWindowTextW.side_effect = fake_get_window_text
    mock_user32.GetWindowRect.side_effect = fake_get_rect
    mock_user32.EnumWindows.side_effect = fake_enum_windows

    with patch.object(agent, "check_happ_vpn_active", side_effect=active_calls), \
         patch("ctypes.windll.user32", mock_user32), \
         patch("ctypes.windll.kernel32", mock_kernel32), \
         patch("pyautogui.click") as mock_click, \
         patch("time.sleep"):
        res = agent.connect_happ_vpn(timeout=1.5)

    assert res["verified"] is True
    assert len(enum_callbacks) == 2
    mock_user32.ShowWindow.assert_called_once_with(visible_hwnd, 9)
    mock_click.assert_called_once()


def test_protobuf_replace_topic_skips_all_wire_types_intact():
    def make_varint(v):
        res = bytearray()
        while v > 0x7F:
            res.append((v & 0x7F) | 0x80)
            v >>= 7
        res.append(v & 0x7F)
        return bytes(res)

    def make_tag(field_num, wire_type):
        return make_varint((field_num << 3) | wire_type)

    buf = bytearray()
    s_val = b"hello world"
    buf.extend(make_tag(1, 2))
    buf.extend(make_varint(len(s_val)))
    buf.extend(s_val)

    buf.extend(make_tag(2, 0))
    buf.extend(make_varint(1337))

    buf.extend(make_tag(3, 1))
    buf.extend(b"\x01\x02\x03\x04\x05\x06\x07\x08")

    buf.extend(make_tag(4, 5))
    buf.extend(b"\x0a\x0b\x0c\x0d")

    buf.extend(make_tag(5, 0))
    buf.extend(make_varint(999))

    raw_data = bytes(buf)
    new_data = ProtobufHelper.replace_unified_topic_entry(raw_data, "test-topic-id", b"payload-bytes-123")
    assert len(new_data) > len(raw_data)
    assert b"hello world" in new_data
    assert b"\x01\x02\x03\x04\x05\x06\x07\x08" in new_data
    assert b"\x0a\x0b\x0c\x0d" in new_data
    assert b"test-topic-id" in new_data


def test_antigravity_find_best_account_rejects_zero_quota():
    mgr = AntigravityAccountManager()
    candidates = [
        {"id": "acc1", "email": "active_zero@test.com", "is_active": True, "min_quota": 0, "status": "active", "has_valid_token": True},
        {"id": "acc2", "email": "inactive_zero@test.com", "is_active": False, "min_quota": 0, "status": "active", "has_valid_token": True},
    ]

    with patch.object(mgr, "list_accounts", return_value=candidates):
        assert mgr.find_best_account() is None

        candidates.append({"id": "acc3", "email": "has_quota@test.com", "is_active": False, "min_quota": 85, "status": "active", "has_valid_token": True})
        best = mgr.find_best_account()
        assert best is not None
        assert best["id"] == "acc3"


def test_antigravity_switch_account_skips_ide_launch_when_not_running():
    mgr = AntigravityAccountManager()
    mgr.is_ide_running = MagicMock(return_value=False)
    mgr.launch_ide = MagicMock(return_value=True)
    mgr.refresh_token_if_needed = MagicMock(return_value={"access_token": "tok"})
    mgr.get_ide_state_paths = MagicMock(return_value=(None, None))

    mock_row = {"id": "acc1", "email": "test@test.com", "name": "test", "device_profile_json": None}
    with patch("sqlite3.connect") as mock_conn:
        cur = MagicMock()
        cur.execute.return_value.fetchone.return_value = mock_row
        mock_conn.return_value.cursor.return_value = cur

        with patch.object(mgr, "get_cloud_db_path") as mock_db_path:
            mock_db_path.return_value.exists.return_value = True
            res = mgr.switch_account("acc1", restart_ide=True)

    assert not mgr.launch_ide.called
    assert res["restarted_ide"] is False


def test_probe_google_https_accepts_204_and_404_rejects_url_error():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 204
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp
        assert VpnManagerMixin.probe_google_https(timeout=1.0) is True

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://oauth2.googleapis.com",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )
        assert VpnManagerMixin.probe_google_https(timeout=1.0) is True

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError(reason="Connection timed out")
        assert VpnManagerMixin.probe_google_https(timeout=1.0) is False


def test_get_incy_servers_filters_russia_and_sorts_by_latency(tmp_path):
    db_file = tmp_path / "incy.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("CREATE TABLE servers (id TEXT, name TEXT, latency INTEGER, isActive INTEGER)")
    cur.executemany(
        "INSERT INTO servers VALUES (?, ?, ?, ?)",
        [
            ("s1", "🇩🇪 Германия", -1, 0),
            ("s2", "🇫🇮 Финляндия", 33, 0),
            ("s3", "🇷🇺 Россия", 20, 0),
            ("s4", "🇸🇪 Швеция", 80, 0),
            ("s5", "🇨🇭 Швейцария", 50, 0),
        ],
    )
    conn.commit()
    conn.close()

    with patch("os.path.expandvars", return_value=str(db_file)):
        servers = VpnManagerMixin.get_incy_servers()

    assert not any("россия" in s["name"].lower() for s in servers)
    de_srv = next(s for s in servers if "германия" in s["name"].lower())
    assert de_srv["latency"] == 9999
    assert [s["name"] for s in servers] == ["🇫🇮 Финляндия", "🇨🇭 Швейцария", "🇸🇪 Швеция", "🇩🇪 Германия"]


def test_switch_next_incy_server_rotates_preferences_and_relaunches(tmp_path):
    pref_file = tmp_path / "preferences.json"
    pref_file.write_text(json.dumps({"selectedServerId": "s1", "autoConnect": False}), encoding="utf-8")

    class DummyAgent(VpnManagerMixin):
        def __init__(self):
            self.config = {"apps": {"INCY": "dummy_incy.exe"}}

        def check_incy_vpn_active(self):
            return True

    agent = DummyAgent()
    mock_servers = [
        {"id": "s1", "name": "Server 1", "latency": 30},
        {"id": "s2", "name": "Server 2", "latency": 45},
    ]

    with patch.object(VpnManagerMixin, "get_incy_servers", return_value=mock_servers), \
         patch("os.path.expandvars", return_value=str(pref_file)), \
         patch("psutil.process_iter", return_value=[]), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("subprocess.Popen") as mock_popen:
        res = agent.switch_next_incy_server()
        assert res["success"] is True
        assert res["server"] == "Server 2"
        assert res["server_id"] == "s2"

        saved_prefs = json.loads(pref_file.read_text(encoding="utf-8"))
        assert saved_prefs["selectedServerId"] == "s2"
        assert saved_prefs["autoConnect"] is True
        assert mock_popen.called


def test_heal_vpn_and_antigravity_orchestrates_switch_probe_and_restart():
    class DummyAgent(VpnManagerMixin):
        def __init__(self):
            self.config = {}

    agent = DummyAgent()
    with patch("core.antigravity_switcher.AntigravityAccountManager.kill_ide") as mock_kill, \
         patch.object(agent, "switch_next_incy_server", return_value={"success": True, "server": "Sweden", "latency": 40}) as mock_switch, \
         patch.object(agent, "probe_google_https", return_value=True) as mock_probe, \
         patch("core.antigravity_switcher.AntigravityAccountManager.launch_ide", return_value=True) as mock_launch:
        res = agent.heal_vpn_and_antigravity(attempts=2)

        assert res["success"] is True
        assert res["server"] == "Sweden"
        assert res["google_https_ok"] is True
        assert res["restarted_ide"] is True
        assert mock_kill.called
        assert mock_switch.called
        assert mock_probe.called
        assert mock_launch.called


def test_action_dispatcher_executes_heal_antigravity():
    class DummyDispatcher(DesktopActionDispatcher, VpnManagerMixin):
        def __init__(self):
            self.acks = []
            self.config = {}

        def acknowledge(self, command_id, status, data=None):
            self.acks.append((command_id, status, data))

    dispatcher = DummyDispatcher()
    with patch.object(dispatcher, "heal_vpn_and_antigravity", return_value={"success": True, "server": "Finland"}) as mock_heal:
        dispatcher.execute_action("cmd_123", "heal_antigravity", {"attempts": 2})
        assert mock_heal.called
        assert len(dispatcher.acks) == 1
        assert dispatcher.acks[0][0] == "cmd_123"
        assert dispatcher.acks[0][1] == "executed"
        assert dispatcher.acks[0][2]["server"] == "Finland"


def test_antigravity_router_dispatches_fix_ag_command_and_callback():
    app = MagicMock()
    app.mqtt.desktop_status = "online"
    app.mqtt.publish_desktop_action.return_value = "cmd_heal_999"

    assert route_antigravity_command(app, 12345, "/fix_ag") is True
    assert app.mqtt.publish_desktop_action.called
    assert app.mqtt.publish_desktop_action.call_args[0][0] == "heal_antigravity"

    app.reset_mock()
    assert route_antigravity_callback(app, "ag_heal", 12345, 999, "cb_1") is True
    assert app.ui.answer_callback.called
    assert app.mqtt.publish_desktop_action.called


def test_codex_removed_from_apps_config_and_workspace_steps():
    config_path = ROOT_DIR / "config.example.json"
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    apps = cfg.get("apps", {})
    assert "Codex" not in apps

    workspace = apps.get("Рабочий сетап", {})
    steps = workspace.get("steps", [])
    step_ids = [s.get("id") if isinstance(s, dict) else s for s in steps]
    assert "codex" not in step_ids

    for step in steps:
        assert isinstance(step, dict)
        assert "id" in step and "label" in step and "command" in step and "verification" in step


def test_codex_absent_from_workspace_steps_but_available_in_apps_menu():
    step_ids = [s["id"] for s in WORKSPACE_STEPS]
    assert "codex" not in step_ids

    ui = TelegramBotApi("mock_token")
    menu = ui.apps_menu_markup()
    buttons = [btn["text"] for row in menu["inline_keyboard"] for btn in row]
    assert "Codex" in buttons


def test_workspace_runner_reports_partial_warning_when_step_unverified():
    ui = MagicMock()
    mqtt = MagicMock()
    mqtt.publish_desktop_action.return_value = "cmd-ws-test"
    app = PcControlBotApp.__new__(PcControlBotApp)
    app.ui = ui
    app.mqtt = mqtt
    app.mqtt.desktop_status = "online"
    app.mqtt.health.get_status.return_value = "online"
    app.power_flow = PowerFlowManager(ui, mqtt)
    app.physical_btn = MagicMock()
    app.monitoring = MagicMock()
    app.save_state = MagicMock()
    app.pending_shutdown_cmd_id = None
    app.active_workspace_cmd_id = None
    app._workspace_running = False
    app._workspace_start_lock = threading.Lock()
    app.workspace_cancel_event = threading.Event()
    app.happ_ready_event = threading.Event()
    app.workspace_executed_event = threading.Event()
    app.minimize_ack_event = threading.Event()
    app.workspace_execution_result = None
    app.minimize_ack_result = None
    app.workspace_card_msg_id = 100
    app.app_statuses = {}

    t = threading.Thread(target=app.run_workspace_setup_steps, args=(12345, 100, "ws-test"), daemon=True)
    t.start()
    time.sleep(0.1)

    cmd_id = app.active_workspace_cmd_id
    for step_id in ["telegram", "yandex_music", "happ_vpn", "antigravity", "chatgpt"]:
        app._on_mqtt_event("ack", {"id": cmd_id, "result": "progress", "data": {"step_id": step_id, "state": "verified"}})

    app._on_mqtt_event("ack", {"id": cmd_id, "result": "executed", "data": {}})
    t.join(timeout=2.0)

    all_sent_texts = [call[0][1] for call in ui.send_message.call_args_list]
    assert not any("Рабочий сетап успешно запущен" in t for t in all_sent_texts)
    assert any("неполным подтверждением" in t for t in all_sent_texts)


def test_happ_vpn_active_checker_returns_bool():
    assert isinstance(PCControlAgent.check_happ_vpn_active(), bool)


def test_connect_happ_vpn_skips_click_when_already_connected():
    agent = PCControlAgent.__new__(PCControlAgent)
    agent.config = {"happ_ready_delay_seconds": 1}
    with patch.object(PCControlAgent, "check_happ_vpn_active", return_value=True):
        res = agent.connect_happ_vpn()
        assert res.get("verified") is True
        assert res.get("already_connected") is True


def test_launcher_recognizes_running_chrome_list_command():
    launcher = LauncherMixin()
    launcher.config = {
        "apps": {
            "Chrome": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                "--profile-directory=Default",
            ]
        }
    }
    mock_proc = MagicMock()
    mock_proc.info = {"name": "chrome.exe", "pid": 1234}
    with patch("psutil.process_iter", return_value=[mock_proc]), \
         patch.object(LauncherMixin, "_has_visible_window", return_value=True):
        res = launcher.launch_app("Chrome")
        assert res.get("app") == "Chrome"
        assert any("already running verified" in item for item in res.get("launched", []))


def test_launcher_relaunches_chrome_when_no_visible_window():
    launcher = LauncherMixin()
    launcher.config = {
        "apps": {
            "Chrome": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                "--profile-directory=Default",
            ]
        }
    }
    mock_proc = MagicMock()
    mock_proc.info = {"name": "chrome.exe", "pid": 1234}

    with patch("psutil.process_iter", return_value=[mock_proc]), \
         patch.object(LauncherMixin, "_has_visible_window", return_value=False), \
         patch("subprocess.Popen") as mock_popen, \
         patch("shutil.which", return_value=r"C:\Program Files\Google\Chrome\Application\chrome.exe"), \
         patch("pathlib.Path.exists", return_value=True):
        mock_popen.return_value.poll.return_value = None
        launcher.launch_app("Chrome")
        assert mock_popen.called
        cmd_launched = mock_popen.call_args[0][0]
        assert "--profile-directory=Default" in cmd_launched


def test_chatgpt_configured_in_apps_and_workspace_steps():
    cfg = json.loads((ROOT_DIR / "config.example.json").read_text(encoding="utf-8"))
    assert "ChatGPT" in cfg.get("apps", {})
    assert "%LOCALAPPDATA%" in cfg["apps"]["ChatGPT"] or "chatgpt" in cfg["apps"]["ChatGPT"].lower()

    workspace_steps = cfg.get("apps", {}).get("Рабочий сетап", {}).get("steps", [])
    chatgpt_step = next((s for s in workspace_steps if s.get("id") == "chatgpt"), None)
    assert chatgpt_step is not None
    assert "ChatGPT" in chatgpt_step.get("label", "")
    assert "chatgpt.exe" in [t.lower() for t in chatgpt_step.get("verification", {}).get("targets", [])]
