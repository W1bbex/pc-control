#!/usr/bin/env python3
"""
Calculate monthly PC usage and screen time.
Parses Windows System & Power Event Logs for the last 30 days
to determine daily and total active session time / uptime.
"""

import collections
import datetime
import subprocess
import sys
import xml.etree.ElementTree as ET

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def get_windows_power_events(days=30):
    start_dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Event IDs:
    # 6005: Event log service started (System startup)
    # 6006: Event log service stopped (Clean shutdown)
    # 1074: Shutdown / restart initiated
    # 7001: User Logon
    # 7002: User Logoff
    # 1: Kernel-General (System time change)
    # 12 / 13: Kernel-General (OS started / OS shutdown)
    # 42: Kernel-Power (Entering sleep)
    # 107: Kernel-Power (Resumed from sleep)
    # 1: Kernel-Power (System resumed from sleep)

    query = (
        f"*[System[TimeCreated[@SystemTime >= '{start_iso}'] and ("
        f"EventID=6005 or EventID=6006 or EventID=7001 or EventID=7002 or "
        f"EventID=1074 or (System[Provider[@Name='Microsoft-Windows-Kernel-Power']] and (EventID=42 or EventID=107 or EventID=1)) or "
        f"(System[Provider[@Name='Microsoft-Windows-Kernel-General']] and (EventID=12 or EventID=13))"
        f")]]"
    )

    cmd = ["wevtutil", "qe", "System", f"/q:{query}", "/f:RenderedXml"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if out.returncode != 0 or not out.stdout.strip():
        return []

    # Wrap raw output into valid XML
    wrapped = f"<Events>{out.stdout}</Events>"
    try:
        root = ET.fromstring(wrapped)
    except Exception:
        # If XML parsing fails due to multiple root elements, split manually
        events = []
        for piece in out.stdout.split("<Event xmlns="):
            if not piece.strip():
                continue
            piece = "<Event xmlns=" + piece
            try:
                elem = ET.fromstring(piece)
                events.append(elem)
            except Exception:
                pass
        return events

    return list(root.iter("{http://schemas.microsoft.com/win/2004/08/events/event}Event"))


def analyze_usage():
    events_raw = get_windows_power_events(days=30)
    parsed_events = []

    for ev in events_raw:
        sys_elem = ev.find("{http://schemas.microsoft.com/win/2004/08/events/event}System")
        if sys_elem is None:
            continue
        eid_elem = sys_elem.find("{http://schemas.microsoft.com/win/2004/08/events/event}EventID")
        time_elem = sys_elem.find("{http://schemas.microsoft.com/win/2004/08/events/event}TimeCreated")
        prov_elem = sys_elem.find("{http://schemas.microsoft.com/win/2004/08/events/event}Provider")

        if eid_elem is None or time_elem is None:
            continue

        try:
            eid = int(eid_elem.text)
            st = time_elem.attrib.get("SystemTime")
            # Parse ISO UTC timestamp
            if st.endswith("Z"):
                st = st[:-1] + "+00:00"
            dt_utc = datetime.datetime.fromisoformat(st)
            dt_local = dt_utc.astimezone()
            provider = prov_elem.attrib.get("Name", "") if prov_elem is not None else ""
            parsed_events.append((dt_local, eid, provider))
        except Exception:
            continue

    parsed_events.sort(key=lambda x: x[0])

    # Classify state intervals (UP vs DOWN)
    # UP events: 6005, 12, 107, 1 (Kernel-Power), 7001
    # DOWN events: 6006, 13, 1074, 42, 7002
    
    # We will build intervals [start_up, end_up]
    intervals = []
    current_up = None

    for dt, eid, provider in parsed_events:
        is_up = eid in (6005, 12, 107) or (provider == "Microsoft-Windows-Kernel-Power" and eid == 1)
        is_down = eid in (6006, 13, 1074, 42)

        if is_up:
            if current_up is None:
                current_up = dt
        elif is_down:
            if current_up is not None:
                if dt > current_up:
                    intervals.append((current_up, dt))
                current_up = None

    # If currently UP, close interval at now
    now = datetime.datetime.now().astimezone()
    if current_up is not None and now > current_up:
        intervals.append((current_up, now))
    elif not intervals and parsed_events:
        # Fallback: if machine hasn't restarted in 30 days, interval is from boot time to now
        pass

    # If intervals is empty, use psutil boot time
    import psutil
    boot_time = datetime.datetime.fromtimestamp(psutil.boot_time()).astimezone()
    if not intervals:
        intervals.append((boot_time, now))
    elif intervals[-1][1] < now and (now - boot_time).total_seconds() > 0:
        # Add current session if not captured
        if intervals[-1][1] < boot_time:
            intervals.append((boot_time, now))
        else:
            intervals[-1] = (intervals[-1][0], now)

    # Distribute hours by calendar day
    daily_seconds = collections.defaultdict(float)

    for start_dt, end_dt in intervals:
        # Clip interval between days
        cur = start_dt
        while cur < end_dt:
            # End of current day
            next_day = cur.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
            chunk_end = min(end_dt, next_day)
            delta = (chunk_end - cur).total_seconds()
            day_key = cur.strftime("%Y-%m-%d")
            daily_seconds[day_key] += delta
            cur = chunk_end

    return daily_seconds, intervals


def main():
    print("=" * 65)
    print(" 📊 СТАТИСТИКА ВРЕМЕНИ ЗА ПК (ЗА ПОСЛЕДНИЙ МЕСЯЦ / 30 ДНЕЙ)")
    print("=" * 65)

    daily_seconds, intervals = analyze_usage()
    if not daily_seconds:
        print("Не удалось извлечь события из журнала Windows.")
        return

    sorted_days = sorted(daily_seconds.keys())
    total_seconds = sum(daily_seconds.values())
    total_hours = total_seconds / 3600.0
    active_days_count = len([d for d in sorted_days if daily_seconds[d] > 300])
    avg_per_active_day = (total_hours / active_days_count) if active_days_count > 0 else 0
    avg_per_month_day = total_hours / max(1, len(sorted_days))

    # Print summary
    print(f"\n📈 ОБЩИЕ ИТОГИ:")
    print(f" • Всего времени за 30 дней:   {total_hours:.1f} часов ({int(total_seconds // 3600)} ч {int((total_seconds % 3600) // 60)} мин)")
    print(f" • Активных дней использования: {active_days_count} из {len(sorted_days)} дней")
    print(f" • Среднее в активный день:    {avg_per_active_day:.1f} ч/день")
    print(f" • Среднее по календарю:       {avg_per_month_day:.1f} ч/день")

    print("\n📅 РАЗБИВКА ПО ДНЯМ (последние 14 дней):")
    print("-" * 65)
    print(f" {'Дата':<12} | {'Часов':<8} | {'Индикатор':<30}")
    print("-" * 65)

    for day in sorted_days[-14:]:
        sec = daily_seconds[day]
        h = sec / 3600.0
        # Progress bar (1 block per hour, max 16)
        bar_len = min(20, int(h * 1.5))
        bar = "█" * bar_len
        dt_obj = datetime.datetime.strptime(day, "%Y-%m-%d")
        weekday = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][dt_obj.weekday()]
        print(f" {day} ({weekday}) | {h:4.1f} ч   | {bar}")

    print("-" * 65)


if __name__ == "__main__":
    main()
