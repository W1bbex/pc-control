#!/usr/bin/env python3
"""Workspace Router: handles callbacks for apps menu, single app launches, workspace setup, and game launches."""

import html
import logging
import time

LOG = logging.getLogger("pc-control-bot.routers.workspace")

try:
    from ..workspace_runner import GAME_INFO
except ImportError:
    from workspace_runner import GAME_INFO


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

    # 1. Apps Menu Navigation
    if data == "apps_menu":
        app.current_menu_view = "apps_menu"
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

    if data == "power_game":
        app.current_menu_view = "game_menu"
        app.ui.answer_callback(callback_id)
        app.ui.send_message(chat_id, "<b>Игра после включения ПК</b>\n\nВыберите игру, затем VPN.",
                            reply_markup=app.ui.game_choice_markup("power_game"), message_id=message_id)
        return True

    if data.startswith("power_game:"):
        game = data.split(":", 1)[1]
        if game not in GAME_INFO:
            return False
        app.ui.answer_callback(callback_id)
        game_name = html.escape(GAME_INFO[game]["name"])
        app.ui.send_message(chat_id, f"<b>{game_name}</b>\n\nВыберите VPN. ПК включится, а игра запустится после входа в Windows.",
                            reply_markup=app.ui.game_vpn_choice_markup(game, "power_start_game"), message_id=message_id)
        return True

    # 3. Unlock Flow Decisions
    if data == "flow_unlock_and_workspace":
        app.ui.answer_callback(callback_id)
        if hasattr(app, "power_flow"):
            app.power_flow.selecting_mode = True
        text = "<b>🌐 Выберите VPN для рабочего сетапа:</b>\n\nСетап и VPN запустятся автоматически сразу после разблокировки ПК."
        markup = app.ui.workspace_vpn_choice_markup(active_vpn=getattr(app, "chosen_vpn", "incy"), prefix="flow_workspace_vpn")
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    if data.startswith("flow_workspace_vpn:"):
        selected_vpn = data.split(":", 1)[1] if ":" in data else "incy"
        app.chosen_vpn = selected_vpn
        if hasattr(app, "save_state"):
            app.save_state()
        vpn_name = "INCY" if selected_vpn == "incy" else "Happ"
        app.ui.answer_callback(callback_id, f"🚀 Сетап ({vpn_name} VPN) запланирован!")
        if hasattr(app, "power_flow"):
            app.power_flow.selecting_mode = False
            app.power_flow.set_auto_launch_workspace(True, vpn=selected_vpn, chat_id=chat_id, message_id=message_id)
        return True

    if data in {"flow_unlock_yes", "flow_unlock_no"}:
        app.ui.answer_callback(callback_id)
        if hasattr(app, "power_flow"):
            app.power_flow.selecting_mode = False
        app.power_flow.handle_unlock_decision(data, chat_id, message_id=message_id)
        return True

    if data == "flow_unlock_retry":
        app.ui.answer_callback(callback_id, "Повторяю разблокировку...")
        if hasattr(app, "power_flow"):
            app.power_flow.selecting_mode = False
        app.power_flow.handle_unlock_retry(chat_id)
        return True

    if data == "flow_unlock_cancel":
        app.ui.answer_callback(callback_id)
        if hasattr(app, "power_flow"):
            app.power_flow.selecting_mode = False
        app.power_flow.handle_unlock_cancel(chat_id)
        return True

    # 4. Game Setup Flow
    if data == "flow_unlock_and_game":
        app.current_menu_view = "game_menu"
        app.ui.answer_callback(callback_id)
        if hasattr(app, "power_flow"):
            app.power_flow.selecting_mode = True
        game_icon = ICONS.get("game")
        game_tag = f'<tg-emoji emoji-id="{game_icon}">🎮</tg-emoji> ' if game_icon else ""
        text = f"<b>{game_tag}Выберите игру для запуска:</b>\n\nИгра и VPN запустятся автоматически после разблокировки ПК."
        markup = app.ui.game_choice_markup("flow_game")
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    if data.startswith("flow_game:"):
        app.current_menu_view = "game_menu"
        if hasattr(app, "power_flow"):
            app.power_flow.selecting_mode = True
        game_key = data.split(":", 1)[1]
        g_info = GAME_INFO.get(game_key, {})
        game_name = g_info.get("name", game_key)
        app.ui.answer_callback(callback_id, f"Выбрана {game_name}")
        net_icon = ICONS.get("network") or ICONS.get("wifi")
        net_tag = f'<tg-emoji emoji-id="{net_icon}">🌐</tg-emoji> ' if net_icon else ""
        text = f"<b>{net_tag}Выберите VPN для {html.escape(str(game_name))}:</b>"
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
        app.ui.answer_callback(callback_id, f"{game_name} ({vpn_name} VPN) запланирована!")
        if hasattr(app, "power_flow"):
            app.power_flow.selecting_mode = False
            app.power_flow.set_auto_launch_game(game=game_key, vpn=selected_vpn, chat_id=chat_id, message_id=message_id)
        return True

    if data == "game_start_direct":
        app.current_menu_view = "game_menu"
        pc_state = app.mqtt.health.evaluate_pc_state(app.monitoring.telemetry_age())
        if pc_state.session_status == "unlocked":
            app.ui.answer_callback(callback_id)
            game_icon = ICONS.get("game")
            game_tag = f'<tg-emoji emoji-id="{game_icon}">🎮</tg-emoji> ' if game_icon else ""
            text = f"<b>{game_tag}Запуск игрового сетапа</b>\n\nВыберите игру для запуска:"
            markup = app.ui.game_choice_markup("direct_game")
            app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        elif pc_state.state in (PCState.LOCKED, PCState.BOOTING, PCState.DEGRADED):
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
            safe_label = html.escape(str(pc_state.label or ""))
            safe_reason = html.escape(str(pc_state.reason or ""))
            text = f"<b>{safe_label}</b>\n\n{safe_reason}\n\nВыберите игру для запуска после включения ПК."
            markup = app.ui.game_choice_markup("power_game")
            app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    if data.startswith("direct_game:"):
        game_key = data.split(":", 1)[1]
        g_info = GAME_INFO.get(game_key, {})
        game_name = g_info.get("name", game_key)
        app.ui.answer_callback(callback_id, f"Выбрана {game_name}")
        text = f"<b>🌐 Выберите VPN для {html.escape(str(game_name))}:</b>"
        markup = app.ui.game_vpn_choice_markup(game_key, "direct_game_vpn")
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    if data.startswith("direct_game_vpn:"):
        parts = data.split(":")
        game_key = parts[1] if len(parts) > 1 else "gta5"
        selected_vpn = parts[2] if len(parts) > 2 else "incy"
        sess_status = getattr(app.mqtt, "system_session", None)
        if sess_status != "unlocked":
            app.ui.answer_callback(callback_id, "🔒 Windows заблокирована. Сначала разблокируйте ПК.", alert=True)
            return True
        if getattr(app.power_flow, "stage", None) == PowerFlowStage.WORKSPACE_RUNNING:
            app.ui.answer_callback(callback_id, "ℹ️ Сценарий уже запускается.", alert=True)
            return True
        g_info = GAME_INFO.get(game_key, {})
        game_name = g_info.get("name", game_key)
        vpn_name = "INCY" if selected_vpn == "incy" else "Happ"
        app.ui.answer_callback(callback_id, f"Запускаю {game_name} ({vpn_name} VPN)...")
        ws_id = app.power_flow.start_direct_setup(chat_id, message_id=message_id, prefix="game")
        app.start_async_game_setup(
            chat_id,
            card_message_id=message_id,
            game=game_key,
            vpn=selected_vpn,
            workspace_id=ws_id,
        )
        return True

    # 5. Workspace Flow
    if data == "flow_workspace_no":
        app.ui.answer_callback(callback_id)
        app.power_flow.handle_workspace_decision(data, chat_id, app.start_async_workspace_setup, message_id=message_id)
        return True

    if data in {"workspace_vpn:incy", "workspace_vpn:happ"}:
        selected_vpn = data.split(":", 1)[1]
        sess_status = getattr(app.mqtt, "system_session", None)
        if sess_status != "unlocked":
            app.ui.answer_callback(callback_id, "🔒 Windows заблокирована. Сначала разблокируйте ПК.", alert=True)
            return True
        if getattr(app.power_flow, "stage", None) == PowerFlowStage.WORKSPACE_RUNNING:
            app.ui.answer_callback(callback_id, "ℹ️ Сценарий уже запускается.", alert=True)
            return True
        app.chosen_vpn = selected_vpn
        if hasattr(app, "save_state"):
            app.save_state()
        vpn_name = "INCY" if selected_vpn == "incy" else "Happ"
        app.ui.answer_callback(callback_id, f"Выбран {vpn_name} VPN! Запускаю...")
        ws_id = app.power_flow.start_direct_setup(chat_id, message_id=message_id, prefix="ws")
        app.start_async_workspace_setup(
            chat_id,
            card_message_id=message_id,
            workspace_id=ws_id,
            vpn=selected_vpn,
        )
        return True

    if data == "workspace_start_direct":
        app.current_menu_view = "workspace_menu"
        pc_state = app.mqtt.health.evaluate_pc_state(app.monitoring.telemetry_age())
        if pc_state.session_status == "unlocked":
            app.ui.answer_callback(callback_id)
            text = app.ui.workspace_vpn_choice_text() if hasattr(app.ui, "workspace_vpn_choice_text") else ""
            if not isinstance(text, str) or not text:
                text = "<b>Запуск рабочего сетапа</b>\n\nВыберите VPN для подключения:"
            markup = app.ui.workspace_vpn_choice_markup(active_vpn=getattr(app, "chosen_vpn", "incy"))
            app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        elif pc_state.state in (PCState.LOCKED, PCState.BOOTING, PCState.DEGRADED):
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
            safe_label = html.escape(str(pc_state.label or ""))
            safe_reason = html.escape(str(pc_state.reason or ""))
            text = f"<b>{safe_label}</b>\n\n{safe_reason}\n\nВыберите VPN. ПК включится, а сетап запустится после входа в Windows."
            markup = app.ui.workspace_vpn_choice_markup(prefix="power_start")
            app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    if data == "flow_workspace_cancel":
        app.ui.answer_callback(callback_id, "Отменяю...")
        app.workspace_cancel_event.set()
        app.power_flow.cancel_workspace(chat_id)
        return True

    # 6. Single App Launch
    if data == "codex_diagnostics":
        app.ui.answer_callback(callback_id)
        icon = ICONS.get("app_chatgpt") or ICONS.get("apps")
        markup = {"inline_keyboard": [
            [{"text": "1 · Напрямую", "style": "primary", "icon_custom_emoji_id": icon, "callback_data": "codex_test:1"}],
            [{"text": "2 · Ярлык через Проводник", "style": "primary", "icon_custom_emoji_id": icon, "callback_data": "codex_test:2"}],
            [{"text": "3 · Приложение Windows", "style": "primary", "icon_custom_emoji_id": icon, "callback_data": "codex_test:3"}],
            app.ui.back_button("apps_menu"),
        ]}
        app.ui.send_message(chat_id, "<b>Проверка запуска Codex</b>\n\nЗакрой Codex полностью и выбери вариант. Проверка займёт до 15 секунд.", reply_markup=markup, message_id=message_id)
        return True

    if data in {"codex_test:1", "codex_test:2", "codex_test:3"}:
        if getattr(app.mqtt, "desktop_status", None) != "online":
            app.ui.answer_callback(callback_id, "ПК заблокирован или выключен.", alert=True)
            return True
        variant = data.rsplit(":", 1)[1]
        command_id = app.mqtt.publish_desktop_action("diagnose_codex_launch", {"variant": variant}, timeout=40)
        app.ui.answer_callback(callback_id, "Проверяю запуск..." if command_id else "Агент недоступен", alert=not bool(command_id))
        return True

    if data == "app:Happ":
        if getattr(app.mqtt, "desktop_status", None) != "online":
            app.ui.answer_callback(callback_id, "ПК заблокирован или выключен.", alert=True)
            return True
        app.ui.answer_callback(callback_id)
        markup = {"inline_keyboard": [
            [{"text": "Открыть и подключить VPN", "style": "success", "icon_custom_emoji_id": ICONS.get("app_happ"), "callback_data": "happ_launch:connect"}],
            [{"text": "Только открыть Happ", "style": "primary", "icon_custom_emoji_id": ICONS.get("app_happ"), "callback_data": "happ_launch:open"}],
            app.ui.back_button("apps_menu"),
        ]}
        app.ui.send_message(chat_id, "<b>Запуск Happ</b>\n\nПодключить VPN или только открыть приложение?", reply_markup=markup, message_id=message_id)
        return True

    if data in {"happ_launch:connect", "happ_launch:open"}:
        if getattr(app.mqtt, "desktop_status", None) != "online":
            app.ui.answer_callback(callback_id, "ПК заблокирован или выключен.", alert=True)
            return True
        connect = data == "happ_launch:connect"
        cmd_id = app.mqtt.publish_desktop_action("launch_app", {"app": "Happ", "connect_vpn": connect})
        if cmd_id:
            app.ui.answer_callback(callback_id, "Запускаю Happ...")
            status_text = "<b>Happ запускается...</b>\n\nКоманда отправлена на desktop-агент."
            app.ui.send_message(
                chat_id,
                status_text,
                reply_markup={"inline_keyboard": [app.ui.back_button("apps_menu")]},
                message_id=message_id,
            )
        else:
            app.ui.answer_callback(callback_id, "❌ Не удалось отправить команду: сбой MQTT", alert=True)
        return True

    if data.startswith("app:"):
        app_name = data.split(":", 1)[1]
        if getattr(app.mqtt, "desktop_status", None) != "online":
            app.ui.answer_callback(
                callback_id,
                "ПК заблокирован или выключен. Разблокируйте Windows для запуска программ.",
                alert=True,
            )
            return True
        cmd_id = app.mqtt.publish_desktop_action("launch_app", {"app": app_name})
        if cmd_id:
            app.ui.answer_callback(callback_id, f"Запускаю {app_name}...")
        else:
            app.ui.answer_callback(callback_id, f"❌ Не удалось запустить {app_name}: сбой MQTT", alert=True)
        return True

    return False
