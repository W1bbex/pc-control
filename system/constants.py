import logging
import logging.handlers
import uuid

from core.runtime_config import (
    AGENT_VERSION,
    BASE_DIR,
    CONFIG_PATH,
    DATA_DIR,
    LOG_DIR,
    PROTECTED_PROCESSES,
    REAL_BOOT_TIME,
    get_real_boot_time,
    resolve_state_path,
)

AGENT_BOOT_ID = f"sys-{uuid.uuid4().hex[:12]}"

STATE_PATH = resolve_state_path("boot-state.json", require_data_dir=True)

TOPIC_COMMAND = "pc/main/windows/system/command"
TOPIC_ACK = "pc/main/windows/system/ack"
TOPIC_STATUS = "pc/main/windows/system/status"
TOPIC_TELEMETRY = "pc/main/windows/system/telemetry"


def configure_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pc-control-system-agent")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(message)s"
    )
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "boot-agent.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


LOG = configure_logging()
