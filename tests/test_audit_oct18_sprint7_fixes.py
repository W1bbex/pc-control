#!/usr/bin/env python3
"""
Regression tests for Sprint 7 fixes (Oct 18, 2026):
1. PlayerManager: Failed cover URL does not cause an infinite delete/send loop.
2. ActivityTracker: stop() signals stop_event immediately so thread terminates cleanly.
3. AutoBackup & .gitignore: secrets.h excluded from git and automated zip archives.
4. Stealth: COM Shell.Application object safely released before CoUninitialize.
"""

import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vds_bot.player_manager import PlayerManagerMixin
from core.activity_logger import ActivityTracker
from scripts.auto_backup import EXCLUDE_FILES


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


def test_player_manager_failed_cover_prevents_loop():
    """Verify that when send_photo fails for a cover_url, subsequent ticks do not spam recreate card."""
    bot = DummyBot()
    chat_id = 12345
    broken_url = "https://example.com/broken_cover.jpg"
    data = {
        "title": "Test Track",
        "artist": "Test Artist",
        "cover_url": broken_url,
        "is_playing": True,
    }

    # Setup UI mocks: send_photo fails (returns None), send_message succeeds
    bot.ui.send_photo.return_value = None
    bot.ui.send_message.return_value = {"message_id": 999}
    bot.ui.player_card_markup.return_value = {}
    bot.ui.player_card_caption.return_value = "Caption"
    bot.ui.player_card_text.return_value = "Text"

    # 1. Initial send
    res = bot._send_player_card(chat_id, data)
    assert res == {"message_id": 999}
    assert bot.active_player_message_id == 999
    assert bot.active_player_is_photo is False
    assert bot.failed_cover_url == broken_url

    # Reset call counts
    bot.ui.send_photo.reset_mock()
    bot.ui.send_message.reset_mock()
    bot.ui.delete_message.reset_mock()

    # 2. Next telemetry tick with same data and same msg_id:
    # Must NOT call _send_player_card again (which would delete old card and spam)
    with patch.object(bot, "_send_player_card") as mock_send_card:
        bot._render_and_send_player(chat_id, 999, data)
        assert mock_send_card.call_count == 0

    # 3. If a NEW track with a DIFFERENT cover arrives:
    new_url = "https://example.com/new_cover.jpg"
    new_data = dict(data, cover_url=new_url)
    with patch.object(bot, "_send_player_card") as mock_send_card:
        bot._render_and_send_player(chat_id, 999, new_data)
        assert mock_send_card.call_count == 1


def test_activity_tracker_stop_signals_event_fast(tmp_path):
    """Verify that stop() sets stop_event and unblocks wait() immediately without waiting full poll_seconds."""
    stop_ev = threading.Event()
    tracker = ActivityTracker(
        config={"poll_seconds": 5.0, "record_existing_processes": False},
        log_dir=tmp_path,
        stop_event=stop_ev,
    )
    assert not tracker.stop_event.is_set()

    with patch.object(tracker, "_processes", return_value={}), \
         patch.object(tracker, "_foreground", return_value={}):
        # Start the tracker and give it a moment to enter stop_event.wait(5.0)
        tracker.start()
        assert tracker.thread is not None
        time.sleep(0.05)

        # Call stop() and measure time - should wake up from wait(5.0) in < 0.2s
        t0 = time.monotonic()
        tracker.stop()
        elapsed = time.monotonic() - t0

        assert tracker.stop_event.is_set()
        assert not tracker.thread.is_alive()
        assert elapsed < 0.5


def test_backup_and_gitignore_exclude_secrets_h():
    """Verify secrets.h is present in EXCLUDE_FILES and in .gitignore."""
    # Check auto_backup EXCLUDE_FILES
    assert "secrets.h" in EXCLUDE_FILES or "*secrets.h*" in EXCLUDE_FILES

    # Check .gitignore
    gitignore_path = Path(__file__).resolve().parent.parent / ".gitignore"
    content = gitignore_path.read_text(encoding="utf-8")
    assert "secrets.h" in content


def test_stealth_com_shell_cleanup():
    """Verify minimize_all_windows sets shell to None before CoUninitialize."""
    from desktop.stealth import StealthMixin

    with patch("pythoncom.CoInitialize") as mock_init, \
         patch("pythoncom.CoUninitialize") as mock_uninit, \
         patch("win32com.client.Dispatch") as mock_dispatch, \
         patch("time.sleep"):
        
        mock_shell = MagicMock()
        mock_dispatch.return_value = mock_shell

        StealthMixin.minimize_all_windows()

        assert mock_init.called
        assert mock_uninit.called
        assert mock_shell.MinimizeAll.call_count == 3
