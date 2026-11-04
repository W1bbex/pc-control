#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SafeKids: Терминальный визуализатор био-отпечатков пальцев (ASCII Hand)
Слушает COM3 и в реальном времени рисует руку со стрелкой на распознанный палец.
"""

import sys
import json
import time
import os

try:
    import serial
except ImportError:
    print("Ошибка: установите pyserial через: pip install pyserial")
    sys.exit(1)

# ASCII Hand Templates with pointers
ASCII_HAND = {
    "IDLE": """
             [MID]
       [IND]   |   [RNG]
         |     |     |   [PNK]
         |     |     |     |
        _.-'''''''''''-._
      .'                 '.
     /      ЛАДОНЬ         \\
    |                       |
    |   [THUMB]             |     >>> ОЖИДАНИЕ КАСАНИЯ...
    |                       |     Держите D6 в ладони, коснитесь D5 пальцем
     \\                     /
      '.                 .'
        '---.........---'
""",
    "THUMB": """
             [MID]
       [IND]   |   [RNG]
         |     |     |   [PNK]
         |     |     |     |
        _.-'''''''''''-._
      .'                 '.
     /      ЛАДОНЬ         \\
    |                       |
 ===|==>[ БОЛЬШОЙ ПАЛЕЦ ]   | <=== [ОБНАРУЖЕН: БОЛЬШОЙ ПАЛЕЦ]
    |                       |
     \\                     /
      '.                 .'
        '---.........---'
""",
    "INDEX": """
             [MID]
  ====>[ УКАЗАТЕЛЬНЫЙ ]    [RNG]
         |     |     |   [PNK]
         |     |     |     |
        _.-'''''''''''-._
      .'                 '.
     /      ЛАДОНЬ         \\
    |                       |
    |   [THUMB]             |      >>> ОБНАРУЖЕН: УКАЗАТЕЛЬНЫЙ ПАЛЕЦ!
    |                       |
     \\                     /
      '.                 .'
        '---.........---'
""",
    "MIDDLE": """
       ======>[ СРЕДНИЙ ПАЛЕЦ ]<======
       [IND]   |   [RNG]
         |     |     |   [PNK]
         |     |     |     |
        _.-'''''''''''-._
      .'                 '.
     /      ЛАДОНЬ         \\
    |                       |
    |   [THUMB]             |      >>> ОБНАРУЖЕН: СРЕДНИЙ ПАЛЕЦ!
    |                       |
     \\                     /
      '.                 .'
        '---.........---'
""",
    "RING": """
             [MID]
       [IND]   |   =====>[ БЕЗЫМЯННЫЙ ПАЛЕЦ ]<=====
         |     |     |   [PNK]
         |     |     |     |
        _.-'''''''''''-._
      .'                 '.
     /      ЛАДОНЬ         \\
    |                       |
    |   [THUMB]             |      >>> ОБНАРУЖЕН: БЕЗЫМЯННЫЙ ПАЛЕЦ!
    |                       |
     \\                     /
      '.                 .'
        '---.........---'
""",
    "PINKY": """
             [MID]
       [IND]   |   [RNG]
         |     |     |   =====>[ МИЗИНЕЦ ]<=====
         |     |     |     |
        _.-'''''''''''-._
      .'                 '.
     /      ЛАДОНЬ         \\
    |                       |
    |   [THUMB]             |      >>> ОБНАРУЖЕН: МИЗИНЕЦ!
    |                       |
     \\                     /
      '.                 .'
        '---.........---'
"""
}

def main():
    port = "COM3"
    baud = 115200

    print(f"[*] Подключение к {port} ({baud} бод)...")
    try:
        ser = serial.Serial(port, baud, timeout=1)
        time.sleep(1.5)
        print("[+] Порт успешно открыт!")
    except Exception as e:
        print(f"[-] Не удалось открыть {port}: {e}")
        return

    last_state = None

    try:
        while True:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                continue

            if line.startswith('{') and line.endswith('}'):
                try:
                    data = json.loads(line)
                    event = data.get("event")

                    if event == "TOUCH":
                        finger_name = data.get("finger", "")
                        cycles = data.get("cycles", 0)
                        us = data.get("us", 0.0)
                        hum = data.get("hum", 0)

                        # Match key
                        key = "INDEX"
                        if "Большой" in finger_name: key = "THUMB"
                        elif "Указательный" in finger_name: key = "INDEX"
                        elif "Средний" in finger_name: key = "MIDDLE"
                        elif "Безымянный" in finger_name: key = "RING"
                        elif "Мизинец" in finger_name: key = "PINKY"

                        if key != last_state:
                            os.system('cls' if os.name == 'nt' else 'clear')
                            print("=" * 65)
                            print(f"  SafeKids: ДЕТЕКЦИЯ ПАЛЬЦЕВ | RC: {us:.1f} мкс | Такты: {cycles} | 50Гц: {hum}")
                            print("=" * 65)
                            print(ASCII_HAND.get(key, ASCII_HAND["IDLE"]))
                            last_state = key

                    elif event == "IDLE":
                        if last_state != "IDLE":
                            os.system('cls' if os.name == 'nt' else 'clear')
                            print("=" * 65)
                            print("  SafeKids: ОЖИДАНИЕ КАСАНИЯ (Сенсор свободен)")
                            print("=" * 65)
                            print(ASCII_HAND["IDLE"])
                            last_state = "IDLE"

                except json.JSONDecodeError:
                    pass
            elif line.startswith(">>"):
                print(line)

    except KeyboardInterrupt:
        print("\n[*] Завершение работы...")
    finally:
        ser.close()

if __name__ == "__main__":
    main()
