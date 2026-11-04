#!/usr/bin/env python3
"""Antigravity Router: manages remote account switching and quota inspection."""

import logging
import time
from typing import Optional

LOG = logging.getLogger("pc-control-bot.routers.antigravity")


def route_antigravity_command(app, chat_id: int, text: str) -> bool:
    """Handle /antigravity, /ag, /fix_ag and text commands. Returns True if handled."""
    lower = text.lower().strip()
    if lower in {"/fix_ag", "/heal_ag", "/ag_fix", "почини аг", "лечение аг", "лечение antigravity"}:
        heal_antigravity(app, chat_id)
        return True
    if lower in {"/antigravity", "/ag", "antigravity", "аккаунты", "профили", "аккаунты антигравити"}:
        open_antigravity_menu(app, chat_id)
        return True
    if lower.startswith(("/ag ", "/antigravity ")):
        parts = text.split(None, 1)
        sub = parts[1].strip() if len(parts) > 1 else ""
        sub_lower = sub.lower()
        if sub_lower in ("heal", "fix", "лечение", "почини"):
            heal_antigravity(app, chat_id)
            return True
        elif sub_lower in ("auto", "авто", "лучший", "best"):
            switch_antigravity_account(app, chat_id, account_id="auto")
            return True
        elif sub_lower in ("restart", "рестарт", "перезапуск"):
            restart_antigravity_ide(app, chat_id)
            return True
        elif sub_lower in ("export", "экспорт"):
            export_antigravity_accounts(app, chat_id)
            return True
        else:
            # Maybe email or account id
            switch_antigravity_account(app, chat_id, account_id=sub)
            return True
    return False


def route_antigravity_callback(app, data: str, chat_id: int, message_id: int, callback_id: str) -> bool:
    """Handle inline button callbacks for Antigravity menu."""
    if data == "ag_noop":
        app.ui.answer_callback(callback_id, "Этот профиль уже активен.")
        return True

    if data in {"antigravity_menu", "ag_refresh"}:
        app.ui.answer_callback(callback_id, "Обновляю данные профилей...")
        open_antigravity_menu(app, chat_id, message_id=message_id)
        return True

    if data.startswith("ag_switch:"):
        account_id = data.split(":", 1)[1].strip()
        app.ui.answer_callback(callback_id, "Переключаю аккаунт...")
        switch_antigravity_account(app, chat_id, account_id=account_id, message_id=message_id)
        return True

    if data == "ag_restart":
        app.ui.answer_callback(callback_id, "Перезапускаю IDE...")
        restart_antigravity_ide(app, chat_id, message_id=message_id)
        return True

    if data in ("ag_heal", "ag_fix"):
        app.ui.answer_callback(callback_id, "Запускаю лечение VPN и Antigravity...")
        heal_antigravity(app, chat_id, message_id=message_id)
        return True

    return False


def open_antigravity_menu(app, chat_id: int, message_id: Optional[int] = None) -> None:
    """Requests account list from desktop agent and renders/updates card."""
    app.current_menu_view = "antigravity_menu"
    if not hasattr(app, "pending_antigravity_cmd_ids"):
        app.pending_antigravity_cmd_ids = {}

    # If desktop is offline, warn or show cached data
    if getattr(app.mqtt, "desktop_status", None) != "online":
        cached = getattr(app, "latest_antigravity_data", None)
        if cached:
            text = app.ui.antigravity_card_text(cached) + "\n\n<i>⚠️ Desktop-агент не в сети (показаны сохраненные квоты).</i>"
            app.ui.send_message(chat_id, text, reply_markup=app.ui.antigravity_card_markup(cached), message_id=message_id)
            return
        app.ui.send_message(
            chat_id,
            "⚠️ <b>ПК сейчас не в сети</b>\nУправление профилями Antigravity доступно при включенном ПК.",
            message_id=message_id,
        )
        return

    cmd_id = app.mqtt.publish_desktop_action("antigravity_list", {})
    if cmd_id:
        app.pending_antigravity_cmd_ids[cmd_id] = (chat_id, message_id, "list")
        if not hasattr(app, "pending_cmd_timestamps"):
            app.pending_cmd_timestamps = {}
        app.pending_cmd_timestamps[cmd_id] = time.time()


def switch_antigravity_account(app, chat_id: int, account_id: str, message_id: Optional[int] = None) -> None:
    """Sends switch command to desktop agent."""
    if not hasattr(app, "pending_antigravity_cmd_ids"):
        app.pending_antigravity_cmd_ids = {}

    if getattr(app.mqtt, "desktop_status", None) != "online":
        app.ui.send_message(chat_id, "❌ Desktop-агент не в сети. Невозможно сменить профиль.", message_id=message_id)
        return

    cmd_id = app.mqtt.publish_desktop_action("antigravity_switch", {"account_id": account_id, "restart": True})
    if cmd_id:
        app.pending_antigravity_cmd_ids[cmd_id] = (chat_id, message_id, "switch")
        if not hasattr(app, "pending_cmd_timestamps"):
            app.pending_cmd_timestamps = {}
        app.pending_cmd_timestamps[cmd_id] = time.time()


def restart_antigravity_ide(app, chat_id: int, message_id: Optional[int] = None) -> None:
    """Sends restart IDE command to desktop agent."""
    if not hasattr(app, "pending_antigravity_cmd_ids"):
        app.pending_antigravity_cmd_ids = {}

    if getattr(app.mqtt, "desktop_status", None) != "online":
        app.ui.send_message(chat_id, "❌ Desktop-агент не в сети.", message_id=message_id)
        return

    cmd_id = app.mqtt.publish_desktop_action("antigravity_restart", {})
    if cmd_id:
        app.pending_antigravity_cmd_ids[cmd_id] = (chat_id, message_id, "restart")
        if not hasattr(app, "pending_cmd_timestamps"):
            app.pending_cmd_timestamps = {}
        app.pending_cmd_timestamps[cmd_id] = time.time()


def export_antigravity_accounts(app, chat_id: int) -> None:
    """Exports accounts from desktop agent."""
    if not hasattr(app, "pending_antigravity_cmd_ids"):
        app.pending_antigravity_cmd_ids = {}

    if getattr(app.mqtt, "desktop_status", None) != "online":
        app.ui.send_message(chat_id, "❌ Desktop-агент не в сети.")
        return

    cmd_id = app.mqtt.publish_desktop_action("antigravity_export", {})
    if cmd_id:
        app.pending_antigravity_cmd_ids[cmd_id] = (chat_id, None, "export")
        if not hasattr(app, "pending_cmd_timestamps"):
            app.pending_cmd_timestamps = {}
        app.pending_cmd_timestamps[cmd_id] = time.time()


def heal_antigravity(app, chat_id: int, message_id: Optional[int] = None) -> None:
    """Sends heal/fix command to desktop agent to rotate VPN and restart Antigravity."""
    if not hasattr(app, "pending_antigravity_cmd_ids"):
        app.pending_antigravity_cmd_ids = {}

    if getattr(app.mqtt, "desktop_status", None) != "online":
        app.ui.send_message(chat_id, "❌ Desktop-агент не в сети. Лечение недоступно при выключенном ПК.", message_id=message_id)
        return

    cmd_id = app.mqtt.publish_desktop_action("heal_antigravity", {"attempts": 3})
    if cmd_id:
        app.pending_antigravity_cmd_ids[cmd_id] = (chat_id, message_id, "heal")
        if not hasattr(app, "pending_cmd_timestamps"):
            app.pending_cmd_timestamps = {}
        app.pending_cmd_timestamps[cmd_id] = time.time()
        app.ui.send_message(
            chat_id,
            "🔄 <b>Запущено авто-лечение Antigravity & VPN:</b>\n"
            "├ Завершаю зависшие процессы IDE...\n"
            "├ Переключаю сервер INCY VPN на рабочую страну...\n"
            "├ Проверяю HTTPS-связь с серверами Google...\n"
            "└ Перезапускаю Antigravity IDE."
        )
