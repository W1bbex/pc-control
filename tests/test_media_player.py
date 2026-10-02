import unittest
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

if __name__ == "__main__":
    unittest.main()
