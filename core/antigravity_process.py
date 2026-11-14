#!/usr/bin/env python3
"""Antigravity IDE Process Lifecycle Management.

Handles:
- Identification of running Antigravity IDE processes on Windows and Linux.
- Graceful termination and force-kill fallbacks.
- Non-blocking execution across standard installation paths and PATH lookup.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence

import psutil

LOG = logging.getLogger("pc-control.antigravity.process")

IDE_PROCESS_NAMES: Sequence[str] = (
    "antigravity.exe",
    "antigravity",
    "antigravity ide.exe",
    "antigravity ide",
)


def is_ide_running(process_names: Sequence[str] = IDE_PROCESS_NAMES) -> bool:
    """Checks if Antigravity.exe / Antigravity IDE.exe is running."""
    for p in psutil.process_iter(["name"]):
        try:
            name = (p.info["name"] or "").lower()
            if name in process_names:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def kill_ide(timeout_sec: float = 8.0, process_names: Sequence[str] = IDE_PROCESS_NAMES) -> None:
    """Gracefully terminates Antigravity.exe / Antigravity IDE.exe processes."""
    targets = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            name = (p.info["name"] or "").lower()
            if name in process_names:
                targets.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not targets:
        return

    LOG.info("Terminating %d Antigravity process(es)...", len(targets))
    for p in targets:
        try:
            p.terminate()
        except Exception:
            pass

    gone, alive = psutil.wait_procs(targets, timeout=timeout_sec)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    if alive:
        try:
            psutil.wait_procs(alive, timeout=3.0)
        except Exception:
            pass


def launch_ide() -> bool:
    """Launches Antigravity.exe non-blocking or raises RuntimeError on failure."""
    if sys.platform != "win32":
        LOG.info("launch_ide called on non-win32 platform; skipping GUI launch.")
        return True

    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "antigravity" / "Antigravity.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Antigravity IDE" / "Antigravity IDE.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Antigravity IDE" / "Antigravity.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Antigravity" / "Antigravity.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Antigravity IDE" / "Antigravity IDE.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                subprocess.Popen([str(candidate)], close_fds=True, creationflags=subprocess.DETACHED_PROCESS)
                return True
            except Exception as e:
                LOG.error("Failed to launch Antigravity at %s: %s", candidate, e)
                raise RuntimeError(f"Failed to launch Antigravity IDE at {candidate}: {e}") from e

    # Fallback to PATH
    for exe_name in ("Antigravity.exe", "Antigravity IDE.exe"):
        resolved = shutil.which(exe_name)
        if resolved:
            try:
                subprocess.Popen([resolved], close_fds=True, creationflags=subprocess.DETACHED_PROCESS)
                return True
            except Exception as e:
                LOG.error("Failed to launch Antigravity at %s: %s", resolved, e)
                raise RuntimeError(f"Failed to launch Antigravity IDE: {e}") from e

    try:
        subprocess.Popen(["Antigravity.exe"], close_fds=True, creationflags=subprocess.DETACHED_PROCESS)
        return True
    except Exception as e:
        LOG.error("Failed to launch Antigravity: %s", e)
        raise RuntimeError(f"Failed to launch Antigravity IDE: {e}") from e
