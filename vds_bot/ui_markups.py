#!/usr/bin/env python3
"""Inline keyboard markup builders for PC Control Bot."""

import hashlib
import logging

try:
    from .ui_icons import (
        ICONS,
        EMOJI_BTN_POWER,
        EMOJI_BTN_SCREENSHOT,
        EMOJI_BTN_APPS,
        EMOJI_BTN_SOUND,
        EMOJI_BTN_SETTINGS,
        DEFAULT_PLAYER_ICONS,
    )
except ImportError:
    from ui_icons import (
        ICONS,
        EMOJI_BTN_POWER,
        EMOJI_BTN_SCREENSHOT,
        EMOJI_BTN_APPS,
        EMOJI_BTN_SOUND,
        EMOJI_BTN_SETTINGS,
        DEFAULT_PLAYER_ICONS,
    )

LOG = logging.getLogger("pc-control-bot.ui_markups")


class UiMarkupsMixin:
    @staticmethod
    def back_button(target="menu"):
        btn = {"text": "Назад", "callback_data": target}
        if ICONS.get("back"):
            btn["icon_custom_emoji_id"] = ICONS.get("back")
        return [btn]

    def main_menu_markup(self, pc_state=None, session=None, system_status=None):
        state_val = ""
        sess_val = ""
        sys_val = ""

        if pc_state is not None:
            raw_state = getattr(pc_state, "state", pc_state)
            state_val = getattr(raw_state, "value", str(raw_state))
            sess_val = getattr(pc_state, "session_status", "")
            sys_val = getattr(pc_state, "system_status", "")

        if session is not None:
            sess_val = session
        if system_status is not None:
            sys_val = system_status

        if sess_val == "unlocked" or state_val in ("READY", "DEGRADED"):
            bottom_btn = {"text": "Выключить", "style": "danger", "callback_data": "shutdown_pc"}
        elif sess_val in ("locked", "login_screen") or state_val == "LOCKED":
            bottom_btn = {"text": "Разблокировать ПК", "style": "success", "callback_data": "unlock_windows_manual"}
        elif state_val in ("SHUTDOWN_OBSERVED", "UNREACHABLE", "OFFLINE") or sys_val == "offline":
            bottom_btn = {"text": "Включить ПК", "style": "success", "callback_data": "power_on"}
        else:
            bottom_btn = {"text": "Выключить", "style": "danger", "callback_data": "shutdown_pc"}

        buttons = [
            [{"text": "Питание", "style": "success", "callback_data": "power_menu", "icon_custom_emoji_id": EMOJI_BTN_POWER}],
            [
                {"text": "Скриншот", "style": "primary", "callback_data": "screenshot", "icon_custom_emoji_id": EMOJI_BTN_SCREENSHOT},
                {"text": "Программы", "style": "primary", "callback_data": "apps_menu", "icon_custom_emoji_id": EMOJI_BTN_APPS},
            ],
            [
                {"text": "Звук", "style": "primary", "callback_data": "sound_menu", "icon_custom_emoji_id": EMOJI_BTN_SOUND},
                {"text": "Настройки", "style": "primary", "callback_data": "settings_menu", "icon_custom_emoji_id": EMOJI_BTN_SETTINGS},
            ],
            [bottom_btn],
        ]

        return {"inline_keyboard": buttons}

    def settings_menu_markup(self):
        return {
            "inline_keyboard": [
                [
                    {"text": "Мониторинг", "style": "primary", "icon_custom_emoji_id": ICONS.get("cpu"), "callback_data": "monitoring_menu"},
                    {"text": "Графики", "style": "primary", "icon_custom_emoji_id": ICONS.get("chart"), "callback_data": "history_menu"},
                ],
                [
                    {"text": "Физическая кнопка", "style": "primary", "icon_custom_emoji_id": ICONS.get("game"), "callback_data": "physical_button"},
                    {"text": "Активность", "style": "primary", "icon_custom_emoji_id": ICONS.get("apps"), "callback_data": "menu_activity"},
                ],
                [
                    {"text": "Подсветка (ARGB)", "style": "primary", "icon_custom_emoji_id": ICONS.get("light"), "callback_data": "led_menu"},
                    {"text": "Профили Antigravity", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_antigravity") or ICONS.get("terminal"), "callback_data": "antigravity_menu"},
                ],
                [
                    {"text": "Консоль", "style": "primary", "icon_custom_emoji_id": ICONS.get("terminal"), "callback_data": "console_open"},
                ],
                self.back_button("menu"),
            ]
        }

    def led_menu_markup(self):
        return {
            "inline_keyboard": [
                [
                    {"text": "Включить", "style": "success", "icon_custom_emoji_id": ICONS.get("light"), "callback_data": "led_action:on"},
                    {"text": "Выключить", "style": "danger", "callback_data": "led_action:off"},
                ],
                [
                    {"text": "Для глаз (Идеал)", "style": "success", "icon_custom_emoji_id": ICONS.get("light"), "callback_data": "led_action:preset:eye"},
                    {"text": "Для фото (Камера)", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "led_action:preset:photo"},
                ],
                [
                    {"text": "30%", "style": "primary", "callback_data": "led_action:bright:80"},
                    {"text": "60%", "style": "primary", "callback_data": "led_action:bright:160"},
                    {"text": "100%", "style": "primary", "callback_data": "led_action:bright:255"},
                ],
                [
                    {"text": "Все оттенки (1..12)", "style": "primary", "callback_data": "led_palette"},
                ],
                self.back_button("settings_menu"),
            ]
        }

    def led_palette_markup(self):
        return {
            "inline_keyboard": [
                [
                    {"text": "1", "style": "primary", "callback_data": "led_action:preset:1"},
                    {"text": "2", "style": "primary", "callback_data": "led_action:preset:2"},
                    {"text": "3", "style": "primary", "callback_data": "led_action:preset:3"},
                ],
                [
                    {"text": "4", "style": "primary", "callback_data": "led_action:preset:4"},
                    {"text": "5", "style": "primary", "callback_data": "led_action:preset:5"},
                    {"text": "6", "style": "primary", "callback_data": "led_action:preset:6"},
                ],
                [
                    {"text": "7", "style": "primary", "callback_data": "led_action:preset:7"},
                    {"text": "8", "style": "primary", "callback_data": "led_action:preset:8"},
                    {"text": "9", "style": "primary", "callback_data": "led_action:preset:9"},
                ],
                [
                    {"text": "10 (Идеал)", "style": "primary", "callback_data": "led_action:preset:10"},
                    {"text": "11", "style": "primary", "callback_data": "led_action:preset:11"},
                    {"text": "12", "style": "primary", "callback_data": "led_action:preset:12"},
                ],
                self.back_button("led_menu"),
            ]
        }

    def sound_menu_markup(self):
        return {
            "inline_keyboard": [
                [{"text": "Вкл / Выкл звук", "style": "primary", "icon_custom_emoji_id": ICONS.get("sound"), "callback_data": "volume:toggle"}],
                [
                    {"text": "25%", "style": "primary", "callback_data": "volume:25"},
                    {"text": "50%", "style": "primary", "callback_data": "volume:50"},
                ],
                [
                    {"text": "75%", "style": "primary", "callback_data": "volume:75"},
                    {"text": "100%", "style": "primary", "callback_data": "volume:100"},
                ],
                self.back_button("menu"),
            ]
        }

    def power_menu_markup(self, pc_state=None, session=None, system_status=None, stealth_active: bool = False):
        state_val = ""
        sess_val = ""
        sys_val = ""

        if pc_state is not None:
            raw_state = getattr(pc_state, "state", pc_state)
            state_val = getattr(raw_state, "value", str(raw_state))
            sess_val = getattr(pc_state, "session_status", "")
            sys_val = getattr(pc_state, "system_status", "")

        if session is not None:
            sess_val = session
        if system_status is not None:
            sys_val = system_status

        is_pc_on = (
            sess_val in ("unlocked", "locked", "login_screen")
            or state_val in ("READY", "DEGRADED", "LOCKED", "BOOTING")
            or sys_val == "online"
        )

        if not is_pc_on:
            rows = [[{"text": "Включить ПК", "style": "success", "callback_data": "power_on"}]]
            if state_val == "UNREACHABLE":
                emerg_btn = {"text": "Аварийное выключение (ESP)", "style": "danger", "callback_data": "emergency_power_off"}
                if ICONS.get("power"):
                    emerg_btn["icon_custom_emoji_id"] = ICONS.get("power")
                rows.append([emerg_btn])
            rows.append(self.back_button("menu"))
            return {"inline_keyboard": rows}

        # PC is ON: 1 main power button, 1 adaptive lock button
        if sess_val == "unlocked":
            lock_btn = {"text": "Заблокировать Windows", "style": "danger", "callback_data": "lock_windows"}
        else:
            lock_btn = {"text": "Разблокировать Windows", "style": "success", "callback_data": "unlock_windows_manual"}
        if ICONS.get("lock"):
            lock_btn["icon_custom_emoji_id"] = ICONS.get("lock")

        shutdown_btn = {"text": "Выключить ПК", "style": "danger", "callback_data": "shutdown_pc"}
        if ICONS.get("power"):
            shutdown_btn["icon_custom_emoji_id"] = ICONS.get("power")

        screen_btn = {"text": "Выключить экран", "style": "primary", "callback_data": "toggle_screen"}
        if ICONS.get("screen"):
            screen_btn["icon_custom_emoji_id"] = ICONS.get("screen")

        reboot_btn = {"text": "Перезагрузить", "style": "primary", "callback_data": "reboot_pc"}
        if ICONS.get("refresh"):
            reboot_btn["icon_custom_emoji_id"] = ICONS.get("refresh")

        rows = [
            [shutdown_btn],
            [lock_btn],
            [
                screen_btn,
                {"text": "Свернуть окна", "style": "primary", "callback_data": "minimize_windows"},
            ],
            [
                (
                    {"text": "Выйти из скрытности", "style": "danger", "callback_data": "stealth_off"}
                    if stealth_active
                    else {"text": "Скрытность (Blackout)", "style": "primary", "callback_data": "stealth_on"}
                ),
            ],
            [
                {"text": "Сон", "style": "primary", "callback_data": "sleep_pc"},
                reboot_btn,
            ],
        ]

        if state_val in ("BOOTING", "DEGRADED") and sys_val != "online":
            emerg_btn = {"text": "Аварийное выключение (ESP)", "style": "danger", "callback_data": "emergency_power_off"}
            if ICONS.get("power"):
                emerg_btn["icon_custom_emoji_id"] = ICONS.get("power")
            rows.append([emerg_btn])

        rows.append(self.back_button("menu"))
        return {"inline_keyboard": rows}

    def stealth_menu_markup(self, active: bool = False):
        if active:
            toggle_btn = {"text": "Выйти из скрытности", "style": "danger", "callback_data": "stealth_off"}
        else:
            toggle_btn = {"text": "Включить скрытность", "style": "primary", "callback_data": "stealth_on"}
        return {
            "inline_keyboard": [
                [toggle_btn],
                self.back_button("menu"),
            ]
        }

    def shutdown_menu_markup(self, scheduled_ts=None):
        rows = [
            [{"text": "Выключить сейчас", "style": "danger", "callback_data": "shutdown_now"}],
            [
                {"text": "15 мин", "style": "primary", "callback_data": "shutdown_after:15"},
                {"text": "30 мин", "style": "primary", "callback_data": "shutdown_after:30"},
                {"text": "1 час", "style": "primary", "callback_data": "shutdown_after:60"},
            ],
            [{"text": "Указать HH:MM (МСК)", "style": "primary", "callback_data": "shutdown_at_time"}],
        ]
        if scheduled_ts:
            rows.append([{"text": "Отменить активный таймер", "style": "danger", "callback_data": "shutdown_schedule_cancel"}])
        rows.append(self.back_button("power_menu"))
        return {"inline_keyboard": rows}

    def reboot_menu_markup(self):
        reboot_unlock_btn = {
            "text": "Перезагрузка + Разблок",
            "style": "success",
            "callback_data": "reboot_ask:unlock",
        }
        if ICONS.get("refresh"):
            reboot_unlock_btn["icon_custom_emoji_id"] = ICONS.get("refresh")

        reboot_plain_btn = {
            "text": "Только перезагрузка",
            "style": "primary",
            "callback_data": "reboot_ask:plain",
        }
        if ICONS.get("refresh"):
            reboot_plain_btn["icon_custom_emoji_id"] = ICONS.get("refresh")

        return {
            "inline_keyboard": [
                [reboot_unlock_btn],
                [reboot_plain_btn],
                self.back_button("power_menu"),
            ]
        }

    def apps_menu_markup(self):
        return {
            "inline_keyboard": [
                [
                    {"text": "Рабочий сетап", "style": "success", "icon_custom_emoji_id": ICONS.get("apps"), "callback_data": "workspace_start_direct"},
                    {"text": "Игровой сетап", "style": "primary", "icon_custom_emoji_id": ICONS.get("game"), "callback_data": "game_start_direct"},
                ],
                [
                    {"text": "Telegram", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_telegram") or ICONS.get("telegram"), "callback_data": "app:Telegram"},
                    {"text": "Музыка", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_yandex_music") or ICONS.get("music"), "callback_data": "app:Музыка"},
                ],
                [
                    {"text": "Dolphin", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_dolphin"), "callback_data": "app:Dolphin"},
                    {"text": "Chrome", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_chrome") or ICONS.get("chrome"), "callback_data": "app:Chrome"},
                ],
                [
                    {"text": "Happ", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_happ") or ICONS.get("network"), "callback_data": "app:Happ"},
                    {"text": "INCY", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_incy") or ICONS.get("network"), "callback_data": "app:INCY"},
                ],
                [
                    {"text": "Antigravity", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_antigravity") or ICONS.get("terminal"), "callback_data": "app:Antigravity"},
                    {"text": "Codex", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_chatgpt") or ICONS.get("bot"), "callback_data": "app:ChatGPT"},
                ],
                [
                    {"text": "Консоль (Терминал)", "style": "primary", "icon_custom_emoji_id": ICONS.get("terminal"), "callback_data": "console_open"},
                    {"text": "Нажать ESC", "style": "primary", "icon_custom_emoji_id": ICONS.get("terminal"), "callback_data": "press_esc"},
                ],
                [
                    {"text": "Codex: варианты запуска", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_chatgpt") or ICONS.get("bot"), "callback_data": "codex_diagnostics"},
                    {"text": "Лечение AG & VPN", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh") or ICONS.get("terminal"), "callback_data": "ag_heal"},
                ],
                self.back_button("menu"),
            ]
        }

    def game_choice_markup(self, prefix="flow_game"):
        cross_icon = ICONS.get("cross")
        cancel_cb = "flow_unlock_no" if prefix == "flow_game" else "menu"
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "Minecraft",
                        "style": "primary",
                        "icon_custom_emoji_id": ICONS.get("game_minecraft") or ICONS.get("game"),
                        "callback_data": f"{prefix}:minecraft",
                    },
                    {
                        "text": "Warcraft 3",
                        "style": "primary",
                        "icon_custom_emoji_id": ICONS.get("game_warcraft3") or ICONS.get("game"),
                        "callback_data": f"{prefix}:warcraft3",
                    },
                ],
                [
                    {
                        "text": "GTA 5 Legacy",
                        "style": "primary",
                        "icon_custom_emoji_id": ICONS.get("game_gta5") or ICONS.get("game"),
                        "callback_data": f"{prefix}:gta5",
                    },
                    {
                        "text": "Roblox",
                        "style": "primary",
                        "icon_custom_emoji_id": ICONS.get("game_roblox") or ICONS.get("game"),
                        "callback_data": f"{prefix}:roblox",
                    },
                ],
                [
                    {
                        "text": "GTA SA",
                        "style": "primary",
                        "icon_custom_emoji_id": ICONS.get("game_gtasa") or ICONS.get("game"),
                        "callback_data": f"{prefix}:gtasa",
                    },
                ],
                [
                    {
                        "text": "Отмена",
                        "style": "danger",
                        "icon_custom_emoji_id": cross_icon,
                        "callback_data": cancel_cb,
                    }
                ],
            ]
        }

    def game_vpn_choice_markup(self, game_key, prefix="flow_game_vpn"):
        happ_label = "Happ (VPN)"
        incy_label = "INCY (VPN)"
        happ_icon = ICONS.get("app_happ") or ICONS.get("network")
        incy_icon = ICONS.get("app_incy") or ICONS.get("network")
        cross_icon = ICONS.get("cross")
        cancel_cb = "flow_unlock_no" if "flow" in prefix else "menu"
        return {
            "inline_keyboard": [
                [
                    {
                        "text": incy_label,
                        "style": "primary",
                        "icon_custom_emoji_id": incy_icon,
                        "callback_data": f"{prefix}:{game_key}:incy",
                    },
                    {
                        "text": happ_label,
                        "style": "primary",
                        "icon_custom_emoji_id": happ_icon,
                        "callback_data": f"{prefix}:{game_key}:happ",
                    },
                ],
                [
                    {
                        "text": "Отмена",
                        "style": "danger",
                        "icon_custom_emoji_id": cross_icon,
                        "callback_data": cancel_cb,
                    }
                ],
            ]
        }

    def workspace_vpn_choice_markup(self, active_vpn="incy", prefix="workspace_vpn"):
        happ_label = "Happ (VPN)"
        incy_label = "INCY (VPN)"
        happ_icon = ICONS.get("app_happ") or ICONS.get("network")
        incy_icon = ICONS.get("app_incy") or ICONS.get("network")
        cross_icon = ICONS.get("cross")
        cancel_cb = "flow_unlock_no" if "flow" in prefix else "menu"
        return {
            "inline_keyboard": [
                [
                    {
                        "text": incy_label,
                        "style": "success" if active_vpn == "incy" and prefix == "workspace_vpn" else "primary",
                        "icon_custom_emoji_id": incy_icon,
                        "callback_data": f"{prefix}:incy",
                    },
                    {
                        "text": happ_label,
                        "style": "success" if active_vpn == "happ" and prefix == "workspace_vpn" else "primary",
                        "icon_custom_emoji_id": happ_icon,
                        "callback_data": f"{prefix}:happ",
                    },
                ],
                [{"text": "Отмена", "style": "danger", "icon_custom_emoji_id": cross_icon, "callback_data": cancel_cb}],
            ]
        }

    def monitoring_menu_markup(self):
        return {
            "inline_keyboard": [
                [{"text": "Обновить состояние", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": "telemetry_now"}],
                [{"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"}],
                [{"text": "Активность за день", "style": "primary", "icon_custom_emoji_id": ICONS.get("clock"), "callback_data": "menu_activity"}],
                [{"text": "Графики", "style": "primary", "icon_custom_emoji_id": ICONS.get("chart"), "callback_data": "history_menu"}],
                self.back_button("settings_menu"),
            ]
        }

    def history_menu_markup(self):
        return {
            "inline_keyboard": [
                [
                    {"text": "1 час", "style": "primary", "callback_data": "history:1h"},
                    {"text": "6 часов", "style": "primary", "callback_data": "history:6h"},
                    {"text": "24 часа", "style": "primary", "callback_data": "history:24h"},
                ],
                [
                    {"text": "7 дней", "style": "primary", "callback_data": "history:7d"},
                    {"text": "30 дней", "style": "primary", "callback_data": "history:30d"},
                ],
                self.back_button("monitoring_menu"),
            ]
        }

    def player_card_markup(self, media_state):
        is_playing = media_state.get("is_playing", False) if media_state else False
        icons = dict(DEFAULT_PLAYER_ICONS)
        if media_state and media_state.get("ui_icons"):
            icons.update(media_state["ui_icons"])

        btn_back10 = icons.get("btn_back10")
        btn_fwd10 = icons.get("btn_fwd10")
        btn_prev = icons.get("btn_prev")
        btn_next = icons.get("btn_next")
        btn_wave = icons.get("btn_wave")
        btn_play_pause = icons.get("btn_pause" if is_playing else "btn_play")

        # Row 1: 10s back, Play/Pause, 10s fwd
        r1 = [
            {"text": "10с", "callback_data": "player_seek_back_10"},
            {"text": "Пауза" if is_playing else "Плей", "style": "primary", "callback_data": "player_toggle_play"},
            {"text": "10с", "callback_data": "player_seek_fwd_10"},
        ]
        if btn_back10:
            r1[0]["icon_custom_emoji_id"] = btn_back10
        if btn_play_pause:
            r1[1]["icon_custom_emoji_id"] = btn_play_pause
        if btn_fwd10:
            r1[2]["icon_custom_emoji_id"] = btn_fwd10

        # Row 2: Prev, Wave, Next (3 buttons, 'Волна' fits perfectly on mobile without truncation)
        r2 = [
            {"text": "Пред", "callback_data": "player_prev"},
            {"text": "Волна", "style": "success", "callback_data": "player_wave"},
            {"text": "След", "callback_data": "player_next"},
        ]
        if btn_prev:
            r2[0]["icon_custom_emoji_id"] = btn_prev
        if btn_wave:
            r2[1]["icon_custom_emoji_id"] = btn_wave
        if btn_next:
            r2[2]["icon_custom_emoji_id"] = btn_next

        # Row 3: Single full-width Like button
        is_liked = media_state.get("is_liked", False) if media_state else False
        btn_heart = icons.get("tag_heart_liked" if is_liked else "tag_heart_unliked")
        if not btn_heart or btn_heart in ("5330276670303674347", "5346066056710498954", "5346025172916807121", "5348530607729190493"):
            btn_heart = "5346267379007530615" if is_liked else "5346165399304055198"
        heart_text = "В любимых" if is_liked else "В избранное"
        r3_btn = {"text": heart_text, "callback_data": "player_toggle_like"}
        if btn_heart:
            r3_btn["icon_custom_emoji_id"] = btn_heart
        r3 = [r3_btn]

        return {"inline_keyboard": [r1, r2, r3]}

    def console_menu_markup(self, target="desktop", shell="powershell"):
        shell_btn_text = "Shell: PowerShell" if (shell or "").lower() == "powershell" else "Shell: CMD"
        target_btn_text = "Цель: Desktop" if (target or "").lower() == "desktop" else "Цель: SYSTEM"
        close_btn = {"text": "Закрыть консоль", "callback_data": "console_close"}
        if ICONS.get("cross") or ICONS.get("back"):
            close_btn["icon_custom_emoji_id"] = ICONS.get("cross") or ICONS.get("back")
        return {
            "inline_keyboard": [
                [
                    {"text": shell_btn_text, "style": "primary", "callback_data": "console_toggle_shell"},
                    {"text": target_btn_text, "style": "primary", "callback_data": "console_toggle_target"},
                ],
                [
                    {"text": "whoami", "callback_data": "console_quick:whoami"},
                    {"text": "ipconfig", "callback_data": "console_quick:ipconfig"},
                ],
                [
                    {"text": "tasklist", "callback_data": "console_quick:tasklist"},
                    {"text": "dir", "callback_data": "console_quick:dir"},
                ],
                [
                    close_btn,
                ],
            ]
        }

    def launch_card_markup(
        self,
        stage: str = "POWERING_ON",
        target_mode: str = None,
        target_label: str = None,
        show_emergency: bool = False,
    ) -> dict:
        apps_icon = ICONS.get("apps")
        lock_icon = ICONS.get("lock")
        game_icon = ICONS.get("game")
        cross_icon = ICONS.get("cross")
        check_icon = ICONS.get("check")
        power_icon = ICONS.get("power")
        alert_icon = ICONS.get("alert") or power_icon

        rows = []
        if target_mode is None and stage == "POWERING_ON":
            hero_btn = {
                "text": "Разблок + Сетап",
                "style": "success",
                "callback_data": "flow_unlock_and_workspace",
            }
            if apps_icon:
                hero_btn["icon_custom_emoji_id"] = apps_icon
            rows.append([hero_btn])

            unlock_btn = {
                "text": "Только разблок",
                "style": "primary",
                "callback_data": "flow_unlock_yes",
            }
            if lock_icon:
                unlock_btn["icon_custom_emoji_id"] = lock_icon

            game_btn = {
                "text": "Разблок + Игра",
                "style": "primary",
                "callback_data": "flow_unlock_and_game",
            }
            if game_icon:
                game_btn["icon_custom_emoji_id"] = game_icon
            rows.append([unlock_btn, game_btn])

            skip_btn = {
                "text": "Без разблокировки",
                "style": "danger",
                "callback_data": "flow_unlock_no",
            }
            if cross_icon:
                skip_btn["icon_custom_emoji_id"] = cross_icon
            rows.append([skip_btn])
        else:
            badge_text = target_label or "Разблок запланирован"
            badge_btn = {
                "text": badge_text,
                "style": "success",
                "callback_data": "noop",
            }
            if check_icon:
                badge_btn["icon_custom_emoji_id"] = check_icon
            rows.append([badge_btn])

            cancel_btn = {
                "text": "Отмена",
                "style": "danger",
                "callback_data": "flow_unlock_no",
            }
            if cross_icon:
                cancel_btn["icon_custom_emoji_id"] = cross_icon
            rows.append([cancel_btn])

        if show_emergency:
            emerg_btn = {
                "text": "Аварийное выключение",
                "style": "danger",
                "callback_data": "emergency_power_off",
            }
            if alert_icon:
                emerg_btn["icon_custom_emoji_id"] = alert_icon
            rows.append([emerg_btn])

        return {"inline_keyboard": rows}

    def emergency_power_off_confirm_markup(self) -> dict:
        power_icon = ICONS.get("power")
        cross_icon = ICONS.get("cross")

        force_btn = {
            "text": "Да, выключить принудительно",
            "style": "danger",
            "callback_data": "emergency_power_off_confirm",
        }
        if power_icon:
            force_btn["icon_custom_emoji_id"] = power_icon

        cancel_btn = {
            "text": "Отмена",
            "style": "primary",
            "callback_data": "power_menu",
        }
        if cross_icon:
            cancel_btn["icon_custom_emoji_id"] = cross_icon

        return {
            "inline_keyboard": [
                [force_btn],
                [cancel_btn],
            ]
        }

    @staticmethod
    def format_antigravity_account_label(acc: dict, accounts: list) -> str:
        """Return account display name, appending email local-part when names collide."""
        raw_email = str(acc.get("email") or "").strip()
        raw_name = str(acc.get("name") or "").strip()
        base_name = raw_name or (raw_email.split("@")[0] if "@" in raw_email else "Без имени")

        norm_target = base_name.casefold()
        same_name_count = 0
        for other in accounts or []:
            o_email = str(other.get("email") or "").strip()
            o_name = str(other.get("name") or "").strip() or (o_email.split("@")[0] if "@" in o_email else "Без имени")
            if o_name.casefold() == norm_target:
                same_name_count += 1

        if same_name_count > 1 and raw_email:
            local_part = raw_email.split("@")[0]
            if local_part and local_part.casefold() != norm_target:
                return f"{base_name} ({local_part})"
            return f"{base_name} ({raw_email})"
        return base_name

    def antigravity_card_markup(self, data: dict) -> dict:
        accounts = data.get("accounts", [])
        rows = []

        refresh_icon = ICONS.get("refresh")
        check_icon = ICONS.get("check")
        back_icon = ICONS.get("back")
        gear_icon = ICONS.get("gear")
        term_icon = ICONS.get("terminal")

        # 1. Accounts grid (buttons to switch, capped at 25 to respect Telegram limits)
        visible_accounts = accounts[:25]
        for acc in visible_accounts:
            is_active = acc.get("is_active", False)
            acc_id = str(acc.get("id", ""))
            name = self.format_antigravity_account_label(acc, accounts)
            has_quota = acc.get("has_quota_data", True)
            pct = acc.get("min_quota")
            pct = 0 if pct is None else pct
            pct_label = f"{pct}%" if has_quota else "н/д"

            cb_data = f"ag_switch:{acc_id}"
            if len(cb_data.encode("utf-8")) > 64:
                short_hash = hashlib.sha256(acc_id.encode("utf-8")).hexdigest()[:16]
                cb_data = f"ag_switch:#{short_hash}"

            if is_active:
                btn = {
                    "text": f"{name} (Активен)",
                    "style": "success",
                    "callback_data": "ag_noop",
                }
                if check_icon:
                    btn["icon_custom_emoji_id"] = check_icon
            else:
                btn = {
                    "text": f"{name} ({pct_label})",
                    "style": "primary",
                    "callback_data": cb_data,
                }
            rows.append([btn])

        if len(accounts) > 25:
            rows.append([{
                "text": f"📋 Показаны 25 из {len(accounts)} аккаунтов",
                "callback_data": "ag_noop",
            }])

        # 2. Auto-switch, Heal & Restart actions
        action_row = [
            {
                "text": "Авто-выбор лучшего",
                "style": "primary",
                "icon_custom_emoji_id": gear_icon,
                "callback_data": "ag_switch:auto",
            },
            {
                "text": "Лечение AG & VPN",
                "icon_custom_emoji_id": refresh_icon,
                "callback_data": "ag_heal",
            },
        ]
        rows.append(action_row)

        rows.append([
            {
                "text": "Перезапуск IDE",
                "icon_custom_emoji_id": term_icon,
                "callback_data": "ag_restart",
            }
        ])

        # 3. Bottom controls
        bottom_row = [
            {
                "text": "Обновить",
                "style": "primary",
                "callback_data": "ag_refresh",
            },
            {
                "text": "Меню",
                "callback_data": "menu",
            },
        ]
        if refresh_icon:
            bottom_row[0]["icon_custom_emoji_id"] = refresh_icon
        if back_icon:
            bottom_row[1]["icon_custom_emoji_id"] = back_icon

        rows.append(bottom_row)
        return {"inline_keyboard": rows}



