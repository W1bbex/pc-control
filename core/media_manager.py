#!/usr/bin/env python3
"""Windows Media Manager for PCControlAgent using WinRT GSMTC.
Provides media telemetry (track, artist, album, duration, position, cover URL)
and hardware playback control (play, pause, next, prev, seek).
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
import winrt.windows.media.control as wmc

LOG = logging.getLogger("media_manager")

_parent = Path(__file__).resolve().parent
BASE_DIR = _parent.parent if _parent.name == "core" else _parent
CONFIG_PATH = BASE_DIR / "config.json"

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass


def _load_config_data() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


_cfg = _load_config_data()
_tg_cfg = _cfg.get("telegram", {})
_ym_cfg = _cfg.get("yandex_music", {})

DEFAULT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or _tg_cfg.get("token") or _tg_cfg.get("bot_token") or ""
_def_user_id = os.getenv("TELEGRAM_USER_ID") or _tg_cfg.get("user_id") or 0
DEFAULT_USER_ID = int(_def_user_id) if _def_user_id else 0

def _load_canonical_icons():
    from pathlib import Path
    for candidate in [
        Path(__file__).resolve().parent.parent / "resources" / "icons.json",
        Path(__file__).resolve().parent / "resources" / "icons.json",
    ]:
        if candidate.exists():
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}

_CANONICAL_ICONS = _load_canonical_icons()

# Pre-uploaded vector UI icons & tags in pc_icons_pack
UI_ICONS = {
    "btn_play": _CANONICAL_ICONS.get("play", "5330015871299527534"),
    "btn_pause": _CANONICAL_ICONS.get("pause", "5330570905628222348"),
    "btn_next": _CANONICAL_ICONS.get("next", "5330255053733276330"),
    "btn_prev": _CANONICAL_ICONS.get("prev", "5330363570376977789"),
    "btn_fwd10": _CANONICAL_ICONS.get("fwd10", "5330317725896059016"),
    "btn_back10": _CANONICAL_ICONS.get("back10", "5330294859490179153"),
    "btn_wave": _CANONICAL_ICONS.get("wave", "5330448799707998190"),
    "tag_note": _CANONICAL_ICONS.get("music", "5330477356945551054"),
    "tag_artist": _CANONICAL_ICONS.get("headphones", "5330526830673831881"),
    "tag_status_play": _CANONICAL_ICONS.get("play", "5330015871299527534"),
    "tag_status_pause": _CANONICAL_ICONS.get("pause", "5330570905628222348"),
    "tag_clock": _CANONICAL_ICONS.get("clock", "5330473500064912704"),
    "tag_slider_dot": _CANONICAL_ICONS.get("dot", "5330193051585391352"),
    "tag_heart_liked": _CANONICAL_ICONS.get("heart_liked", _CANONICAL_ICONS.get("heart", "5346267379007530615")),
    "tag_heart_unliked": _CANONICAL_ICONS.get("heart_unliked", "5346066056710498954"),
}

YANDEX_TOKEN = os.getenv("YANDEX_MUSIC_TOKEN") or _ym_cfg.get("token") or ""
_def_yuid = os.getenv("YANDEX_MUSIC_UID") or _ym_cfg.get("uid") or 0
YANDEX_UID = int(_def_yuid) if _def_yuid else 0


class MediaManager:
    def __init__(
        self,
        token: Optional[str] = None,
        user_id: Optional[int] = None,
        on_status_change=None,
        yandex_token: Optional[str] = None,
        yandex_uid: Optional[int] = None,
    ):
        self.token = token or DEFAULT_TOKEN
        self.user_id = user_id if user_id is not None else DEFAULT_USER_ID
        self.on_status_change = on_status_change
        self.last_state: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._loop = None
        self._thread = None
        self._stop_event = threading.Event()

        # Yandex Music Account & Likes
        self.yandex_token = yandex_token or YANDEX_TOKEN
        self.yandex_uid = yandex_uid if yandex_uid is not None else YANDEX_UID
        self.liked_track_ids = set()
        self._last_liked_fetch = 0.0
        self.current_track_id = None
        self.current_is_liked = False

        # HTTP session for connection pooling (avoids repeated TLS handshakes)
        self._http_session = requests.Session()

        # Local playback clock anchor
        self._anchor_time = time.time()
        self._anchor_pos = 0.0
        self._last_title = ""
        self._last_playing = False
        self._resolve_cache = {}
        self._session_manager = None

        # Pre-warm likes in background so first track change is fast
        threading.Thread(
            target=self._sync_likes_if_needed,
            kwargs={"force": True},
            name="media-likes-prewarm",
            daemon=True,
        ).start()

    async def _get_manager(self):
        if self._session_manager is None:
            self._session_manager = await wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
        return self._session_manager

    def _get_yandex_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"OAuth {self.yandex_token}",
            "X-Yandex-Music-Client": "YandexMusicDesktopAppWindows/5.34.1",
            "X-Yandex-Music-Frontend": "new",
            "X-Yandex-Music-Without-Invocation-Info": "1",
        }

    def _sync_likes_if_needed(self, force: bool = False):
        now = time.time()
        if not force and now - self._last_liked_fetch < 60.0 and self.liked_track_ids:
            return
        try:
            r = self._http_session.get(
                f"https://api.music.yandex.net/users/{self.yandex_uid}/likes/tracks",
                headers=self._get_yandex_headers(),
                timeout=5,
            ).json()
            data = r.get("result", r) if isinstance(r, dict) else {}
            tracks = data.get("library", {}).get("tracks", []) if isinstance(data, dict) else []
            self.liked_track_ids = {str(x.get("id")) for x in tracks if x.get("id")}
            self._last_liked_fetch = now
            LOG.info("Synced %d liked tracks from Yandex Music", len(self.liked_track_ids))
        except Exception as e:
            LOG.warning("Failed syncing Yandex Music likes: %s", e)

    def _resolve_track(self, title: str, artist: str, duration: float = 0.0) -> Tuple[str, str, Optional[str], bool, Optional[str]]:
        if not title and not artist:
            return title, artist, None, False, None

        cache_key = (title, artist, round(duration, 0))
        with self._lock:
            if hasattr(self, "_resolve_cache") and cache_key in self._resolve_cache:
                cached = self._resolve_cache[cache_key]
                tid = cached[2]
                is_liked = (str(tid) in self.liked_track_ids) if tid else cached[3]
                cover_url = cached[4] if len(cached) > 4 else None
                return cached[0], cached[1], tid, is_liked, cover_url

        self._sync_likes_if_needed()

        clean_title = "".join(c for c in title if c != "\ufffd" and ord(c) != 0xFFFD).strip(" ()[]-–_")
        clean_artist = "".join(c for c in artist if c != "\ufffd" and ord(c) != 0xFFFD).strip()

        queries_to_try = []
        if clean_title and clean_artist:
            queries_to_try.append(f"{clean_title} {clean_artist}")
        if clean_title:
            queries_to_try.append(clean_title)
        if clean_artist:
            queries_to_try.append(clean_artist)

        tracks = []
        for q in queries_to_try:
            try:
                r = self._http_session.get(
                    f"https://api.music.yandex.net/search?text={requests.utils.quote(q)}&type=track&page=0",
                    headers=self._get_yandex_headers(),
                    timeout=3,
                ).json()
                tracks_data = r.get("result", {}).get("tracks", {}) if "result" in r else r.get("tracks", {})
                found = tracks_data.get("results", tracks_data.get("items", []))
                if found:
                    tracks.extend(found)
                    break
            except Exception as e:
                LOG.warning("Yandex search query '%s' failed: %s", q, e)

        best_track = None
        best_score = 9999.0
        best_diff = 9999.0
        for t in tracks:
            t_dur = t.get("durationMs", 0) / 1000.0
            diff = abs(t_dur - duration) if duration > 0 else 0.0
            t_title = t.get("title", "").lower()
            t_artists = [a.get("name", "").lower() for a in t.get("artists", [])]
            t_matched = bool(clean_title and clean_title.lower() in t_title)
            a_matched = bool(clean_artist and any(clean_artist.lower() in a or a in clean_artist.lower() for a in t_artists))
            t_id = str(t.get("id"))
            t_real_id = str(t.get("realId"))
            t_liked = (t_id in self.liked_track_ids or t_real_id in self.liked_track_ids)

            bonus = 0.0
            if t_matched:
                bonus += 15.0
            if a_matched:
                bonus += 15.0
            if t_liked and (t_matched or a_matched):
                bonus += 25.0

            score = diff - bonus
            if score < best_score:
                best_score = score
                best_diff = diff
                best_track = t

        is_good_match = False
        if best_track:
            artist_matched = any(
                clean_artist.lower() in a.get("name", "").lower() or a.get("name", "").lower() in clean_artist.lower()
                for a in best_track.get("artists", [])
            ) if (best_track and clean_artist) else False
            title_matched = (clean_title.lower() in best_track.get("title", "").lower()) if (best_track and clean_title) else False
            best_t_id = str(best_track.get("id"))
            best_t_real_id = str(best_track.get("realId"))
            best_is_liked = (best_t_id in self.liked_track_ids or best_t_real_id in self.liked_track_ids)

            if best_diff < 5.0:
                is_good_match = True
            elif best_is_liked and (title_matched or artist_matched) and best_diff < 120.0:
                is_good_match = True
            elif title_matched and artist_matched and best_diff < 60.0:
                is_good_match = True
            elif (title_matched or not clean_title) and not duration:
                is_good_match = True
            elif title_matched and best_diff < 15.0:
                is_good_match = True

        if best_track and is_good_match:
            real_title = best_track.get("title", "")
            version = best_track.get("version")
            if version and version not in real_title:
                real_title = f"{real_title} ({version})"
            artists_list = best_track.get("artists", [])
            real_artist = ", ".join(a.get("name", "") for a in artists_list) if artists_list else artist
            tid = str(best_track.get("id"))
            real_id = str(best_track.get("realId"))

            # Check if this ID, realId, or ANY matching version in search is in likes
            is_liked = (tid in self.liked_track_ids) or (real_id in self.liked_track_ids)
            if not is_liked:
                for cand in tracks:
                    c_title = cand.get("title", "").lower()
                    c_artists = [a.get("name", "").lower() for a in cand.get("artists", [])]
                    c_t_matched = bool(clean_title and clean_title.lower() in c_title)
                    c_a_matched = bool(clean_artist and any(clean_artist.lower() in a for a in c_artists))
                    if c_t_matched and (not clean_artist or c_a_matched):
                        c_id = str(cand.get("id"))
                        c_real = str(cand.get("realId"))
                        if c_id in self.liked_track_ids or c_real in self.liked_track_ids:
                            is_liked = True
                            tid = c_id if c_id in self.liked_track_ids else c_real
                            break

            cover_uri = best_track.get("coverUri")
            cover_url = ("https://" + cover_uri.replace("%%", "400x400")) if cover_uri else None
            LOG.info(
                "Track '%s' by '%s' resolved to ID %s ('%s' by '%s', liked=%s, diff=%.2fs, cover=%s)",
                title, artist, tid, real_title, real_artist, is_liked, best_diff, cover_url
            )
            res = (real_title, real_artist, tid, is_liked, cover_url)
            with self._lock:
                if not hasattr(self, "_resolve_cache"):
                    self._resolve_cache = {}
                self._resolve_cache[cache_key] = res
            return res

        res = (title, artist, None, False, None)
        with self._lock:
            if not hasattr(self, "_resolve_cache"):
                self._resolve_cache = {}
            self._resolve_cache[cache_key] = res
        return res

    def _toggle_like(self) -> bool:
        if not self.current_track_id and self._last_title:
            res_info = self._resolve_track(
                self._last_title, getattr(self, "_last_artist", ""), getattr(self, "_last_duration", 0.0)
            )
            self.current_track_id = res_info[2]
            self.current_is_liked = res_info[3]
            if len(res_info) > 4:
                self.current_cover_url = res_info[4]

        if not self.current_track_id:
            LOG.warning("Cannot toggle like: track ID could not be resolved for '%s'", self._last_title)
            return self.current_is_liked

        try:
            headers = self._get_yandex_headers()
            if self.current_is_liked:
                # Remove from likes
                r = self._http_session.post(
                    f"https://api.music.yandex.net/users/{self.yandex_uid}/likes/tracks/remove",
                    data={"track-ids": str(self.current_track_id)},
                    headers=headers,
                    timeout=5,
                )
                LOG.info("Removed track %s from likes, status: %s", self.current_track_id, r.status_code)
                if r.status_code == 200:
                    self.liked_track_ids.discard(str(self.current_track_id))
                    self.current_is_liked = False
            else:
                # Add to likes
                r = self._http_session.post(
                    f"https://api.music.yandex.net/users/{self.yandex_uid}/likes/tracks/add-multiple",
                    data={"track-ids": str(self.current_track_id)},
                    headers=headers,
                    timeout=5,
                )
                LOG.info("Added track %s to likes, status: %s", self.current_track_id, r.status_code)
                if r.status_code == 200:
                    self.liked_track_ids.add(str(self.current_track_id))
                    self.current_is_liked = True

            with self._lock:
                if hasattr(self, "_resolve_cache") and self._last_title:
                    cache_key = (self._last_title, getattr(self, "_last_artist", ""), round(getattr(self, "_last_duration", 0.0), 0))
                    if cache_key in self._resolve_cache:
                        c = self._resolve_cache[cache_key]
                        self._resolve_cache[cache_key] = (c[0], c[1], c[2], self.current_is_liked, c[4] if len(c) > 4 else None)
                if self.last_state:
                    self.last_state["is_liked"] = self.current_is_liked
                    state_copy = dict(self.last_state)
                else:
                    state_copy = None

            if state_copy and self.on_status_change:
                self.on_status_change(state_copy)

            return True
        except Exception as e:
            LOG.error("Failed toggling track like: %s", e)
            return False

    def _get_target_session(self, manager):
        try:
            sessions = manager.get_sessions()
            ignored_apps = {"ayugram", "telegram", "discord", "whatsapp", "viber", "skype"}

            # 1. Look for Yandex Music specifically
            for s in sessions:
                app_name = (s.source_app_user_model_id or "").lower()
                if any(m in app_name for m in ignored_apps):
                    continue
                if "yandex" in app_name or "яндекс" in app_name or "music" in app_name or "\ufffd" in app_name:
                    return s

            # 2. Look for any actively playing session (excluding messengers)
            for s in sessions:
                app_name = (s.source_app_user_model_id or "").lower()
                if any(m in app_name for m in ignored_apps):
                    continue
                info = s.get_playback_info()
                if info and info.playback_status == 4:
                    return s

            # 3. Fallback to Windows current session if not a messenger
            curr = manager.get_current_session()
            if curr:
                app_name = (curr.source_app_user_model_id or "").lower()
                if not any(m in app_name for m in ignored_apps):
                    return curr
            return None
        except Exception:
            return None

    async def get_current_state(self) -> Dict[str, Any]:
        try:
            manager = await self._get_manager()
            session = self._get_target_session(manager)
            if not session:
                return {"active": False, "updated_at": time.time()}

            app_id = session.source_app_user_model_id or ""
            props = await session.try_get_media_properties_async()
            title = props.title if props else ""
            artist = props.artist if props else ""
            album = props.album_title if props else ""

            info = session.get_playback_info()
            status_code = info.playback_status if info else 0
            # PlaybackStatus: 4 = Playing, 5 = Paused, 3 = Stopped, 0 = Closed
            is_playing = (status_code == 4)
            status_str = "playing" if is_playing else ("paused" if status_code == 5 else "stopped")

            timeline = session.get_timeline_properties()
            position = round(timeline.position.total_seconds(), 1) if timeline and timeline.position else 0.0
            duration = round(timeline.end_time.total_seconds(), 1) if timeline and timeline.end_time else 0.0

            emoji_ids = []
            artist_avatar_id = None

            # Local playback clock anchor for continuous smooth ticking
            now = time.time()
            track_changed = (title != self._last_title or artist != getattr(self, "_last_artist", ""))
            if track_changed:
                self._last_title = title
                self._last_artist = artist
                self._last_duration = duration
                self._anchor_time = now
                self._anchor_pos = position
                self._last_playing = is_playing
                self._last_raw_position = position
                resolved_title, resolved_artist, tid, is_liked, cover_url = await asyncio.to_thread(
                    self._resolve_track, title, artist, duration
                )
                self.current_track_id = tid
                self.current_is_liked = is_liked
                self.current_cover_url = cover_url
                if resolved_title and ("\ufffd" not in resolved_title or "\ufffd" in title):
                    title = resolved_title
                if resolved_artist and ("\ufffd" not in resolved_artist or "\ufffd" in artist):
                    artist = resolved_artist
                self._resolved_title = title
                self._resolved_artist = artist
                now = time.time()
            else:
                title = getattr(self, "_resolved_title", title)
                artist = getattr(self, "_resolved_artist", artist)
                if getattr(self, "current_track_id", None):
                    self.current_is_liked = (str(self.current_track_id) in self.liked_track_ids)
                if is_playing != self._last_playing:
                    self._last_playing = is_playing
                    self._anchor_time = now
                    self._anchor_pos = position
                    self._last_raw_position = position
                elif position != getattr(self, "_last_raw_position", -1.0):
                    self._last_raw_position = position
                    self._anchor_time = now
                    self._anchor_pos = position

            if is_playing:
                calculated_pos = self._anchor_pos + (now - self._anchor_time)
                if duration > 0:
                    calculated_pos = min(duration, max(0.0, calculated_pos))
            else:
                calculated_pos = position
                self._anchor_time = now
                self._anchor_pos = position

            state = {
                "active": True,
                "app": app_id,
                "title": title,
                "artist": artist,
                "album": album,
                "is_playing": is_playing,
                "status": status_str,
                "position": round(calculated_pos, 1),
                "duration": duration,
                "cover_url": getattr(self, "current_cover_url", None),
                "emoji_ids": emoji_ids,
                "artist_avatar_id": artist_avatar_id,
                "is_liked": self.current_is_liked,
                "ui_icons": UI_ICONS,
                "updated_at": time.time(),
            }
            return state
        except Exception as e:
            LOG.exception("Error getting current media state: %s", e)
            self._session_manager = None
            return {"active": False, "error": str(e), "updated_at": time.time()}

    async def _execute_control_async(self, action: str, **kwargs) -> bool:
        try:
            if action == "toggle_like":
                return self._toggle_like()

            manager = await self._get_manager()
            session = self._get_target_session(manager)
            if not session:
                LOG.info("No active SMTC session for %s, simulating Windows media key", action)
                if action in ("toggle_play", "play", "pause"):
                    ctypes.windll.user32.keybd_event(0xB3, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(0xB3, 0, 2, 0)
                    return True
                elif action in ("next", "wave"):
                    ctypes.windll.user32.keybd_event(0xB0, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(0xB0, 0, 2, 0)
                    return True
                elif action == "prev":
                    ctypes.windll.user32.keybd_event(0xB1, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(0xB1, 0, 2, 0)
                    return True
                return False

            if action == "toggle_play":
                return await session.try_toggle_play_pause_async()
            elif action == "play":
                return await session.try_play_async()
            elif action == "pause":
                return await session.try_pause_async()
            elif action == "next":
                return await session.try_skip_next_async()
            elif action == "prev":
                return await session.try_skip_previous_async()
            elif action == "seek":
                delta = float(kwargs.get("delta", 10.0))
                timeline = session.get_timeline_properties()
                current_pos = timeline.position.total_seconds() if timeline and timeline.position else 0.0
                end_pos = timeline.end_time.total_seconds() if timeline and timeline.end_time else 300.0
                target_pos = max(0.0, min(end_pos, current_pos + delta))
                ticks = int(target_pos * 10_000_000)
                try:
                    return await session.try_change_playback_position_async(ticks)
                except TypeError:
                    from datetime import timedelta
                    return await session.try_change_playback_position_async(timedelta(seconds=target_pos))
            elif action == "wave":
                # For Yandex Music: skip to next track in My Wave or play
                return await session.try_skip_next_async()
            else:
                LOG.warning("Unknown media control action: %s", action)
                return False
        except Exception as e:
            LOG.exception("Error executing media control %s: %s", action, e)
            return False

    def execute_command(self, action: str, **kwargs) -> bool:
        if action == "toggle_like":
            res = self._toggle_like()
            self.trigger_poll()
            return res
        if self._loop and self._loop.is_running():
            async def _run_command():
                try:
                    await self._execute_control_async(action, **kwargs)
                except Exception as e:
                    LOG.error("Control execution failed: %s", e)
                finally:
                    self.trigger_poll()
                    if action in ("next", "prev", "wave"):
                        self._loop.call_later(0.35, self.trigger_poll)
                        self._loop.call_later(0.8, self.trigger_poll)

            asyncio.run_coroutine_threadsafe(_run_command(), self._loop)
            return True
        return False

    def trigger_poll(self) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._poll_step(force=True), self._loop)

    async def _poll_step(self, force: bool = False) -> None:
        state = await self.get_current_state()
        changed = force
        now = time.time()
        with self._lock:
            if not changed:
                if not self.last_state:
                    changed = True
                elif (
                    self.last_state.get("title") != state.get("title")
                    or self.last_state.get("artist") != state.get("artist")
                    or self.last_state.get("is_playing") != state.get("is_playing")
                    or self.last_state.get("active") != state.get("active")
                    or self.last_state.get("is_liked") != state.get("is_liked")
                    or self.last_state.get("cover_url") != state.get("cover_url")
                    or (
                        state.get("is_playing")
                        and (now - getattr(self, "_last_published_time", 0.0) >= 5.0)
                    )
                ):
                    changed = True

            if changed:
                self._last_published_pos = state.get("position", 0.0)
                self._last_published_time = now
                self.last_state = state

        if changed and self.on_status_change:
            try:
                self.on_status_change(state)
            except Exception as e:
                LOG.error("Error in on_status_change callback: %s", e)

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._poll_step(force=False)
            except Exception as e:
                LOG.exception("Error in media manager poll step: %s", e)

            # Sleep 3.5s if playing, 4.0s if active/paused, or 5.0s if idle (near 0% CPU for Explorer and Python)
            sleep_ticks = 35 if (self.last_state and self.last_state.get("is_playing")) else (40 if (self.last_state and self.last_state.get("active")) else 50)
            for _ in range(sleep_ticks):
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(0.1)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()

        def _target():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._run_loop())

        self._thread = threading.Thread(
            target=_target,
            name="media-manager-winrt",
            daemon=True,
        )
        self._thread.start()
        LOG.info("MediaManager started in background WinRT thread.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        LOG.info("MediaManager stopped.")
