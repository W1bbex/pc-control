#!/usr/bin/env python3
"""
Workspace and Game Launch Automation Runner for PC Control Bot.
Handles launching applications, VPN connectivity verification, and progress reporting.
"""

import inspect
import logging
import threading
import time

try:
    from .telegram_ui import ICONS
except ImportError:
    from telegram_ui import ICONS

LOG = logging.getLogger("pc-control-bot.workspace")


def get_workspace_steps(vpn="incy"):
    vpn_is_happ = str(vpn).lower() == "happ"
    vpn_item = (
        {
            "id": "happ_vpn",
            "label": "Happ (VPN)",
            "emoji_id": ICONS.get("app_happ"),
            "fallback_emoji": "🌐",
            "icon_key": "app_happ",
        }
        if vpn_is_happ
        else {
            "id": "incy_vpn",
            "label": "INCY (VPN)",
            "emoji_id": ICONS.get("app_incy"),
            "fallback_emoji": "🌐",
            "icon_key": "app_incy",
        }
    )
    return [
        {
            "id": "telegram",
            "label": "Telegram",
            "emoji_id": ICONS.get("app_telegram") or ICONS.get("telegram"),
            "fallback_emoji": "💬",
            "icon_key": "telegram",
        },
        {
            "id": "yandex_music",
            "label": "Yandex Music",
            "emoji_id": ICONS.get("app_yandex_music") or ICONS.get("music"),
            "fallback_emoji": "🎵",
            "icon_key": "music",
        },
        vpn_item,
        {
            "id": "chrome",
            "label": "Google Chrome",
            "emoji_id": ICONS.get("app_chrome") or ICONS.get("chrome"),
            "fallback_emoji": "🌐",
            "icon_key": "chrome",
        },
        {
            "id": "antigravity",
            "label": "Antigravity",
            "emoji_id": ICONS.get("app_antigravity") or ICONS.get("terminal"),
            "fallback_emoji": "🚀",
            "icon_key": "terminal",
        },
    ]


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


def get_game_steps(game_key="gta5", vpn="incy"):
    vpn_is_happ = str(vpn).lower() == "happ"
    vpn_item = (
        {
            "id": "happ_vpn",
            "label": "Happ (VPN)",
            "emoji_id": ICONS.get("app_happ"),
            "fallback_emoji": "🌐",
            "icon_key": "app_happ",
        }
        if vpn_is_happ
        else {
            "id": "incy_vpn",
            "label": "INCY (VPN)",
            "emoji_id": ICONS.get("app_incy"),
            "fallback_emoji": "🌐",
            "icon_key": "app_incy",
        }
    )
    g_info = GAME_INFO.get(game_key, GAME_INFO["gta5"])
    return [
        vpn_item,
        {
            "id": "antigravity",
            "label": "Antigravity",
            "emoji_id": ICONS.get("app_antigravity") or ICONS.get("terminal"),
            "fallback_emoji": "🚀",
            "icon_key": "terminal",
        },
        {
            "id": g_info["id"],
            "label": g_info["name"],
            "emoji_id": g_info["emoji_id"],
            "fallback_emoji": g_info["fallback_emoji"],
            "icon_key": "game",
        },
    ]


def format_workspace_step_line(step, status_str):
    app_emoji_id = step.get("emoji_id")
    fallback = step.get("fallback_emoji", "📱")
    if app_emoji_id:
        app_icon_tag = f'<tg-emoji emoji-id="{app_emoji_id}">{fallback}</tg-emoji> '
    else:
        app_icon_tag = f"{fallback} "

    clock_id = ICONS.get("clock")
    check_id = ICONS.get("check")
    cross_id = ICONS.get("cross")
    refresh_id = ICONS.get("refresh")

    raw_status = str(status_str or "⏳ В очереди")
    if raw_status in ("⏳ В очереди", "queued", "None"):
        status_display = f'<tg-emoji emoji-id="{clock_id}">⏳</tg-emoji> В очереди' if clock_id else "⏳ В очереди"
    elif raw_status in ("🔄 Запуск...", "starting", "running"):
        status_display = f'<tg-emoji emoji-id="{refresh_id}">🔄</tg-emoji> Запуск...' if refresh_id else "🔄 Запуск..."
    elif raw_status in ("✅", "✅ Запущен", "verified", "already_running_verified"):
        status_display = f'<tg-emoji emoji-id="{check_id}">✅</tg-emoji>' if check_id else "✅"
    elif "❌" in raw_status or "Ошибка" in raw_status or raw_status == "failed":
        status_display = f'<tg-emoji emoji-id="{cross_id}">❌</tg-emoji> {raw_status.replace("❌", "").strip()}' if cross_id else raw_status
    else:
        status_display = raw_status

    return f"{app_icon_tag}{step['label']} — {status_display}"


WORKSPACE_STEPS = get_workspace_steps("happ")


class WorkspaceRunnerMixin:
    """
    Mixin for PcControlBotApp providing asynchronous workspace and gaming setup execution.
    """

    def _wait_event_or_cancel(self, event, timeout):
        start = time.time()
        while time.time() - start < timeout:
            if getattr(self, "workspace_cancel_event", threading.Event()).is_set():
                return False
            if event.wait(timeout=0.5):
                return True
        return False

    def _update_workspace_card_ui(self):
        msg_id = getattr(self, "workspace_card_msg_id", None)
        if not msg_id:
            return

        lines = [
            format_workspace_step_line(step, getattr(self, "app_statuses", {}).get(step["id"], "⏳ В очереди"))
            for step in getattr(self, "current_workspace_steps", WORKSPACE_STEPS)
        ]
        text = "<b>Запуск рабочего сетапа...</b>\n\n" + "\n".join(lines)

        cancel_btn = {"text": "Отменить запуск", "style": "danger", "callback_data": "flow_workspace_cancel"}
        if ICONS.get("cross"):
            cancel_btn["icon_custom_emoji_id"] = ICONS.get("cross")
        markup = {"inline_keyboard": [[cancel_btn]]}

        target_user = getattr(self, "allowed_user_id", None)
        self.ui.send_message(target_user, text, reply_markup=markup, message_id=msg_id)

    def start_async_workspace_setup(self, chat_id, card_message_id=None, workspace_id=None, vpn=None):
        if not hasattr(self, "_workspace_start_lock"):
            self._workspace_start_lock = threading.Lock()
        with self._workspace_start_lock:
            if getattr(self, "_workspace_running", False):
                LOG.warning("Workspace setup runner is already active, ignoring duplicate start request")
                return
            self._workspace_running = True

        selected_vpn = vpn or getattr(self, "chosen_vpn", None) or "incy"

        def _runner():
            try:
                sig = inspect.signature(self.run_workspace_setup_steps)
                if "vpn" in sig.parameters or any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                    self.run_workspace_setup_steps(chat_id, card_message_id, workspace_id, vpn=selected_vpn)
                else:
                    self.run_workspace_setup_steps(chat_id, card_message_id, workspace_id)
            except Exception:
                LOG.exception("Workspace setup runner crashed unexpectedly")
                if hasattr(self, "power_flow") and self.power_flow:
                    try:
                        self.power_flow.reset_flow()
                    except Exception:
                        pass
            finally:
                self._workspace_running = False

        threading.Thread(
            target=_runner,
            name="workspace-runner-thread",
            daemon=True,
        ).start()

    def run_workspace_setup_steps(self, chat_id, card_message_id=None, workspace_id=None, vpn=None):
        """
        Asynchronous workspace setup steps driven entirely by real progress ACKs from agent.py:
        - Issue launch_app command with workspace_id and selected vpn.
        - Checklist card updates dynamically as progress ACKs arrive with step_id.
        - Strict VPN check: verifies VPN ready ACK. Halts & renders error if VPN fails/times out.
        - Strict full execution ACK check: verifies launch_app executed ACK.
        - Strict verification check: every step must be verified (no queued/unverified checkmarks).
        - Strict minimize_all ACK check: verifies minimize_all executed ACK before rendering final success!
        """
        active_vpn = vpn or getattr(self, "chosen_vpn", None)
        if not active_vpn:
            if hasattr(self, "happ_ready_event") and not hasattr(self, "vpn_ready_event"):
                active_vpn = "happ"
            else:
                active_vpn = "incy"
        self.chosen_vpn = active_vpn
        steps = get_workspace_steps(active_vpn)
        self.current_workspace_steps = steps
        self.app_statuses = {step["id"]: "⏳ В очереди" for step in steps}
        self.workspace_cancel_event.clear()

        vpn_ev = getattr(self, "vpn_ready_event", None)
        if vpn_ev:
            vpn_ev.clear()
        happ_ev = getattr(self, "happ_ready_event", None)
        if happ_ev:
            happ_ev.clear()

        self.workspace_executed_event.clear()
        self.minimize_ack_event.clear()
        self.workspace_execution_result = None
        self.minimize_ack_result = None

        def render_checklist():
            lines = [
                format_workspace_step_line(step, self.app_statuses.get(step["id"], "⏳ В очереди"))
                for step in self.current_workspace_steps
            ]
            return "<b>Запуск рабочего сетапа...</b>\n\n" + "\n".join(lines)

        cancel_btn = {"text": "Отменить запуск", "style": "danger", "callback_data": "flow_workspace_cancel"}
        if ICONS.get("cross"):
            cancel_btn["icon_custom_emoji_id"] = ICONS.get("cross")
        markup = {"inline_keyboard": [[cancel_btn]]}

        res = self.ui.send_message(chat_id, render_checklist(), reply_markup=markup, message_id=card_message_id)
        msg_id = res["message_id"] if res and res.get("message_id") else card_message_id
        self.workspace_card_msg_id = msg_id

        # 0. Wait up to 15s for desktop agent to be online if needed
        if self.mqtt.desktop_status != "online":
            LOG.info("Desktop agent not online yet, waiting up to 15s...")
            for _ in range(30):
                if self.workspace_cancel_event.is_set():
                    self.power_flow.reset_flow()
                    return
                if self.mqtt.desktop_status == "online":
                    break
                time.sleep(0.5)

        if self.mqtt.desktop_status != "online":
            LOG.warning("Desktop agent still not online after 15s wait.")
            error_text = render_checklist() + "\n\n<b>Desktop-агент не в сети.</b>\nWindows загружается или desktop-агент ещё не подключился к брокеру."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить сетап", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": "workspace_start_direct"}],
                    [{"text": "Отмена", "style": "danger", "icon_custom_emoji_id": ICONS.get("cross"), "callback_data": "menu"}],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # Issue launch_app command to agent.py
        ws_id = workspace_id or f"desk-ws-{int(time.time())}"
        cmd_id = self.mqtt.publish_desktop_action(
            "launch_app",
            {"app": "Рабочий сетап", "workspace_id": ws_id, "vpn": active_vpn},
            timeout=120,
        )
        self.active_workspace_cmd_id = cmd_id

        # 1. Wait for Happ / INCY VPN readiness (up to 55 seconds)
        vpn_event = getattr(self, "vpn_ready_event", None) or getattr(self, "happ_ready_event", None)
        vpn_ready = self._wait_event_or_cancel(vpn_event, 55.0) if vpn_event else True

        if self.workspace_cancel_event.is_set():
            LOG.info("Runner thread detected cancellation during VPN wait. Halting.")
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.power_flow.reset_flow()
            return

        vpn_step_id = "incy_vpn" if active_vpn == "incy" else "happ_vpn"
        vpn_name = "INCY" if active_vpn == "incy" else "Happ"

        if not vpn_ready and (vpn_event and not vpn_event.is_set()):
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.app_statuses[vpn_step_id] = "❌ Ошибка VPN"
            error_text = render_checklist() + f"\n\n<b>Не удалось подтвердить подключение {vpn_name} (VPN).</b>\nЗапуск зависимых приложений отменён в целях безопасности."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить сетап", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": "workspace_start_direct"}],
                    [
                        {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                        {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                    ],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 2. Wait for full workspace scenario execution ACK from agent.py (up to 65 seconds)
        ws_executed = self._wait_event_or_cancel(self.workspace_executed_event, 65.0)

        if self.workspace_cancel_event.is_set():
            LOG.info("Runner thread detected cancellation during workspace execution wait. Halting.")
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.power_flow.reset_flow()
            return

        if not ws_executed or self.workspace_execution_result == "failed":
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            error_text = render_checklist() + "\n\n<b>Ошибка выполнения рабочего сетапа.</b>\nDesktop-агент сообщил о сбое или запуск превысил таймаут."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить сетап", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": "workspace_start_direct"}],
                    [
                        {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                        {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                    ],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 3. Strict verification check: every step must be strictly verified
        unverified = [s["label"] for s in self.current_workspace_steps if self.app_statuses.get(s["id"]) != "✅"]
        if unverified:
            notice_text = render_checklist() + f"\n\n<b>Рабочий сетап завершён с неполным подтверждением.</b>\nНе подтверждены: {', '.join(unverified)}"
            notice_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить сетап", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": "workspace_start_direct"}],
                    [
                        {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                        {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                    ],
                ]
            }
            self.ui.send_message(chat_id, notice_text, reply_markup=notice_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 4. Standalone minimize_all and wait for ACK (only if not already minimized by agent)
        already_minimized = (
            isinstance(self.workspace_execution_result, dict)
            and self.workspace_execution_result.get("desktop_minimized")
        )
        if not already_minimized:
            min_cmd_id = self.mqtt.publish_desktop_action("minimize_all")
            self.pending_minimize_cmd_id = min_cmd_id
            min_ack_received = self._wait_event_or_cancel(self.minimize_ack_event, 10.0)

            if self.workspace_cancel_event.is_set():
                LOG.info("Runner thread detected cancellation during minimize_all wait. Halting.")
                self.power_flow.reset_flow()
                return

            # Check min_ack_received and minimize_ack_result strictly
            if not min_ack_received or self.minimize_ack_result != "executed":
                notice_text = render_checklist() + "\n\n<b>Рабочий сетап запущен, но не удалось свернуть окна.</b>"
                notice_markup = {
                    "inline_keyboard": [
                        [{"text": "Свернуть окна", "style": "primary", "icon_custom_emoji_id": ICONS.get("screen"), "callback_data": "minimize_windows"}],
                        [
                            {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                            {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                        ],
                    ]
                }
                self.ui.send_message(chat_id, notice_text, reply_markup=notice_markup, message_id=msg_id)
                self.power_flow.reset_flow()
                return

        # 5. Final Success (Only when minimize_all ACK executed!)
        final_text = render_checklist() + "\n\n<b>✅ Рабочий сетап успешно запущен!</b>\nВсе приложения открыты, VPN активен, окна свернуты."
        final_markup = {
            "inline_keyboard": [
                [
                    {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                    {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                ],
            ]
        }
        self.ui.send_message(chat_id, final_text, reply_markup=final_markup, message_id=msg_id)
        self.power_flow.reset_flow()

    def start_async_game_setup(self, chat_id, card_message_id=None, game="gta5", vpn=None, workspace_id=None):
        if not hasattr(self, "_workspace_start_lock"):
            self._workspace_start_lock = threading.Lock()
        with self._workspace_start_lock:
            if getattr(self, "_workspace_running", False):
                LOG.warning("Setup runner is already active, ignoring duplicate start request")
                return
            self._workspace_running = True

        selected_vpn = vpn or getattr(self, "chosen_vpn", None) or "incy"

        def _runner():
            try:
                self.run_game_setup_steps(chat_id, card_message_id, game=game, vpn=selected_vpn, workspace_id=workspace_id)
            except Exception:
                LOG.exception("Game setup runner crashed unexpectedly")
                if hasattr(self, "power_flow") and self.power_flow:
                    try:
                        self.power_flow.reset_flow()
                    except Exception:
                        pass
            finally:
                self._workspace_running = False

        threading.Thread(
            target=_runner,
            name="game-runner-thread",
            daemon=True,
        ).start()

    def run_game_setup_steps(self, chat_id, card_message_id=None, game="gta5", vpn=None, workspace_id=None):
        active_vpn = vpn or getattr(self, "chosen_vpn", None) or "incy"
        self.chosen_vpn = active_vpn
        g_info = GAME_INFO.get(game, GAME_INFO["gta5"])
        game_name = g_info["name"]
        scenario_name = g_info["scenario"]

        steps = get_game_steps(game, active_vpn)
        self.current_workspace_steps = steps
        self.app_statuses = {step["id"]: "⏳ В очереди" for step in steps}
        self.workspace_cancel_event.clear()

        vpn_ev = getattr(self, "vpn_ready_event", None)
        if vpn_ev:
            vpn_ev.clear()
        happ_ev = getattr(self, "happ_ready_event", None)
        if happ_ev:
            happ_ev.clear()

        self.workspace_executed_event.clear()
        self.workspace_execution_result = None

        def render_checklist():
            lines = [
                format_workspace_step_line(step, self.app_statuses.get(step["id"], "⏳ В очереди"))
                for step in self.current_workspace_steps
            ]
            return f"<b>{scenario_name}...</b>\n\n" + "\n".join(lines)

        cancel_btn = {"text": "Отменить запуск", "style": "danger", "callback_data": "flow_workspace_cancel"}
        if ICONS.get("cross"):
            cancel_btn["icon_custom_emoji_id"] = ICONS.get("cross")
        markup = {"inline_keyboard": [[cancel_btn]]}

        res = self.ui.send_message(chat_id, render_checklist(), reply_markup=markup, message_id=card_message_id)
        msg_id = res["message_id"] if res and res.get("message_id") else card_message_id
        self.workspace_card_msg_id = msg_id

        # 0. Wait up to 15s for desktop agent to be online
        if self.mqtt.desktop_status != "online":
            LOG.info("Desktop agent not online yet, waiting up to 15s...")
            for _ in range(30):
                if self.workspace_cancel_event.is_set():
                    self.power_flow.reset_flow()
                    return
                if self.mqtt.desktop_status == "online":
                    break
                time.sleep(0.5)

        if self.mqtt.desktop_status != "online":
            LOG.warning("Desktop agent still not online after 15s wait.")
            error_text = render_checklist() + "\n\n<b>Desktop-агент не в сети.</b>\nWindows загружается или desktop-агент ещё не подключился к брокеру."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить запуск", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": f"direct_game_vpn:{game}:{active_vpn}"}],
                    [{"text": "Отмена", "style": "danger", "icon_custom_emoji_id": ICONS.get("cross"), "callback_data": "menu"}],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 1. Issue launch_app command to agent.py
        ws_id = workspace_id or f"game-ws-{int(time.time())}"
        cmd_id = self.mqtt.publish_desktop_action(
            "launch_app",
            {"app": scenario_name, "workspace_id": ws_id, "vpn": active_vpn},
            timeout=120,
        )
        self.active_workspace_cmd_id = cmd_id

        # 2. Wait for Happ / INCY VPN readiness
        vpn_event = getattr(self, "vpn_ready_event", None) or getattr(self, "happ_ready_event", None)
        vpn_ready = self._wait_event_or_cancel(vpn_event, 55.0) if vpn_event else True

        if self.workspace_cancel_event.is_set():
            LOG.info("Runner thread detected cancellation during VPN wait. Halting.")
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.power_flow.reset_flow()
            return

        vpn_step_id = "incy_vpn" if active_vpn == "incy" else "happ_vpn"
        vpn_name = "INCY" if active_vpn == "incy" else "Happ"

        if not vpn_ready and (vpn_event and not vpn_event.is_set()):
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.app_statuses[vpn_step_id] = "❌ Ошибка VPN"
            error_text = render_checklist() + f"\n\n<b>Не удалось подтвердить подключение {vpn_name} (VPN).</b>\nЗапуск игры отменён в целях безопасности."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить запуск", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": f"direct_game_vpn:{game}:{active_vpn}"}],
                    [
                        {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                        {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                    ],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 3. Wait for full execution ACK from agent.py (up to 65 seconds)
        ws_executed = self._wait_event_or_cancel(self.workspace_executed_event, 65.0)

        if self.workspace_cancel_event.is_set():
            LOG.info("Runner thread detected cancellation during game execution wait. Halting.")
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            self.power_flow.reset_flow()
            return

        if not ws_executed or self.workspace_execution_result == "failed":
            self.mqtt.publish_desktop_action("cancel_workspace", {"workspace_id": ws_id})
            error_text = render_checklist() + f"\n\n<b>Ошибка запуска {game_name}.</b>\nDesktop-агент сообщил о сбое или запуск превысил таймаут."
            error_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить запуск", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": f"direct_game_vpn:{game}:{active_vpn}"}],
                    [
                        {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                        {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                    ],
                ]
            }
            self.ui.send_message(chat_id, error_text, reply_markup=error_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 4. Strict verification check: every step must be strictly verified
        unverified = [s["label"] for s in self.current_workspace_steps if self.app_statuses.get(s["id"]) != "✅"]
        if unverified:
            notice_text = render_checklist() + f"\n\n<b>Игровой сетап завершён с неполным подтверждением.</b>\nНе подтверждены: {', '.join(unverified)}"
            notice_markup = {
                "inline_keyboard": [
                    [{"text": "Повторить запуск", "style": "primary", "icon_custom_emoji_id": ICONS.get("refresh"), "callback_data": f"direct_game_vpn:{game}:{active_vpn}"}],
                    [
                        {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                        {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                    ],
                ]
            }
            self.ui.send_message(chat_id, notice_text, reply_markup=notice_markup, message_id=msg_id)
            self.power_flow.reset_flow()
            return

        # 5. Final Success!
        final_text = (
            render_checklist()
            + f"\n\n<b>✅ Игровой сетап успешно запущен!</b>\n"
            f"{vpn_name} VPN подключен, Antigravity активен, {game_name} запущена."
        )
        final_markup = {
            "inline_keyboard": [
                [
                    {"text": "⚡ Нажать ESC", "style": "primary", "icon_custom_emoji_id": ICONS.get("terminal"), "callback_data": "press_esc"},
                ],
                [
                    {"text": "Скриншот", "style": "primary", "icon_custom_emoji_id": ICONS.get("screenshot"), "callback_data": "screenshot"},
                    {"text": "Главное меню", "style": "primary", "icon_custom_emoji_id": ICONS.get("back"), "callback_data": "menu"},
                ],
            ]
        }
        self.ui.send_message(chat_id, final_text, reply_markup=final_markup, message_id=msg_id)
        self.power_flow.reset_flow()
