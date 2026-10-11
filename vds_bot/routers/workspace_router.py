#!/usr/bin/env python3
"""Workspace Router: handles callbacks for apps menu, single app launches, workspace setup, and game launches."""

import logging
import time

LOG = logging.getLogger("pc-control-bot.routers.workspace")


def route_workspace_callback(app, data: str, chat_id: int, message_id: int, callback_id: str) -> bool:
    """Handle workspace, game, and app callback queries. Returns True if handled, False otherwise."""
    try:
        from vds_bot.power_flow import PowerFlowStage
        from vds_bot.health_engine import PCState
        from vds_bot.telegram_ui import ICONS
    except ImportError:
        from power_flow import PowerFlowStage
        from health_engine import PCState
        from telegram_ui import ICONS

    GAME_INFO = {
        "gta5": {
            "id": "game_gta5",
            "name": "GTA 5 Legacy",
            "scenario": "Игровой сетап: GTA 5 Legacy",
            "emoji_id": ICONS.get("game"),
            "fallback_emoji": "🚗",
        },
        "roblox": {
            "id": "game_roblox",
            "name": "Roblox",
            "scenario": "Игровой сетап: Roblox",
            "emoji_id": ICONS.get("game"),
            "fallback_emoji": "🟥",
        },
        "gtasa": {
            "id": "game_gtasa",
            "name": "GTA SA",
            "scenario": "Игровой сетап: GTA SA",
            "emoji_id": ICONS.get("game"),
            "fallback_emoji": "🚗",
        },
    }

    # 1. Apps Menu Navigation
    if data == "apps_menu":
        app.ui.answer_callback(callback_id)
        app.ui.send_message(chat_id, app.ui.apps_menu_text(), reply_markup=app.ui.apps_menu_markup(), message_id=message_id)
        return True

    # 2. ESC Simulation
    if data == "press_esc":
        app.ui.answer_callback(callback_id, "⚡ Нажимаю ESC...")
        app.mqtt.publish_desktop_action("press_esc", {"count": 1})
        return True

    if data == "noop":
        app.ui.answer_callback(callback_id)
        return True

    # 3. Unlock Flow Decisions
    if data == "flow_unlock_and_workspace":
        app.ui.answer_callback(callback_id)
        text = "<b>🌐 Выберите VPN для рабочего сетапа:</b>\n\nСетап и VPN запустятся автоматически сразу после разблокировки ПК."
        markup = app.ui.workspace_vpn_choice_markup(active_vpn=getattr(app, "chosen_vpn", "incy"), prefix="flow_workspace_vpn")
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    if data.startswith("flow_workspace_vpn:"):
        selected_vpn = data.split(":", 1)[1] if ":" in data else "incy"
        app.chosen_vpn = selected_vpn
        vpn_name = "INCY" if selected_vpn == "incy" else "Happ"
        app.ui.answer_callback(callback_id, f"🚀 Сетап ({vpn_name} VPN) запланирован!")
        app.power_flow.chat_id = chat_id
        app.power_flow.message_id = message_id
        app.power_flow.set_auto_launch_workspace(True, vpn=selected_vpn, chat_id=chat_id)
        return True

    if data in {"flow_unlock_yes", "flow_unlock_no"}:
        app.ui.answer_callback(callback_id)
        app.power_flow.handle_unlock_decision(data, chat_id)
        return True

    if data == "flow_unlock_retry":
        app.ui.answer_callback(callback_id, "Повторяю разблокировку...")
        app.power_flow.handle_unlock_retry(chat_id)
        return True

    if data == "flow_unlock_cancel":
        app.ui.answer_callback(callback_id)
        app.power_flow.handle_unlock_cancel(chat_id)
        return True

    # 4. Game Setup Flow
    if data == "flow_unlock_and_game":
        app.ui.answer_callback(callback_id)
        text = "<b>🎮 Выберите игру для запуска:</b>\n\nИгра и VPN запустятся автоматически после разблокировки ПК."
        markup = app.ui.game_choice_markup("flow_game")
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    if data.startswith("flow_game:"):
        game_key = data.split(":", 1)[1]
        g_info = GAME_INFO.get(game_key, {})
        game_name = g_info.get("name", game_key)
        app.ui.answer_callback(callback_id, f"Выбрана {game_name}")
        text = f"<b>🌐 Выберите VPN для {game_name}:</b>"
        markup = app.ui.game_vpn_choice_markup(game_key, "flow_game_vpn")
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    if data.startswith("flow_game_vpn:"):
        parts = data.split(":")
        game_key = parts[1] if len(parts) > 1 else "gta5"
        selected_vpn = parts[2] if len(parts) > 2 else "incy"
        g_info = GAME_INFO.get(game_key, {})
        game_name = g_info.get("name", game_key)
        vpn_name = "INCY" if selected_vpn == "incy" else "Happ"
        app.ui.answer_callback(callback_id, f"🎮 {game_name} ({vpn_name} VPN) запланирована!")
        app.power_flow.chat_id = chat_id
        app.power_flow.message_id = message_id
        app.power_flow.set_auto_launch_game(game=game_key, vpn=selected_vpn, chat_id=chat_id)
        return True

    if data == "game_start_direct":
        pc_state = app.mqtt.health.evaluate_pc_state(app.monitoring.telemetry_age())
        if pc_state.session_status == "unlocked" or getattr(pc_state, "state", None) in (PCState.READY, PCState.DEGRADED):
            app.ui.answer_callback(callback_id)
            text = "<b>🎮 Запуск игрового сетапа</b>\n\nВыберите игру для запуска:"
            markup = app.ui.game_choice_markup("direct_game")
            app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        elif pc_state.state == "LOCKED":
            app.ui.answer_callback(callback_id)
            text = "<b>Windows не разблокирована.</b>\n\nРазблокировать сейчас?"
            markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": "Разблокировать и запустить игру",
                            "style": "success",
                            "icon_custom_emoji_id": ICONS.get("game"),
                            "callback_data": "flow_unlock_and_game",
                        },
                    ],
                    [
                        {
                            "text": "Отмена",
                            "style": "danger",
                            "icon_custom_emoji_id": ICONS.get("cross"),
                            "callback_data": "menu",
                        },
                    ]
                ]
            }
            app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        else:
            app.ui.answer_callback(callback_id)
            text = f"<b>{pc_state.label}</b>\n\n{pc_state.reason}\n\nВключить ПК сейчас?"
            markup = {
                "inline_keyboard": [
                    [{"text": "Включить ПК", "style": "success", "callback_data": "power_on"}],
                    app.ui.back_button("menu"),
                ]
            }
            app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    if data.startswith("direct_game:"):
        game_key = data.split(":", 1)[1]
        g_info = GAME_INFO.get(game_key, {})
        game_name = g_info.get("name", game_key)
        app.ui.answer_callback(callback_id, f"Выбрана {game_name}")
        text = f"<b>🌐 Выберите VPN для {game_name}:</b>"
        markup = app.ui.game_vpn_choice_markup(game_key, "direct_game_vpn")
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    if data.startswith("direct_game_vpn:"):
        parts = data.split(":")
        game_key = parts[1] if len(parts) > 1 else "gta5"
        selected_vpn = parts[2] if len(parts) > 2 else "incy"
        g_info = GAME_INFO.get(game_key, {})
        game_name = g_info.get("name", game_key)
        vpn_name = "INCY" if selected_vpn == "incy" else "Happ"
        app.ui.answer_callback(callback_id, f"Запускаю {game_name} ({vpn_name} VPN)...")
        app.power_flow.chat_id = chat_id
        app.power_flow.message_id = message_id
        app.power_flow.stage = PowerFlowStage.WORKSPACE_RUNNING
        app.power_flow.workspace_cancelled = False
        app.power_flow.active_workspace_id = f"game-{int(time.time())}"
        app.power_flow.save_state()
        app.start_async_game_setup(
            chat_id,
            card_message_id=message_id,
            game=game_key,
            vpn=selected_vpn,
            workspace_id=app.power_flow.active_workspace_id,
        )
        return True

    # 5. Workspace Flow
    if data in {"flow_workspace_yes", "flow_workspace_no"}:
        if data == "flow_workspace_yes":
            app.ui.answer_callback(callback_id)
            text = app.ui.workspace_vpn_choice_text() if hasattr(app.ui, "workspace_vpn_choice_text") else ""
            if not isinstance(text, str) or not text:
                text = "<b>Запуск рабочего сетапа</b>\n\nВыберите VPN для подключения:"
            markup = app.ui.workspace_vpn_choice_markup(active_vpn=getattr(app, "chosen_vpn", "incy"))
            app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
            return True
        app.ui.answer_callback(callback_id)
        app.power_flow.handle_workspace_decision(data, chat_id, app.start_async_workspace_setup)
        return True

    if data in {"workspace_vpn:incy", "workspace_vpn:happ"}:
        selected_vpn = data.split(":", 1)[1]
        app.chosen_vpn = selected_vpn
        vpn_name = "INCY" if selected_vpn == "incy" else "Happ"
        app.ui.answer_callback(callback_id, f"Выбран {vpn_name} VPN! Запускаю...")
        app.power_flow.chat_id = chat_id
        app.power_flow.message_id = message_id
        app.power_flow.stage = PowerFlowStage.WORKSPACE_RUNNING
        app.power_flow.workspace_cancelled = False
        app.power_flow.active_workspace_id = f"ws-{int(time.time())}"
        app.power_flow.save_state()
        app.start_async_workspace_setup(
            chat_id,
            card_message_id=message_id,
            workspace_id=app.power_flow.active_workspace_id,
            vpn=selected_vpn,
        )
        return True

    if data == "workspace_start_direct":
        pc_state = app.mqtt.health.evaluate_pc_state(app.monitoring.telemetry_age())
        if pc_state.session_status == "unlocked" or getattr(pc_state, "state", None) in (PCState.READY, PCState.DEGRADED):
            app.ui.answer_callback(callback_id)
            text = app.ui.workspace_vpn_choice_text() if hasattr(app.ui, "workspace_vpn_choice_text") else ""
            if not isinstance(text, str) or not text:
                text = "<b>Запуск рабочего сетапа</b>\n\nВыберите VPN для подключения:"
            markup = app.ui.workspace_vpn_choice_markup(active_vpn=getattr(app, "chosen_vpn", "incy"))
            app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        elif pc_state.state == "LOCKED":
            app.ui.answer_callback(callback_id)
            text = "<b>Windows не разблокирована.</b>\n\nРазблокировать сейчас?"
            markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": "Разблокировать и запустить",
                            "style": "success",
                            "icon_custom_emoji_id": ICONS.get("apps"),
                            "callback_data": "flow_unlock_and_workspace",
                        },
                    ],
                    [
                        {
                            "text": "Отмена",
                            "style": "danger",
                            "icon_custom_emoji_id": ICONS.get("cross"),
                            "callback_data": "menu",
                        },
                    ]
                ]
            }
            app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        else:
            app.ui.answer_callback(callback_id)
            text = f"<b>{pc_state.label}</b>\n\n{pc_state.reason}\n\nВключить ПК сейчас?"
            markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": "Включить ПК",
                            "style": "success",
                            "icon_custom_emoji_id": ICONS.get("power"),
                            "callback_data": "power_on",
                        },
                    ],
                    [
                        {
                            "text": "Отмена",
                            "style": "danger",
                            "icon_custom_emoji_id": ICONS.get("cross"),
                            "callback_data": "menu",
                        },
                    ]
                ]
            }
            app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    if data == "flow_workspace_cancel":
        app.ui.answer_callback(callback_id, "Отменяю...")
        app.power_flow.cancel_workspace(chat_id)
        app.workspace_cancel_event.set()
        return True

    # 6. Single App Launch
    if data.startswith("app:"):
        app_name = data.split(":", 1)[1]
        if getattr(app.mqtt, "desktop_status", None) != "online":
            app.ui.answer_callback(
                callback_id,
                "ПК заблокирован или выключен. Разблокируйте Windows для запуска программ.",
                alert=True,
            )
            return True
        app.ui.answer_callback(callback_id, f"Запускаю {app_name}...")
        app.mqtt.publish_desktop_action("launch_app", {"app": app_name})
        return True

    return False
