#!/usr/bin/env python3
"""Power Flow State and Persistence Layer.

Provides:
- PowerFlowStage state enumeration.
- Atomic file-based state serialization to flow_state.json.
- TTL expiration checking and state validation.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
from typing import Any, Dict, Optional

LOG = logging.getLogger("pc-control-bot.power_state")

FLOW_STATE_PATH = Path(os.environ.get("FLOW_STATE_PATH", "/var/lib/pc-control-bot/flow_state.json"))
FLOW_TTL_SECONDS = 300  # 5 minutes


class PowerFlowStage:
    IDLE = "IDLE"
    POWERING_ON = "POWERING_ON"
    REBOOTING = "REBOOTING"
    UNLOCKING = "UNLOCKING"
    UNLOCK_FAILED = "UNLOCK_FAILED"
    WAITING_WORKSPACE_DECISION = "WAITING_WORKSPACE_DECISION"
    WORKSPACE_RUNNING = "WORKSPACE_RUNNING"


def load_persisted_flow_state(path: Path = FLOW_STATE_PATH) -> Optional[Dict[str, Any]]:
    """Loads and returns persisted flow state if present, else None."""
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        LOG.exception("Could not read flow state from %s", path)
        return None


def save_persisted_flow_state(data: Dict[str, Any], path: Path = FLOW_STATE_PATH) -> bool:
    """Atomically writes flow state dict to target path using PID/tid temp file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except Exception:
            pass
        os.replace(temp, path)
        return True
    except Exception:
        LOG.exception("Could not atomically save flow state to %s", path)
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def clear_persisted_flow_state(path: Path = FLOW_STATE_PATH) -> None:
    """Safely removes persisted flow state file if present."""
    try:
        if path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        LOG.warning("Failed to delete flow state at %s", path)
