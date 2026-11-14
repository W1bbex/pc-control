"""Shared constants and environment configuration for desktop agent modules."""

import json
import sys
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CORE_DIR = BASE_DIR / "core"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from core.runtime_config import (  # noqa: E402
    AGENT_VERSION,
    CONFIG_PATH,
    DATA_DIR,
    LOG_DIR,
    PROTECTED_PROCESSES,
    REAL_BOOT_TIME,
    get_real_boot_time,
    load_env_file as _load_env_file,
    resolve_state_path,
)

AGENT_BOOT_ID = f"desktop-{uuid.uuid4().hex[:12]}"

STATE_PATH = resolve_state_path("state.json", require_data_dir=False)
AGENT_START_TIME = time.time()


def _load_canonical_icons():
    candidate = BASE_DIR / "resources" / "icons.json"
    if candidate.exists():
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


ICONS = _load_canonical_icons()

TOPIC_COMMAND = "pc/main/windows/command"
TOPIC_ACK = "pc/main/windows/ack"
TOPIC_STATUS = "pc/main/windows/status"
TOPIC_TELEMETRY = "pc/main/windows/telemetry"
TOPIC_SCREENSHOT = "pc/main/windows/screenshot"
TOPIC_MEDIA_STATUS = "pc/main/windows/media/status"
TOPIC_MEDIA_COMMAND = "pc/main/windows/media/command"
TOPIC_ACTIVITY_QUERY = "pc/main/windows/activity/query"
TOPIC_ACTIVITY_RESPONSE = "pc/main/windows/activity/response"

