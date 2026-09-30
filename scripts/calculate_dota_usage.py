#!/usr/bin/env python3
"""
Calculate Dota 2 playtime vs Total PC usage over the last 30 days.
Extracts:
1. Steam local playtime / LastPlayed timestamps.
2. Windows Activity Logs (activity.jsonl) process and foreground window intervals.
3. Windows BAM (Background Activity Moderator) & Prefetch execution logs.
"""

import collections
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import winreg

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def analyze_activity_logs():
    paths = sorted(pathlib.Path("logs").glob("activity.jsonl*"))
    events = []
    for p in paths:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                pass

    events.sort(key=lambda x: x.get("epoch", 0))

    # Calculate time spent where dota2.exe was active or running
    dota_intervals = []
    current_start = None
    prev_epoch = None

    for ev in events:
        kind = ev.get("event")
        proc = str(ev.get("process") or "").lower()
        title = str(ev.get("title") or "").lower()
        fg = ev.get("foreground") or {}
        fg_proc = str(fg.get("process") or "").lower()
        fg_title = str(fg.get("title") or "").lower()
        epoch = ev.get("epoch")

        is_dota = "dota" in proc or "dota" in title or "dota" in fg_proc or "dota" in fg_title

        if is_dota:
            if current_start is None:
                current_start = epoch
            prev_epoch = epoch
        else:
            if current_start is not None and epoch is not None and prev_epoch is not None:
                # If gap is small, keep going, else close interval
                if epoch - prev_epoch > 60:
                    dota_intervals.append((current_start, prev_epoch))
                    current_start = None
                    prev_epoch = None

    if current_start is not None and prev_epoch is not None:
        dota_intervals.append((current_start, prev_epoch))

    total_dota_sec = sum(end - start for start, end in dota_intervals if end > start)
    return total_dota_sec, dota_intervals, events


def check_steam_user_playtime():
    steam_userdata = pathlib.Path("C:/Program Files (x86)/Steam/userdata")
    results = {}
    if not steam_userdata.exists():
        return results

    for vdf in steam_userdata.rglob("localconfig.vdf"):
        try:
            content = vdf.read_text(encoding="utf-8", errors="replace")
            # Find "570" section
            m = re.search(r'\"570\"\s*\{([^}]+)\}', content)
            if m:
                block = m.group(1)
                data = {}
                for line in block.splitlines():
                    kv = re.findall(r'\"([^\"]+)\"\s*\"([^\"]+)\"', line)
                    for k, v in kv:
                        data[k] = v
                user_id = vdf.parent.parent.name
                results[user_id] = data
        except Exception:
            pass

    return results


def check_bam_registry():
    # Background Activity Moderator stores execution times
    dota_times = []
    try:
        base_path = r"SYSTEM\CurrentControlSet\Services\bam\State\UserSettings"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_path) as root_key:
            num_subkeys = winreg.QueryInfoKey(root_key)[0]
            for i in range(num_subkeys):
                sid = winreg.EnumKey(root_key, i)
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{base_path}\\{sid}") as user_key:
                        num_vals = winreg.QueryInfoKey(user_key)[1]
                        for j in range(num_vals):
                            name, val, _ = winreg.EnumValue(user_key, j)
                            if "dota" in name.lower() and isinstance(val, bytes) and len(val) >= 8:
                                ft = int.from_bytes(val[:8], "little")
                                # Convert Windows FILETIME to epoch
                                # 100-nanosecond intervals since Jan 1, 1601 UTC
                                epoch = (ft - 116444736000000000) / 10000000.0
                                if epoch > 0:
                                    dt = datetime.datetime.fromtimestamp(epoch).astimezone()
                                    dota_times.append((dt, name))
                except Exception:
                    pass
    except Exception:
        pass
    return dota_times


def main():
    print("=" * 65)
    print(" 🎮 АНАЛИЗ ВРЕМЕНИ В DOTA 2 vs ОБЩЕЕ ВРЕМЯ ЗА ПК")
    print("=" * 65)

    # 1. Total PC usage from Windows Event Log
    from calculate_monthly_usage import analyze_usage
    daily_seconds, intervals = analyze_usage()
    total_pc_hours = sum(daily_seconds.values()) / 3600.0

    # 2. Activity logs Dota time
    dota_log_sec, dota_intervals, events = analyze_activity_logs()
    dota_log_hours = dota_log_sec / 3600.0

    # 3. Steam playtime
    steam_info = check_steam_user_playtime()
    print("\n1. Данные из Steam:")
    dota_playtime_minutes = 0
    dota_2weeks_minutes = 0
    dota_last_played = None

    for uid, data in steam_info.items():
        lp = data.get("LastPlayed")
        pt = data.get("Playtime")
        p2w = data.get("Playtime2weeks") or data.get("playtime_2weeks")
        print(f" • Аккаунт Steam ID: {uid}")
        if pt:
            dota_playtime_minutes = max(dota_playtime_minutes, int(pt))
            print(f"   - Общее время в Dota 2: {int(pt) / 60.0:.1f} часов")
        if p2w:
            dota_2weeks_minutes = max(dota_2weeks_minutes, int(p2w))
            print(f"   - За последние 2 недели: {int(p2w) / 60.0:.1f} часов")
        if lp:
            dota_last_played = datetime.datetime.fromtimestamp(int(lp)).astimezone()
            print(f"   - Последний запуск: {dota_last_played.strftime('%d.%m.%Y %H:%M')}")

    # 4. Windows BAM registry
    bam_times = check_bam_registry()
    if bam_times:
        print("\n2. Реестр активности Windows (BAM):")
        for dt, exe in bam_times:
            print(f" • Последний запуск {exe.split(chr(92))[-1]}: {dt.strftime('%d.%m.%Y %H:%M')}")

    # 5. Activity log calculation
    print(f"\n3. Логи активности PCControlAgent (активные окна):")
    print(f" • Зафиксировано в фокусе Dota 2 за период логов: {dota_log_hours:.1f} ч ({int(dota_log_sec // 3600)} ч {int((dota_log_sec % 3600) // 60)} мин)")

    # 6. Combined calculation for month
    # If 2weeks playtime is known, estimate month (~2x or precise)
    # Total PC month = 257.2 hours
    estimated_dota_month_hours = (dota_2weeks_minutes / 60.0) * 2 if dota_2weeks_minutes > 0 else dota_log_hours
    if estimated_dota_month_hours == 0 and dota_log_hours > 0:
        estimated_dota_month_hours = dota_log_hours

    pc_without_dota = max(0.0, total_pc_hours - estimated_dota_month_hours)

    print("\n" + "=" * 65)
    print(" 📊 ИТОГОВЫЙ БАЛАНС ЗА МЕСЯЦ (30 ДНЕЙ):")
    print("=" * 65)
    print(f" • Общее время работы ПК:          {total_pc_hours:5.1f} ч (100%)")
    print(f" • Время в Dota 2:                 {estimated_dota_month_hours:5.1f} ч ({estimated_dota_month_hours / total_pc_hours * 100:.1f}%)" if total_pc_hours > 0 else "")
    print(f" • Чистое время за ПК БЕЗ Доты:    {pc_without_dota:5.1f} ч ({pc_without_dota / total_pc_hours * 100:.1f}%)" if total_pc_hours > 0 else "")
    print(f" • В среднем в день без Доты:      {pc_without_dota / 29.0:5.1f} ч/день")
    print("=" * 65)


if __name__ == "__main__":
    main()
