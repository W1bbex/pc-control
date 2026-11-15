#!/usr/bin/env python3
"""UI layout templates and text formatters for Telegram Bot."""

import html
import logging

try:
    from .ui_icons import (
        ICONS,
        CANONICAL_ICONS,
        EMOJI_BTN_POWER,
        EMOJI_BTN_SCREENSHOT,
        EMOJI_BTN_APPS,
        EMOJI_BTN_SOUND,
        EMOJI_BTN_SETTINGS,
        DEFAULT_PLAYER_ICONS,
    )
    from .ui_markups import UiMarkupsMixin
except ImportError:
    from ui_icons import (
        ICONS,
        CANONICAL_ICONS,
        EMOJI_BTN_POWER,
        EMOJI_BTN_SCREENSHOT,
        EMOJI_BTN_APPS,
        EMOJI_BTN_SOUND,
        EMOJI_BTN_SETTINGS,
        DEFAULT_PLAYER_ICONS,
    )
    from ui_markups import UiMarkupsMixin

LOG = logging.getLogger("pc-control-bot.ui_templates")


class UiTemplatesMixin(UiMarkupsMixin):
    def player_card_text(self, media_state):
        if not media_state or not media_state.get("active"):
            return "🎵 <b>Яндекс Музыка</b>\n\n<i>Сейчас ничего не играет или плеер закрыт.</i>"

        title = media_state.get("title") or "Неизвестный трек"
        artist = media_state.get("artist") or "Неизвестный исполнитель"
        pos = float(media_state.get("position") or 0.0)
        dur = float(media_state.get("duration") or 0.0)
        is_playing = media_state.get("is_playing", False)
        icons = dict(DEFAULT_PLAYER_ICONS)
        if media_state and media_state.get("ui_icons"):
            icons.update(media_state["ui_icons"])

        tag_note = icons.get("tag_note")
        tag_artist = icons.get("tag_artist")
        note_badge = f'<tg-emoji emoji-id="{tag_note}">🎵</tg-emoji> ' if tag_note else ""

        # Artist Avatar: Use artist circular avatar custom emoji if available, fallback to white headphone tag
        artist_avatar_id = media_state.get("artist_avatar_id") if media_state else None
        if artist_avatar_id:
            artist_badge = f'<tg-emoji emoji-id="{artist_avatar_id}">👤</tg-emoji> '
        elif tag_artist:
            artist_badge = f'<tg-emoji emoji-id="{tag_artist}">🎧</tg-emoji> '
        else:
            artist_badge = ""

        # Title and artist formatting (clamp plaintext so HTML tags never exceed Telegram 1024-char caption limit)
        raw_title = str(title).strip()
        raw_artist = str(artist).strip()
        if len(raw_title) > 180:
            raw_title = raw_title[:179] + "…"
        if len(raw_artist) > 140:
            raw_artist = raw_artist[:139] + "…"
        safe_title = html.escape(raw_title, quote=False)
        safe_artist = html.escape(raw_artist, quote=False)

        # 2. Format timeline & progress bar
        # Add +2s latency compensation when playing, and +1s to duration
        display_pos = max(0.0, (pos + 2.0) if is_playing else pos)
        display_dur = max(0.0, (dur + 1.0) if dur > 0 else 0.0)
        if display_dur > 0:
            display_pos = min(display_dur, display_pos)

        def format_time(seconds):
            total_sec = max(0, int(round(seconds)))
            m = total_sec // 60
            s = total_sec % 60
            return f"{m:02d}:{s:02d}"

        pos_str = format_time(display_pos)
        dur_str = format_time(display_dur) if display_dur > 0 else "--:--"

        tag_dot = icons.get("tag_slider_dot") or ICONS.get("dot", "5330193051585391352")
        dot = f'<tg-emoji emoji-id="{tag_dot}">⚪</tg-emoji>'

        bar_len = 12
        if display_dur > 0:
            pct = min(1.0, max(0.0, display_pos / display_dur))
            filled = int(round(pct * (bar_len - 1)))
            bar = "━" * filled + dot + "━" * (bar_len - 1 - filled)
        else:
            bar = "━" * (bar_len // 2) + dot + "━" * (bar_len // 2)

        # Vector play/pause status icon matching the minimalist button style
        btn_play = icons.get("btn_play") or ICONS.get("play", "5330015871299527534")
        btn_pause = icons.get("btn_pause") or ICONS.get("pause", "5330570905628222348")
        status_eid = (btn_play if is_playing else btn_pause) or icons.get("tag_status_play" if is_playing else "tag_status_pause")
        if status_eid:
            status_badge = f'<tg-emoji emoji-id="{status_eid}">{"▶️" if is_playing else "⏸"}</tg-emoji>'
        else:
            status_badge = "▶️" if is_playing else "⏸"

        text = (
            f'{note_badge}<b>{safe_title}</b>\n'
            f'{artist_badge}<i>{safe_artist}</i>\n\n'
            f'{bar}\n'
            f'{status_badge} <b>{pos_str}</b> / {dur_str}'
        )
        return text

    def player_card_caption(self, media_state):
        text = self.player_card_text(media_state)
        if len(text) <= 1024:
            return text
        # Trim overlong custom emoji / metadata gracefully while preserving HTML validity
        trimmed_state = dict(media_state or {})
        trimmed_state["title"] = str(trimmed_state.get("title") or "")[:60]
        trimmed_state["artist"] = str(trimmed_state.get("artist") or "")[:50]
        text = self.player_card_text(trimmed_state)
        return text[:1024]

    def activity_card_text(self, summary_data, date_str):
        safe_date = html.escape(str(date_str))
        if not summary_data:
            return f"📊 <b>Активность за {safe_date}</b>\n\n<i>Записей за этот день пока нет.</i>"
        lines = [f"📊 <b>Активность за {safe_date}</b>:\n"]
        for i, item in enumerate(summary_data[:12], 1):
            app = str(item.get("app_name", "unknown"))
            if len(app) > 80:
                app = app[:79] + "…"
            dur = max(0.0, float(item.get("total_duration") or 0.0))
            eid = item.get("emoji_id")
            emoji_tag = f'<tg-emoji emoji-id="{eid}">🪟</tg-emoji> ' if eid else "📱 "
            m = int(dur) // 60
            h = m // 60
            rem_m = m % 60
            time_str = f"{h}ч {rem_m}м" if h > 0 else f"{rem_m}м"
            lines.append(f"{i}. {emoji_tag}<b>{html.escape(app)}</b> — {time_str}")
        while len(lines) > 2 and sum(len(ln) + 1 for ln in lines) > 4000:
            lines.pop()
        return "\n".join(lines)

    def console_card_text(self, target="desktop", shell="powershell", last_cmd=None, is_executing=False):
        shell_name = "PowerShell" if (shell or "").lower() == "powershell" else "CMD"
        target_name = "Desktop (User)" if (target or "").lower() == "desktop" else "SYSTEM (Admin)"
        
        status_act_id = ICONS.get("status_active") or ICONS.get("check")
        status_clock_id = ICONS.get("clock")
        
        if is_executing:
            status_icon = f'<tg-emoji emoji-id="{status_clock_id}">🕒</tg-emoji> Выполняется...' if status_clock_id else "⏳ Выполняется..."
        else:
            status_icon = f'<tg-emoji emoji-id="{status_act_id}">✔️</tg-emoji> Готова к вводу' if status_act_id else "🟢 Готова к вводу"

        term_id = ICONS.get("terminal")
        header_icon = f'<tg-emoji emoji-id="{term_id}">⌨️</tg-emoji> ' if term_id else "💻 "

        text = (
            f"{header_icon}<b>Консоль Windows</b>\n\n"
            f"• <b>Окружение:</b> <code>{target_name}</code>\n"
            f"• <b>Оболочка:</b> <code>{shell_name}</code>\n"
            f"• <b>Статус:</b> {status_icon}\n\n"
        )
        if is_executing and last_cmd:
            safe_cmd = html.escape(str(last_cmd))
            text += f"<i>Текущая команда:</i> <code>{safe_cmd}</code>\n\n"
        text += (
            "Отправьте команду обычным сообщением в чат.\n"
            "Быстрые команды:\n"
            "• <code>/cmd &lt;команда&gt;</code> — в CMD\n"
            "• <code>/ps &lt;команда&gt;</code> — в PowerShell\n"
            "• <code>/exit</code> — закрыть консоль"
        )
        return text

    def power_menu_text(self, pc_state, session=None, desktop_status=None, esp_status=None):
        power_id = ICONS.get("power")
        user_id = ICONS.get("user")
        green_pulse = ICONS.get("status_active") or "5345809221961167372"
        yellow_pulse = ICONS.get("status_warning") or ICONS.get("pulse_yellow") or "5359432244043754958"
        red_pulse = ICONS.get("status_inactive") or "5345825096160291312"

        power_tag = f'<tg-emoji emoji-id="{power_id}">⚡</tg-emoji> ' if power_id else ""
        user_tag = f'<tg-emoji emoji-id="{user_id}">👤</tg-emoji> ' if user_id else ""

        state_val = getattr(pc_state, "state", None)
        if hasattr(state_val, "value"):
            state_val = state_val.value
        state_val = str(state_val or "").upper()

        if state_val in ("READY", "VERIFIED", "ONLINE"):
            status_icon = f'<tg-emoji emoji-id="{green_pulse}">🟢</tg-emoji>'
        elif state_val in ("BOOTING", "STARTING", "CONNECTING", "DEGRADED", "LOCKED"):
            status_icon = f'<tg-emoji emoji-id="{yellow_pulse}">🟡</tg-emoji>'
        else:
            status_icon = f'<tg-emoji emoji-id="{red_pulse}">🔴</tg-emoji>'

        sess_str = str(session or "").lower().strip()
        if sess_str == "unlocked":
            sess_icon = f'<tg-emoji emoji-id="{green_pulse}">🟢</tg-emoji> '
            sess_ru = f"{sess_icon}Разблокирована"
        elif sess_str in ("locked", "login_screen"):
            sess_icon = f'<tg-emoji emoji-id="{yellow_pulse}">🟡</tg-emoji> '
            sess_ru = f"{sess_icon}Заблокирована"
        elif sess_str in ("unknown", "none", ""):
            sess_icon = f'<tg-emoji emoji-id="{red_pulse}">🔴</tg-emoji> '
            sess_ru = f"{sess_icon}Неизвестно"
        else:
            sess_icon = f'<tg-emoji emoji-id="{red_pulse}">🔴</tg-emoji> '
            sess_ru = f"{sess_icon}{html.escape(sess_str.capitalize())}"

        desk_str = str(desktop_status or "").lower().strip()
        if desk_str == "online":
            desk_icon = f'<tg-emoji emoji-id="{green_pulse}">🟢</tg-emoji> '
            desk_ru = f"{desk_icon}В сети"
        elif desk_str == "offline":
            desk_icon = f'<tg-emoji emoji-id="{red_pulse}">🔴</tg-emoji> '
            desk_ru = f"{desk_icon}Не в сети"
        else:
            desk_icon = f'<tg-emoji emoji-id="{red_pulse}">🔴</tg-emoji> '
            desk_ru = f"{desk_icon}Ожидание"

        esp_str = str(esp_status or "").lower().strip()
        if esp_str == "online":
            esp_icon = f'<tg-emoji emoji-id="{green_pulse}">🟢</tg-emoji> '
            esp_ru = f"{esp_icon}В сети"
        elif esp_str == "offline":
            esp_icon = f'<tg-emoji emoji-id="{yellow_pulse}">🟡</tg-emoji> '
            esp_ru = f"{esp_icon}Не в сети (отключена)"
        else:
            esp_icon = f'<tg-emoji emoji-id="{yellow_pulse}">🟡</tg-emoji> '
            esp_ru = f"{esp_icon}Недоступна"

        clean_label = str(getattr(pc_state, "label", "Готов к работе") or "Готов к работе")
        for e in ("🟢", "🟡", "🔴", "⚪", "⚪️"):
            clean_label = clean_label.replace(e, "").strip()
        safe_label = html.escape(clean_label)

        reason = getattr(pc_state, "reason", None)
        reason_line = f"\n\n<i>{html.escape(str(reason))}</i>" if reason else ""

        return (
            f"{power_tag}<b>ПК и экран</b>\n\n"
            f"{user_tag}<b>Состояние</b>\n"
            f" ├ Статус: {status_icon} {safe_label}\n"
            f" ├ Сессия: {sess_ru}\n"
            f" ├ Desktop-агент: {desk_ru}\n"
            f" └ Контроллер ESP: {esp_ru}"
            f"{reason_line}"
        )

    def apps_menu_text(self):
        apps_id = ICONS.get("apps") or ICONS.get("game")
        apps_tag = f'<tg-emoji emoji-id="{apps_id}">📱</tg-emoji> ' if apps_id else ""
        return f"{apps_tag}<b>Программы</b>\n\nЗапуск установленных приложений и рабочего сетапа:"

    def sound_menu_text(self, volume=None):
        sound_id = ICONS.get("sound")
        sound_tag = f'<tg-emoji emoji-id="{sound_id}">🔊</tg-emoji> ' if sound_id else ""
        if isinstance(volume, dict):
            pct = volume.get("percent")
            muted = bool(volume.get("muted", False))
            if pct is not None:
                mute_suffix = " (🔇 без звука)" if muted else ""
                vol_line = f" ├ Текущая громкость: {pct}%{mute_suffix}\n"
            elif muted:
                vol_line = " ├ Звук: 🔇 без звука\n"
            else:
                vol_line = ""
        elif volume is not None:
            vol_line = f" ├ Текущая громкость: {volume}%\n"
        else:
            vol_line = ""
        return (
            f"{sound_tag}<b>Управление звуком</b>\n\n"
            f"<b>Параметры аудио</b>\n"
            f"{vol_line}"
            f" └ Выберите действие:"
        )

    def history_menu_text(self, count=0):
        chart_id = ICONS.get("chart")
        chart_tag = f'<tg-emoji emoji-id="{chart_id}">📈</tg-emoji> ' if chart_id else ""
        return (
            f"{chart_tag}<b>Графики и телеметрия</b>\n\n"
            f"<b>История метрик</b>\n"
            f" ├ Точек в базе: {count}\n"
            f" └ Выберите период для графика нагрузки, RAM, диска и сети:"
        )

    def workspace_vpn_choice_text(self):
        net_id = ICONS.get("network") or ICONS.get("wifi")
        net_tag = f'<tg-emoji emoji-id="{net_id}">📡</tg-emoji> ' if net_id else ""
        return (
            f"{net_tag}<b>Рабочий сетап — Выбор VPN</b>\n\n"
            f"Выберите VPN-туннель для подключения:"
        )

    def launch_card_text(
        self,
        stage: str,
        elapsed_seconds: int = 0,
        session_status: str = "unknown",
        system_status: str = "offline",
        target_mode: str = None,
        target_detail: str = None,
        unlock_attempts: int = 0,
        win_online_elapsed: int = None,
        logon_ready_elapsed: int = None,
        unlocked_elapsed: int = None,
        is_delayed: bool = False,
    ) -> str:
        power_id = ICONS.get("power")
        pc_id = ICONS.get("pc")
        check_id = ICONS.get("check")
        spinner_id = ICONS.get("spinner")
        clock_id = ICONS.get("clock")
        lock_id = ICONS.get("lock")
        apps_id = ICONS.get("apps")
        game_id = ICONS.get("game")
        cross_id = ICONS.get("cross")

        power_tag = f'<tg-emoji emoji-id="{power_id}">⚡</tg-emoji> ' if power_id else "⚡ "
        pc_tag = f'<tg-emoji emoji-id="{pc_id}">💻</tg-emoji> ' if pc_id else "💻 "
        check_tag = f'<tg-emoji emoji-id="{check_id}">✅</tg-emoji>' if check_id else "✅"
        spinner_tag = f'<tg-emoji emoji-id="{spinner_id}">⚪</tg-emoji>' if spinner_id else "🔄"
        clock_tag = f'<tg-emoji emoji-id="{clock_id}">🕒</tg-emoji>' if clock_id else "⏳"
        lock_tag = f'<tg-emoji emoji-id="{lock_id}">🔒</tg-emoji>' if lock_id else "🔒"
        cross_tag = f'<tg-emoji emoji-id="{cross_id}">❌</tg-emoji>' if cross_id else "❌"

        def _fmt(sec):
            if sec is None:
                return "--:--"
            s = max(0, int(sec))
            return f"{s // 60:02d}:{s % 60:02d}"

        elapsed_seconds = max(0, int(elapsed_seconds or 0))
        if target_mode == "workspace":
            vpn_name = html.escape(str(target_detail or "INCY VPN"))
            mode_label = f"Разблок + Сетап ({vpn_name})"
            note_line = "\n<i>Запуск начнётся сразу после разблокировки.</i>\n"
        elif target_mode == "game":
            g_name = html.escape(str(target_detail or "Игра"))
            mode_label = f"Разблок + Игра ({g_name})"
            note_line = "\n<i>Запуск начнётся сразу после разблокировки.</i>\n"
        elif target_mode == "unlock":
            mode_label = "Только авторазблок"
            note_line = ""
        elif target_mode is None and stage == "POWERING_ON":
            mode_label = "Ожидание выбора режима"
            note_line = ""
        else:
            mode_label = "Без авторазблокировки"
            note_line = ""

        sess_clean = str(session_status or "").lower()
        if sess_clean == "unlocked":
            status_desc = "Windows разблокирована"
        elif stage == "UNLOCK_FAILED":
            status_desc = "Сбой ввода пароля"
        elif stage == "UNLOCKING" and sess_clean in ("login_screen", "locked"):
            status_desc = f"Ввод пароля (попытка {max(1, unlock_attempts)}/2)..."
        elif system_status == "online":
            status_desc = "Windows в сети, ожидание экрана входа..."
        else:
            status_desc = "Загрузка ядра и служб Windows..."

        log_lines = []

        # Step 1: ESP Relay Pulse
        log_lines.append(f"{check_tag} 00:00 · Подача питания (ESP8266)")

        # Step 2: Windows System Agent
        if system_status == "online" or win_online_elapsed is not None:
            t_win = _fmt(win_online_elapsed if win_online_elapsed is not None else elapsed_seconds)
            log_lines.append(f"{check_tag} {t_win} · Windows в сети (System Agent)")
        else:
            log_lines.append(f"{spinner_tag} {_fmt(elapsed_seconds)} · Загрузка Windows (⏱ {elapsed_seconds}с)")

        # Step 3: Logon Screen Ready
        if sess_clean in ("login_screen", "locked", "unlocked") or logon_ready_elapsed is not None:
            t_logon = _fmt(logon_ready_elapsed if logon_ready_elapsed is not None else elapsed_seconds)
            log_lines.append(f"{check_tag} {t_logon} · Экран входа готов")
        elif system_status == "online":
            log_lines.append(f"{spinner_tag} {_fmt(elapsed_seconds)} · Инициализация экрана входа...")
        else:
            log_lines.append(f"{clock_tag} --:-- · Экран входа Windows")

        # Step 4: Unlock
        if sess_clean == "unlocked" or unlocked_elapsed is not None:
            t_unl = _fmt(unlocked_elapsed if unlocked_elapsed is not None else elapsed_seconds)
            log_lines.append(f"{check_tag} {t_unl} · Сессия разблокирована")
        elif stage == "UNLOCK_FAILED":
            log_lines.append(f"{cross_tag} {_fmt(elapsed_seconds)} · Ошибка ввода пароля")
        elif stage == "UNLOCKING":
            if sess_clean in ("login_screen", "locked"):
                log_lines.append(f"{spinner_tag} {_fmt(elapsed_seconds)} · Ввод пароля в Windows...")
            else:
                log_lines.append(f"{clock_tag} {_fmt(elapsed_seconds)} · Авторазблок в очереди")
        elif target_mode is None:
            log_lines.append(f"{lock_tag} --:-- · Разблокировка (ожидание решения)")
        else:
            log_lines.append(f"{lock_tag} --:-- · Разблокировка пропущена")

        # Step 5: Setup / Game (if targeted)
        safe_detail_ws = html.escape(str(target_detail or "INCY"))
        safe_detail_game = html.escape(str(target_detail or "Игра"))
        if target_mode == "workspace":
            app_tag = f'<tg-emoji emoji-id="{apps_id}">🗂️</tg-emoji>' if apps_id else "📱"
            if sess_clean == "unlocked":
                log_lines.append(f"{spinner_tag} {_fmt(elapsed_seconds)} · Запуск сетапа ({safe_detail_ws})...")
            else:
                log_lines.append(f"{app_tag} --:-- · Рабочий сетап ({safe_detail_ws})")
        elif target_mode == "game":
            g_tag = f'<tg-emoji emoji-id="{game_id}">🎮</tg-emoji>' if game_id else "🎮"
            if sess_clean == "unlocked":
                log_lines.append(f"{spinner_tag} {_fmt(elapsed_seconds)} · Запуск игры ({safe_detail_game})...")
            else:
                log_lines.append(f"{g_tag} --:-- · Игровой сетап ({safe_detail_game})")

        log_quote = "<blockquote>\n" + "\n".join(log_lines) + "\n</blockquote>"

        delay_warning = ""
        if is_delayed or (elapsed_seconds >= 60 and system_status != "online"):
            delay_warning = "\n\n⚠️ <i>Загрузка занимает больше минуты. Если ПК завис, доступно аварийное выключение.</i>"

        return (
            f"{power_tag}<b>Запуск компьютера</b>\n\n"
            f"{pc_tag}<b>Инициализация</b>\n"
            f" ├ Время старта: ⏱ {elapsed_seconds} сек\n"
            f" ├ Статус: <i>{status_desc}</i>\n"
            f" └ Режим: <i>{mode_label}</i>\n"
            f"{note_line}\n"
            f"{log_quote}"
            f"{delay_warning}"
        )

    def emergency_power_off_confirm_text(self) -> str:
        alert_tag = f'<tg-emoji emoji-id="{ICONS.get("alert")}">⚠️</tg-emoji> ' if ICONS.get("alert") else "⚠️ "
        return (
            f"{alert_tag}<b>Аварийное выключение ПК</b>\n\n"
            "Зажать кнопку питания на 5 секунд через ESP8266?\n\n"
            "<i>Это приведет к принудительному обесточиванию материнской платы (Hard Power Off).\n"
            "Используйте только если Windows зависла или не отвечает на команды.</i>"
        )

    def antigravity_card_text(self, data: dict) -> str:
        accounts = data.get("accounts", [])
        active = data.get("active") or {}
        is_running = data.get("is_running", False)

        tag_ag = f'<tg-emoji emoji-id="{ICONS.get("apps")}">📱</tg-emoji> ' if ICONS.get("apps") else ""
        lines = [
            f"{tag_ag}<b>Antigravity • Управление аккаунтами</b>\n",
        ]

        if not accounts:
            lines.append("<i>Список аккаунтов пуст или не удалось прочитать базу.</i>")
            return "\n".join(lines)

        active_email = active.get("email") or "—"
        active_name = active.get("name") or "—"
        ide_status = "В сети (запущена)" if is_running else "Остановлена"
        reset_time = active.get("reset_time") or "—"
        if reset_time and "T" in reset_time:
            # Format 2026-10-15T03:23:09Z -> 15.10 03:23 UTC
            try:
                date_part, time_part = reset_time.split("T")
                y, m, d = date_part.split("-")
                hh, mm = time_part.split(":")[:2]
                reset_time = f"{d}.{m} {hh}:{mm} UTC"
            except Exception:
                pass

        lines.append("<b>Активный профиль:</b>")
        lines.append(f"├ 👤 <b>{html.escape(str(active_name))}</b> (<code>{html.escape(str(active_email))}</code>)")
        lines.append(f"├ 🖥️ IDE: <b>{ide_status}</b>")
        lines.append(f"└ ⏳ Сброс квот: <code>{html.escape(str(reset_time))}</code>\n")

        # Quotas breakdown for active profile
        quotas = active.get("models_quota", {})
        if quotas:
            lines.append("<b>Остаток квот активного аккаунта:</b>")
            # Highlight key models
            model_keys = [
                ("gemini-3.1-pro-high", "Gemini 3.1 Pro"),
                ("gemini-3.7-flash-high", "Gemini 3.7 Flash"),
                ("gemini-3.5-flash-low", "Gemini 3.5 Flash"),
                ("gemini-3.1-flash-lite", "Flash Lite"),
            ]
            items = []
            for k, label in model_keys:
                if k in quotas:
                    raw_pct = quotas[k].get("percentage")
                    pct = 0 if raw_pct is None else raw_pct
                    items.append((label, pct))
            if not items:
                # Fallback to any models
                for k, v in list(quotas.items())[:4]:
                    raw_pct = v.get("percentage")
                    items.append((v.get("name", k), 0 if raw_pct is None else raw_pct))

            for i, (m_label, pct) in enumerate(items):
                prefix = "└" if i == len(items) - 1 else "├"
                try:
                    pct_num = float(pct)
                except (TypeError, ValueError):
                    pct_num = 0.0
                bar = "🟩" if pct_num >= 50 else ("🟨" if pct_num > 15 else "🟥")
                lines.append(f"{prefix} {bar} {html.escape(str(m_label))}: <b>{html.escape(str(pct))}%</b>")
            lines.append("")

        # Other available accounts summary
        others = [a for a in accounts if not a.get("is_active")]
        if others:
            lines.append(f"<b>Резервные аккаунты ({len(others)}):</b>")
            max_others = 20
            shown_others = others[:max_others]
            for i, o in enumerate(shown_others):
                is_last = (i == len(shown_others) - 1) and len(others) <= max_others
                prefix = "└" if is_last else "├"
                raw_email = o.get("email") or ""
                raw_name = o.get("name")
                o_name = raw_name or (raw_email.split("@")[0] if "@" in raw_email else "Без имени")
                raw_opct = o.get("min_quota")
                o_pct = 0 if raw_opct is None else raw_opct
                lines.append(f"{prefix} 👤 {html.escape(str(o_name))}: <b>{html.escape(str(o_pct))}%</b> квоты")
            if len(others) > max_others:
                lines.append(f"└ <i>...и ещё {len(others) - max_others}</i>")
            lines.append("")

        footer = "<i>⚡ Выберите аккаунт ниже для мгновенного переключения:</i>"
        while len(lines) > 2 and (sum(len(ln) + 1 for ln in lines) + len(footer) > 4000):
            lines.pop()
        lines.append(footer)
        return "\n".join(lines)

