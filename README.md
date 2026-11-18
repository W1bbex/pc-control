<div align="center">

# 🖥️ PC Control

**Распределённая система внеполосного управления, разблокировки Windows через Session 0 и телеметрии рабочей станции через Telegram.**

[![Version](https://img.shields.io/badge/Version-2.0-8B5CF6?style=for-the-badge)](./core/runtime_config.py)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Firmware](https://img.shields.io/badge/ESP8266-Bare--Metal%20C%2B%2B-00979D?style=for-the-badge&logo=espressif&logoColor=white)](./hardware/esp8266/pc_remote_mqtt/pc_remote_mqtt.ino)
[![Tests](https://img.shields.io/badge/tests-636%20passed-22C55E?style=for-the-badge)](#-тестирование)
[![Docs](https://img.shields.io/badge/Обзор%20проекта-w1bbex.ru-0F172A?style=for-the-badge)](https://w1bbex.ru/ru/pc-control/)

</div>

> [!NOTE]
> **Pet-проект для ознакомления и изучения.**  
> Система написана под конкретный домашний билд (NodeMCU ESP8266, материнская плата без 3-pin 5V ARGB, Windows 10 Enterprise LTSC и личный Linux VDS), а не как универсальный «коробочный» продукт. Исходный код открыт для изучения архитектуры, схемотехники и инженерных приёмов — использовать как готовое решение «в один клик» не рекомендуется без адаптации под своё железо и окружение.

---

## 💡 Что это и зачем

**PC Control** связывает микроконтроллер **ESP8266**, системную службу **Windows Session 0**, пользовательского агента **Session 1+** и **Telegram-координатор на Linux VDS** поверх **MQTT over TLS**.

Ключевые возможности:
- ⚡ **Холодное включение и жёсткий сброс без Wake-on-LAN:** оптическое реле на материнской плате (`PWR_SW`), физическая кнопка с аппаратным кодом Морзе и анти-брутфорсом в EEPROM, а также автономное управление 5V ARGB-подсветкой кулера.
- 🔐 **Удалённый вход в Windows без USB HID-донгла:** агент `Session 0` отслеживает готовность консоли через WTS API и запускает ввод настроенного пароля на защищённом столе `Winlogon`.
- 📊 **Телеметрия и аналитика:** датчики температуры, частот и питания через `LibreHardwareMonitorLib` (`C#/.NET`), трекинг активных окон и сетевого трафика в SQLite WAL, серверная генерация графиков `Matplotlib`.
- 🎵 **Мультимедиа, скриншоты и лаунчер:** управление системным плеером через WinRT GSMTC и PyCaw, сжатые скриншоты всех мониторов, экспорт иконок `.exe`/`.lnk` в Telegram Custom Emoji, оркестрация VPN-клиентов и быстрый запуск рабочих пространств.

> Инженерный разбор проекта и деталей сборки: [w1bbex.ru/ru/pc-control](https://w1bbex.ru/ru/pc-control/).

---

## 🏗️ Архитектура

```text
Telegram UI  <──HTTPS──>  VDS Bot & FSM (Linux, Python)
                              │
                        MQTT TLS (:8883)
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
      ESP8266 MCU      Session 0 Agent     Session 1+ Agent
   (PWR_SW / Morse     (SYSTEM / WTS /     (GSMTC / PyCaw /
    / ARGB / TLS)     Winlogon Unlock)   Launcher / Sensors)
```

1. **Аппаратный узел ([`hardware/esp8266/`](./hardware/esp8266/README.md))** — прошивка `pc_remote_mqtt.ino` для NodeMCU ESP8266 (160 MHz):
   - замыкание `PWR_SW` через реле (`GPIO5`), аппаратные прерывания кнопки в `IRAM_ATTR` (`GPIO14`), ARGB-лента (`GPIO4`);
   - защита от подбора в EEPROM, MQTT поверх BearSSL TLS 1.2 и кольцевой журнал сетевой диагностики в LittleFS.
2. **Системный агент Session 0 ([`boot_agent.py`](./boot_agent.py), [`system/`](./system/))** — служба под `NT AUTHORITY\SYSTEM`:
   - различает холодный старт и Fast Startup, отслеживает консоль через `WTS API`;
   - при команде разблокировки запускает [`scripts/type_password.py`](./scripts/type_password.py) на столе `Winlogon` (проверка раскладки, Caps Lock и `VkKeyScanW` до нажатия Enter).
3. **Пользовательский агент Session 1+ ([`agent.py`](./agent.py), [`desktop/`](./desktop/), [`core/`](./core/))** — фоновый демон текущей сессии:
   - опрос сенсоров (`hardware/sensors/AdminSensorCollector.exe`), трекинг активности окон (`SQLite WAL`);
   - WinRT GSMTC + CoreAudio-пульт, GDI-экстрактор иконок, VPN-роутинг, скриншоты, патчи устойчивости `paho-mqtt` для Windows (`WinError 10054/1236`).
4. **Облачный координатор ([`vds_bot/`](./vds_bot/))** — Telegram-бот для Linux VDS:
   - конечный автомат питания (`power_flow.py`), фильтрация устаревших/retained-статусов и 20-секундное окно тишины (`health_engine.py`);
   - маршрутизаторы команд, графики нагрузки на `Matplotlib` и атомарный деплой с откатом ([`scripts/deploy_vds.py`](./scripts/deploy_vds.py)).

---

## 📁 Структура репозитория

```text
pc-control/
├── agent.py                      # Точка входа пользовательского агента (Session 1+)
├── boot_agent.py                 # Точка входа системного агента (Session 0 / SYSTEM)
├── config.example.json           # Шаблон несекретных настроек агентов и лаунчера
├── core/                         # Общая конфигурация (.env), MQTT-патчи, криптография, SQLite, медиа
├── desktop/                      # Модули пользовательской сессии (телеметрия, сенсоры, VPN, launcher)
├── system/                       # Модули системной сессии (WTS, процессы, аппаратный мониторинг)
├── vds_bot/                      # Облачный Telegram-бот, FSM питания, health_engine, роутеры, systemd-юнит
├── hardware/
│   ├── esp8266/                  # Прошивка Arduino/ESP8266, диагностика и secrets.h.example
│   └── sensors/                  # Сборщик телеметрии (.NET + LibreHardwareMonitorLib)
├── scripts/                      # Скрипты установки служб, ввода Winlogon и VDS-деплоя
├── resources/                    # Маппинг кастомных эмодзи и ресурсов UI
└── tests/                        # Набор из 636 модульных и интеграционных тестов (pytest)
```

---

## ⚙️ Конфигурация и запуск

Проект разделяет **секреты** (`.env`, `secrets.h`) и **операционные настройки** (`config.json`).

### 1. Секреты и конфиг на Windows-хосте

1. Скопируйте шаблон конфигурации и заполните пути к нужным приложениям и параметры брокера:
   ```powershell
   copy config.example.json config.json
   ```
2. Создайте `.env` в корне репозитория с секретами:
   ```env
   MQTT_HOST=mqtt.example.com
   MQTT_PORT=8883
   MQTT_USERNAME=your_mqtt_user
   MQTT_PASSWORD=your_mqtt_password
   TELEGRAM_BOT_TOKEN=your_telegram_bot_token
   TELEGRAM_USER_ID=123456789
   WINDOWS_PASSWORD=your_windows_password
   YANDEX_MUSIC_TOKEN=
   YANDEX_MUSIC_UID=
   ```
3. Установите зависимости в виртуальное окружение и при необходимости зарегистрируйте агенты через скрипты в [`scripts/install/`](./scripts/install/).

Десктоп-агент автоматически загружает иконку приложения в Telegram, когда впервые видит его активное окно. По умолчанию он использует отдельный набор `pc_apps_pack_by_<username бота>` и сохраняет соответствия в `data/app_icon_cache.json`. Пока иконка AyuGram загружается, дашборд показывает иконку Telegram.

### 2. Координатор на Linux VDS

1. Скопируйте шаблоны окружения и сервиса: [`vds_bot/pc-control-bot.env.example`](./vds_bot/pc-control-bot.env.example) и [`vds_bot/pc-control-bot.service`](./vds_bot/pc-control-bot.service).
2. Установите зависимости из [`vds_bot/requirements.txt`](./vds_bot/requirements.txt):
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r vds_bot/requirements.txt
   ```
3. Для выката обновлений на сервер с локальной машины используется [`scripts/deploy_vds.py`](./scripts/deploy_vds.py) с проверкой SHA-256 манифеста и автоматическим откатом.

### 3. Прошивка ESP8266

1. Скопируйте [`hardware/esp8266/pc_remote_mqtt/secrets.h.example`](./hardware/esp8266/pc_remote_mqtt/secrets.h.example) в `secrets.h` и укажите параметры Wi-Fi и MQTT.
2. Соберите прошивку через Arduino CLI:
   ```bash
   arduino-cli compile --fqbn esp8266:esp8266:nodemcuv2 hardware/esp8266/pc_remote_mqtt
   ```
   Подробности по диагностике и восстановлению описаны в [`hardware/esp8266/README.md`](./hardware/esp8266/README.md).

---

## 🧪 Тестирование

Установите зависимости разработки из [`requirements-dev.txt`](./requirements-dev.txt) и запустите весь набор проверок из корня:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

- Суммарный набор: **636** автоматических тестов (FSM питания, `health_engine`, протоколы агентов, WTS/Win32-адаптеры, маршрутизаторы Telegram UI, модель логики ESP8266 и регрессионные проверки).
- Тесты запускаются с моками системных/сетевых вызовов; прошивка C++ и живой интерактивный вход на реальный `Winlogon` проверяются отдельно на целевом стенде.

---

## 🔒 Примечания по безопасности

- Все каналы между ESP8266, Windows-агентами и брокером защищены TLS; физический ввод на кнопке защищён лимитом попыток с сохранением состояния блокировки в EEPROM.
- Удалённая разблокировка **не отключает** стандартную аутентификацию Windows — используется настроенный пароль учётной записи, а скрипт ввода прерывается до нажатия `Enter` при невозможности корректного маппинга символов или отсутствии целевого стола `Winlogon`.
- **Режим скрытности ([`core/stealth_guard.py`](./core/stealth_guard.py))** предназначен только для локальной киоск-блокировки ввода/экрана во время удалённой сессии владельца: отключён по умолчанию, не прописывается в автозагрузку реестра, поддерживает аварийный локальный выход (`5× Escape` за 2 секунды) и проверяет доступность сети (`check_network_connectivity`) для автоматического выхода при потере связи с брокером.
- Репозиторий не содержит реальных токенов, паролей, ключей и приватных конфигураций: все файлы `.env`, `secrets.h`, `data/`, `logs/` и `*.db` исключены через [`.gitignore`](./.gitignore).
