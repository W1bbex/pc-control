import logging
import logging.handlers
import os
import time
import uuid
from pathlib import Path

import psutil

AGENT_VERSION = "2.0"
AGENT_BOOT_ID = f"sys-{uuid.uuid4().hex[:12]}"

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "config.json"
_data_state = DATA_DIR / "boot-state.json"
_root_state = BASE_DIR / "boot-state.json"
STATE_PATH = _data_state if (DATA_DIR.exists() or not _root_state.exists()) else _root_state
_data_snapshot = DATA_DIR / "admin-sensors.json"
_hw_sensors_snapshot = BASE_DIR / "hardware" / "sensors" / "admin-sensors.json"
_hw_snapshot = BASE_DIR / "hardware" / "admin-sensors.json"
LOG_DIR = BASE_DIR / "logs"


def _load_env_file(path: Path):
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
    except Exception:
        pass
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_env_file(BASE_DIR / ".env")

TOPIC_COMMAND = "pc/main/windows/system/command"
TOPIC_ACK = "pc/main/windows/system/ack"
TOPIC_STATUS = "pc/main/windows/system/status"
TOPIC_TELEMETRY = "pc/main/windows/system/telemetry"


def get_real_boot_time():
    """Returns actual boot timestamp using Windows Kernel-Boot event log or psutil fallback."""
    try:
        import win32evtlog
        import xml.etree.ElementTree as ET
        from datetime import datetime
        query_str = '*[System[Provider[@Name="Microsoft-Windows-Kernel-Boot"] and (EventID=27 or EventID=20)]]'
        query = win32evtlog.EvtQuery(
            'System',
            win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection,
            query_str
        )
        try:
            events = win32evtlog.EvtNext(query, 1)
            if events:
                try:
                    xml_data = win32evtlog.EvtRender(events[0], win32evtlog.EvtRenderEventXml)
                    root = ET.fromstring(xml_data)
                    for elem in root.iter():
                        if elem.tag.endswith('TimeCreated') and 'SystemTime' in elem.attrib:
                            st = elem.attrib['SystemTime']
                            dt = datetime.fromisoformat(st.replace('Z', '+00:00'))
                            return dt.timestamp()
                finally:
                    try:
                        win32evtlog.EvtClose(events[0])
                    except Exception:
                        pass
        finally:
            try:
                win32evtlog.EvtClose(query)
            except Exception:
                pass
    except Exception:
        pass
    try:
        return psutil.boot_time()
    except Exception:
        return time.time()


REAL_BOOT_TIME = get_real_boot_time()

PROTECTED_PROCESSES = {
    "csrss.exe",
    "dwm.exe",
    "lsass.exe",
    "services.exe",
    "smss.exe",
    "system",
    "wininit.exe",
    "winlogon.exe",
    "python.exe",
    "pythonw.exe",
    "svchost.exe",
    "spoolsv.exe",
}


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
