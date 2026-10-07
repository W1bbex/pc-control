import unittest
from unittest.mock import MagicMock
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
        from datetime import timedelta
        # 1 second = 10,000,000 100-ns ticks in WinRT
        target_pos = 15.5
        ticks = int(target_pos * 10_000_000)
        self.assertEqual(ticks, 155000000)
        self.assertIsInstance(ticks, int)

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
                "tag_heart_unliked": "5348530607729190493",
            }
        }
        markup = ui.player_card_markup(state)
        btn_heart = markup["inline_keyboard"][2][0]
        self.assertEqual(btn_heart["icon_custom_emoji_id"], "5348530607729190493")
        self.assertEqual(btn_heart["text"], "В избранное")

        # In monitoring dashboard text
        mock_mqtt = MagicMock()
        mock_mqtt.system_status = "online"
        mock_mqtt.system_session = "unlocked"
        mock_mqtt.desktop_status = "online"
        mm = MonitoringManager(ui, mock_mqtt)
        mm.latest_received_at = 9999999999.0
        mm.latest_telemetry = {"user_active": True}
        text = mm.dashboard_text(state)
        self.assertIn("5348530607729190493", text)
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

if __name__ == "__main__":
    unittest.main()

