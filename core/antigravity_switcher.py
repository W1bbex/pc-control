#!/usr/bin/env python3
"""Antigravity Switcher Engine: Safe, cross-platform Antigravity account management.

Supports:
- Windows host and Linux/VDS environments.
- Decrypting/encrypting accounts & tokens from ~/.antigravity-agent/cloud_accounts.db.
- Auto-refreshing OAuth access tokens via Google OAuth.
- Injecting credentials into Antigravity IDE state (state.vscdb & storage.json).
- Auto-switching accounts when quotas are depleted.
- Portable export/import of accounts across machines (Windows -> Linux/VDS).
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import psutil

LOG = logging.getLogger("pc-control.antigravity")

# Google OAuth Constants (Antigravity Enterprise Client)
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_CLIENT_ID = os.getenv("ANTIGRAVITY_CLIENT_ID", "")
DEFAULT_CLIENT_SECRET = os.getenv("ANTIGRAVITY_CLIENT_SECRET", "")

ENC_PREFIX = "agm_enc_v1:"


# =====================================================================
# 1. DPAPI & Cryptography Helpers
# =====================================================================

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


if sys.platform == "win32":
    ctypes.windll.crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPWSTR,
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    ctypes.windll.crypt32.CryptUnprotectData.restype = wintypes.BOOL
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p


def _dpapi_decrypt(data: bytes) -> bytes:
    """Decrypt Windows DPAPI protected blob."""
    if sys.platform != "win32":
        raise RuntimeError("Windows DPAPI is only supported on win32")

    blob_in = _DATA_BLOB(len(data), ctypes.create_string_buffer(data, len(data)))
    blob_out = _DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        err = ctypes.GetLastError()
        raise RuntimeError(f"CryptUnprotectData failed with error {err}")

    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    kernel32.LocalFree(blob_out.pbData)
    return result


class AntigravityCrypto:
    """Handles master key recovery and AES-256-GCM encryption/decryption."""

    def __init__(self, master_key_hex: Optional[str] = None):
        self._cached_key_bytes: Optional[bytes] = None
        if master_key_hex:
            self._cached_key_bytes = bytes.fromhex(master_key_hex.strip())

    def get_master_key(self) -> bytes:
        if self._cached_key_bytes:
            return self._cached_key_bytes

        # 1. Environment variable override (ideal for Linux/VDS)
        env_key = os.environ.get("ANTIGRAVITY_MASTER_KEY")
        if env_key:
            self._cached_key_bytes = bytes.fromhex(env_key.strip())
            return self._cached_key_bytes

        # 2. Windows Local State + .mk file
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            if not appdata:
                raise FileNotFoundError("APPDATA environment variable is missing")

            local_state_path = Path(appdata) / "Antigravity Manager" / "Local State"
            mk_path = Path(appdata) / "Antigravity Manager" / ".mk"

            if not local_state_path.exists():
                raise FileNotFoundError(f"Local State not found at {local_state_path}")
            if not mk_path.exists():
                raise FileNotFoundError(f".mk master key not found at {mk_path}")

            with open(local_state_path, "r", encoding="utf-8") as f:
                local_state = json.load(f)

            encrypted_key_b64 = local_state.get("os_crypt", {}).get("encrypted_key")
            if not encrypted_key_b64:
                raise ValueError("os_crypt.encrypted_key missing in Local State")

            raw_enc_key = base64.b64decode(encrypted_key_b64)
            if raw_enc_key.startswith(b"DPAPI"):
                raw_enc_key = raw_enc_key[5:]

            oscrypt_key = _dpapi_decrypt(raw_enc_key)

            with open(mk_path, "rb") as f:
                mk_enc = f.read()

            if not mk_enc.startswith(b"v10"):
                raise ValueError(f"Unexpected header in .mk: {mk_enc[:3]!r}")

            nonce = mk_enc[3:15]
            ciphertext_and_tag = mk_enc[15:]
            aesgcm = AESGCM(oscrypt_key)
            decrypted_hex = aesgcm.decrypt(nonce, ciphertext_and_tag, None).decode("utf-8")

            self._cached_key_bytes = bytes.fromhex(decrypted_hex.strip())
            return self._cached_key_bytes

        # 3. Portable fallback key file
        portable_key_file = Path.home() / ".antigravity-agent" / "master_key.hex"
        if portable_key_file.exists():
            key_hex = portable_key_file.read_text(encoding="utf-8").strip()
            self._cached_key_bytes = bytes.fromhex(key_hex)
            return self._cached_key_bytes

        raise RuntimeError(
            "Could not locate Antigravity master key. On Linux/VDS set ANTIGRAVITY_MASTER_KEY or provide master_key.hex."
        )

    def decrypt_payload(self, enc_str: str) -> Optional[Dict[str, Any]]:
        """Decrypt agm_enc_v1:{iv}:{tag}:{ct} payload into dict."""
        if not enc_str:
            return None
        if not enc_str.startswith(ENC_PREFIX):
            # Plain JSON fallback
            try:
                return json.loads(enc_str)
            except Exception:
                return None

        parts = enc_str[len(ENC_PREFIX):].split(":")
        if len(parts) != 3:
            raise ValueError(f"Malformed encrypted payload structure: {enc_str[:25]}")

        iv = bytes.fromhex(parts[0])
        tag = bytes.fromhex(parts[1])
        ct = bytes.fromhex(parts[2])

        key = self.get_master_key()
        aesgcm = AESGCM(key)
        decrypted_bytes = aesgcm.decrypt(iv, ct + tag, None)
        return json.loads(decrypted_bytes.decode("utf-8"))

    def encrypt_payload(self, data: Dict[str, Any]) -> str:
        """Encrypt dict into agm_enc_v1:{iv}:{tag}:{ct}."""
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        iv = os.urandom(16)
        key = self.get_master_key()
        aesgcm = AESGCM(key)
        # In cryptography AESGCM, encrypt returns ct + tag (last 16 bytes is tag)
        enc = aesgcm.encrypt(iv, raw, None)
        ct = enc[:-16]
        tag = enc[-16:]
        return f"{ENC_PREFIX}{iv.hex()}:{tag.hex()}:{ct.hex()}"


# =====================================================================
# 2. Protobuf Serialization Helpers
# =====================================================================

class ProtobufHelper:
    """Helper for reading and constructing unified Antigravity state protobufs."""

    @staticmethod
    def encode_varint(value: int) -> bytes:
        out = bytearray()
        while value >= 0x80:
            out.append((value & 0x7F) | 0x80)
            value >>= 7
        out.append(value & 0x7F)
        return bytes(out)

    @staticmethod
    def read_varint(data: bytes, offset: int) -> Tuple[int, int]:
        val = 0
        shift = 0
        pos = offset
        while pos < len(data):
            b = data[pos]
            pos += 1
            val |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return val, pos

    @classmethod
    def encode_len_delim(cls, field_num: int, data: bytes) -> bytes:
        tag = (field_num << 3) | 2
        return cls.encode_varint(tag) + cls.encode_varint(len(data)) + data

    @classmethod
    def encode_string(cls, field_num: int, text: str) -> bytes:
        return cls.encode_len_delim(field_num, text.encode("utf-8"))

    @classmethod
    def encode_varint_field(cls, field_num: int, value: int) -> bytes:
        tag = (field_num << 3) | 0
        return cls.encode_varint(tag) + cls.encode_varint(value)

    @classmethod
    def create_oauth_info(
        cls,
        access_token: str,
        refresh_token: str,
        expiry_timestamp: int,
        is_gcp_tos: bool = False,
        id_token: Optional[str] = None,
        email: Optional[str] = None,
    ) -> bytes:
        """Create OAuthTokenInfo protobuf payload."""
        b_access = cls.encode_string(1, access_token)
        b_bearer = cls.encode_string(2, "Bearer")
        b_refresh = cls.encode_string(3, refresh_token)
        d = cls.encode_varint_field(1, expiry_timestamp) + cls.encode_varint_field(2, 0)
        b_expiry = cls.encode_len_delim(4, d)
        b_id = cls.encode_string(5, id_token) if id_token else b""
        b_tos = cls.encode_varint_field(6, 1) if is_gcp_tos else b""
        return b_access + b_bearer + b_refresh + b_expiry + b_id + b_tos

    @classmethod
    def create_unified_topic_entry(cls, sentinel_key: str, payload_bytes: bytes) -> bytes:
        """Creates a single topic data entry inside the unified state."""
        payload_b64 = base64.b64encode(payload_bytes).decode("ascii")
        r = cls.encode_string(1, payload_b64)
        inner = cls.encode_string(1, sentinel_key) + cls.encode_len_delim(2, r)
        return cls.encode_len_delim(1, inner)

    @classmethod
    def create_unified_state_entry(cls, sentinel_key: str, payload_bytes: bytes) -> str:
        """Wrap topic entry in base64 string for state.vscdb storage."""
        entry = cls.create_unified_topic_entry(sentinel_key, payload_bytes)
        return base64.b64encode(entry).decode("ascii")

    @classmethod
    def create_minimal_user_status_payload(cls, email: str) -> bytes:
        return cls.encode_string(3, email) + cls.encode_string(7, email)

    @classmethod
    def replace_unified_topic_entry(cls, existing_raw: bytes, sentinel_key: str, new_payload: bytes) -> bytes:
        """Safely updates or inserts a sentinel topic entry without destroying others."""
        filtered = bytearray()
        pos = 0
        while pos < len(existing_raw):
            start_pos = pos
            tag, pos = cls.read_varint(existing_raw, pos)
            wire_type = tag & 0x07
            field_num = tag >> 3
            if wire_type == 2 and field_num == 1:
                length, pos = cls.read_varint(existing_raw, pos)
                entry_bytes = existing_raw[pos:pos + length]
                end_pos = pos + length
                pos = end_pos

                e_pos = 0
                e_tag, e_pos = cls.read_varint(entry_bytes, e_pos)
                if (e_tag >> 3) == 1 and (e_tag & 7) == 2:
                    s_len, e_pos = cls.read_varint(entry_bytes, e_pos)
                    s_key = entry_bytes[e_pos:e_pos + s_len].decode("utf-8", errors="ignore")
                    if s_key == sentinel_key:
                        # Skip old matching entry
                        continue
                filtered.extend(existing_raw[start_pos:end_pos])
            elif wire_type == 0:
                # Varint field
                _, pos = cls.read_varint(existing_raw, pos)
                filtered.extend(existing_raw[start_pos:pos])
            elif wire_type == 1:
                # 64-bit fixed field
                pos += 8
                if pos > len(existing_raw):
                    break
                filtered.extend(existing_raw[start_pos:pos])
            elif wire_type == 2:
                # Other length-delimited field
                length, pos = cls.read_varint(existing_raw, pos)
                pos += length
                if pos > len(existing_raw):
                    break
                filtered.extend(existing_raw[start_pos:pos])
            elif wire_type == 5:
                # 32-bit fixed field
                pos += 4
                if pos > len(existing_raw):
                    break
                filtered.extend(existing_raw[start_pos:pos])
            else:
                # Unknown wire type, cannot safely parse further
                break

        new_entry = cls.create_unified_topic_entry(sentinel_key, new_payload)
        filtered.extend(new_entry)
        return bytes(filtered)


# =====================================================================
# 3. Main Antigravity Account Switcher Manager
# =====================================================================

class AntigravityAccountManager:
    """High-level management of Antigravity accounts, tokens, quotas, and switching."""

    def __init__(self, crypto: Optional[AntigravityCrypto] = None):
        self.crypto = crypto or AntigravityCrypto()
        self.proto = ProtobufHelper()

    # --- Path Resolvers ---

    @staticmethod
    def get_cloud_db_path() -> Path:
        """Locates ~/.antigravity-agent/cloud_accounts.db."""
        custom = os.environ.get("ANTIGRAVITY_CLOUD_DB")
        if custom:
            return Path(custom)
        return Path.home() / ".antigravity-agent" / "cloud_accounts.db"

    @staticmethod
    def get_ide_state_paths() -> Tuple[Optional[Path], Optional[Path]]:
        """Returns (state.vscdb_path, storage.json_path)."""
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA", "")
            candidates_vscdb = [
                Path(appdata) / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb",
                Path(appdata) / "Antigravity" / "User" / "globalStorage" / "state.vscdb",
            ]
            candidates_storage = [
                Path(appdata) / "Antigravity IDE" / "User" / "globalStorage" / "storage.json",
                Path(appdata) / "Antigravity" / "User" / "globalStorage" / "storage.json",
                Path(appdata) / "Antigravity" / "storage.json",
            ]
        elif sys.platform == "darwin":
            home = Path.home()
            candidates_vscdb = [
                home / "Library" / "Application Support" / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb",
                home / "Library" / "Application Support" / "Antigravity" / "User" / "globalStorage" / "state.vscdb",
            ]
            candidates_storage = [
                home / "Library" / "Application Support" / "Antigravity IDE" / "User" / "globalStorage" / "storage.json",
                home / "Library" / "Application Support" / "Antigravity" / "User" / "globalStorage" / "storage.json",
            ]
        else:
            # Linux / POSIX / VDS
            home = Path.home()
            candidates_vscdb = [
                home / ".config" / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb",
                home / ".config" / "Antigravity" / "User" / "globalStorage" / "state.vscdb",
                home / ".gemini" / "state.vscdb",
            ]
            candidates_storage = [
                home / ".config" / "Antigravity IDE" / "User" / "globalStorage" / "storage.json",
                home / ".config" / "Antigravity" / "User" / "globalStorage" / "storage.json",
                home / ".gemini" / "storage.json",
            ]

        vscdb_path = next((p for p in candidates_vscdb if p.exists()), None)
        storage_path = next((p for p in candidates_storage if p.exists()), None)
        return vscdb_path, storage_path

    # --- Account & Quota Operations ---

    def list_accounts(self) -> List[Dict[str, Any]]:
        """List all accounts with decrypted quotas and metadata."""
        db_path = self.get_cloud_db_path()
        if not db_path.exists():
            LOG.warning("Cloud accounts db not found at %s", db_path)
            return []

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        accounts: List[Dict[str, Any]] = []

        try:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT id, provider, email, name, avatar_url, token_json, quota_json, "
                "device_profile_json, created_at, last_used, status, is_active FROM accounts"
            ).fetchall()

            for r in rows:
                item = dict(r)
                is_active = bool(item.get("is_active"))

                # Decrypt quota
                quota_raw = item.get("quota_json")
                quotas = {}
                min_quota = 100
                reset_time = None
                if quota_raw:
                    try:
                        quota_dict = self.crypto.decrypt_payload(quota_raw) or {}
                        # Extract popular models
                        for m_key, m_val in quota_dict.items():
                            if isinstance(m_val, dict) and "percentage" in m_val:
                                pct = int(m_val["percentage"])
                                quotas[m_key] = {
                                    "percentage": pct,
                                    "reset_time": m_val.get("resetTime"),
                                    "name": m_val.get("display_name", m_key),
                                }
                                if pct < min_quota:
                                    min_quota = pct
                                if not reset_time and m_val.get("resetTime"):
                                    reset_time = m_val.get("resetTime")
                    except Exception as e:
                        LOG.debug("Could not decrypt quota for %s: %s", item.get("email"), e)

                # Token status
                has_valid_token = False
                token_raw = item.get("token_json")
                if token_raw:
                    try:
                        token_dict = self.crypto.decrypt_payload(token_raw) or {}
                        has_valid_token = bool(token_dict.get("refresh_token") or token_dict.get("access_token"))
                    except Exception:
                        pass

                accounts.append({
                    "id": item["id"],
                    "email": item["email"],
                    "name": item.get("name") or item["email"].split("@")[0],
                    "is_active": is_active,
                    "status": item.get("status", "active"),
                    "last_used": item.get("last_used") or 0,
                    "min_quota": min_quota,
                    "reset_time": reset_time,
                    "models_quota": quotas,
                    "has_valid_token": has_valid_token,
                })
        finally:
            conn.close()

        # Sort: active first, then by last_used descending
        accounts.sort(key=lambda a: (not a["is_active"], -a["last_used"]))
        return accounts

    def get_active_account(self) -> Optional[Dict[str, Any]]:
        """Returns the currently active account."""
        accounts = self.list_accounts()
        for a in accounts:
            if a["is_active"]:
                return a
        return accounts[0] if accounts else None

    def find_best_account(self, preferred_model: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Finds the account with the highest available quota to switch to.
        
        Useful for auto-switching when current account reaches rate limit.
        """
        accounts = self.list_accounts()
        if not accounts:
            return None

        # Filter out accounts with disabled status
        candidates = [a for a in accounts if a["status"] == "active" and a["has_valid_token"]]
        if not candidates:
            return None

        def score_account(acc: Dict[str, Any]) -> float:
            score = 0.0
            quotas = acc.get("models_quota", {})
            if preferred_model and preferred_model in quotas:
                score += quotas[preferred_model]["percentage"] * 2.0
            elif "gemini-3.1-pro-high" in quotas:
                score += quotas["gemini-3.1-pro-high"]["percentage"] * 1.5
            elif "gemini-3.7-flash-high" in quotas:
                score += quotas["gemini-3.7-flash-high"]["percentage"]
            else:
                score += acc.get("min_quota", 50)

            # Prefer switching away from current active if looking for best fallback
            if acc["is_active"]:
                score -= 500.0

            # Slightly prioritize accounts not used recently
            now = time.time()
            elapsed_hours = (now - acc.get("last_used", 0)) / 3600.0
            score += min(elapsed_hours * 2.0, 20.0)
            return score

        # Candidates must have remaining quota to be eligible fallback
        viable = [acc for acc in candidates if acc.get("min_quota", 0) > 0]
        if not viable:
            return None
        viable.sort(key=score_account, reverse=True)
        return viable[0]

    # --- Token Refresh ---

    def refresh_token_if_needed(self, account_row: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        """Refreshes the OAuth access token if expired or close to expiry."""
        token_raw = account_row.get("token_json")
        if not token_raw:
            raise ValueError(f"Account {account_row.get('email')} has no token_json")

        token_obj = self.crypto.decrypt_payload(token_raw) or {}
        access_token = token_obj.get("access_token")
        refresh_token = token_obj.get("refresh_token")
        expiry = token_obj.get("expiry_timestamp") or 0

        now = int(time.time())
        # If valid for at least 5 more minutes, reuse
        if access_token and not force and expiry > now + 300:
            return token_obj

        if not refresh_token:
            if access_token:
                LOG.warning("No refresh token for %s; returning existing access token", account_row.get("email"))
                return token_obj
            raise ValueError(f"No refresh_token found for {account_row.get('email')}")

        LOG.info("Refreshing Google OAuth access token for %s...", account_row.get("email"))

        payload = urllib.parse.urlencode({
            "client_id": DEFAULT_CLIENT_ID,
            "client_secret": DEFAULT_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")

        req = urllib.request.Request(GOOGLE_TOKEN_URL, data=payload, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            LOG.error("Failed to refresh token for %s: %s", account_row.get("email"), e)
            if access_token:
                LOG.warning("Reusing existing access token despite refresh error")
                return token_obj
            raise

        new_access_token = res_data.get("access_token")
        expires_in = int(res_data.get("expires_in", 3600))
        token_obj["access_token"] = new_access_token
        token_obj["expiry_timestamp"] = now + expires_in
        if "id_token" in res_data:
            token_obj["id_token"] = res_data["id_token"]

        # Encrypt and save back to cloud_accounts.db
        new_enc = self.crypto.encrypt_payload(token_obj)
        db_path = self.get_cloud_db_path()
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE accounts SET token_json = ? WHERE id = ?",
                (new_enc, account_row["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        LOG.info("Successfully refreshed and saved token for %s", account_row.get("email"))
        return token_obj

    # --- Process Management ---

    @staticmethod
    def is_ide_running() -> bool:
        """Checks if Antigravity.exe is running."""
        for p in psutil.process_iter(["name"]):
            try:
                name = (p.info["name"] or "").lower()
                if name in ("antigravity.exe", "antigravity"):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    @staticmethod
    def kill_ide(timeout_sec: float = 8.0) -> None:
        """Gracefully terminates Antigravity.exe processes."""
        targets = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                name = (p.info["name"] or "").lower()
                if name in ("antigravity.exe", "antigravity"):
                    targets.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not targets:
            return

        LOG.info("Terminating %d Antigravity process(es)...", len(targets))
        for p in targets:
            try:
                p.terminate()
            except Exception:
                pass

        gone, alive = psutil.wait_procs(targets, timeout=timeout_sec)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
        if alive:
            try:
                psutil.wait_procs(alive, timeout=3.0)
            except Exception:
                pass

    @staticmethod
    def launch_ide() -> bool:
        """Launches Antigravity.exe non-blocking."""
        if sys.platform != "win32":
            LOG.info("launch_ide called on non-win32 platform; skipping GUI launch.")
            return True

        local_app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "antigravity" / "Antigravity.exe"
        if local_app.exists():
            subprocess.Popen([str(local_app)], close_fds=True, creationflags=subprocess.DETACHED_PROCESS)
            return True

        # Fallback to PATH
        try:
            subprocess.Popen(["Antigravity.exe"], close_fds=True, creationflags=subprocess.DETACHED_PROCESS)
            return True
        except Exception as e:
            LOG.error("Failed to launch Antigravity: %s", e)
            return False

    # --- Switch Execution ---

    def switch_account(self, account_id_or_email: str, restart_ide: bool = True) -> Dict[str, Any]:
        """Switches the active Antigravity account, applying tokens and device profiles."""
        db_path = self.get_cloud_db_path()
        if not db_path.exists():
            raise FileNotFoundError(f"Accounts DB not found at {db_path}")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            target = cur.execute(
                "SELECT * FROM accounts WHERE id = ? OR email = ?",
                (account_id_or_email, account_id_or_email),
            ).fetchone()
            if not target:
                raise ValueError(f"Account '{account_id_or_email}' not found")
            account_row = dict(target)
        finally:
            conn.close()

        email = account_row["email"]
        account_id = account_row["id"]
        LOG.info("Switching Antigravity account to %s (%s)...", email, account_id)

        # 1. Refresh OAuth token if necessary
        token_obj = self.refresh_token_if_needed(account_row)
        access_token = token_obj.get("access_token")
        refresh_token = token_obj.get("refresh_token")
        expiry = token_obj.get("expiry_timestamp") or int(time.time()) + 3600
        id_token = token_obj.get("id_token")

        # 2. Check if IDE was running
        was_running = self.is_ide_running()
        if was_running:
            self.kill_ide()

        # 3. Locate IDE state files
        vscdb_path, storage_path = self.get_ide_state_paths()

        # 4. Apply device profile (fingerprint) to storage.json
        dev_profile = {}
        if account_row.get("device_profile_json"):
            try:
                parsed_dev = json.loads(account_row["device_profile_json"])
                dev_profile = parsed_dev.get("profile", {})
            except Exception:
                pass

        if storage_path and storage_path.exists():
            # Backup
            shutil.copy2(storage_path, storage_path.with_suffix(".json.backup"))
            try:
                storage_data = json.loads(storage_path.read_text(encoding="utf-8"))
            except Exception:
                storage_data = {}

            if dev_profile.get("machineId"):
                storage_data["telemetry.machineId"] = dev_profile["machineId"]
            if dev_profile.get("macMachineId"):
                storage_data["telemetry.macMachineId"] = dev_profile["macMachineId"]
            if dev_profile.get("devDeviceId"):
                storage_data["telemetry.devDeviceId"] = dev_profile["devDeviceId"]
                storage_data["storage.serviceMachineId"] = dev_profile["devDeviceId"]
            if dev_profile.get("sqmId"):
                storage_data["telemetry.sqmId"] = dev_profile["sqmId"]

            storage_path.write_text(json.dumps(storage_data, indent=2), encoding="utf-8")
            LOG.info("Updated device fingerprint in %s", storage_path)

        # 5. Inject tokens and auth into state.vscdb
        if vscdb_path and vscdb_path.exists():
            # Backup
            shutil.copy2(vscdb_path, vscdb_path.with_suffix(".vscdb.backup"))
            state_conn = sqlite3.connect(str(vscdb_path), timeout=5.0)
            try:
                s_cur = state_conn.cursor()

                # A. antigravityAuthStatus
                auth_status = json.dumps({
                    "name": account_row.get("name") or email.split("@")[0],
                    "email": email,
                    "apiKey": access_token,
                })
                s_cur.execute(
                    "INSERT INTO ItemTable (key, value) VALUES ('antigravityAuthStatus', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (auth_status,),
                )
                s_cur.execute(
                    "INSERT INTO ItemTable (key, value) VALUES ('antigravityOnboarding', 'true') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                )

                # B. storage.serviceMachineId
                if dev_profile.get("devDeviceId"):
                    s_cur.execute(
                        "INSERT INTO ItemTable (key, value) VALUES ('storage.serviceMachineId', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (dev_profile["devDeviceId"],),
                    )

                # C. antigravityUnifiedStateSync.userStatus
                user_status_payload = self.proto.create_minimal_user_status_payload(email)
                user_status_entry = self.proto.create_unified_state_entry("userStatusSentinelKey", user_status_payload)
                s_cur.execute(
                    "INSERT INTO ItemTable (key, value) VALUES ('antigravityUnifiedStateSync.userStatus', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (user_status_entry,),
                )

                # D. antigravityUnifiedStateSync.oauthToken
                oauth_payload = self.proto.create_oauth_info(
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expiry_timestamp=expiry,
                    is_gcp_tos=False,
                    id_token=id_token,
                    email=email,
                )
                existing_oauth_row = s_cur.execute(
                    "SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.oauthToken'"
                ).fetchone()

                if existing_oauth_row and existing_oauth_row[0]:
                    try:
                        existing_bytes = base64.b64decode(existing_oauth_row[0])
                        merged = self.proto.replace_unified_topic_entry(
                            existing_bytes, "oauthTokenInfoSentinelKey", oauth_payload
                        )
                        oauth_val = base64.b64encode(merged).decode("ascii")
                    except Exception as e:
                        LOG.warning("Failed to merge unified oauth state; creating new: %s", e)
                        oauth_val = self.proto.create_unified_state_entry("oauthTokenInfoSentinelKey", oauth_payload)
                else:
                    oauth_val = self.proto.create_unified_state_entry("oauthTokenInfoSentinelKey", oauth_payload)

                s_cur.execute(
                    "INSERT INTO ItemTable (key, value) VALUES ('antigravityUnifiedStateSync.oauthToken', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (oauth_val,),
                )

                # E. Cleanup old key
                s_cur.execute("DELETE FROM ItemTable WHERE key = 'jetskiStateSync.agentManagerInitState'")

                state_conn.commit()
                LOG.info("Successfully injected OAuth tokens and auth status into state.vscdb")
            finally:
                state_conn.close()

        # 6. Update cloud_accounts.db (mark active)
        now_ts = int(time.time())
        db_conn = sqlite3.connect(str(db_path))
        try:
            db_conn.execute("UPDATE accounts SET is_active = 0")
            db_conn.execute(
                "UPDATE accounts SET is_active = 1, last_used = ? WHERE id = ?",
                (now_ts, account_id),
            )
            # Update settings table
            db_conn.execute(
                "INSERT INTO settings (key, value) VALUES ('active_cloud_account.classic', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(account_id),),
            )
            db_conn.commit()
            LOG.info("Updated cloud_accounts.db active state")
        finally:
            db_conn.close()

        # 7. Relaunch IDE only if requested and it was already running
        restarted = False
        if restart_ide and was_running:
            restarted = self.launch_ide()

        return {
            "success": True,
            "account_id": account_id,
            "email": email,
            "name": account_row.get("name") or email.split("@")[0],
            "restarted_ide": restarted,
        }

    # --- Portability & Export/Import ---

    def export_accounts(self, dest_path: Optional[Path] = None) -> Dict[str, Any]:
        """Exports all accounts with decrypted tokens into portable JSON.
        
        Allows transferring all 5 accounts to Linux/VDS or creating offline backup.
        """
        db_path = self.get_cloud_db_path()
        if not db_path.exists():
            raise FileNotFoundError(f"Accounts DB not found at {db_path}")

        master_key_hex = self.crypto.get_master_key().hex()
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        accounts_export = []
        try:
            rows = conn.execute("SELECT * FROM accounts").fetchall()
            for r in rows:
                row_dict = dict(r)
                # Decrypt token
                token_obj = None
                if row_dict.get("token_json"):
                    try:
                        token_obj = self.crypto.decrypt_payload(row_dict["token_json"])
                    except Exception:
                        pass
                quota_obj = None
                if row_dict.get("quota_json"):
                    try:
                        quota_obj = self.crypto.decrypt_payload(row_dict["quota_json"])
                    except Exception:
                        pass

                row_dict["token_decrypted"] = token_obj
                row_dict["quota_decrypted"] = quota_obj
                accounts_export.append(row_dict)
        finally:
            conn.close()

        bundle = {
            "version": 1,
            "exported_at": int(time.time()),
            "master_key_hex": master_key_hex,
            "accounts": accounts_export,
        }

        if dest_path:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")
            LOG.info("Exported %d accounts to %s", len(accounts_export), dest_path)

        return bundle

    def import_accounts(self, source_path_or_bundle: Any) -> int:
        """Imports accounts from a portable bundle into cloud_accounts.db."""
        if isinstance(source_path_or_bundle, (str, Path)):
            bundle = json.loads(Path(source_path_or_bundle).read_text(encoding="utf-8"))
        else:
            bundle = source_path_or_bundle

        accounts = bundle.get("accounts", [])
        if not accounts:
            return 0

        # If bundle has master_key_hex, ensure our crypto uses it
        if bundle.get("master_key_hex"):
            self.crypto = AntigravityCrypto(bundle["master_key_hex"])
            # Save portable key file on non-Windows
            if sys.platform != "win32":
                p_key = Path.home() / ".antigravity-agent" / "master_key.hex"
                p_key.parent.mkdir(parents=True, exist_ok=True)
                p_key.write_text(bundle["master_key_hex"], encoding="utf-8")

        db_path = self.get_cloud_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        imported_count = 0
        try:
            # Ensure tables exist
            conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            for a in accounts:
                # Re-encrypt tokens with current key if needed
                token_json = a.get("token_json")
                if a.get("token_decrypted"):
                    token_json = self.crypto.encrypt_payload(a["token_decrypted"])
                quota_json = a.get("quota_json")
                if a.get("quota_decrypted"):
                    quota_json = self.crypto.encrypt_payload(a["quota_decrypted"])

                conn.execute("""
                    INSERT INTO accounts (
                        id, provider, email, name, avatar_url, token_json, quota_json,
                        device_profile_json, device_history_json, created_at, last_used,
                        status, status_reason, is_active, proxy_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        token_json = excluded.token_json,
                        quota_json = excluded.quota_json,
                        name = excluded.name,
                        status = excluded.status,
                        last_used = excluded.last_used
                """, (
                    a["id"], a.get("provider", "google"), a["email"], a.get("name"),
                    a.get("avatar_url"), token_json, quota_json, a.get("device_profile_json"),
                    a.get("device_history_json"), a.get("created_at", int(time.time())),
                    a.get("last_used"), a.get("status", "active"), a.get("status_reason"),
                    a.get("is_active", 0), a.get("proxy_url")
                ))
                imported_count += 1

            conn.commit()
        finally:
            conn.close()

        LOG.info("Imported %d accounts into %s", imported_count, db_path)
        return imported_count
