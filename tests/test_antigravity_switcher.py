#!/usr/bin/env python3
"""Comprehensive unit tests for Antigravity Switcher engine and Telegram UI."""

import base64
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import pytest

# Ensure project root in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.antigravity_switcher import (
    AntigravityCrypto,
    ProtobufHelper,
    AntigravityAccountManager,
)
from vds_bot.telegram_ui import TelegramBotApi
from vds_bot.routers.antigravity_router import (

    route_antigravity_command,
    route_antigravity_callback,
)


# =====================================================================
# 1. Crypto Tests
# =====================================================================

def test_crypto_roundtrip():
    # 32-byte key (64 hex characters)
    test_key_hex = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    crypto = AntigravityCrypto(master_key_hex=test_key_hex)
    assert crypto.get_master_key() == bytes.fromhex(test_key_hex)

    data = {
        "access_token": "ya29.test_token_12345",
        "refresh_token": "1//test_refresh_token_67890",
        "expiry_timestamp": 1800000000,
        "is_gcp_tos": False,
    }

    enc = crypto.encrypt_payload(data)
    assert enc.startswith("agm_enc_v1:")
    assert len(enc.split(":")) == 4  # prefix + iv + tag + ct

    dec = crypto.decrypt_payload(enc)
    assert dec == data


def test_crypto_plain_json_fallback():
    crypto = AntigravityCrypto(master_key_hex="01" * 32)
    plain = '{"key": "value"}'
    assert crypto.decrypt_payload(plain) == {"key": "value"}
    assert crypto.decrypt_payload("") is None
    assert crypto.decrypt_payload(None) is None


# =====================================================================
# 2. Protobuf Serialization Tests
# =====================================================================

def test_protobuf_varint_and_len_delim():
    proto = ProtobufHelper()
    encoded = proto.encode_varint(300)
    val, pos = proto.read_varint(encoded, 0)
    assert val == 300
    assert pos == len(encoded)

    s_bytes = proto.encode_string(1, "hello")
    tag, pos = proto.read_varint(s_bytes, 0)
    assert tag >> 3 == 1
    assert tag & 7 == 2


def test_create_oauth_info_and_unified_entry():
    proto = ProtobufHelper()
    oauth_bytes = proto.create_oauth_info(
        access_token="test_at",
        refresh_token="test_rt",
        expiry_timestamp=1799999999,
        is_gcp_tos=False,
        id_token="test_id",
        email="user@gmail.com",
    )
    assert len(oauth_bytes) > 20
    assert b"test_at" in oauth_bytes
    assert b"test_rt" in oauth_bytes
    assert b"Bearer" in oauth_bytes

    unified_b64 = proto.create_unified_state_entry("oauthTokenInfoSentinelKey", oauth_bytes)
    assert isinstance(unified_b64, str)
    raw = base64.b64decode(unified_b64)
    assert b"oauthTokenInfoSentinelKey" in raw


def test_replace_unified_topic_entry():
    proto = ProtobufHelper()
    # Create entry A and entry B
    entry_a = proto.create_unified_topic_entry("keyA", b"payloadA")
    entry_b = proto.create_unified_topic_entry("keyB", b"payloadB")
    combined = entry_a + entry_b

    # Replace keyA with new payload
    replaced = proto.replace_unified_topic_entry(combined, "keyA", b"newPayloadA")
    assert b"keyA" in replaced
    assert b"keyB" in replaced
    assert base64.b64encode(b"newPayloadA") in replaced
    assert base64.b64encode(b"payloadA") not in replaced


# =====================================================================
# 3. Account Manager Integration Tests (Isolated Temp DB)
# =====================================================================

@pytest.fixture
def temp_environment():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        cloud_db = tmp / "cloud_accounts.db"
        vscdb_path = tmp / "state.vscdb"
        storage_path = tmp / "storage.json"

        # Init cloud_accounts.db
        conn = sqlite3.connect(str(cloud_db))
        conn.execute("""
            CREATE TABLE accounts (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                email TEXT NOT NULL,
                name TEXT,
                avatar_url TEXT,
                token_json TEXT,
                quota_json TEXT,
                device_profile_json TEXT,
                device_history_json TEXT,
                created_at INTEGER NOT NULL,
                last_used INTEGER,
                status TEXT NOT NULL,
                status_reason TEXT,
                is_active INTEGER DEFAULT 0,
                proxy_url TEXT
            )
        """)
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
        conn.close()

        # Init state.vscdb
        v_conn = sqlite3.connect(str(vscdb_path))
        v_conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        v_conn.commit()
        v_conn.close()

        # Init storage.json
        storage_path.write_text("{}", encoding="utf-8")

        test_key = "ab" * 32
        crypto = AntigravityCrypto(master_key_hex=test_key)
        mgr = AntigravityAccountManager(crypto=crypto)

        # Monkeypatch paths
        mgr.get_cloud_db_path = lambda: cloud_db
        mgr.get_ide_state_paths = lambda: (vscdb_path, storage_path)
        # Disable real process killing/launching
        mgr.is_ide_running = lambda: False
        mgr.kill_ide = lambda: None
        mgr.launch_ide = lambda: True

        yield {
            "mgr": mgr,
            "crypto": crypto,
            "cloud_db": cloud_db,
            "vscdb_path": vscdb_path,
            "storage_path": storage_path,
        }


def test_manager_list_and_best_account(temp_environment):
    mgr = temp_environment["mgr"]
    crypto = temp_environment["crypto"]
    cloud_db = temp_environment["cloud_db"]

    # Insert two test accounts
    token1 = crypto.encrypt_payload({"access_token": "at1", "refresh_token": "rt1", "expiry_timestamp": 1900000000})
    quota1 = crypto.encrypt_payload({"gemini-3.1-pro-high": {"percentage": 20, "resetTime": "2026-10-15T00:00:00Z"}})
    dev1 = json.dumps({"schemaVersion": 1, "profile": {"machineId": "m1", "macMachineId": "mac1", "devDeviceId": "dev1", "sqmId": "{sqm1}"}})

    token2 = crypto.encrypt_payload({"access_token": "at2", "refresh_token": "rt2", "expiry_timestamp": 1900000000})
    quota2 = crypto.encrypt_payload({"gemini-3.1-pro-high": {"percentage": 100, "resetTime": "2026-10-15T00:00:00Z"}})
    dev2 = json.dumps({"schemaVersion": 1, "profile": {"machineId": "m2", "macMachineId": "mac2", "devDeviceId": "dev2", "sqmId": "{sqm2}"}})

    conn = sqlite3.connect(str(cloud_db))
    conn.execute(
        "INSERT INTO accounts (id, provider, email, name, token_json, quota_json, device_profile_json, created_at, last_used, status, is_active) "
        "VALUES (?, 'google', ?, ?, ?, ?, ?, 1000, 1000, 'active', 1)",
        ("acc1", "acc1@gmail.com", "Account One", token1, quota1, dev1),
    )
    conn.execute(
        "INSERT INTO accounts (id, provider, email, name, token_json, quota_json, device_profile_json, created_at, last_used, status, is_active) "
        "VALUES (?, 'google', ?, ?, ?, ?, ?, 1000, 500, 'active', 0)",
        ("acc2", "acc2@gmail.com", "Account Two", token2, quota2, dev2),
    )
    conn.commit()
    conn.close()

    accounts = mgr.list_accounts()
    assert len(accounts) == 2
    assert accounts[0]["email"] == "acc1@gmail.com"
    assert accounts[0]["is_active"] is True
    assert accounts[0]["models_quota"]["gemini-3.1-pro-high"]["percentage"] == 20

    active = mgr.get_active_account()
    assert active["id"] == "acc1"

    # Best account should pick acc2 because it has 100% quota and acc1 has only 20%
    best = mgr.find_best_account()
    assert best is not None
    assert best["id"] == "acc2"


def test_manager_switch_account(temp_environment):
    mgr = temp_environment["mgr"]
    crypto = temp_environment["crypto"]
    cloud_db = temp_environment["cloud_db"]
    vscdb_path = temp_environment["vscdb_path"]
    storage_path = temp_environment["storage_path"]

    token = crypto.encrypt_payload({"access_token": "sw_at", "refresh_token": "sw_rt", "expiry_timestamp": 1900000000})
    quota = crypto.encrypt_payload({"gemini-3.7-flash-high": {"percentage": 90}})
    dev = json.dumps({"schemaVersion": 1, "profile": {"machineId": "sw_m", "macMachineId": "sw_mac", "devDeviceId": "sw_dev", "sqmId": "{sw_sqm}"}})

    conn = sqlite3.connect(str(cloud_db))
    conn.execute(
        "INSERT INTO accounts (id, provider, email, name, token_json, quota_json, device_profile_json, created_at, last_used, status, is_active) "
        "VALUES ('target_id', 'google', 'target@test.com', 'Target User', ?, ?, ?, 1000, 1000, 'active', 0)",
        (token, quota, dev),
    )
    conn.commit()
    conn.close()

    res = mgr.switch_account("target_id", restart_ide=False)
    assert res["success"] is True
    assert res["email"] == "target@test.com"

    # Verify cloud_accounts.db updated
    c_conn = sqlite3.connect(str(cloud_db))
    row = c_conn.execute("SELECT is_active FROM accounts WHERE id = 'target_id'").fetchone()
    assert row[0] == 1
    c_conn.close()

    # Verify storage.json has new machine IDs
    st_data = json.loads(storage_path.read_text(encoding="utf-8"))
    assert st_data.get("telemetry.machineId") == "sw_m"
    assert st_data.get("telemetry.devDeviceId") == "sw_dev"

    # Verify state.vscdb injected
    v_conn = sqlite3.connect(str(vscdb_path))
    v_rows = dict(v_conn.execute("SELECT key, value FROM ItemTable").fetchall())
    v_conn.close()

    assert "antigravityAuthStatus" in v_rows
    auth_obj = json.loads(v_rows["antigravityAuthStatus"])
    assert auth_obj["email"] == "target@test.com"
    assert auth_obj["apiKey"] == "sw_at"
    assert v_rows.get("antigravityOnboarding") == "true"
    assert v_rows.get("storage.serviceMachineId") == "sw_dev"
    assert "antigravityUnifiedStateSync.oauthToken" in v_rows


def test_manager_export_import(temp_environment):
    mgr = temp_environment["mgr"]
    crypto = temp_environment["crypto"]
    cloud_db = temp_environment["cloud_db"]

    token = crypto.encrypt_payload({"access_token": "exp_at", "refresh_token": "exp_rt"})
    conn = sqlite3.connect(str(cloud_db))
    conn.execute(
        "INSERT INTO accounts (id, provider, email, name, token_json, quota_json, device_profile_json, created_at, last_used, status, is_active) "
        "VALUES ('exp_id', 'google', 'export@test.com', 'Export User', ?, NULL, NULL, 1000, 1000, 'active', 1)",
        (token,),
    )
    conn.commit()
    conn.close()

    bundle = mgr.export_accounts()
    assert bundle["version"] == 1
    assert len(bundle["accounts"]) == 1
    assert bundle["accounts"][0]["email"] == "export@test.com"
    assert bundle["accounts"][0]["token_decrypted"]["access_token"] == "exp_at"

    # Test import into a fresh db
    with tempfile.TemporaryDirectory() as tmp2:
        new_db = Path(tmp2) / "new_cloud.db"
        mgr2 = AntigravityAccountManager(crypto=crypto)
        mgr2.get_cloud_db_path = lambda: new_db
        count = mgr2.import_accounts(bundle)
        assert count == 1
        accs = mgr2.list_accounts()
        assert len(accs) == 1
        assert accs[0]["email"] == "export@test.com"


# =====================================================================
# 4. Telegram UI & Router Tests
# =====================================================================

def test_telegram_ui_rendering():
    ui = TelegramBotApi("mock_token")
    mock_data = {
        "accounts": [
            {
                "id": "acc1",
                "email": "user1@gmail.com",
                "name": "User 1",
                "is_active": True,
                "min_quota": 80,
                "reset_time": "2026-10-15T03:00:00Z",
                "models_quota": {
                    "gemini-3.1-pro-high": {"percentage": 85},
                    "gemini-3.7-flash-high": {"percentage": 100},
                },
            },
            {
                "id": "acc2",
                "email": "user2@gmail.com",
                "name": "User 2",
                "is_active": False,
                "min_quota": 100,
                "reset_time": None,
                "models_quota": {},
            },
        ],
        "active": {
            "id": "acc1",
            "email": "user1@gmail.com",
            "name": "User 1",
            "is_active": True,
            "min_quota": 80,
            "reset_time": "2026-10-15T03:00:00Z",
            "models_quota": {
                "gemini-3.1-pro-high": {"percentage": 85},
                "gemini-3.7-flash-high": {"percentage": 100},
            },
        },
        "is_running": True,
    }

    text = ui.antigravity_card_text(mock_data)
    assert "Antigravity • Управление аккаунтами" in text
    assert "User 1" in text
    assert "user1@gmail.com" in text
    assert "Gemini 3.1 Pro" in text
    assert "85%" in text
    assert "User 2" in text

    markup = ui.antigravity_card_markup(mock_data)
    buttons = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
    assert "ag_noop" in buttons
    assert "ag_switch:acc2" in buttons
    assert "ag_switch:auto" in buttons
    assert "ag_restart" in buttons
    assert "ag_refresh" in buttons


class DummyApp:
    def __init__(self):
        self.pending_antigravity_cmd_ids = {}
        self.published_actions = []
        self.answered_callbacks = []
        self.sent_messages = []
        self.ui = self
        self.mqtt = self
        self.desktop_status = "online"

    def publish_desktop_action(self, action, args=None):
        self.published_actions.append((action, args))
        return f"cmd_{len(self.published_actions)}"

    def answer_callback(self, cb_id, text=None, alert=False):
        self.answered_callbacks.append((cb_id, text, alert))

    def send_message(self, chat_id, text, reply_markup=None, message_id=None):
        self.sent_messages.append((chat_id, text, reply_markup, message_id))


def test_antigravity_router_commands():
    app = DummyApp()
    assert route_antigravity_command(app, 123, "/antigravity") is True
    assert len(app.published_actions) == 1
    assert app.published_actions[0][0] == "antigravity_list"

    assert route_antigravity_command(app, 123, "/ag auto") is True
    assert len(app.published_actions) == 2
    assert app.published_actions[1][0] == "antigravity_switch"
    assert app.published_actions[1][1]["account_id"] == "auto"

    assert route_antigravity_command(app, 123, "/ag restart") is True
    assert len(app.published_actions) == 3
    assert app.published_actions[2][0] == "antigravity_restart"


def test_antigravity_router_callbacks():
    app = DummyApp()
    assert route_antigravity_callback(app, "ag_noop", 123, 456, "cb_1") is True
    assert len(app.answered_callbacks) == 1
    assert "уже активен" in app.answered_callbacks[0][1]

    assert route_antigravity_callback(app, "ag_switch:acc_test", 123, 456, "cb_2") is True
    assert len(app.published_actions) == 1
    assert app.published_actions[0][0] == "antigravity_switch"
    assert app.published_actions[0][1]["account_id"] == "acc_test"

    assert route_antigravity_callback(app, "ag_restart", 123, 456, "cb_3") is True
    assert len(app.published_actions) == 2
    assert app.published_actions[1][0] == "antigravity_restart"
