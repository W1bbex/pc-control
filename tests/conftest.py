import sys, os
import pytest

_orig_allowed_user_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "123456789")


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_env():
    yield
    if _orig_allowed_user_id is None:
        os.environ.pop("TELEGRAM_ALLOWED_USER_ID", None)
    else:
        os.environ["TELEGRAM_ALLOWED_USER_ID"] = _orig_allowed_user_id

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
CORE_DIR = os.path.join(ROOT_DIR, 'core')
VDS_DIR = os.path.join(ROOT_DIR, 'vds_bot')
SCRIPTS_DIR = os.path.join(ROOT_DIR, 'scripts')
for p in (ROOT_DIR, CORE_DIR, VDS_DIR, SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    import _pytest.pathlib
    _orig_cleanup_symlinks = _pytest.pathlib.cleanup_dead_symlinks
    def _safe_cleanup_symlinks(root):
        try:
            _orig_cleanup_symlinks(root)
        except (PermissionError, OSError):
            pass
    _pytest.pathlib.cleanup_dead_symlinks = _safe_cleanup_symlinks

    _orig_cleanup_numbered = _pytest.pathlib.cleanup_numbered_dir
    def _safe_cleanup_numbered(root, prefix, keep, lock_timeout):
        try:
            _orig_cleanup_numbered(root, prefix, keep, lock_timeout)
        except (PermissionError, OSError):
            pass
    _pytest.pathlib.cleanup_numbered_dir = _safe_cleanup_numbered
except Exception:
    pass


@pytest.fixture(autouse=True)
def _isolate_vds_runtime_state(tmp_path, monkeypatch):
    flow_state = tmp_path / "flow_state.json"
    bot_state = tmp_path / "state.json"
    bot_lock = tmp_path / "bot.lock"
    history_db = tmp_path / "telemetry_history.db"

    monkeypatch.setenv("FLOW_STATE_PATH", str(flow_state))
    monkeypatch.setenv("STATE_PATH", str(bot_state))
    monkeypatch.setenv("BOT_LOCK_PATH", str(bot_lock))
    monkeypatch.setenv("TELEMETRY_HISTORY_DB_PATH", str(history_db))

    for mod_name in ("vds_bot.power_flow", "power_flow"):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            monkeypatch.setattr(mod, "FLOW_STATE_PATH", flow_state, raising=False)

    for mod_name in ("vds_bot.bot", "bot"):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            monkeypatch.setattr(mod, "STATE_PATH", bot_state, raising=False)
            monkeypatch.setattr(mod, "LOCK_PATH", bot_lock, raising=False)

    for mod_name in ("vds_bot.monitoring", "monitoring"):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            monkeypatch.setattr(mod, "HISTORY_DB_PATH", history_db, raising=False)

