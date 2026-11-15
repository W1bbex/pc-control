#!/usr/bin/env python3
"""Power Flow Presentation & Keyboard Markup Layer.

Pure presentation layer conforming to User Rule 3:
- Accepts state DTOs and parameters.
- Returns formatted strings and Telegram inline markups.
- Contains ZERO network I/O, database access, or state mutations.
"""

from __future__ import annotations

import html
from typing import Any, Dict, Optional, Tuple

try:
    from .telegram_ui import ICONS
except ImportError:
    from telegram_ui import ICONS


def build_flow_btn(text: str, style: str, callback_data: str, icon: Optional[str] = None) -> Dict[str, Any]:
    """Helper to build consistent Telegram inline keyboard button dict."""
    btn: Dict[str, Any] = {"text": text, "style": style, "callback_data": callback_data}
    if icon:
        btn["icon_custom_emoji_id"] = icon
    return btn


def resolve_target_mode_info(
    auto_launch_workspace: bool,
    auto_launch_game: bool,
    target_workspace_vpn: Optional[str],
    target_game: Optional[str],
    target_game_vpn: Optional[str],
    stage: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolves target mode identifiers and human labels."""
    if auto_launch_workspace:
        vpn = target_workspace_vpn or "incy"
        vpn_label = "INCY VPN" if vpn == "incy" else "Happ VPN"
        return "workspace", vpn_label, f"Сетап ({vpn_label})"
    elif auto_launch_game:
        game = target_game or "Игра"
        game_map = {
            "minecraft": "Minecraft",
            "warcraft3": "Warcraft 3",
            "gta5": "GTA 5 Legacy",
            "roblox": "Roblox",
            "gtasa": "GTA SA",
        }
        game_label = game_map.get(str(game).lower(), str(game))
        vpn = target_game_vpn or "incy"
        vpn_label = "INCY VPN" if vpn == "incy" else "Happ VPN"
        return "game", f"{game_label} ({vpn_label})", f"Игра: {game_label}"
    elif stage == "UNLOCKING":
        return "unlock", None, "Разблок запланирован"
    return None, None, None


def get_unlocking_markup(auto_game: bool = False, auto_workspace: bool = False) -> Dict[str, Any]:
    """Builds inline keyboard for in-progress unlocking stage."""
    cancel_btn = build_flow_btn("Отмена", "danger", "flow_unlock_no", ICONS.get("cross"))
    if auto_game:
        top_btn = build_flow_btn("Игра запланирована", "success", "noop", ICONS.get("game") or ICONS.get("check"))
    elif auto_workspace:
        top_btn = build_flow_btn("Сетап запланирован", "success", "noop", ICONS.get("check"))
    else:
        top_btn = build_flow_btn("+ Запустить рабочий сетап", "primary", "flow_unlock_and_workspace", ICONS.get("apps"))
    return {"inline_keyboard": [[top_btn], [cancel_btn]]}


def get_unlock_success_markup(manual: bool = False) -> Tuple[str, Dict[str, Any]]:
    """Builds message text and markup for successful unlock."""
    status_title = "<b>ℹ️ Windows разблокирована вручную.</b>" if manual else "<b>✅ Windows разблокирована!</b>"
    happ_icon = ICONS.get("app_happ") or ICONS.get("network")
    incy_icon = ICONS.get("app_incy") or ICONS.get("network")
    cancel_btn = build_flow_btn("Не сейчас", "danger", "flow_workspace_no", ICONS.get("cross"))

    text = f"{status_title}\n\nЗапустить рабочий сетап? Выберите VPN:"
    markup = {
        "inline_keyboard": [
            [
                build_flow_btn("Сетап с INCY", "primary", "workspace_vpn:incy", incy_icon),
                build_flow_btn("Сетап с Happ", "primary", "workspace_vpn:happ", happ_icon),
            ],
            [cancel_btn],
        ]
    }
    return text, markup


def get_unlock_failed_markup(reason: str) -> Tuple[str, Dict[str, Any]]:
    """Builds message text and markup for failed unlock."""
    safe_reason = html.escape(str(reason))
    text = (
        f"<b>Ошибка разблокировки Windows</b>\n\n"
        f"{safe_reason}\n\n"
        "Пароль отклонён или экран входа недоступен."
    )
    retry_btn = build_flow_btn("Попробовать снова", "success", "flow_unlock_retry", ICONS.get("refresh"))
    cancel_btn = build_flow_btn("Отмена", "danger", "flow_unlock_cancel", ICONS.get("cross"))
    emerg_btn = build_flow_btn("⚡ Аварийное выключение", "danger", "emergency_power_off", ICONS.get("power"))
    markup = {"inline_keyboard": [[retry_btn, cancel_btn], [emerg_btn]]}
    return text, markup
