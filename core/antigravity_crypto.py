#!/usr/bin/env python3
"""Antigravity Cryptography & Protobuf Serialization Engine.

Provides:
- AES-256-GCM encryption/decryption of account credentials and tokens.
- Windows DPAPI decryption of Chromium/Antigravity master keys.
- Portable master_key.hex fallback resolution.
- Native binary Protobuf varint and topic serialization without heavy dependencies.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

LOG = logging.getLogger("pc-control.antigravity.crypto")

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
            if appdata:
                local_state_path = Path(appdata) / "Antigravity Manager" / "Local State"
                mk_path = Path(appdata) / "Antigravity Manager" / ".mk"

                if local_state_path.exists() and mk_path.exists():
                    try:
                        with open(local_state_path, "r", encoding="utf-8") as f:
                            local_state = json.load(f)

                        encrypted_key_b64 = local_state.get("os_crypt", {}).get("encrypted_key")
                        if encrypted_key_b64:
                            raw_enc_key = base64.b64decode(encrypted_key_b64)
                            if raw_enc_key.startswith(b"DPAPI"):
                                raw_enc_key = raw_enc_key[5:]

                            oscrypt_key = _dpapi_decrypt(raw_enc_key)

                            with open(mk_path, "rb") as f:
                                mk_enc = f.read()

                            if mk_enc.startswith(b"v10"):
                                nonce = mk_enc[3:15]
                                ciphertext_and_tag = mk_enc[15:]
                                aesgcm = AESGCM(oscrypt_key)
                                decrypted_hex = aesgcm.decrypt(nonce, ciphertext_and_tag, None).decode("utf-8")

                                self._cached_key_bytes = bytes.fromhex(decrypted_hex.strip())
                                return self._cached_key_bytes
                    except Exception as e:
                        LOG.warning("Failed to decrypt master key via Antigravity Manager: %s", e)

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
        refresh_token: Optional[str],
        expiry_timestamp: int,
        is_gcp_tos: bool = False,
        id_token: Optional[str] = None,
        email: Optional[str] = None,
    ) -> bytes:
        """Create OAuthTokenInfo protobuf payload."""
        if not access_token or not isinstance(access_token, str):
            raise ValueError("OAuth access_token must be a non-empty string")
        b_access = cls.encode_string(1, access_token)
        b_bearer = cls.encode_string(2, "Bearer")
        b_refresh = cls.encode_string(3, refresh_token) if refresh_token else b""
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
