#!/usr/bin/env python3

"""Telemetry formatting, history and threshold alerts for the Telegram UI."""

import html
import logging
import time
from pathlib import Path

from history import TelemetryHistory

LOG = logging.getLogger("pc-control-bot.monitoring")

def _load_icons():
    import json
    for p in [
        Path(__file__).resolve().parent / "resources" / "icons.json",
        Path(__file__).resolve().parent.parent / "resources" / "icons.json",
    ]:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}

ICONS = _load_icons()

EMOJI_HEADER_PC = ICONS.get("pc", "5330499003580721030")        # 💻 PC Control
EMOJI_OS_WINDOWS = ICONS.get("windows", "5332280534540264957")    # 🪟 Windows OS
EMOJI_OS_LINUX = ICONS.get("linux", "5332695978136874502")        # 🐧 Linux OS
EMOJI_SECTION_SESSION = ICONS.get("user", "5330478284658488376")  # 👤 Сессия
EMOJI_STATUS_ACTIVE = ICONS.get("status_active", "5345809221961167372")    # 🟢 Зеленый пульсирующий круг
EMOJI_STATUS_INACTIVE = ICONS.get("status_inactive", "5345825096160291312")  # 🔴 Красный пульсирующий круг
EMOJI_DEFAULT_APP = ICONS.get("apps", "5346196795514989290")      # 🪟 Windows Explorer / Window icon
EMOJI_PLAYER_PLAY = ICONS.get("play", "5330015871299527534")      # ▶️ Minimalist play
EMOJI_PLAYER_PAUSE = ICONS.get("pause", "5330570905628222348")     # ⏸️ Minimalist pause
EMOJI_PLAYER_HEART = ICONS.get("tag_heart_liked", ICONS.get("heart", "5346267379007530615"))     # ❤️ Heart liked
EMOJI_SECTION_HARDWARE = ICONS.get("power", "5346321083278600717") # ⚡️ Железо
EMOJI_SECTION_DISKS = ICONS.get("folder", "5330394360997517972")    # 📁 Накопители


def format_duration(seconds):
    if seconds is None:
        return "н/д"
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "н/д"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if seconds or not parts:
        parts.append(f"{seconds} сек")
    return " ".join(parts)


def format_number(value, suffix="", digits=0):
    if value is None:
        return "н/д"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "н/д"
    return f"{number:.{digits}f}{suffix}" if digits else f"{int(round(number))}{suffix}"


class MonitoringManager:
    def __init__(self, telegram_ui, mqtt_gateway, history_db_path=None):
        self.ui = telegram_ui
        self.mqtt = mqtt_gateway
        db_path = history_db_path or Path("/var/lib/pc-control-bot/telemetry_history.db")
        self.history = TelemetryHistory(db_path)
        self.latest_telemetry = None
        self.latest_received_at = 0.0
        self.latest_desktop_at = 0.0
        self.alerts = {}

    def process_telemetry(self, telemetry_data):
        if not isinstance(telemetry_data, dict):
            return []

        mode = telemetry_data.get("agent_mode", "desktop")
        now = time.time()
        self.latest_received_at = now

        if mode == "desktop":
            self.latest_desktop_at = now
            self.latest_telemetry = telemetry_data
        elif mode == "system":
            # If desktop agent has sent rich telemetry recently (<20s), preserve desktop session and GUI fields
            if self.latest_telemetry and (now - getattr(self, "latest_desktop_at", 0.0)) < 20.0:
                merged = dict(self.latest_telemetry)
                for key in ("cpu", "gpu", "memory", "disks", "sensors", "network"):
                    if key in telemetry_data and telemetry_data[key]:
                        merged[key] = telemetry_data[key]
                if telemetry_data.get("uptime_seconds"):
                    merged["uptime_seconds"] = telemetry_data["uptime_seconds"]
                self.latest_telemetry = merged
            else:
                self.latest_telemetry = telemetry_data
        else:
            self.latest_telemetry = telemetry_data

        self.history.insert(self.latest_telemetry)
        return self.check_alerts(self.latest_telemetry)

    def check_alerts(self, telemetry):
        cpu_temp = (telemetry.get("cpu") or {}).get("temp_c")
        gpu_temp = (telemetry.get("gpu") or {}).get("temp_c")
        alerts_to_send = []
        now = time.time()
        for name, label, temp in (("cpu_temp", "ЦП", cpu_temp), ("gpu_temp", "GPU", gpu_temp)):
            try:
                too_hot = float(temp) > 85
            except (TypeError, ValueError):
                too_hot = False
            if too_hot and now - self.alerts.get(name, 0) > 600:
                alerts_to_send.append(f"⚠️ <b>Внимание:</b> высокая температура {label} ({float(temp):.0f}°C)!")
                self.alerts[name] = now
        return alerts_to_send

    def telemetry_age(self):
        if not self.latest_received_at:
            return None
        return max(0.0, time.time() - self.latest_received_at)

    def _session_lines(self, telemetry):
        is_shutting_down = getattr(self, "is_shutting_down", None)
        if is_shutting_down and is_shutting_down():
            return "⏳ Выключение...", "н/д"

        session = self.mqtt.system_session
        if session in {"locked", "login_screen"}:
            return f'<tg-emoji emoji-id="{EMOJI_STATUS_INACTIVE}">🔴</tg-emoji> Заблокирован', "н/д"

        desk_status = getattr(self.mqtt, "desktop_status", None)
        age = self.telemetry_age()
        is_desk_fresh = (age is not None and age <= 90.0)
        has_desk_activity = bool(telemetry and (telemetry.get("session_seconds") is not None or telemetry.get("active_application")))

        is_unlocked = (session == "unlocked") or (
            session in ("unknown", None, "") and (desk_status == "online" or (is_desk_fresh and has_desk_activity))
        )

        if is_unlocked:
            if telemetry and telemetry.get("user_active", False):
                status_icon = f'<tg-emoji emoji-id="{EMOJI_STATUS_ACTIVE}">🟢</tg-emoji> В сети'
            else:
                clock_eid = ICONS.get("clock", "5330473500064912704")
                status_icon = f'<tg-emoji emoji-id="{clock_eid}">🟡</tg-emoji> В сети (AFK)'
            dur = telemetry.get("session_seconds") if telemetry else None
            return status_icon, format_duration(dur)

        return f'<tg-emoji emoji-id="{EMOJI_STATUS_INACTIVE}">🔴</tg-emoji> Офлайн', "н/д"

    def get_os_type(self, telemetry=None) -> str:
        """Detect operating system ('windows', 'linux', or 'unknown') from telemetry or environment."""
        telem = telemetry if telemetry is not None else (self.latest_telemetry or {})

        # 1. Explicit os / os_type / platform_system field
        explicit_os = telem.get("os") or telem.get("os_type") or telem.get("platform_system")
        if explicit_os:
            val = str(explicit_os).lower()
            if "win" in val:
                return "windows"
            if "linux" in val:
                return "linux"

        # 2. Check "windows" field in telemetry (agent.py and boot_agent.py emit "windows": platform.platform())
        win_field = telem.get("windows")
        if win_field:
            if isinstance(win_field, str) and ("win" in win_field.lower() or "10" in win_field or "11" in win_field):
                return "windows"
            elif bool(win_field):
                return "windows"

        # 3. Check "platform" field
        plat = telem.get("platform")
        if plat:
            val = str(plat).lower()
            if "win" in val:
                return "windows"
            if "linux" in val:
                return "linux"

        # 4. Check disk mountpoints or devices
        disks = telem.get("disks") or []
        if isinstance(disks, list):
            for d in disks:
                dev = str(d.get("drive", "") or d.get("device", "") or d.get("mountpoint", "")).lower()
                if dev.startswith(("/", "/dev/")):
                    return "linux"
                if len(dev) >= 2 and dev[1] == ":" and dev[0].isalpha():
                    return "windows"

        # 5. Default fallback for PC Control host
        return "windows"

    def header_os_tag(self, telemetry=None) -> str:
        """Return adaptive custom emoji tag for OS in the header."""
        os_type = self.get_os_type(telemetry)
        if os_type == "windows":
            eid = ICONS.get("windows") or EMOJI_OS_WINDOWS
            return f'<tg-emoji emoji-id="{eid}">🪟</tg-emoji>'
        elif os_type == "linux":
            eid = ICONS.get("linux") or EMOJI_OS_LINUX
            return f'<tg-emoji emoji-id="{eid}">🐧</tg-emoji>'
        else:
            eid = ICONS.get("pc") or EMOJI_HEADER_PC
            return f'<tg-emoji emoji-id="{eid}">💻</tg-emoji>'

    def dashboard_text(self, media_state=None):
        """Build the full dashboard from the nested schema emitted by agent.py."""
        pc_state = self.mqtt.health.evaluate_pc_state(telemetry_age=self.telemetry_age())

        if pc_state.state in ("UNREACHABLE", "SHUTDOWN_OBSERVED"):
            header = "⚪" if pc_state.state == "UNREACHABLE" else f'<tg-emoji emoji-id="{EMOJI_STATUS_INACTIVE}">🔴</tg-emoji>'
            last_telem_info = ""
            if self.latest_telemetry:
                ts = self.latest_telemetry.get("ts") or self.latest_telemetry.get("timestamp")
                if ts:
                    diff = int(time.time() - float(ts))
                    last_telem_info = f"\n\n<i>Последняя телеметрия получена {format_duration(diff)} назад.</i>"

            os_tag = self.header_os_tag()
            os_type = self.get_os_type()
            os_label = "Windows" if os_type == "windows" else ("Linux" if os_type == "linux" else "Система")
            return (
                f'{os_tag} <b>PC Control</b> — {header}\n'
                f"{os_label}: {pc_state.label}\n\n"
                f"<i>{pc_state.reason}</i>{last_telem_info}\n\n"
                f"<i>Агенты: System {pc_state.system_status} | Desktop {pc_state.desktop_status} | ESP {pc_state.esp_status}</i>"
            )

        telemetry = self.latest_telemetry or {}
        cpu = telemetry.get("cpu") or {}
        gpu = telemetry.get("gpu") or {}
        memory = telemetry.get("memory") or {}
        disks = telemetry.get("disks") or []
        status_icon, session_duration = self._session_lines(telemetry)
        active_app_raw = telemetry.get("active_application")
        active_title = telemetry.get("active_window_title")
        if not active_app_raw:
            active_application = "Рабочий стол"
        else:
            app_lower = str(active_app_raw).lower()
            telegram_clients = ("telegram", "ayugram", "kotatogram", "64gram", "unigram")
            if any(c in app_lower for c in telegram_clients) and active_title:
                import re
                clean = active_title.strip()
                if "@" in clean or re.match(r"^\(\d+\)", clean):
                    clean = re.sub(r"^\(\d+\)\s*", "", clean)
                    clean = re.sub(r"\s+@\s+[^!]+!\s*\(\d+\)$", "", clean)
                    clean = re.sub(r"\s+@\s+[^!]+!$", "", clean)
                    active_application = html.escape(f"Telegram — {clean}") if clean else html.escape(str(active_app_raw))
                elif clean.lower() in ("telegram", "ayugram"):
                    active_application = "Telegram"
                else:
                    active_application = html.escape(str(active_app_raw))
            else:
                active_application = html.escape(str(active_app_raw))

        cpu_text = f"{format_number(cpu.get('load_percent'), '%')} · {format_number(cpu.get('temp_c'), '°C')}"
        gpu_parts = [format_number(gpu.get("load_percent"), "%"), format_number(gpu.get("temp_c"), "°C")]
        if gpu.get("power_w") is not None:
            gpu_parts.append(format_number(gpu.get("power_w"), " Вт", 1))
        ram_text = (
            f"{format_number(memory.get('used_percent'), '%', 1)} · "
            f"{format_number(memory.get('used_gb'), ' ГБ', 1)} / "
            f"{format_number(memory.get('total_gb'), ' ГБ', 1)}"
        )

        disk_lines = []
        for disk in disks:
            drive = html.escape(str(disk.get("drive") or "Диск"))
            disk_lines.append(
                f" ├ [{drive}] {format_number(disk.get('used_percent'), '%', 1)}"
                f" · свободно {format_number(disk.get('free_gb'), ' ГБ', 1)}"
            )
        if disk_lines:
            disk_lines[-1] = disk_lines[-1].replace(" ├", " └", 1)
        else:
            disk_lines.append(" └ Нет свежих данных")

        telemetry_note = ""
        is_shutting_down = getattr(self, "is_shutting_down", None)
        shutting_down = is_shutting_down() if is_shutting_down else False
        age = self.telemetry_age()

        if shutting_down and (age is None or age > 10):
            telemetry_note = "\n\n<i>⏳ Выполняется выключение ПК...</i>"
        elif age is None:
            telemetry_note = "\n\n<i>⏳ Ожидаю получение первой телеметрии...</i>"
        elif age > 90:
            telemetry_note = f"\n\n<i><tg-emoji emoji-id=\"{EMOJI_STATUS_INACTIVE}\">❌</tg-emoji> Телеметрия устарела ({format_duration(age)} назад).</i>"
        elif age > 30:
            telemetry_note = f"\n\n<i>⚠️ Данные задерживаются ({format_duration(age)} назад).</i>"

        active_emoji_id = telemetry.get("active_application_emoji_id")
        if active_emoji_id:
            app_emoji = f'<tg-emoji emoji-id="{active_emoji_id}">🪟</tg-emoji>'
        else:
            app_emoji = f'<tg-emoji emoji-id="{EMOJI_DEFAULT_APP}">🪟</tg-emoji>'

        # Current media playback line
        if media_state is None:
            media_state = getattr(self.mqtt, "last_media_state", None) or {}
        media_line = ""
        if media_state and media_state.get("active") and media_state.get("title"):
            m_title = html.escape(str(media_state.get("title") or "").strip())
            m_artist = html.escape(str(media_state.get("artist") or "").strip())
            m_playing = media_state.get("is_playing", False)
            m_liked = media_state.get("is_liked", False)
            icons = media_state.get("ui_icons") or {}
            play_eid = icons.get("tag_status_play") or EMOJI_PLAYER_PLAY
            heart_eid = icons.get("tag_heart_liked") or EMOJI_PLAYER_HEART
            if heart_eid == "5330276670303674347":
                heart_eid = "5346267379007530615"
            unliked_eid = icons.get("tag_heart_unliked") or ICONS.get("heart_unliked", "5346165399304055198")
            if unliked_eid in ("5330276670303674347", "5346066056710498954", "5346025172916807121", "5348530607729190493"):
                unliked_eid = "5346165399304055198"

            if m_playing:
                status_symbol = f'<tg-emoji emoji-id="{play_eid}">▶️</tg-emoji>'
            else:
                status_symbol = f'<tg-emoji emoji-id="{pause_eid}">⏸️</tg-emoji>'

            if m_liked:
                liked_icon = f' <tg-emoji emoji-id="{heart_eid}">❤️</tg-emoji>'
            else:
                liked_icon = f' <tg-emoji emoji-id="{unliked_eid}">💔</tg-emoji>'

            track_info = m_title
            if m_artist:
                track_info += f" — {m_artist}"
            if len(track_info) > 65:
                track_info = track_info[:62] + "..."

            media_line = f" └ Музыка: {status_symbol} {track_info}{liked_icon}\n"

        if media_line:
            app_line = f" ├ Открыт: {app_emoji} {active_application}\n" + media_line
        else:
            app_line = f" └ Открыт: {app_emoji} {active_application}\n"

        dur_str = f"  ({session_duration})" if session_duration and session_duration != "н/д" else ""
        os_tag = self.header_os_tag(telemetry)
        return (
            f'{os_tag} <b>PC Control</b>\n\n'
            f'<b><tg-emoji emoji-id="{EMOJI_SECTION_SESSION}">👤</tg-emoji> Сессия</b>\n'
            f' ├ Статус: {status_icon}{dur_str}\n'
            + app_line
            + "\n"
            f'<b><tg-emoji emoji-id="{EMOJI_SECTION_HARDWARE}">⚡️</tg-emoji> Железо</b>\n'
            f' ├ CPU: {cpu_text}\n'
            f' ├ GPU: {" · ".join(gpu_parts)}\n'
            f' └ RAM: {ram_text}\n\n'
            f'<b><tg-emoji emoji-id="{EMOJI_SECTION_DISKS}">📁</tg-emoji> Накопители</b>\n'
            + "\n".join(disk_lines)
            + telemetry_note
        )
