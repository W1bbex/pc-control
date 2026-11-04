"""Tests for Antigravity blank window fix, INCY VPN server rotation, and strategic duplicate emoji prevention."""

import json
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock, patch
import urllib.error

import pytest

from desktop.action_dispatcher import ActionDispatcherMixin
from desktop.vpn_manager import VpnManagerMixin
from vds_bot.routers.antigravity_router import (
    heal_antigravity,
    route_antigravity_callback,
    route_antigravity_command,
)
from vds_bot.telegram_ui import EMOJI_PREFIX_RE, TelegramBotApi
from vds_bot.ui_markups import UiMarkupsMixin


def test_telegram_ui_sanitizer_strips_duplicate_emojis():
    """Verify that _sanitize_reply_markup automatically strips leading Unicode emojis from buttons with custom icons."""
    markup = {
        "inline_keyboard": [
            [
                {
                    "text": "🔮 Профили AG",
                    "style": "primary",
                    "icon_custom_emoji_id": "5348024810905581079",
                    "callback_data": "antigravity_menu",
                },
                {
                    "text": "⚡ Нажать ESC",
                    "style": "primary",
                    "icon_custom_emoji_id": "5346172035028531557",
                    "callback_data": "press_esc",
                },
            ],
            [
                {
                    "text": "📊 Активность за день",
                    "style": "primary",
                    "icon_custom_emoji_id": "5330473500064912704",
                    "callback_data": "menu_activity",
                },
                {
                    "text": "🕶️ Скрытность (Blackout)",
                    "style": "primary",
                    "icon_custom_emoji_id": "5346102804450681939",
                    "callback_data": "stealth_on",
                },
            ],
        ]
    }

    sanitized = TelegramBotApi._sanitize_reply_markup(markup)
    rows = sanitized["inline_keyboard"]

    assert rows[0][0]["text"] == "Профили AG"
    assert rows[0][0]["icon_custom_emoji_id"] == "5348024810905581079"

    assert rows[0][1]["text"] == "Нажать ESC"
    assert rows[0][1]["icon_custom_emoji_id"] == "5346172035028531557"

    assert rows[1][0]["text"] == "Активность за день"
    assert rows[1][1]["text"] == "Скрытность (Blackout)"


def test_emoji_prefix_regex_handles_all_common_emojis():
    """Verify that EMOJI_PREFIX_RE handles various unicode emoji blocks, variation selectors, and spaces."""
    samples = [
        ("🔮 Crystal", "Crystal"),
        ("⚡ Lightning", "Lightning"),
        ("📊 Chart", "Chart"),
        ("✖ Cross", "Cross"),
        ("☀️ Sun", "Sun"),
        ("🕶️ Shades", "Shades"),
        ("🔄 Restart", "Restart"),
        ("⚙️ Gear", "Gear"),
        ("⌨️ ⚡ Double Emoji", "Double Emoji"),
        ("Already Clean", "Already Clean"),
    ]
    for raw, expected in samples:
        assert EMOJI_PREFIX_RE.sub("", raw).strip() == expected


def test_apps_menu_markup_no_profiles_ag_and_clean_layout():
    """Verify that apps_menu_markup does not contain 'Профили AG', pairs Antigravity & Terminal, and adds Heal AG button."""
    mixin = UiMarkupsMixin()
    markup = mixin.apps_menu_markup()
    keyboard = markup.get("inline_keyboard", [])

    all_button_texts = [btn["text"] for row in keyboard for btn in row if isinstance(btn, dict) and "text" in btn]
    all_callbacks = [btn.get("callback_data") for row in keyboard for btn in row if isinstance(btn, dict)]

    # 'Профили AG' removed
    assert not any("профили ag" in t.lower() for t in all_button_texts)
    assert not any("🔮" in t for t in all_button_texts)

    # Clean ESC button
    assert "Нажать ESC" in all_button_texts
    assert not any("⚡ нажать esc" in t.lower() for t in all_button_texts)

    # Healing button present
    assert any("лечение" in t.lower() for t in all_button_texts)
    assert "ag_heal" in all_callbacks


def test_ui_markups_no_raw_emojis_in_button_texts():
    """Verify that no button text in standard menus starts with raw unicode emojis."""
    import re
    emoji_check = re.compile(r"^[\U0001f000-\U0001faff\u2600-\u27bf\u2300-\u23ff\u2b50]")

    mixin = UiMarkupsMixin()
    menus = [
        mixin.apps_menu_markup(),
        mixin.monitoring_menu_markup(),
        mixin.power_menu_markup(session="locked", system_status="online", stealth_active=False),
        mixin.power_menu_markup(session="unlocked", system_status="online", stealth_active=True),
        mixin.stealth_menu_markup(active=True),
        mixin.stealth_menu_markup(active=False),
        mixin.emergency_power_off_confirm_markup(),
        mixin.antigravity_card_markup({"accounts": [{"id": "1", "email": "test@gmail.com", "is_active": True}]}),
    ]

    for m in menus:
        for row in m.get("inline_keyboard", []):
            for btn in row:
                if isinstance(btn, dict) and "text" in btn:
                    assert not emoji_check.search(btn["text"]), f"Raw emoji found in button text: {btn['text']!r}"


def test_probe_google_https_success_and_failure():
    """Verify probe_google_https logic under successful 204, 404 and connection errors."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 204
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        assert VpnManagerMixin.probe_google_https(timeout=1.0) is True

    with patch("urllib.request.urlopen") as mock_urlopen:
        # HTTPError 404 still indicates TLS connection reached Google server
        err = urllib.error.HTTPError(url="https://oauth2.googleapis.com", code=404, msg="Not Found", hdrs={}, fp=None)
        mock_urlopen.side_effect = err

        assert VpnManagerMixin.probe_google_https(timeout=1.0) is True

    with patch("urllib.request.urlopen") as mock_urlopen:
        # URLError indicates connection failed / blocked
        err = urllib.error.URLError(reason="Connection timed out")
        mock_urlopen.side_effect = err

        assert VpnManagerMixin.probe_google_https(timeout=1.0) is False


def test_get_incy_servers_reads_sqlite_and_sorts(tmp_path):
    """Verify get_incy_servers reads SQLite, ignores Russian servers, and sorts by latency."""
    db_file = tmp_path / "incy.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE servers (id TEXT, name TEXT, latency INTEGER, isActive INTEGER)"
    )
    cur.executemany(
        "INSERT INTO servers VALUES (?, ?, ?, ?)",
        [
            ("s1", "🇩🇪 Германия", -1, 0),
            ("s2", "🇫🇮 Финляндия", 33, 0),
            ("s3", "🇷🇺 Россия", 20, 0),
            ("s4", "🇸🇪 Швеция", 80, 0),
            ("s5", "🇨🇭 Швейцария", 50, 0),
        ],
    )
    conn.commit()
    conn.close()

    with patch("os.path.expandvars", return_value=str(db_file)):
        servers = VpnManagerMixin.get_incy_servers()

    # Russia should be filtered out
    assert not any("россия" in s["name"].lower() for s in servers)
    # German server with -1 latency should have 9999 latency
    de_srv = next(s for s in servers if "германия" in s["name"].lower())
    assert de_srv["latency"] == 9999

    # Should be sorted ascending: Finland (33), Switzerland (50), Sweden (80), Germany (9999)
    names = [s["name"] for s in servers]
    assert names == ["🇫🇮 Финляндия", "🇨🇭 Швейцария", "🇸🇪 Швеция", "🇩🇪 Германия"]


def test_switch_next_incy_server_rotates_preferences(tmp_path):
    """Verify switch_next_incy_server updates preferences.json with next server and restarts process."""
    pref_file = tmp_path / "preferences.json"
    pref_file.write_text(
        json.dumps({"selectedServerId": "s1", "autoConnect": False}), encoding="utf-8"
    )

    class DummyAgent(VpnManagerMixin):
        def __init__(self):
            self.config = {"apps": {"INCY": "dummy_incy.exe"}}

        def check_incy_vpn_active(self):
            return True

    agent = DummyAgent()
    mock_servers = [
        {"id": "s1", "name": "Server 1", "latency": 30},
        {"id": "s2", "name": "Server 2", "latency": 45},
    ]

    with patch.object(VpnManagerMixin, "get_incy_servers", return_value=mock_servers), \
         patch("os.path.expandvars", return_value=str(pref_file)), \
         patch("psutil.process_iter", return_value=[]), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("subprocess.Popen") as mock_popen:

        res = agent.switch_next_incy_server()
        assert res["success"] is True
        assert res["server"] == "Server 2"
        assert res["server_id"] == "s2"

        saved_prefs = json.loads(pref_file.read_text(encoding="utf-8"))
        assert saved_prefs["selectedServerId"] == "s2"
        assert saved_prefs["autoConnect"] is True
        assert mock_popen.called


def test_heal_vpn_and_antigravity_workflow():
    """Verify heal_vpn_and_antigravity kills IDE, switches server, probes Google, and restarts IDE."""
    class DummyAgent(VpnManagerMixin):
        def __init__(self):
            self.config = {}

    agent = DummyAgent()

    with patch("core.antigravity_switcher.AntigravityAccountManager.kill_ide") as mock_kill, \
         patch.object(agent, "switch_next_incy_server", return_value={"success": True, "server": "Sweden", "latency": 40}) as mock_switch, \
         patch.object(agent, "probe_google_https", return_value=True) as mock_probe, \
         patch("core.antigravity_switcher.AntigravityAccountManager.launch_ide", return_value=True) as mock_launch:

        res = agent.heal_vpn_and_antigravity(attempts=2)

        assert res["success"] is True
        assert res["server"] == "Sweden"
        assert res["google_https_ok"] is True
        assert res["restarted_ide"] is True

        assert mock_kill.called
        assert mock_switch.called
        assert mock_probe.called
        assert mock_launch.called


def test_action_dispatcher_heal_antigravity():
    """Verify action dispatcher dispatches 'heal_antigravity' to heal_vpn_and_antigravity."""
    class DummyDispatcher(ActionDispatcherMixin, VpnManagerMixin):
        def __init__(self):
            self.acks = []
            self.config = {}

        def acknowledge(self, command_id, status, data=None):
            self.acks.append((command_id, status, data))

    dispatcher = DummyDispatcher()

    with patch.object(dispatcher, "heal_vpn_and_antigravity", return_value={"success": True, "server": "Finland"}) as mock_heal:
        dispatcher.execute_action("cmd_123", "heal_antigravity", {"attempts": 2})

        assert mock_heal.called
        assert len(dispatcher.acks) == 1
        assert dispatcher.acks[0][0] == "cmd_123"
        assert dispatcher.acks[0][1] == "executed"
        assert dispatcher.acks[0][2]["server"] == "Finland"


def test_antigravity_router_handles_fix_ag_command_and_callback():
    """Verify /fix_ag command and ag_heal callback trigger heal_antigravity."""
    app = MagicMock()
    app.mqtt.desktop_status = "online"
    app.mqtt.publish_desktop_action.return_value = "cmd_heal_999"

    # Command routing
    handled = route_antigravity_command(app, 12345, "/fix_ag")
    assert handled is True
    assert app.mqtt.publish_desktop_action.called
    assert app.mqtt.publish_desktop_action.call_args[0][0] == "heal_antigravity"

    # Callback routing
    app.reset_mock()
    handled_cb = route_antigravity_callback(app, "ag_heal", 12345, 999, "cb_1")
    assert handled_cb is True
    assert app.ui.answer_callback.called
    assert app.mqtt.publish_desktop_action.called
