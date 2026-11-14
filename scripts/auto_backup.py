#!/usr/bin/env python3
"""
Automated Weekly Code Backup Engine for PCControlAgent.
Backs up pure source code across:
  1. Local PC Agent & Core Subsystems
  2. ESP8266 Firmware & Hardware Sensor collectors
  3. Live Production Code from VDS server (/opt/pc-control-bot)

Strictly ignores databases (activity.db), caches, logs, binaries, and venvs.
Detects code changes via SHA-256 tree hashing and size delta.
Only creates archives if code actually changed (or --force).
"""

from __future__ import annotations

import argparse
from datetime import datetime
import fnmatch
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time
import zipfile

# Ensure UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "scripts" else SCRIPT_DIR
BACKUPS_DIR = BASE_DIR / "backups"
STATE_FILE = BACKUPS_DIR / ".backup_state.json"
LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "auto_backup.log"

VDS_HOST = os.getenv("VDS_HOST", "vds")
REMOTE_VDS_DIR = "/opt/pc-control-bot"

# Extensions considered "source code"
CODE_EXTENSIONS = {
    ".py", ".json", ".example", ".txt", ".ini", ".cfg",
    ".cs", ".csproj", ".sln", ".config",
    ".ino", ".h", ".hpp", ".c", ".cpp",
    ".bat", ".ps1", ".sh", ".service", ".yaml", ".yml", ".md"
}

# Directories and patterns strictly excluded from code backup
EXCLUDE_DIRS = {
    "__pycache__", ".cache", ".pytest_cache", ".venv", "venv",
    ".git", ".agents", "backups", "logs", "data", "bin", "obj",
    "releases", "web_review"
}

EXCLUDE_FILES = {
    "*.db", "*.sqlite", "*.sqlite3", "*.wal", "*.shm",
    "*.exe", "*.dll", "*.pdb", "*.obj", "*.bin", "*.zip",
    "*.tar", "*.gz", "*.7z", "*.iso", "*.pyc", "*.pyo",
    "*.png", "*.jpg", "*.jpeg", "*.ico", "*.svg", "*.webp",
    "*.mp3", "*.wav", "*.flac", "*.log",
    ".env", "*.env", "secrets.h", "*secrets.h*"
}

VDS_MANIFEST_FILES = [
    "action_helpers.py",
    "bot.py",
    "console_service.py",
    "health_engine.py",
    "history.py",
    "monitoring.py",
    "mqtt_dispatcher.py",
    "mqtt_gateway.py",
    "physical_button.py",
    "player_manager.py",
    "power_flow.py",
    "power_scheduler.py",
    "telegram_ui.py",
    "ui_icons.py",
    "ui_markups.py",
    "ui_templates.py",
    "workspace_runner.py",
    "requirements.txt",
    "pc-control-bot.env.example",
    "pc-control-bot.service",
    "resources/icons.json",
    "routers/__init__.py",
    "routers/command_router.py",
    "routers/console_router.py",
    "routers/monitoring_router.py",
    "routers/physical_router.py",
    "routers/player_router.py",
    "routers/power_router.py",
    "routers/workspace_router.py",
    "routers/antigravity_router.py",
]

MAX_BACKUP_RETENTION = 12  # Keep last 12 weekly archives


def setup_logger(scheduled: bool = False) -> logging.Logger:
    logger = logging.getLogger("pc_control_backup")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # File handler for scheduled / background runs
    if scheduled or LOGS_DIR.exists():
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger


def is_excluded_file(path: Path) -> bool:
    for part in path.parts:
        if part in EXCLUDE_DIRS:
            return True
    name = path.name.lower()
    for pat in EXCLUDE_FILES:
        if fnmatch.fnmatch(name, pat):
            return True
    if path.suffix.lower() not in CODE_EXTENSIONS and name not in {".gitignore"}:
        return True
    return False


def collect_local_code_files(base_path: Path) -> list[Path]:
    code_files: list[Path] = []
    for root, dirs, files in os.walk(base_path):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        root_path = Path(root)
        for f in files:
            p = root_path / f
            if not is_excluded_file(p.relative_to(base_path)):
                code_files.append(p)
    code_files.sort(key=lambda p: str(p.relative_to(base_path)))
    return code_files


def run_ssh_command(cmd: str, timeout: int = 15) -> tuple[int, str, str]:
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", VDS_HOST, cmd],
            capture_output=True,
            text=False,
            timeout=timeout,
            **kwargs,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        return proc.returncode, stdout, stderr
    except Exception as e:
        return 1, "", str(e)


def collect_vds_code_files(logger: logging.Logger) -> dict[str, bytes]:
    logger.info("Connecting to VDS (%s) to fetch live production code in single batch...", VDS_HOST)
    vds_files: dict[str, bytes] = {}

    # 1. Batch fetch via single SSH command and compressed tar stream
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        file_args = " ".join(VDS_MANIFEST_FILES)
        remote_cmd = f"tar --ignore-failed-read -czf - -C {REMOTE_VDS_DIR} {file_args} 2>/dev/null"
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", VDS_HOST, remote_cmd],
            capture_output=True,
            timeout=20,
            **kwargs,
        )
        if proc.returncode == 0 and proc.stdout:
            with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:gz") as tar:
                for member in tar.getmembers():
                    if member.isfile():
                        f = tar.extractfile(member)
                        if f is not None:
                            with f:
                                clean_name = member.name.lstrip("./")
                                vds_files[clean_name] = f.read()
                                logger.debug("Fetched remote %s (%d bytes)", clean_name, len(vds_files[clean_name]))
        elif proc.returncode != 0:
            err_text = proc.stderr.decode("utf-8", errors="replace").strip() if isinstance(proc.stderr, bytes) else (proc.stderr or "")
            logger.warning("Single-batch fetch exited with code %s: %s", proc.returncode, err_text)
    except Exception as e:
        logger.warning("Single-batch fetch failed: %s. Falling back to per-file fetch...", e)

    # 2. Fallback if single batch returned no files
    if not vds_files:
        for fn in VDS_MANIFEST_FILES:
            code, stdout, stderr = run_ssh_command(f"cat {REMOTE_VDS_DIR}/{fn} 2>/dev/null", timeout=10)
            if code == 0 and stdout:
                vds_files[fn] = stdout.encode("utf-8")
                logger.debug("Fetched remote %s (%d bytes)", fn, len(stdout))
            else:
                logger.debug("Remote file %s not present or empty on VDS", fn)

    logger.info("Fetched %d files from VDS production.", len(vds_files))
    return vds_files


def build_manifest(local_files: list[Path], vds_files: dict[str, bytes], base_path: Path) -> dict:
    file_map = {}
    total_bytes = 0
    tree_hasher = hashlib.sha256()

    # 1. Local files
    for fp in local_files:
        rel = str(fp.relative_to(base_path)).replace("\\", "/")
        try:
            content = fp.read_bytes()
        except Exception:
            continue
        h = hashlib.sha256(content).hexdigest()
        size = len(content)
        total_bytes += size
        file_map[f"local:{rel}"] = {"size": size, "sha256": h}
        tree_hasher.update(f"{rel}:{h}\n".encode("utf-8"))

    # 2. VDS files
    for fn, content in sorted(vds_files.items()):
        h = hashlib.sha256(content).hexdigest()
        size = len(content)
        total_bytes += size
        file_map[f"vds:{fn}"] = {"size": size, "sha256": h}
        tree_hasher.update(f"vds:{fn}:{h}\n".encode("utf-8"))

    combined_sha256 = tree_hasher.hexdigest()

    return {
        "timestamp": int(time.time()),
        "iso_date": datetime.now().isoformat(timespec="seconds"),
        "total_code_bytes": total_bytes,
        "total_files": len(file_map),
        "combined_sha256": combined_sha256,
        "files": file_map,
    }


def compare_manifests(current: dict, previous: dict) -> tuple[bool, list[str], int]:
    if not previous:
        return True, ["Initial backup (no previous state found)"], current["total_code_bytes"]

    if current["combined_sha256"] == previous.get("combined_sha256"):
        return False, [], 0

    curr_files = current.get("files", {})
    prev_files = previous.get("files", {})

    changes: list[str] = []
    for k, v in curr_files.items():
        if k not in prev_files:
            changes.append(f"+ Added: {k} ({v['size']} bytes)")
        elif v["sha256"] != prev_files[k]["sha256"]:
            delta = v["size"] - prev_files[k]["size"]
            sign = "+" if delta >= 0 else ""
            changes.append(f"~ Modified: {k} (delta {sign}{delta} bytes)")

    for k in prev_files:
        if k not in curr_files:
            changes.append(f"- Removed: {k}")

    size_delta = current["total_code_bytes"] - previous.get("total_code_bytes", 0)
    return True, changes, size_delta


def rotate_backups(backups_dir: Path, logger: logging.Logger, keep: int = MAX_BACKUP_RETENTION):
    archives = sorted(backups_dir.glob("pc_control_backup_*.zip"), key=lambda p: p.stat().st_mtime)
    if len(archives) > keep:
        to_delete = archives[:-keep]
        for arc in to_delete:
            try:
                arc.unlink()
                logger.info("Rotated old backup archive: %s", arc.name)
            except Exception as e:
                logger.warning("Could not delete old archive %s: %s", arc.name, e)


def create_backup_archive(
    archive_path: Path,
    local_files: list[Path],
    vds_files: dict[str, bytes],
    manifest: dict,
    base_path: Path,
    logger: logging.Logger,
):
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = archive_path.with_suffix(".tmp.zip")

    logger.info("Creating backup archive: %s", archive_path.name)
    try:
        with zipfile.ZipFile(temp_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            # 1. Local files
            for fp in local_files:
                rel = str(fp.relative_to(base_path)).replace("\\", "/")
                arc_name = f"local/{rel}"
                zf.write(fp, arcname=arc_name)

            # 2. VDS live production files
            for fn, content in vds_files.items():
                arc_name = f"vds_server_live/{fn}"
                zf.writestr(arc_name, content)

            # 3. Manifest
            manifest_str = json.dumps(manifest, indent=2, ensure_ascii=False)
            zf.writestr("backup_manifest.json", manifest_str)

        temp_path.replace(archive_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    logger.info("Backup archive finalized! File size: %.2f KB", archive_path.stat().st_size / 1024)


def run_backup(force: bool = False, dry_run: bool = False, skip_vds: bool = False, scheduled: bool = False):
    logger = setup_logger(scheduled)
    logger.info("=== PCControlAgent Code Backup Started ===")

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Collect local files
    local_files = collect_local_code_files(BASE_DIR)
    logger.info("Indexed %d local source code files.", len(local_files))

    # 2. Collect VDS files
    vds_files: dict[str, bytes] = {}
    if not skip_vds:
        try:
            vds_files = collect_vds_code_files(logger)
        except Exception as e:
            logger.warning("VDS fetch encountered an error: %s. Continuing with local code.", e)
    else:
        logger.info("VDS check skipped via --skip-vds flag.")

    # 3. Build current manifest
    current_manifest = build_manifest(local_files, vds_files, BASE_DIR)
    current_size = current_manifest["total_code_bytes"]
    logger.info("Current total clean code size: %d bytes (%.2f KB) across %d files.",
                current_size, current_size / 1024, current_manifest["total_files"])

    # 4. Compare with last state
    previous_manifest = {}
    if STATE_FILE.exists():
        try:
            previous_manifest = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to read previous state file: %s", e)

    has_changed, changes, size_delta = compare_manifests(current_manifest, previous_manifest)

    if not has_changed and not force:
        logger.info("✓ No code changes detected since last backup. SHA-256 tree matches exactly.")
        logger.info("Backup skipped (archive not needed). Total code size unchanged: %.2f KB.", current_size / 1024)
        return 0

    if has_changed:
        sign = "+" if size_delta >= 0 else ""
        logger.info("Detected code changes (%d items, total delta: %s%d bytes):", len(changes), sign, size_delta)
        for ch in changes[:15]:
            logger.info("  %s", ch)
        if len(changes) > 15:
            logger.info("  ... and %d more changes", len(changes) - 15)
    elif force:
        logger.info("Force flag specified: generating backup regardless of changes.")

    if dry_run:
        logger.info("[DRY-RUN] Archive creation skipped.")
        return 0

    # 5. Create Archive
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name = f"pc_control_backup_{tag}.zip"
    archive_path = BACKUPS_DIR / archive_name

    create_backup_archive(archive_path, local_files, vds_files, current_manifest, BASE_DIR, logger)

    # 6. Update state file (skip if VDS fetch failed unexpectedly, and write atomically)
    if skip_vds or vds_files:
        tmp_state = STATE_FILE.with_suffix(".tmp.json")
        tmp_state.write_text(json.dumps(current_manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_state.replace(STATE_FILE)
        logger.info("Updated backup state in %s", STATE_FILE.name)
    else:
        logger.warning("Skipped overwriting %s because VDS fetch returned 0 files.", STATE_FILE.name)

    # 7. Rotate old backups
    rotate_backups(BACKUPS_DIR, logger, keep=MAX_BACKUP_RETENTION)

    logger.info("=== PCControlAgent Code Backup Completed Successfully ===")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Automated PCControlAgent Code Backup Tool")
    parser.add_argument("--force", action="store_true", help="Force backup creation even if no code changed")
    parser.add_argument("--dry-run", action="store_true", help="Scan and check differences without creating archive")
    parser.add_argument("--skip-vds", action="store_true", help="Skip fetching live code from VDS server")
    parser.add_argument("--scheduled", action="store_true", help="Run in scheduled mode (logs to file)")
    args = parser.parse_args()

    sys.exit(run_backup(force=args.force, dry_run=args.dry_run, skip_vds=args.skip_vds, scheduled=args.scheduled))


if __name__ == "__main__":
    main()
