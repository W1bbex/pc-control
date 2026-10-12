import sys, os
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
