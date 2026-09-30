#!/usr/bin/env python3

"""Telemetry formatting, history and threshold alerts for the Telegram UI."""

import html
import logging
import time
from pathlib import Path

from history import TelemetryHistory

LOG = logging.getLogger("pc-control-bot.monitoring")

EMOJI_HEADER_PC = "5967816500415827773"        # 💻 PC Control
EMOJI_SECTION_SESSION = "5886412370347036129"  # 👤 Сессия
EMOJI_STATUS_ACTIVE = "5325945307454789973"    # 🟢 Active
EMOJI_STATUS_INACTIVE = "5326056199215406977"  # ❌ Inactive
EMOJI_DEFAULT_APP = "5251242026447446579"      # 🪟 Windows Explorer / Window icon
EMOJI_PLAYER_PLAY = "5251372554798539686"      # ▶️ Minimalist white play
EMOJI_PLAYER_PAUSE = "5251752543440119050"     # ⏸️ Minimalist white pause (2 bars)
EMOJI_PLAYER_HEART = "5249190011332502742"     # ❤️ Heart liked
EMOJI_SECTION_HARDWARE = "5843553939672274145" # ⚡️ Железо
EMOJI_SECTION_DISKS = "5877332341331857066"    # 📁 Накопители


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
            return "⏳", "Выключение..."

        session = self.mqtt.system_session
        if session == "unlocked":
            if telemetry and telemetry.get("user_active", False):
                status_icon = f'<tg-emoji emoji-id="{EMOJI_STATUS_ACTIVE}">🟢</tg-emoji>'
            else:
                status_icon = "🟡"
            return status_icon, format_duration(telemetry.get("session_seconds"))
        if session in {"locked", "login_screen"}:
            return f'<tg-emoji emoji-id="{EMOJI_STATUS_INACTIVE}">❌</tg-emoji>', "н/д"
        return f'<tg-emoji emoji-id="{EMOJI_STATUS_INACTIVE}">❌</tg-emoji>', "н/д"

    def dashboard_text(self, media_state=None):
        """Build the full dashboard from the nested schema emitted by agent.py."""
        pc_state = self.mqtt.health.evaluate_pc_state(telemetry_age=self.telemetry_age())

        if pc_state.state in ("UNREACHABLE", "SHUTDOWN_OBSERVED"):
            header = "⚪" if pc_state.state == "UNREACHABLE" else f'<tg-emoji emoji-id="{EMOJI_STATUS_INACTIVE}">❌</tg-emoji>'
            last_telem_info = ""
            if self.latest_telemetry:
                ts = self.latest_telemetry.get("ts") or self.latest_telemetry.get("timestamp")
                if ts:
                    diff = int(time.time() - float(ts))
                    last_telem_info = f"\n\n<i>Последняя телеметрия получена {format_duration(diff)} назад.</i>"

            return (
                f'<tg-emoji emoji-id="{EMOJI_HEADER_PC}">💻</tg-emoji> <b>PC Control</b> — {header}\n'
                f"Windows: {pc_state.label}\n\n"
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
            pause_eid = icons.get("tag_status_pause") or EMOJI_PLAYER_PAUSE
            heart_eid = icons.get("tag_heart_liked") or EMOJI_PLAYER_HEART

            if m_playing:
                status_symbol = f'<tg-emoji emoji-id="{play_eid}">▶️</tg-emoji>'
            else:
                status_symbol = f'<tg-emoji emoji-id="{pause_eid}">⏸️</tg-emoji>'

            liked_icon = f' <tg-emoji emoji-id="{heart_eid}">❤️</tg-emoji>' if m_liked else ""

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

        return (
            f'<tg-emoji emoji-id="{EMOJI_HEADER_PC}">💻</tg-emoji> <b>PC Control</b>\n\n'
            f'<b><tg-emoji emoji-id="{EMOJI_SECTION_SESSION}">👤</tg-emoji> Сессия</b>\n'
            f' ├ Статус: {status_icon}  ({session_duration})\n'
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
