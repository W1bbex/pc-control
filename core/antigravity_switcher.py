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
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import urllib.request

from core.antigravity_crypto import AntigravityCrypto, ProtobufHelper, ENC_PREFIX
from core.antigravity_process import (
    IDE_PROCESS_NAMES,
    is_ide_running,
    kill_ide,
    launch_ide,
)

LOG = logging.getLogger("pc-control.antigravity")

# Google OAuth Constants
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_CLIENT_ID = os.getenv("ANTIGRAVITY_CLIENT_ID", "")
DEFAULT_CLIENT_SECRET = os.getenv("ANTIGRAVITY_CLIENT_SECRET", "")


# =====================================================================
# Main Antigravity Account Switcher Manager
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

    @staticmethod
    def _parse_iso_ts(value: Any) -> Optional[float]:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            raw = value.strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None

    @classmethod
    def _parse_quota_payload(cls, quota_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Parse both nested AntigravityManager quota_json and legacy flat model maps."""
        if not isinstance(quota_dict, dict):
            return {
                "models_quota": {},
                "quota_groups": [],
                "min_quota": 0,
                "has_quota_data": False,
                "reset_time": None,
                "subscription_tier": None,
                "is_forbidden": False,
            }

        models_source = quota_dict.get("models")
        if not isinstance(models_source, dict):
            # Legacy flat format: {model_id: {"percentage": ...}}
            models_source = quota_dict

        quotas: Dict[str, Dict[str, Any]] = {}
        all_pcts: List[int] = []
        reset_candidates: List[Tuple[float, str]] = []
        fallback_reset: Optional[str] = None

        def _record_reset(rt: Any) -> None:
            nonlocal fallback_reset
            if not isinstance(rt, str) or not rt.strip():
                return
            if fallback_reset is None:
                fallback_reset = rt
            ts = cls._parse_iso_ts(rt)
            if ts is not None:
                reset_candidates.append((ts, rt))

        for m_key, m_val in models_source.items():
            if not isinstance(m_val, dict) or "percentage" not in m_val:
                continue
            try:
                pct = max(0, min(100, int(round(float(m_val["percentage"])))))
            except (TypeError, ValueError):
                continue
            m_reset = m_val.get("resetTime") or m_val.get("reset_time")
            quotas[m_key] = {
                "percentage": pct,
                "reset_time": m_reset,
                "name": m_val.get("display_name") or m_key,
            }
            all_pcts.append(pct)
            _record_reset(m_reset)

        quota_groups: List[Dict[str, Any]] = []
        raw_groups = quota_dict.get("quota_groups")
        if isinstance(raw_groups, list):
            for grp in raw_groups:
                if not isinstance(grp, dict):
                    continue
                buckets_out: List[Dict[str, Any]] = []
                for b in grp.get("buckets") or []:
                    if not isinstance(b, dict):
                        continue
                    pct = None
                    if b.get("remaining_fraction") is not None:
                        try:
                            pct = max(0, min(100, int(round(float(b["remaining_fraction"]) * 100))))
                        except (TypeError, ValueError):
                            pct = None
                    elif b.get("percentage") is not None:
                        try:
                            pct = max(0, min(100, int(round(float(b["percentage"])))))
                        except (TypeError, ValueError):
                            pct = None
                    if pct is not None:
                        all_pcts.append(pct)
                    b_reset = b.get("reset_time") or b.get("resetTime")
                    _record_reset(b_reset)
                    buckets_out.append({
                        "bucket_id": b.get("bucket_id") or "",
                        "window": b.get("window") or "",
                        "percentage": pct,
                        "reset_time": b_reset,
                        "display_name": b.get("display_name") or "",
                    })
                if buckets_out:
                    quota_groups.append({
                        "display_name": grp.get("display_name") or "",
                        "buckets": buckets_out,
                    })

        has_quota_data = bool(all_pcts)
        min_quota = min(all_pcts) if all_pcts else 0
        reset_time = min(reset_candidates, key=lambda item: item[0])[1] if reset_candidates else fallback_reset

        return {
            "models_quota": quotas,
            "quota_groups": quota_groups,
            "min_quota": min_quota,
            "has_quota_data": has_quota_data,
            "reset_time": reset_time,
            "subscription_tier": quota_dict.get("subscription_tier"),
            "is_forbidden": bool(quota_dict.get("is_forbidden", False)),
        }

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
                parsed_quota = {
                    "models_quota": {},
                    "quota_groups": [],
                    "min_quota": 0,
                    "has_quota_data": False,
                    "reset_time": None,
                    "subscription_tier": None,
                    "is_forbidden": False,
                }
                if quota_raw:
                    try:
                        quota_dict = self.crypto.decrypt_payload(quota_raw) or {}
                        parsed_quota = self._parse_quota_payload(quota_dict)
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

                raw_email = item.get("email") or ""
                accounts.append({
                    "id": item["id"],
                    "email": raw_email,
                    "name": item.get("name") or raw_email.split("@")[0],
                    "is_active": is_active,
                    "status": item.get("status", "active"),
                    "last_used": item.get("last_used") or 0,
                    "min_quota": parsed_quota["min_quota"],
                    "has_quota_data": parsed_quota["has_quota_data"],
                    "reset_time": parsed_quota["reset_time"],
                    "models_quota": parsed_quota["models_quota"],
                    "quota_groups": parsed_quota["quota_groups"],
                    "subscription_tier": parsed_quota["subscription_tier"],
                    "is_forbidden": parsed_quota["is_forbidden"],
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

        # Filter out accounts with disabled/forbidden status
        candidates = [
            a for a in accounts
            if a["status"] == "active" and a["has_valid_token"] and not a.get("is_forbidden", False)
        ]
        if not candidates:
            return None

        primary_models = (
            "gemini-4-hydrogen-xhigh",
            "gemini-4-hydrogen-high",
            "claude-opus-5-5-high",
            "claude-sonnet-5-5-high",
            "gemini-4-argon-high",
            "gemini-3.8-flash-high",
            "gemini-3.1-pro-high",
            "gemini-3.7-flash-high",
        )

        def score_account(acc: Dict[str, Any]) -> float:
            score = 0.0
            quotas = acc.get("models_quota", {})
            eff_min = acc.get("min_quota", 0) if acc.get("has_quota_data", True) else -100.0
            if preferred_model and preferred_model in quotas:
                score += quotas[preferred_model]["percentage"] * 2.0 + max(0.0, eff_min) * 0.5
            else:
                model_pct = None
                for key in primary_models:
                    if key in quotas:
                        model_pct = quotas[key]["percentage"]
                        break
                if model_pct is not None and acc.get("has_quota_data", True):
                    # Combine flagship model quota with effective lower bound (weekly/group limits)
                    score += min(model_pct, eff_min) * 1.5 + model_pct * 0.2
                else:
                    score += eff_min * 1.5

            # Prefer switching away from current active if looking for best fallback
            if acc["is_active"]:
                score -= 500.0

            # Slightly prioritize accounts not used recently
            now = time.time()
            elapsed_hours = (now - acc.get("last_used", 0)) / 3600.0
            score += min(elapsed_hours * 2.0, 20.0)
            return score

        # Candidates must have remaining quota to be eligible fallback
        def is_viable(acc: Dict[str, Any]) -> bool:
            if not acc.get("has_quota_data", True):
                return bool(acc.get("has_valid_token", True)) and acc.get("min_quota", 1) > 0
            quotas = acc.get("models_quota", {})
            if preferred_model and preferred_model in quotas:
                return quotas[preferred_model].get("percentage", 0) > 0
            if acc.get("min_quota", 0) > 0:
                return True
            return any(m.get("percentage", 0) > 0 for m in quotas.values())

        viable = [acc for acc in candidates if is_viable(acc)]
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
            if access_token and now < expiry:
                LOG.warning("Reusing valid existing access token despite refresh error")
                return token_obj
            raise

        new_access_token = res_data.get("access_token")
        if not new_access_token or not isinstance(new_access_token, str):
            raise ValueError(f"Google OAuth refresh response missing access_token for {account_row.get('email')}")
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

    IDE_PROCESS_NAMES = IDE_PROCESS_NAMES

    @staticmethod
    def is_ide_running() -> bool:
        """Checks if Antigravity.exe / Antigravity IDE.exe is running."""
        return is_ide_running()

    @staticmethod
    def kill_ide(timeout_sec: float = 8.0) -> None:
        """Gracefully terminates Antigravity.exe / Antigravity IDE.exe processes."""
        kill_ide(timeout_sec=timeout_sec)

    @staticmethod
    def launch_ide() -> bool:
        """Launches Antigravity.exe non-blocking or raises RuntimeError on failure."""
        return launch_ide()

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
        expiry = int(token_obj.get("expiry_timestamp") or (time.time() + 3600))
        id_token = token_obj.get("id_token")
        if not access_token or not isinstance(access_token, str):
            raise ValueError(f"Refreshed OAuth payload missing valid access_token for {account_row.get('email')}")

        # 2. Check if IDE was running
        was_running = self.is_ide_running()
        if was_running:
            self.kill_ide()

        # 3. Locate IDE state files
        vscdb_path, storage_path = self.get_ide_state_paths()
        if vscdb_path and not vscdb_path.exists():
            raise FileNotFoundError(f"Antigravity IDE state database (state.vscdb) not found at {vscdb_path}")

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

            tmp_storage = storage_path.with_name(f".{storage_path.name}.tmp")
            tmp_storage.write_text(json.dumps(storage_data, indent=2), encoding="utf-8")
            tmp_storage.replace(storage_path)
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
        launch_error = None
        if restart_ide and was_running:
            try:
                restarted = bool(self.launch_ide())
            except Exception as e:
                launch_error = str(e)
                LOG.error("Account switched, but failed to relaunch Antigravity IDE: %s", e)

        result = {
            "success": True,
            "account_id": account_id,
            "email": email,
            "name": account_row.get("name") or email.split("@")[0],
            "restarted_ide": restarted,
        }
        if launch_error:
            result["launch_error"] = launch_error
        return result

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
