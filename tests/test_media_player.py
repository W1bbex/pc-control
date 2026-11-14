import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import time
from vds_bot.telegram_ui import TelegramBotApi

class TestMediaPlayerUI(unittest.TestCase):
    def setUp(self):
        self.ui = TelegramBotApi("dummy_token")

    def test_player_card_text_with_emojis(self):
        state = {
            "active": True,
            "title": "Meant To Be X Lucky Number 3",
            "artist": "Virtuals",
            "position": 75.0,
            "duration": 255.0,
            "is_playing": True,
            "emoji_ids": ["111", "222", "333", "444"],
            "ui_icons": {
                "tag_note": "555",
                "tag_artist": "666",
                "tag_status_play": "777",
            }
        }
        text = self.ui.player_card_text(state)
        # Check that 2x2 custom emoji cover grid is not in text (cover sent as photo)
        self.assertNotIn('<tg-emoji emoji-id="111">⬛</tg-emoji>', text)
        self.assertNotIn('<tg-emoji emoji-id="333">⬛</tg-emoji>', text)
        # Check track title and artist
        self.assertIn("<b>Meant To Be X Lucky Number 3</b>", text)
        self.assertIn("<i>Virtuals</i>", text)
        # Check timeline formatted (+2s latency compensation: 75s+2s=77s -> 01:17)
        self.assertIn("01:17", text)
        self.assertIn('<tg-emoji emoji-id="5330193051585391352">⚪</tg-emoji>', text)

    def test_player_card_with_artist_avatar_and_white_icon(self):
        state = {
            "active": True,
            "title": "Numb",
            "artist": "Linkin Park",
            "position": 30.0,
            "duration": 185.0,
            "is_playing": True,
            "artist_avatar_id": "999999",
            "ui_icons": {
                "btn_play": "10",
                "btn_pause": "11",
                "tag_status_play": "777",
            }
        }
        text = self.ui.player_card_text(state)
        # Verify artist avatar emoji is rendered
        self.assertIn('<tg-emoji emoji-id="999999">👤</tg-emoji>', text)
        # Verify white btn_play emoji is used instead of colored tag_status_play
        self.assertIn('<tg-emoji emoji-id="10">▶️</tg-emoji>', text)


    def test_player_card_markup_styles_and_icons(self):
        state = {
            "active": True,
            "is_playing": True,
            "ui_icons": {
                "btn_play": "10",
                "btn_pause": "11",
                "btn_fwd10": "12",
                "btn_back10": "13",
                "btn_prev": "14",
                "btn_next": "15",
                "btn_wave": "16",
            }
        }
        markup = self.ui.player_card_markup(state)
        rows = markup["inline_keyboard"]
        self.assertEqual(len(rows), 3)  # Exactly 3 rows: controls, navigation, like

        # Row 1: back10, pause (primary blue), fwd10
        r1 = rows[0]
        self.assertEqual(len(r1), 3)
        self.assertEqual(r1[0]["text"], "10с")
        self.assertEqual(r1[0]["icon_custom_emoji_id"], "13")
        self.assertEqual(r1[1]["text"], "Пауза")
        self.assertEqual(r1[1]["style"], "primary")
        self.assertEqual(r1[1]["icon_custom_emoji_id"], "11")
        self.assertEqual(r1[2]["text"], "10с")
        self.assertEqual(r1[2]["icon_custom_emoji_id"], "12")

        # Row 2: prev, wave (success green), next (3 buttons)
        r2 = rows[1]
        self.assertEqual(len(r2), 3)
        self.assertEqual(r2[0]["text"], "Пред")
        self.assertEqual(r2[0]["icon_custom_emoji_id"], "14")
        self.assertEqual(r2[1]["text"], "Волна")
        self.assertEqual(r2[1]["style"], "success")
        self.assertEqual(r2[1]["icon_custom_emoji_id"], "16")
        self.assertEqual(r2[2]["text"], "След")
        self.assertEqual(r2[2]["icon_custom_emoji_id"], "15")

        # Row 3: like button alone (no duplicate emoji in text)
        r3 = rows[2]
        self.assertEqual(len(r3), 1)
        self.assertEqual(r3[0]["text"], "В избранное")
        self.assertEqual(r3[0]["callback_data"], "player_toggle_like")

    def test_player_card_paused_state(self):
        state = {
            "active": True,
            "is_playing": False,
            "ui_icons": {"btn_play": "10"}
        }
        markup = self.ui.player_card_markup(state)
        # Button should show 'Плей' when paused
        self.assertEqual(markup["inline_keyboard"][0][1]["text"], "Плей")
    def test_settings_menu_has_activity(self):
        markup = self.ui.settings_menu_markup()
        all_cbs = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
        self.assertIn("menu_activity", all_cbs)

    def test_seek_ticks_conversion(self):
        import asyncio
        from datetime import timedelta
        from core.media_manager import MediaManager

        mm = MediaManager.__new__(MediaManager)
        calls = []

        class FakeSession:
            def get_timeline_properties(self):
                return MagicMock(position=timedelta(seconds=5.5), end_time=timedelta(seconds=120.0))

            async def try_change_playback_position_async(self, value):
                calls.append(value)
                if isinstance(value, timedelta):
                    raise TypeError("expected ticks")
                return True

        mm._get_manager = MagicMock(return_value=asyncio.sleep(0, result=MagicMock()))
        mm._get_target_session = MagicMock(return_value=FakeSession())
        ok = asyncio.run(mm._execute_control_async("seek", delta=10.0))
        self.assertTrue(ok)
        self.assertEqual(calls[0], timedelta(seconds=15.5))
        self.assertEqual(calls[1], 155000000)

    def test_multi_artist_matching_in_resolve_track(self):
        from core.media_manager import MediaManager
        mm = MediaManager.__new__(MediaManager)
        mm._lock = MagicMock()
        mm.yandex_token = "dummy"
        mm.yandex_uid = 123
        mm.liked_track_ids = {"155806688"}
        mm._last_liked_fetch = 9999999999.0
        mm._http_session = MagicMock()

        # Mock Yandex search returning track with list of artists
        fake_track = {
            "id": 155806688,
            "realId": 155806688,
            "title": "idk",
            "artists": [{"name": "седьмоенебо"}, {"name": "sekairotten"}],
            "durationMs": 125340,
        }
        mm._http_session.get.return_value.json.return_value = {
            "tracks": {"results": [fake_track]}
        }

        # Search with comma-separated artists and slight duration difference
        title, artist, tid, is_liked, cover = mm._resolve_track("idk", "седьмоенебо, sekairotten", 130.0)
        self.assertEqual(tid, "155806688")
        self.assertTrue(is_liked)

    def test_obsolete_heart_emoji_id_remapped(self):
        from vds_bot.monitoring import MonitoringManager
        from vds_bot.telegram_ui import TelegramBotApi

        ui = TelegramBotApi("dummy")
        # Obsolete heart ID in ui_icons
        state = {
            "active": True,
            "title": "idk",
            "artist": "test",
            "is_playing": True,
            "is_liked": True,
            "ui_icons": {
                "tag_heart_liked": "5330276670303674347",
            }
        }
        markup = ui.player_card_markup(state)
        # Check that like button remapped obsolete ID to 5346267379007530615
        btn_heart = markup["inline_keyboard"][2][0]
        self.assertEqual(btn_heart["icon_custom_emoji_id"], "5346267379007530615")

    def test_unliked_heart_crossed_out(self):
        from vds_bot.monitoring import MonitoringManager
        from vds_bot.telegram_ui import TelegramBotApi

        ui = TelegramBotApi("dummy")
        state = {
            "active": True,
            "title": "sekairotten",
            "artist": "sekairotten",
            "is_playing": True,
            "is_liked": False,
            "ui_icons": {
                "tag_heart_unliked": "5346165399304055198",
            }
        }
        markup = ui.player_card_markup(state)
        btn_heart = markup["inline_keyboard"][2][0]
        self.assertEqual(btn_heart["icon_custom_emoji_id"], "5346165399304055198")
        self.assertEqual(btn_heart["text"], "В избранное")

        # In monitoring dashboard text
        mock_mqtt = MagicMock()
        mock_mqtt.system_status = "online"
        mock_mqtt.system_session = "unlocked"
        mock_mqtt.desktop_status = "online"
        with tempfile.TemporaryDirectory() as tmpdir:
            mm = MonitoringManager(ui, mock_mqtt, history_db_path=Path(tmpdir) / "telemetry.db")
            mm.latest_received_at = 9999999999.0
            mm.latest_telemetry = {"user_active": True}
            text = mm.dashboard_text(state)
        self.assertIn("5346165399304055198", text)
        self.assertIn("💔", text)

    def test_title_match_required_in_resolve_track(self):
        from core.media_manager import MediaManager
        mm = MediaManager.__new__(MediaManager)
        mm._lock = MagicMock()
        mm.yandex_token = "dummy"
        mm.yandex_uid = 123
        mm.liked_track_ids = {"155806688"}  # 'idk' is in likes
        mm._last_liked_fetch = 9999999999.0
        mm._http_session = MagicMock()

        # Mock search returning:
        # 1) 'sekairotten' by 'sekairotten' (not liked)
        # 2) 'idk' by 'седьмоенебо, sekairotten' (liked)
        mm._http_session.get.return_value.json.return_value = {
            "tracks": {
                "results": [
                    {
                        "id": 154887343,
                        "realId": 154887343,
                        "title": "sekairotten",
                        "artists": [{"name": "sekairotten"}],
                        "durationMs": 122000,
                    },
                    {
                        "id": 155806688,
                        "realId": 155806688,
                        "title": "idk",
                        "artists": [{"name": "седьмоенебо"}, {"name": "sekairotten"}],
                        "durationMs": 125340,
                    },
                ]
            }
        }

        # Resolve 'sekairotten' - must NEVER pick 'idk' despite 'idk' being in likes!
        title, artist, tid, is_liked, cover = mm._resolve_track("sekairotten", "sekairotten", 122.0)
        self.assertEqual(title, "sekairotten")
        self.assertEqual(tid, "154887343")
        self.assertFalse(is_liked)


def test_media_manager_imports_ctypes_for_fallback_controls():
    import asyncio
    from core.media_manager import MediaManager

    mm = MediaManager.__new__(MediaManager)
    mm._get_manager = MagicMock(return_value=asyncio.sleep(0, result=MagicMock()))
    mm._get_target_session = MagicMock(return_value=None)
    with patch("core.media_manager.ctypes.windll.user32.keybd_event") as mock_keybd:
        ok = asyncio.run(mm._execute_control_async("toggle_play"))

    assert ok is True
    assert mock_keybd.call_count == 2
    assert mock_keybd.call_args_list[0][0] == (0xB3, 0, 0, 0)
    assert mock_keybd.call_args_list[1][0] == (0xB3, 0, 2, 0)


def test_media_manager_resolve_track_handles_none_title_and_artist():
    from core.media_manager import MediaManager

    mm = MediaManager.__new__(MediaManager)
    mm._lock = threading.Lock()
    mm._sync_likes_if_needed = lambda: None
    mm.liked_track_ids = set()

    assert mm._resolve_track("Прямой эфир", None)[0] == "Прямой эфир"
    assert mm._resolve_track(None, "Исполнитель")[1] == "Исполнитель"


def test_media_manager_toggle_like_async_thread_offload():
    from core.media_manager import MediaManager

    mm = MediaManager.__new__(MediaManager)
    mm._toggle_like = MagicMock(return_value=True)
    assert asyncio.run(mm._execute_control_async("toggle_like")) is True
    assert mm._toggle_like.called


def test_media_manager_execute_command_toggle_like_nonblocking():
    from core.media_manager import MediaManager

    mm = MediaManager.__new__(MediaManager)
    mm._toggle_like = MagicMock()
    mm.trigger_poll = MagicMock()
    mm._loop = MagicMock()
    mm._loop.is_running.return_value = True

    with patch("asyncio.run_coroutine_threadsafe") as mock_run:
        assert mm.execute_command("toggle_like") is True
        assert mock_run.called
        mock_run.call_args[0][0].close()


def test_media_manager_toggle_like_updates_liked_set_under_lock():
    from core.media_manager import MediaManager

    mm = MediaManager.__new__(MediaManager)
    mm.current_is_liked = False
    mm.current_track_id = 999
    mm.yandex_uid = "12345"
    mm.yandex_token = "fake_token"
    mm.liked_track_ids = set()
    mm._lock = MagicMock()
    mm._lock.__enter__ = MagicMock()
    mm._lock.__exit__ = MagicMock()
    mm._resolve_cache = {}
    mm._last_title = None

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mm._http_session = MagicMock()
    mm._http_session.post.return_value = mock_resp

    mm._toggle_like()
    assert mm._lock.__enter__.called
    assert "999" in mm.liked_track_ids


def test_media_manager_resolve_track_tolerates_null_metadata():
    from core.media_manager import MediaManager

    mgr = MediaManager.__new__(MediaManager)
    mgr._lock = threading.Lock()
    mgr._last_title = "Test Song"
    mgr._last_artist = "Test Artist"
    mgr._last_duration = 180.0
    mgr._last_liked_fetch = 0.0
    mgr._resolve_cache = {}
    mgr.liked_track_ids = {"liked-99"}
    mgr._get_yandex_headers = MagicMock(return_value={})

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "result": {
            "tracks": {
                "results": [
                    {
                        "id": "12345",
                        "realId": "12345",
                        "title": None,
                        "durationMs": None,
                        "artists": [{"name": None}, None],
                    },
                    {
                        "id": "liked-99",
                        "realId": "liked-99",
                        "title": "Test Song",
                        "durationMs": 180000,
                        "artists": [{"name": "Test Artist"}],
                    },
                ]
            }
        }
    }
    mgr._request_with_retry = MagicMock(return_value=mock_resp)

    res = mgr._resolve_track("Test Song", "Test Artist", 180.0)
    assert isinstance(res, tuple)
    assert res[2] == "liked-99"
    assert res[3] is True


def test_media_manager_request_with_retry_recovers_from_oserror():
    from core.media_manager import MediaManager

    mgr = MediaManager.__new__(MediaManager)
    mock_session = MagicMock()
    mock_session.get.side_effect = OSError(10049, "The requested address is not valid in its context")
    mgr._http_session = mock_session

    fallback_session = MagicMock()
    fallback_session.get.return_value = MagicMock(status_code=200)

    with patch("requests.Session", return_value=fallback_session):
        res = mgr._request_with_retry("GET", "https://api.music.yandex.net/test")
        assert res.status_code == 200
        assert mgr._http_session == fallback_session


def test_player_manager_failed_cover_prevents_card_recreation_loop():
    from vds_bot.player_manager import PlayerManagerMixin

    class DummyBot(PlayerManagerMixin):
        def __init__(self):
            self.ui = MagicMock()
            self.player_edit_lock = threading.Lock()
            self.last_player_edit_time = 0.0
            self.active_player_message_id = None
            self.active_player_chat_id = None
            self.active_player_business_connection_id = None
            self.active_player_is_photo = False
            self.active_player_cover_url = None
            self.failed_cover_url = None
            self.allowed_user_id = 12345

        def save_state(self):
            pass

    bot = DummyBot()
    chat_id = 12345
    broken_url = "https://example.com/broken_cover.jpg"
    data = {
        "title": "Test Track",
        "artist": "Test Artist",
        "cover_url": broken_url,
        "is_playing": True,
    }

    bot.ui.send_photo.return_value = None
    bot.ui.send_message.return_value = {"message_id": 999}
    bot.ui.player_card_markup.return_value = {}
    bot.ui.player_card_caption.return_value = "Caption"
    bot.ui.player_card_text.return_value = "Text"

    res = bot._send_player_card(chat_id, data)
    assert res == {"message_id": 999}
    assert bot.active_player_message_id == 999
    assert bot.active_player_is_photo is False
    assert bot.failed_cover_url == broken_url

    bot.ui.send_photo.reset_mock()
    bot.ui.send_message.reset_mock()
    bot.ui.delete_message.reset_mock()

    with patch.object(bot, "_send_player_card") as mock_send_card:
        bot._render_and_send_player(chat_id, 999, data)
        assert mock_send_card.call_count == 0

    new_data = dict(data, cover_url="https://example.com/new_cover.jpg")
    with patch.object(bot, "_send_player_card") as mock_send_card:
        bot._render_and_send_player(chat_id, 999, new_data)
        assert mock_send_card.call_count == 1


def test_media_manager_concurrent_oserror_recreates_http_session_once():
    from core.media_manager import MediaManager

    mgr = MediaManager.__new__(MediaManager)
    mgr._session_lock = threading.Lock()
    barrier = threading.Barrier(4)

    def _fail_together(*args, **kwargs):
        barrier.wait(timeout=2.0)
        raise OSError(10049, "The requested address is not valid in its context")

    old_session = MagicMock()
    old_session.get.side_effect = _fail_together
    mgr._http_session = old_session

    fallback_session = MagicMock()
    fallback_session.get.return_value = MagicMock(status_code=200)

    results = []
    errors = []

    def _worker():
        try:
            resp = mgr._request_with_retry("GET", "https://api.music.yandex.net/test")
            results.append(resp.status_code)
        except Exception as exc:
            errors.append(exc)

    with patch("core.media_manager.requests.Session", return_value=fallback_session) as mock_session_cls:
        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=3.0)

    assert not errors
    assert results == [200, 200, 200, 200]
    old_session.close.assert_called_once()
    assert mock_session_cls.call_count == 1
    assert mgr._http_session is fallback_session


def test_media_manager_resolve_track_snapshots_likes_safely_during_concurrent_sync():
    from core.media_manager import MediaManager

    mgr = MediaManager.__new__(MediaManager)
    mgr._lock = threading.Lock()
    mgr._session_lock = threading.Lock()
    mgr.yandex_token = "dummy"
    mgr.yandex_uid = 123
    mgr.liked_track_ids = {str(i) for i in range(200)}
    mgr._last_liked_fetch = time.time()
    mgr._resolve_cache = {}
    mgr._http_session = MagicMock()

    likes_payload = {"library": {"tracks": [{"id": str(i)} for i in range(200, 400)]}}
    search_payload = {
        "tracks": {
            "results": [
                {
                    "id": 250,
                    "realId": 250,
                    "title": f"Track {i}",
                    "artists": [{"name": "Artist"}],
                    "durationMs": 120000,
                }
                for i in range(20)
            ]
        }
    }

    def _mock_get(url, **kwargs):
        resp = MagicMock()
        if "likes/tracks" in url:
            resp.json.return_value = likes_payload
        else:
            resp.json.return_value = search_payload
        return resp

    mgr._http_session.get.side_effect = _mock_get
    errors = []

    def _sync_worker():
        try:
            for _ in range(15):
                mgr._sync_likes_if_needed(force=True)
        except Exception as exc:
            errors.append(exc)

    def _resolve_worker(idx):
        try:
            for j in range(15):
                mgr._resolve_track(f"Track 0", "Artist", 120.0)
                with mgr._lock:
                    mgr._resolve_cache.clear()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_sync_worker)] + [
        threading.Thread(target=_resolve_worker, args=(i,)) for i in range(3)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=5.0)

    assert not errors


def test_request_with_retry_remounts_adapter_after_oserror():
    from core.media_manager import MediaManager

    mgr = MediaManager.__new__(MediaManager)
    mgr._lock = threading.RLock()
    mgr._session_lock = threading.Lock()
    initial_session = MagicMock()
    initial_session.get.side_effect = OSError("network changed")
    mgr._http_session = initial_session

    recreated_session = MagicMock()
    ok_resp = MagicMock(status_code=200)
    recreated_session.get.return_value = ok_resp

    with patch("core.media_manager.requests.Session", return_value=recreated_session), \
         patch.object(MediaManager, "_mount_phys_adapter") as mock_mount:
        res = mgr._request_with_retry("GET", "https://api.music.yandex.net/account/status", timeout=5)

    assert res is ok_resp
    mock_mount.assert_called_once_with(recreated_session)
    assert mgr._http_session is recreated_session


def test_toggle_like_http_500_returns_false_without_flipping_state():
    from core.media_manager import MediaManager

    mgr = MediaManager.__new__(MediaManager)
    mgr._lock = threading.RLock()
    mgr._session_lock = threading.Lock()
    mgr.yandex_uid = "42"
    mgr.yandex_token = "tok"
    mgr.liked_track_ids = set()
    mgr.current_track_id = "777"
    mgr.current_is_liked = False

    fail_resp = MagicMock(status_code=500)
    with patch.object(mgr, "_request_with_retry", return_value=fail_resp):
        result = mgr._toggle_like()

    assert result is False
    assert mgr.current_is_liked is False
    assert "777" not in mgr.liked_track_ids


if __name__ == "__main__":
    unittest.main()

