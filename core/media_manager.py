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
_MEDIA_LOCK_INIT_GUARD = threading.Lock()

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

# Pre-uploaded vector UI icons & tags
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
    "tag_heart_unliked": _CANONICAL_ICONS.get("heart_unliked", "5346165399304055198"),
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
        self._session_lock = threading.Lock()
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
        self._mount_phys_adapter(self._http_session)

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

    def _get_lock(self):
        lock = getattr(self, "_lock", None)
        if lock is not None:
            return lock
        with _MEDIA_LOCK_INIT_GUARD:
            lock = getattr(self, "_lock", None)
            if lock is None:
                lock = threading.Lock()
                self._lock = lock
            return lock

    def _get_session_lock(self):
        lock = getattr(self, "_session_lock", None)
        if lock is not None:
            return lock
        with _MEDIA_LOCK_INIT_GUARD:
            lock = getattr(self, "_session_lock", None)
            if lock is None:
                lock = threading.Lock()
                self._session_lock = lock
            return lock

    @staticmethod
    def _mount_phys_adapter(session: requests.Session) -> None:
        """Bind physical LAN interface for Yandex Music so requests bypass active VPN TUN."""
        try:
            import socket
            from requests.adapters import HTTPAdapter

            class _SourceAddressAdapter(HTTPAdapter):
                def __init__(self, source_address, **kwargs):
                    self.source_address = source_address
                    super().__init__(**kwargs)

                def init_poolmanager(self, *args, **kwargs):
                    kwargs["source_address"] = self.source_address
                    super().init_poolmanager(*args, **kwargs)

            def _get_best_phys_ip():
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                        sock.connect(("77.88.8.8", 53))
                        ip = sock.getsockname()[0]
                        if ip and not ip.startswith(("127.", "198.18.", "169.254.")):
                            return ip
                except Exception:
                    pass
                try:
                    ips = socket.gethostbyname_ex(socket.gethostname())[2]
                    phys_ips = [ip for ip in ips if not ip.startswith(("127.", "198.18.", "169.254."))]
                    return phys_ips[0] if phys_ips else None
                except Exception:
                    return None

            phys_ip = _get_best_phys_ip()
            if phys_ip:
                adapter = _SourceAddressAdapter((phys_ip, 0))
                session.mount("https://api.music.yandex.net/", adapter)
                session.mount("http://api.music.yandex.net/", adapter)
        except Exception as e:
            LOG.debug("Could not mount physical source address adapter: %s", e)

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        session_lock = self._get_session_lock()
        with session_lock:
            session = self._http_session
        func = getattr(session, method.lower(), session.request)
        try:
            return func(url, **kwargs)
        except OSError as e:
            LOG.warning(
                "Yandex Music request failed with OSError (%s: %s). Recreating session with refreshed LAN binding...",
                type(e).__name__,
                e,
            )
            with session_lock:
                if self._http_session is session:
                    try:
                        session.close()
                    except Exception:
                        pass
                    recreated = requests.Session()
                    self._mount_phys_adapter(recreated)
                    self._http_session = recreated
                new_session = self._http_session
            func = getattr(new_session, method.lower(), new_session.request)
            return func(url, **kwargs)

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
        if not getattr(self, "yandex_token", None) or not getattr(self, "yandex_uid", None):
            return
        now = time.time()
        lock = self._get_lock()
        with lock:
            last_fetch = getattr(self, "_last_liked_fetch", 0.0)
            has_cached = bool(getattr(self, "liked_track_ids", ()))
        if not force and now - last_fetch < 60.0 and has_cached:
            return
        try:
            resp = self._request_with_retry(
                "GET",
                f"https://api.music.yandex.net/users/{self.yandex_uid}/likes/tracks",
                headers=self._get_yandex_headers(),
                timeout=5,
            )
            status = getattr(resp, "status_code", None)
            if isinstance(status, int) and status >= 400:
                LOG.warning("Yandex Music likes sync returned HTTP %s", status)
                return
            r = resp.json()
            data = r.get("result", r) if isinstance(r, dict) else {}
            tracks = data.get("library", {}).get("tracks", []) if isinstance(data, dict) else []
            if tracks:
                new_liked = {str(x.get("id")) for x in tracks if x.get("id")}
                with lock:
                    self.liked_track_ids = new_liked
                    self._last_liked_fetch = now
                    liked_count = len(new_liked)
                LOG.info("Synced %d liked tracks from Yandex Music", liked_count)
            else:
                with lock:
                    liked_count = len(getattr(self, "liked_track_ids", ()))
                if liked_count:
                    LOG.warning("Yandex Music likes sync returned empty or error (%s), keeping %d cached tracks", r.get("name") if isinstance(r, dict) else "", liked_count)
        except Exception as e:
            LOG.warning("Failed syncing Yandex Music likes: %s", e)

    def _resolve_track(self, title: str, artist: str, duration: float = 0.0) -> Tuple[str, str, Optional[str], bool, Optional[str]]:
        title = title or ""
        artist = artist or ""
        if not title and not artist:
            return title, artist, None, False, None

        cache_key = (title, artist, round(duration, 0))
        lock = self._get_lock()
        with lock:
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
                resp = self._request_with_retry(
                    "GET",
                    f"https://api.music.yandex.net/search?text={requests.utils.quote(q)}&type=all&page=0",
                    headers=self._get_yandex_headers(),
                    timeout=3,
                )
                status = getattr(resp, "status_code", None)
                if isinstance(status, int) and status >= 400:
                    LOG.warning("Yandex search query '%s' returned HTTP %s", q, status)
                    continue
                r = resp.json()
                res = r.get("result", r) if isinstance(r, dict) else {}
                tracks_data = res.get("tracks", {})
                found = tracks_data.get("results", tracks_data.get("items", []))
                if found:
                    tracks.extend(found)
                    break
            except Exception as e:
                LOG.warning("Yandex search query '%s' failed: %s", q, e)

        with lock:
            liked_ids = set(getattr(self, "liked_track_ids", ()))

        best_track = None
        best_score = 9999.0
        best_diff = 9999.0
        for t in tracks:
            t_dur = (t.get("durationMs") or 0) / 1000.0
            diff = abs(t_dur - duration) if duration > 0 else 0.0
            t_title = (t.get("title") or "").lower()
            t_artists = [(a.get("name") or "").lower() for a in (t.get("artists") or []) if isinstance(a, dict)]
            t_matched = bool(clean_title and (clean_title.lower() in t_title or t_title in clean_title.lower()))
            a_matched = bool(clean_artist and any(clean_artist.lower() in a or a in clean_artist.lower() for a in t_artists))

            # Strictly require title match if clean_title is present!
            # Never match another song just because the artist or likes match!
            if clean_title and not t_matched:
                continue

            t_id = str(t.get("id"))
            t_real_id = str(t.get("realId"))
            t_liked = (t_id in liked_ids or t_real_id in liked_ids)

            bonus = 0.0
            if t_matched:
                bonus += 15.0
            if a_matched:
                bonus += 15.0
            if t_liked:
                bonus += 5.0

            score = diff - bonus
            if score < best_score:
                best_score = score
                best_diff = diff
                best_track = t

        is_good_match = False
        if best_track:
            artist_matched = any(
                clean_artist.lower() in (a.get("name") or "").lower() or (a.get("name") or "").lower() in clean_artist.lower()
                for a in (best_track.get("artists") or []) if isinstance(a, dict)
            ) if (best_track and clean_artist) else False
            best_t_title = (best_track.get("title") or "").lower()
            title_matched = bool(clean_title and (clean_title.lower() in best_t_title or best_t_title in clean_title.lower())) if (best_track and clean_title) else False

            # Title match is strictly required if clean_title is present
            if clean_title and not title_matched:
                is_good_match = False
            elif best_diff < 5.0 and (title_matched or artist_matched):
                is_good_match = True
            elif title_matched and artist_matched and best_diff < 60.0:
                is_good_match = True
            elif title_matched and best_diff < 15.0:
                is_good_match = True
            elif (title_matched or not clean_title) and not duration:
                is_good_match = True

        if best_track and is_good_match:
            real_title = best_track.get("title") or title
            version = best_track.get("version")
            if version and version not in real_title:
                real_title = f"{real_title} ({version})"
            artists_list = [a for a in (best_track.get("artists") or []) if isinstance(a, dict)]
            real_artist = ", ".join((a.get("name") or "") for a in artists_list if a.get("name")) if artists_list else artist
            tid = str(best_track.get("id"))
            real_id = str(best_track.get("realId"))

            # Check if this ID, realId, or ANY matching version in search is in likes
            is_liked = (tid in liked_ids) or (real_id in liked_ids)
            if not is_liked:
                for cand in tracks:
                    c_title = (cand.get("title") or "").lower()
                    c_artists = [
                        (a.get("name") or "").lower()
                        for a in (cand.get("artists") or [])
                        if isinstance(a, dict)
                    ]
                    c_t_matched = bool(clean_title and clean_title.lower() in c_title)
                    c_a_matched = bool(clean_artist and any(clean_artist.lower() in a for a in c_artists))
                    if c_t_matched and (not clean_artist or c_a_matched):
                        c_id = str(cand.get("id"))
                        c_real = str(cand.get("realId"))
                        if c_id in liked_ids or c_real in liked_ids:
                            is_liked = True
                            tid = c_id if c_id in liked_ids else c_real
                            break

            cover_uri = best_track.get("coverUri")
            cover_url = ("https://" + cover_uri.replace("%%", "400x400")) if cover_uri else None
            LOG.info(
                "Track '%s' by '%s' resolved to ID %s ('%s' by '%s', liked=%s, diff=%.2fs, cover=%s)",
                title, artist, tid, real_title, real_artist, is_liked, best_diff, cover_url
            )
            res = (real_title, real_artist, tid, is_liked, cover_url)
            with lock:
                if not hasattr(self, "_resolve_cache"):
                    self._resolve_cache = {}
                self._resolve_cache[cache_key] = res
            return res

        res = (title, artist, None, False, None)
        with lock:
            if not hasattr(self, "_resolve_cache"):
                self._resolve_cache = {}
            self._resolve_cache[cache_key] = res
        return res

    def _toggle_like(self) -> bool:
        lock = self._get_lock()
        with lock:
            track_id = self.current_track_id
            last_title = getattr(self, "_last_title", "")
            last_artist = getattr(self, "_last_artist", "")
            last_duration = getattr(self, "_last_duration", 0.0)
            is_liked = self.current_is_liked

        if not track_id and last_title:
            res_info = self._resolve_track(last_title, last_artist, last_duration)
            with lock:
                self.current_track_id = res_info[2]
                self.current_is_liked = res_info[3]
                if len(res_info) > 4:
                    self.current_cover_url = res_info[4]
                track_id = self.current_track_id
                is_liked = self.current_is_liked

        if not track_id:
            LOG.warning("Cannot toggle like: track ID could not be resolved for '%s'", last_title)
            return is_liked

        try:
            headers = self._get_yandex_headers()
            target_url = (
                f"https://api.music.yandex.net/users/{self.yandex_uid}/likes/tracks/remove"
                if is_liked
                else f"https://api.music.yandex.net/users/{self.yandex_uid}/likes/tracks/add-multiple"
            )
            r = self._request_with_retry(
                "POST",
                target_url,
                data={"track-ids": str(track_id)},
                headers=headers,
                timeout=5,
            )
            status = getattr(r, "status_code", None)
            action_word = "Removed track" if is_liked else "Added track"
            LOG.info("%s %s %s likes, status: %s", action_word, track_id, "from" if is_liked else "to", status)
            if status != 200:
                if isinstance(status, int) and status >= 400:
                    LOG.warning("Yandex Music toggle_like failed with HTTP %s for track %s", status, track_id)
                return False

            new_liked_state = not is_liked
            with lock:
                if new_liked_state:
                    self.liked_track_ids.add(str(track_id))
                else:
                    self.liked_track_ids.discard(str(track_id))
                self.current_is_liked = new_liked_state

                if hasattr(self, "_resolve_cache") and last_title:
                    cache_key = (last_title, last_artist, round(last_duration, 0))
                    if cache_key in self._resolve_cache:
                        c = self._resolve_cache[cache_key]
                        self._resolve_cache[cache_key] = (
                            c[0],
                            c[1],
                            c[2],
                            new_liked_state,
                            c[4] if len(c) > 4 else None,
                        )
                last_st = getattr(self, "last_state", None)
                if last_st is not None:
                    last_st["is_liked"] = new_liked_state
                    state_copy = dict(last_st)
                else:
                    state_copy = None

            if state_copy and getattr(self, "on_status_change", None):
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
                resolved_title, resolved_artist, tid, is_liked, cover_url = await asyncio.to_thread(
                    self._resolve_track, title, artist, duration
                )
                with self._get_lock():
                    self._last_title = title
                    self._last_artist = artist
                    self._last_duration = duration
                    self._anchor_time = time.time()
                    self._anchor_pos = position
                    self._last_playing = is_playing
                    self._last_raw_position = position
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
                with self._get_lock():
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

            with self._get_lock():
                cover_snapshot = getattr(self, "current_cover_url", None)
                liked_snapshot = bool(self.current_is_liked)

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
                "cover_url": cover_snapshot,
                "emoji_ids": emoji_ids,
                "artist_avatar_id": artist_avatar_id,
                "is_liked": liked_snapshot,
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
                return await asyncio.to_thread(self._toggle_like)

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
                from datetime import timedelta
                try:
                    return await session.try_change_playback_position_async(timedelta(seconds=target_pos))
                except TypeError:
                    ticks = int(target_pos * 10_000_000)
                    return await session.try_change_playback_position_async(ticks)
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
            if self._loop and self._loop.is_running():
                async def _run_like():
                    try:
                        await asyncio.to_thread(self._toggle_like)
                    except Exception as e:
                        LOG.error("Toggle like failed: %s", e)
                    finally:
                        self.trigger_poll()

                asyncio.run_coroutine_threadsafe(_run_like(), self._loop)
                return True
            else:
                threading.Thread(target=self._toggle_like, daemon=True).start()
                return True
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
        with self._get_lock():
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
            with self._get_lock():
                last_st = dict(self.last_state) if self.last_state else {}
            sleep_ticks = 35 if last_st.get("is_playing") else (40 if last_st.get("active") else 50)
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
            try:
                self._loop.run_until_complete(self._run_loop())
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass

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
        with self._get_session_lock():
            if hasattr(self, "_http_session") and self._http_session:
                try:
                    self._http_session.close()
                except Exception:
                    pass
        LOG.info("MediaManager stopped.")
