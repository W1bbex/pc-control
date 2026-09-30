#!/usr/bin/env python3

"""Print a readable activity timeline for a local date/time interval."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_DIR = BASE_DIR / "logs"


def parse_local(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def iter_log_paths(log_dir):
    return sorted(log_dir.glob("activity.jsonl*"))


def load_events(log_dir, start, end):
    events = []
    for path in iter_log_paths(log_dir):
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                        timestamp = datetime.fromisoformat(event["ts"])
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
                    if start <= timestamp <= end:
                        events.append(event)
        except OSError:
            continue
    return sorted(events, key=lambda item: item.get("epoch", 0))


def describe(event):
    kind = event.get("event", "unknown")
    if kind in {"process_start", "process_exit", "process_existing"}:
        verb = {
            "process_start": "START",
            "process_exit": "EXIT ",
            "process_existing": "OPEN ",
        }[kind]
        return f"{verb} {event.get('process')} pid={event.get('pid')}"
    if kind == "foreground":
        title = event.get("title") or "(без заголовка)"
        return f"FOCUS {event.get('process') or 'unknown'} - {title}"
    if kind == "heartbeat":
        foreground = event.get("foreground") or {}
        return (
            f"ALIVE {foreground.get('process') or 'unknown'}"
            f" idle={event.get('idle_seconds')}s"
        )
    if kind in {"idle_start", "idle_end"}:
        return f"{kind.upper()} idle={event.get('idle_seconds')}s"
    if kind in {"network_snapshot", "network_change"}:
        return kind.upper()
    return kind.upper()


def main():
    parser = argparse.ArgumentParser(
        description="Показать локальную активность за интервал"
    )
    parser.add_argument("start", help="Начало: 2026-08-14T20:35")
    parser.add_argument("end", help="Конец: 2026-08-14T20:50")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument(
        "--events",
        help="Типы событий через запятую, например foreground,process_start",
    )
    args = parser.parse_args()

    allowed = None
    if args.events:
        allowed = {item.strip() for item in args.events.split(",") if item.strip()}

    for event in load_events(
        args.log_dir,
        parse_local(args.start),
        parse_local(args.end),
    ):
        if allowed is not None and event.get("event") not in allowed:
            continue
        timestamp = datetime.fromisoformat(event["ts"]).astimezone()
        print(timestamp.strftime("%Y-%m-%d %H:%M:%S"), describe(event))


if __name__ == "__main__":
    main()
