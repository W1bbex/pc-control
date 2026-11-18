"""Controlled, user-triggered comparison of Windows application launch methods."""

import logging
import os
import subprocess
import threading
import time

import psutil

LOG = logging.getLogger("pc-control-agent")
_launch_lock = threading.Lock()
_PACKAGE_APP_ID = "OpenAI.Codex_2p2nqsd0c76g0!App"


def _codex_processes():
    return [p.info["pid"] for p in psutil.process_iter(["pid", "name"])
            if (p.info.get("name") or "").lower() == "chatgpt.exe"]


def diagnose_codex_launch(variant, alias, has_visible_window, timeout=15, allow_existing=False, cancel_event=None):
    variant = str(variant)
    if variant not in {"1", "2", "3"}:
        raise ValueError("Unknown Codex launch variant")
    if not _launch_lock.acquire(blocking=False):
        return {"codex_diagnostic": True, "variant": variant, "state": "busy"}
    try:
        existing = _codex_processes()
        if existing:
            if not allow_existing:
                return {"codex_diagnostic": True, "variant": variant, "state": "already_running"}
            if has_visible_window(["chatgpt.exe"]):
                return {"codex_diagnostic": True, "variant": variant, "state": "window_visible", "pids": existing}
        if cancel_event is not None and cancel_event.is_set():
            return {"codex_diagnostic": True, "variant": variant, "state": "cancelled"}
        alias = os.path.expandvars(alias) if alias else ""
        if variant in {"1", "2"} and not os.path.isfile(alias):
            raise FileNotFoundError("Codex application alias is unavailable")
        explorer = os.path.join(os.environ.get("WINDIR", ""), "explorer.exe")
        commands = {
            "1": [alias],
            "2": [explorer, alias],
            "3": [explorer, "shell:AppsFolder\\" + _PACKAGE_APP_ID],
        }
        LOG.info("Starting Codex diagnostic variant %s", variant)
        process = subprocess.Popen(
            commands[variant],
            cwd=(os.path.dirname(alias) or None) if variant == "1" else None,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        deadline = time.monotonic() + timeout
        seen = False
        last_window = None
        visible_since = None
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return {"codex_diagnostic": True, "variant": variant, "state": "cancelled"}
            pids = _codex_processes()
            seen = seen or bool(pids)
            window = has_visible_window(["chatgpt.exe"]) if pids else None
            if window:
                if window != last_window:
                    visible_since = time.monotonic()
                elif time.monotonic() - visible_since >= 2:
                    LOG.info("Codex diagnostic variant %s: visible window, pids=%s", variant, pids)
                    return {"codex_diagnostic": True, "variant": variant, "state": "window_visible", "pids": pids}
            else:
                visible_since = None
            last_window = window
            time.sleep(0.5)
        pids = _codex_processes()
        exit_code = process.poll()
        state = "process_without_window" if pids else "process_exited" if seen else "not_started"
        LOG.warning("Codex diagnostic variant %s: %s, launcher exit=%s", variant, state, exit_code)
        return {"codex_diagnostic": True, "variant": variant, "state": state, "exit_code": exit_code}
    finally:
        _launch_lock.release()
