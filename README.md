<div align="center">

# 🖥️ PC Control

**Включить домашний ПК из Telegram, войти в Windows и собрать рабочий сетап с VPN.**

[![Version](https://img.shields.io/badge/Version-2.0-8B5CF6?style=for-the-badge)](./core/runtime_config.py)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Firmware](https://img.shields.io/badge/ESP8266-Arduino%20C%2B%2B-00979D?style=for-the-badge&logo=espressif&logoColor=white)](./hardware/esp8266/pc_remote_mqtt/pc_remote_mqtt.ino)
[![Tests](https://img.shields.io/badge/pytest-600%2B%20tests-22C55E?style=for-the-badge)](#-тестирование)
[![Docs](https://img.shields.io/badge/Обзор%20проекта-w1bbex.ru-0F172A?style=for-the-badge)](https://w1bbex.ru/ru/pc-control/)

</div>

**PC Control** объединяет Telegram-бота на Linux VDS, два Windows-агента и отдельный ESP8266. Микроконтроллер управляет питанием через разъём `PWR_SW` и работает, пока Windows выключена. Системный агент доступен до входа в Windows, а десктоп-агент управляет приложениями уже внутри пользовательской сессии.

---

## Что умеет PC Control

### От холодного старта до рабочего сетапа

В меню включения и разблокировки можно заранее выбрать Happ или INCY для рабочего сетапа либо сначала выбрать игру, а затем VPN. При перезагрузке с авторазблокировкой VPN для рабочего сетапа доступен на экране подтверждения. Системный агент ждёт экран входа, а после подтверждённой разблокировки десктоп-агент выполняет выбранный сценарий. Список приложений и команды запуска задаются в конфигурации. [Пример рабочего сетапа](./config.example.json)

Бот показывает прогресс каждого шага. Если подключение VPN не подтверждается, сценарий останавливается. Сетап можно отменить из карточки запуска, а после успешного рабочего запуска агент сворачивает окна, включая Codex. [Оркестрация сценариев](./vds_bot/workspace_runner.py)

### Автоматическая синхронизация иконок Windows с Telegram

Когда агент замечает активное приложение без сохранённой иконки, он извлекает значок из `.exe`, загружает его в отдельный набор **Telegram Custom Emoji** и кэширует ID. Бот подставляет настоящую иконку приложения рядом с активным окном и в дневной сводке. Пока значок AyuGram ещё не получен, дашборд показывает иконку Telegram. [Синхронизация и кэш](./core/app_emoji_manager.py)

### Дашборд и управление из чата

- **Состояние ПК и железа.** Активное окно, время сессии, загрузка и температуры CPU/GPU, память и диски. В чате доступны графики за периоды от часа до 30 дней и дневная сводка времени по приложениям. [Дашборд](./vds_bot/monitoring.py) · [Графики](./vds_bot/history.py)
- **Пульт Windows.** Скриншот всех мониторов, блокировка и выключение экрана, сон, перезагрузка с авторазблокировкой и выключение по таймеру. Если Windows не отвечает, аварийное выключение можно выполнить через реле ESP8266. [Управление питанием](./vds_bot/routers/power_router.py) · [Скриншоты](./desktop/os_events.py)
- **Консоль.** PowerShell и CMD с выбором пользовательского или системного агента. Бот возвращает в чат результат и вывод команды. [Консоль](./vds_bot/console_service.py)
- **Медиаплеер.** Трек, обложка при наличии, пауза, переключение и перемотка из Telegram. Громкость управляется отдельно. При настройке Яндекс Музыки можно ставить и снимать лайки из карточки плеера. [Плеер](./vds_bot/routers/player_router.py)

### Инструменты под конкретный ПК

- **Antigravity и Codex.** У Antigravity есть карточка профилей с квотами и переключением аккаунта с перезапуском IDE. Восстановление связи перебирает серверы INCY, проверяет доступ к Google по HTTPS и заново запускает IDE. Для Codex доступны три способа запуска с диагностикой появления окна. [Профили Antigravity](./vds_bot/routers/antigravity_router.py) · [Восстановление связи](./desktop/vpn_manager.py) · [Диагностика Codex](./desktop/codex_launch.py)
- **Физическая кнопка и ARGB.** ESP8266 распознаёт код Морзе из коротких и длинных нажатий и ограничивает число попыток. Жест разблокировки настраивается в Telegram. ESP8266 также служит контроллером 5 В ARGB-подсветки кулера. [Физическая кнопка](./vds_bot/physical_button.py) · [Прошивка ESP8266](./hardware/esp8266/pc_remote_mqtt/pc_remote_mqtt.ino)
- **Диагностика при обрывах связи.** Бот различает выключенный ПК, экран входа, задержку телеметрии и потерю связи с компонентами. ESP8266 пытается восстановить подключение, сохраняет сетевые события во flash и отправляет журнал на VDS после восстановления связи. [Состояния ПК](./vds_bot/health_engine.py) · [Диагностика ESP8266](./hardware/esp8266/README.md)

Проект собран на домашнем ПК с Windows 10 Enterprise LTSC, NodeMCU ESP8266 и личным Linux VDS. Для другого ПК потребуется адаптировать подключение реле, настройки Windows и список приложений. [Инженерный разбор и детали сборки](https://w1bbex.ru/ru/pc-control/)

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
2. **Системный агент Session 0 ([`boot_agent.py`](./boot_agent.py), [`system/`](./system/))** — задача планировщика под `NT AUTHORITY\SYSTEM`:
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
└── tests/                        # Модульные и интеграционные тесты (pytest)
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

Для синхронизации иконок агент использует `TELEGRAM_BOT_TOKEN` и `TELEGRAM_USER_ID`. Имя набора по умолчанию — `pc_apps_pack_by_<username бота>`, локальный кэш — `data/app_icon_cache.json`.

### 2. Координатор на Linux VDS

1. Скопируйте шаблоны окружения и сервиса: [`vds_bot/pc-control-bot.env.example`](./vds_bot/pc-control-bot.env.example) и [`vds_bot/pc-control-bot.service`](./vds_bot/pc-control-bot.service).
2. В каталоге `/opt/pc-control-bot` создайте окружение `venv` и установите зависимости из [`vds_bot/requirements.txt`](./vds_bot/requirements.txt), размещённого в каталоге развёртывания как `requirements.txt`.
   ```bash
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
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

- Более 600 автоматических тестов покрывают сценарии питания, состояния ПК, протоколы агентов, WTS/Win32, интерфейс бота и Python-модель логики ESP8266.
- Тесты запускаются с моками системных/сетевых вызовов; прошивка C++ и живой интерактивный вход на реальный `Winlogon` проверяются отдельно на целевом стенде.

---

## 🔒 Примечания по безопасности

- Все каналы между ESP8266, Windows-агентами и брокером защищены TLS; физический ввод на кнопке защищён лимитом попыток с сохранением состояния блокировки в EEPROM.
- Удалённая разблокировка **не отключает** стандартную аутентификацию Windows — используется настроенный пароль учётной записи, а скрипт ввода прерывается до нажатия `Enter` при невозможности корректного маппинга символов или отсутствии целевого стола `Winlogon`.
- **Режим скрытности ([`core/stealth_guard.py`](./core/stealth_guard.py))** предназначен только для локальной киоск-блокировки ввода/экрана во время удалённой сессии владельца: отключён по умолчанию, не прописывается в автозагрузку реестра, поддерживает аварийный локальный выход (`5× Escape` за 2 секунды) и проверяет доступность сети (`check_network_connectivity`) для автоматического выхода при потере связи с брокером.
- Репозиторий не содержит реальных токенов, паролей, ключей и приватных конфигураций: все файлы `.env`, `secrets.h`, `data/`, `logs/` и `*.db` исключены через [`.gitignore`](./.gitignore).
