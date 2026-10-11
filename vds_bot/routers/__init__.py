"""Routers package for modular handling of Telegram Bot callbacks and commands."""

from .command_router import route_message
from .power_router import route_power_callback
from .workspace_router import route_workspace_callback
from .player_router import route_player_callback
from .console_router import route_console_callback
from .monitoring_router import route_monitoring_callback
from .physical_router import route_physical_callback
from .antigravity_router import route_antigravity_command, route_antigravity_callback

__all__ = [
    "route_message",
    "route_power_callback",
    "route_workspace_callback",
    "route_player_callback",
    "route_console_callback",
    "route_monitoring_callback",
    "route_physical_callback",
    "route_antigravity_command",
    "route_antigravity_callback",
]

