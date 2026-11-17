"""Private export and missing IDE-state checks with synthetic accounts only."""

from contextlib import closing
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.antigravity_crypto import AntigravityCrypto
from core.antigravity_switcher import AntigravityAccountManager
from desktop import action_dispatcher
from vds_bot.bot import PcControlBotApp


@pytest.fixture
def manager(tmp_path, monkeypatch):
    db = tmp_path / "accounts.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, email TEXT, token_json TEXT, is_active INTEGER, last_used INTEGER)")
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO accounts VALUES ('old', 'old@example.test', '{}', 1, 0)")
        conn.execute("INSERT INTO accounts VALUES ('new', 'new@example.test', '{}', 0, 0)")
        conn.commit()
    mgr = AntigravityAccountManager(crypto=AntigravityCrypto(master_key_hex="ab" * 32))
    mgr.get_cloud_db_path = lambda: db
    mgr.is_ide_running = MagicMock(return_value=True)
    mgr.kill_ide = MagicMock()
    mgr.refresh_token_if_needed = MagicMock(return_value={"access_token": "offline-placeholder"})
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty-appdata"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "empty-home")
    return mgr, db


def test_missing_real_resolver_state_fails_before_process_or_database_effects(manager):
    mgr, db = manager
    assert mgr.get_ide_state_paths() == (None, None)
    with pytest.raises(FileNotFoundError, match="state.vscdb"):
        mgr.switch_account("new", restart_ide=False)
    mgr.is_ide_running.assert_not_called()
    mgr.kill_ide.assert_not_called()
    mgr.refresh_token_if_needed.assert_not_called()
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT id FROM accounts WHERE is_active=1").fetchone()[0] == "old"


def test_remote_export_creates_local_file_and_ack_has_no_bundle(manager, tmp_path, monkeypatch):
    mgr, _ = manager
    destination = tmp_path / "exports" / "accounts.json"
    monkeypatch.setattr(action_dispatcher, "EXPORT_PATH", destination, raising=False)
    app = SimpleNamespace(antigravity_manager=mgr, acknowledge=MagicMock())
    action_dispatcher.ActionDispatcherMixin.execute_action(app, "desk-export", "antigravity_export", {})
    assert destination.is_file()
    bundle = json.loads(destination.read_text(encoding="utf-8"))
    assert len(bundle["accounts"]) == 2 and bundle["version"] == 1
    _, result, data = app.acknowledge.call_args.args
    assert result == "executed"
    assert data["export_path"] == str(destination)
    assert "accounts" not in data and "master_key_hex" not in data
    if os.name == "nt":
        import win32security
        descriptor = win32security.GetNamedSecurityInfo(str(destination), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
        acl = descriptor.GetSecurityDescriptorDacl()
        assert descriptor.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
        assert acl.GetAceCount() <= 2
    else:
        assert destination.stat().st_mode & 0o777 == 0o600


def test_export_write_error_returns_failure_without_success_message(manager, tmp_path, monkeypatch):
    mgr, _ = manager
    destination = tmp_path / "exports" / "accounts.json"
    monkeypatch.setattr(action_dispatcher, "EXPORT_PATH", destination, raising=False)
    mgr.export_accounts = MagicMock(side_effect=OSError("offline disk full"))
    app = SimpleNamespace(antigravity_manager=mgr, acknowledge=MagicMock())
    action_dispatcher.ActionDispatcherMixin.execute_action(app, "desk-export", "antigravity_export", {})
    _, result, data = app.acknowledge.call_args.args
    assert result == "failed"
    bot = PcControlBotApp.__new__(PcControlBotApp)
    bot.allowed_user_id = 123
    bot.ui, bot.power_flow, bot.physical_btn = MagicMock(), MagicMock(), MagicMock()
    bot.save_state = MagicMock()
    bot.pending_antigravity_cmd_ids = {"desk-export": (123, None, "export")}
    bot._on_mqtt_event("ack", {"id": "desk-export", "result": result, "data": data})
    texts = [call.args[1] for call in bot.ui.send_message.call_args_list]
    assert texts and any("экспорт" in text.lower() for text in texts)
    assert all("успешно" not in text.lower() for text in texts)

