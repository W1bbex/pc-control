#!/usr/bin/env python3
"""Console Router: handles callbacks for interactive terminal, target/shell toggling, and quick commands."""

import logging

LOG = logging.getLogger("pc-control-bot.routers.console")


def route_console_callback(app, data: str, chat_id: int, message_id: int, callback_id: str) -> bool:
    """Handle console callback queries. Returns True if handled, False otherwise."""
    # 1. Open Console
    if data == "console_open":
        app.ui.answer_callback(callback_id)
        app.console_active_chat_id = chat_id
        app.current_menu_view = "console"
        text = app.ui.console_card_text(target=app.console_target, shell=app.console_shell)
        markup = app.ui.console_menu_markup(target=app.console_target, shell=app.console_shell)
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    # 2. Close Console
    if data == "console_close":
        app.ui.answer_callback(callback_id)
        app.console_active_chat_id = None
        app.current_menu_view = "main"
        if message_id:
            app.active_main_menu_message_id = message_id
        if hasattr(app, "save_state"):
            app.save_state()
        app.ui.send_message(
            chat_id,
            app.monitoring.dashboard_text(),
            reply_markup=app._main_menu_markup(),
            message_id=message_id,
        )
        return True

    # 3. Toggle Shell (CMD <-> PowerShell)
    if data == "console_toggle_shell":
        app.ui.answer_callback(callback_id)
        app.console_shell = "cmd" if app.console_shell == "powershell" else "powershell"
        app.save_state()
        text = app.ui.console_card_text(target=app.console_target, shell=app.console_shell)
        markup = app.ui.console_menu_markup(target=app.console_target, shell=app.console_shell)
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    # 4. Toggle Target (Desktop <-> SYSTEM)
    if data == "console_toggle_target":
        app.ui.answer_callback(callback_id)
        app.console_target = "system" if app.console_target == "desktop" else "desktop"
        app.save_state()
        text = app.ui.console_card_text(target=app.console_target, shell=app.console_shell)
        markup = app.ui.console_menu_markup(target=app.console_target, shell=app.console_shell)
        app.ui.send_message(chat_id, text, reply_markup=markup, message_id=message_id)
        return True

    # 5. Quick Console Commands
    if data.startswith("console_quick:"):
        q_cmd = data.split(":", 1)[1]
        app.ui.answer_callback(callback_id, f"Выполняю {q_cmd}...")
        app.execute_console_command(chat_id, q_cmd, target=app.console_target, shell=app.console_shell)
        return True

    return False
