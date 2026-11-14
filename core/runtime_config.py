"""Shared runtime paths, environment loading, boot time, and safety constants for Windows agents."""

import os
import time
from pathlib import Path

import psutil

BASE_DIR = Path(__file__).resolve().parent.parent
AGENT_VERSION = "2.0"

DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"

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


def load_env_file(path: Path) -> None:
    """Populate os.environ from a .env file without overriding existing variables."""
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
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


load_env_file(BASE_DIR / ".env")


def resolve_state_path(filename: str, *, require_data_dir: bool = False) -> Path:
    """Resolve persistent JSON state path inside data/ with legacy root fallback."""
    data_state = DATA_DIR / filename
    root_state = BASE_DIR / filename
    if require_data_dir:
        return data_state if (DATA_DIR.exists() or not root_state.exists()) else root_state
    return data_state if (data_state.exists() or not root_state.exists()) else root_state


def get_real_boot_time() -> float:
    """Return actual boot timestamp using Windows Kernel-Boot event log or psutil fallback."""
    try:
        import xml.etree.ElementTree as ET
        from datetime import datetime
        import win32evtlog

        query_str = (
            '*[System[Provider[@Name="Microsoft-Windows-Kernel-Boot"] '
            "and (EventID=27 or EventID=20)]]"
        )
        query = win32evtlog.EvtQuery(
            "System",
            win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection,
            query_str,
        )
        try:
            events = win32evtlog.EvtNext(query, 1)
            if events:
                try:
                    xml_data = win32evtlog.EvtRender(events[0], win32evtlog.EvtRenderEventXml)
                    root = ET.fromstring(xml_data)
                    for elem in root.iter():
                        if elem.tag.endswith("TimeCreated") and "SystemTime" in elem.attrib:
                            system_time = elem.attrib["SystemTime"]
                            dt = datetime.fromisoformat(system_time.replace("Z", "+00:00"))
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
