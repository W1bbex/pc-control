#!/usr/bin/env python3
"""
Fast Startup (Hybrid Boot) Diagnostic & Inspection Tool.
Reads Windows Event Log and Registry to report boot type and Hiberboot status.
Does NOT modify any Windows settings.
"""

import subprocess
import sys
import winreg
import xml.etree.ElementTree as ET

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def check_fast_startup_registry():
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Power",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "HiberbootEnabled")
            return bool(val), val
    except Exception as e:
        return None, str(e)


def check_last_boot_event():
    try:
        out = subprocess.run(
            [
                "wevtutil", "qe", "System",
                "/q:*[System[Provider[@Name='Microsoft-Windows-Kernel-Boot'] and (EventID=27 or EventID=20)]]",
                "/rd:true", "/c:1", "/f:RenderedXml",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        stdout_text = (out.stdout or "").strip()
        if out.returncode != 0 or not stdout_text:
            return None, "wevtutil returned no events"

        root = ET.fromstring(stdout_text)
        data = {}
        for el in root.iter("{http://schemas.microsoft.com/win/2004/08/events/event}Data"):
            name = el.attrib.get("Name")
            if name:
                data[name] = el.text

        boot_raw = str(data.get("BootType", "")).strip()
        boot_code = int(boot_raw, 0) if boot_raw else None
        return boot_code, data
    except Exception as e:
        return None, str(e)


def main():
    print("=" * 60)
    print(" PC CONTROL — ДИАГНОСТИКА FAST STARTUP (БЫСТРЫЙ ЗАПУСК)")
    print("=" * 60)

    # 1. Registry status
    enabled, raw_val = check_fast_startup_registry()
    print(f"\n1. Настройка в реестре (HiberbootEnabled):")
    if enabled is True:
        print("   [!] Fast Startup ВКЛЮЧЁН (HiberbootEnabled = 1)")
    elif enabled is False:
        print("   [✓] Fast Startup ВЫКЛЮЧЕН (HiberbootEnabled = 0)")
    else:
        print(f"   [?] Не удалось прочесть реестр: {raw_val}")

    # 2. Last Boot Event
    boot_code, data = check_last_boot_event()
    print(f"\n2. Последнее событие загрузки (Kernel-Boot Event ID 27):")
    if boot_code == 0:
        print("   [✓] Тип: 0x0 — Cold Boot (полноценный чистый запуск)")
    elif boot_code == 1:
        print("   [!] Тип: 0x1 — Hybrid Boot / Fast Startup (гибридный запуск)")
        print("       (Ядро Windows восстановлено из файла гибернации, аптайм ядра сохраняется)")
    elif boot_code == 2:
        print("   [i] Тип: 0x2 — Resume (выход из гибернации)")
    else:
        print(f"   [?] Код загрузки: {boot_code}, данные: {data}")

    # 3. Information and instructions
    print("\n" + "=" * 60)
    print("ИНФОРМАЦИЯ И РЕКОМЕНДАЦИИ:")
    print("=" * 60)
    print("При включённом Fast Startup (HiberbootEnabled=1):")
    print(" - 'Завершение работы' в Windows не выгружает ядро, а сохраняет его на диск.")
    print(" - При включении аптайм ядра может показывать десятки часов.")
    print(" - Консольная сессия может оставаться в заблокированном состоянии дольше обычного.")
    print("\nДля отключения Fast Startup (требуются права Администратора):")
    print(" 1. Запустить cmd от имени Администратора.")
    print(" 2. Выполнить команду:")
    print("    powercfg /h off")
    print("    ИЛИ в реестре:")
    print('    reg add "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power" /v HiberbootEnabled /t REG_DWORD /d 0 /f')
    print("=" * 60)


if __name__ == "__main__":
    main()
