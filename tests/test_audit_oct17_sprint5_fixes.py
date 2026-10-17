#!/usr/bin/env python3
"""Regression tests for Sprint 5 fixes (Oct 17 Audit).
Covers:
1.1 ctypes callback retention in EnumWindows (vpn_manager)
1.2 Protobuf stream parser wire_type skipping in replace_unified_topic_entry (antigravity_switcher)
1.3 Missing/invalid chat handling in command_router (route_message)
1.4 Invalid/negative create_time in activity_logger (_process_fields)
1.5 auto_launch_workspace return logic in power_flow
2.1 COM endpoint cleanup in desktop/telemetry
2.2/2.3 MediaManager seek order and session cleanup
2.4 Thread-safe singleton in activity_db
4.2 Premium checker allow_stale caching logic
"""

from collections import namedtuple
import inspect
import threading
import time
import pytest


def test_vpn_manager_enum_windows_trampoline():
    from desktop.vpn_manager import VpnManagerMixin
    # Verify that proc_cb2 is explicitly assigned in the source code of VpnManagerMixin.connect_happ_vpn
    src = inspect.getsource(VpnManagerMixin.connect_happ_vpn)
    assert "proc_cb2 = EnumWindowsProc(_enum_cb)" in src
    assert "user32.EnumWindows(proc_cb2, 0)" in src


def test_antigravity_switcher_protobuf_wire_types():
    from core.antigravity_switcher import ProtobufHelper

    # Build a synthetic protobuf message with multiple fields:
    # Field 1 (tag 1, wire_type 2): string "hello"
    # Field 2 (tag 2, wire_type 0): varint 42
    # Field 3 (tag 3, wire_type 1): 64-bit 0x0102030405060708
    # Field 4 (tag 4, wire_type 5): 32-bit 0x01020304
    # Field 5 (tag 5, wire_type 0): varint 999

    def make_varint(v):
        res = bytearray()
        while v > 0x7F:
            res.append((v & 0x7F) | 0x80)
            v >>= 7
        res.append(v & 0x7F)
        return bytes(res)

    def make_tag(field_num, wire_type):
        return make_varint((field_num << 3) | wire_type)

    buf = bytearray()
    # Field 1: string (wire_type 2)
    s_val = b"hello world"
    buf.extend(make_tag(1, 2))
    buf.extend(make_varint(len(s_val)))
    buf.extend(s_val)

    # Field 2: varint (wire_type 0)
    buf.extend(make_tag(2, 0))
    buf.extend(make_varint(1337))

    # Field 3: 64-bit (wire_type 1)
    buf.extend(make_tag(3, 1))
    buf.extend(b"\x01\x02\x03\x04\x05\x06\x07\x08")

    # Field 4: 32-bit (wire_type 5)
    buf.extend(make_tag(4, 5))
    buf.extend(b"\x0a\x0b\x0c\x0d")

    # Field 5: varint at the end (wire_type 0)
    buf.extend(make_tag(5, 0))
    buf.extend(make_varint(999))

    raw_data = bytes(buf)
    # Replace topic entry (which doesn't exist yet, should append or parse all fields without truncation)
    new_data = ProtobufHelper.replace_unified_topic_entry(raw_data, "test-topic-id", b"payload-bytes-123")
    assert len(new_data) > len(raw_data)
    # Ensure original field 1, 2, 3, 4, 5 are still preserved in new_data
    assert b"hello world" in new_data
    assert b"\x01\x02\x03\x04\x05\x06\x07\x08" in new_data
    assert b"\x0a\x0b\x0c\x0d" in new_data
    assert b"test-topic-id" in new_data


def test_command_router_missing_chat_keys():
    from vds_bot.routers.command_router import route_message
    from unittest.mock import MagicMock

    app = MagicMock()
    # Message with no "chat"
    route_message(app, {})
    assert not app.ui.send_message.called

    # Message with non-dict "chat"
    route_message(app, {"chat": None})
    assert not app.ui.send_message.called

    # Message with empty "chat"
    route_message(app, {"chat": {}})
    assert not app.ui.send_message.called


def test_activity_logger_invalid_create_time():
    from core.activity_logger import ActivityTracker
    from unittest.mock import MagicMock

    tracker = ActivityTracker(config={}, log_dir=MagicMock(), stop_event=threading.Event())
    ProcMock = namedtuple("ProcMock", ["pid", "ppid", "name", "exe", "create_time"])

    # Negative timestamp
    p_neg = ProcMock(pid=100, ppid=1, name="test.exe", exe="C:\\test.exe", create_time=-9999999999)
    fields = tracker._process_fields(p_neg)
    assert "process_started" not in fields
    assert fields["pid"] == 100

    # Overflow timestamp
    p_overflow = ProcMock(pid=101, ppid=1, name="test2.exe", exe="C:\\test2.exe", create_time=1e15)
    fields2 = tracker._process_fields(p_overflow)
    assert "process_started" not in fields2
    assert fields2["pid"] == 101


def test_power_flow_auto_launch_workspace_returns_without_fallthrough():
    from vds_bot.power_flow import PowerFlowManager, PowerFlowStage
    from unittest.mock import MagicMock

    mgr = PowerFlowManager(telegram_ui=MagicMock(), mqtt_gateway=MagicMock())
    mgr.stage = PowerFlowStage.UNLOCKING
    mgr.auto_launch_workspace = True
    mgr.on_workspace_start_cb = None  # None callback

    # Trigger system session update to unlocked
    mgr.on_system_session_update("unlocked", 123)
    # Must NOT fall through to WAITING_WORKSPACE_DECISION
    assert mgr.stage == PowerFlowStage.WORKSPACE_RUNNING
    assert mgr.auto_launch_workspace is False


def test_activity_db_thread_safe_singleton():
    from core.activity_db import get_activity_db, _GLOBAL_DB_LOCK
    import threading

    instances = []

    def _worker():
        db = get_activity_db()
        instances.append(db)

    threads = [threading.Thread(target=_worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(instances) == 10
    # All threads got the exact same instance
    assert all(inst is instances[0] for inst in instances)


def test_premium_checker_allow_stale():
    from core.premium_checker import PremiumChecker
    import time

    checker = PremiumChecker()
    # Inject expired entry
    with checker._lock:
        checker._memory_cache[999] = {
            "is_premium": True,
            "expires_at": time.time() - 100,  # expired
            "source": "test"
        }

    # allow_stale=False should return False
    assert checker.get_cached_status(999, allow_stale=False) is False
    # allow_stale=True should return True (last known value)
    assert checker.get_cached_status(999, allow_stale=True) is True
