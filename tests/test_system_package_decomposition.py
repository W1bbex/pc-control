"""
Tests for Sprint 2 decomposition of boot_agent.py into system package mixins.
Verifies line ceiling, mixin hierarchy, and backward compatibility of all exports.
"""

from pathlib import Path
import pytest

import boot_agent
from system import (
    HardwareMonitorMixin,
    ProcessManagerMixin,
    ActionDispatcherMixin,
    STATE_PATH,
    TOPIC_COMMAND,
    TOPIC_STATUS,
    active_console_session,
    find_active_interactive_session_id,
    get_boot_info,
)


def test_boot_agent_line_ceiling():
    """Verify boot_agent.py is well below the 1,000-line ceiling (previously 1,661 lines)."""
    boot_agent_path = Path(boot_agent.__file__).resolve()
    lines = boot_agent_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 600, f"boot_agent.py has {len(lines)} lines; must be < 600 lines!"


def test_system_agent_inherits_all_mixins():
    """Verify SystemAgent properly inherits from all three decomposed mixins."""
    assert issubclass(boot_agent.SystemAgent, HardwareMonitorMixin)
    assert issubclass(boot_agent.SystemAgent, ProcessManagerMixin)
    assert issubclass(boot_agent.SystemAgent, ActionDispatcherMixin)


def test_backward_compatibility_reexports_on_boot_agent():
    """Verify legacy scripts and tests can import symbols directly from boot_agent."""
    assert hasattr(boot_agent, "STATE_PATH")
    assert hasattr(boot_agent, "TOPIC_COMMAND")
    assert hasattr(boot_agent, "TOPIC_STATUS")
    assert hasattr(boot_agent, "TOPIC_ACK")
    assert hasattr(boot_agent, "TOPIC_TELEMETRY")
    assert hasattr(boot_agent, "WTSINFOEX_LEVEL1_W")
    assert hasattr(boot_agent, "WTSINFOEXW")
    assert hasattr(boot_agent, "active_console_session")
    assert hasattr(boot_agent, "find_active_interactive_session_id")
    assert hasattr(boot_agent, "get_boot_info")
    assert hasattr(boot_agent, "acquire_single_instance")
    assert hasattr(boot_agent, "LOG")


def test_system_package_exports():
    """Verify system package exports all public symbols correctly."""
    import system
    expected_exports = [
        "AGENT_VERSION",
        "AGENT_BOOT_ID",
        "BASE_DIR",
        "DATA_DIR",
        "CONFIG_PATH",
        "STATE_PATH",
        "LOG_DIR",
        "TOPIC_COMMAND",
        "TOPIC_ACK",
        "TOPIC_STATUS",
        "TOPIC_TELEMETRY",
        "REAL_BOOT_TIME",
        "PROTECTED_PROCESSES",
        "LOG",
        "get_real_boot_time",
        "configure_logging",
        "acquire_single_instance",
        "get_boot_info",
        "WTSINFOEX_LEVEL1_W",
        "WTSINFOEXW",
        "active_console_session",
        "find_active_interactive_session_id",
        "HardwareMonitorMixin",
        "ProcessManagerMixin",
        "ActionDispatcherMixin",
    ]
    for export_name in expected_exports:
        assert hasattr(system, export_name), f"system package is missing export {export_name}"
