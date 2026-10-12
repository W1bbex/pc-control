#!/usr/bin/env python3
"""Custom Telegram emoji icons registry and canonical mapping."""

import json
import logging
import os
from pathlib import Path

LOG = logging.getLogger("pc-control-bot.ui_icons")

CANONICAL_ICONS = {
    "power": "5346321083278600717",
    "screenshot": "5345959885118942220",
    "sound": "5348313432707868130",
    "sound_mute": "5330388528431935587",
    "screen": "5346102804450681939",
    "apps": "5346196795514989290",
    "game": "5346155860181689133",
    "gear": "5348024810905581079",
    "cpu": "5348117861372044294",
    "hardware": "5348117861372044294",
    "ram": "5348445923859016939",
    "disk": "5346264376825393526",
    "network": "5346016016046532285",
    "lock": "5348293839067068050",
    "light": "5346214490780246375",
    "music": "5346016960939337497",
    "telegram": "5345922274090330566",
    "chrome": "5346208250192767289",
    "terminal": "5346172035028531557",
    "pc": "5330499003580721030",
    "user": "5330478284658488376",
    "folder": "5330394360997517972",
    "back": "5330209080403340437",
    "refresh": "5330544792227063795",
    "cross": "5330015179809794606",
    "trash": "5330144857757362833",
    "pencil": "5330305068627436798",
    "search": "5330079544189695031",
    "play": "5330015871299527534",
    "pause": "5330570905628222348",
    "fwd10": "5330317725896059016",
    "back10": "5330294859490179153",
    "next": "5330255053733276330",
    "prev": "5330363570376977789",
    "repeat": "5330448799707998190",
    "heart": "5346267379007530615",
    "heart_liked": "5346267379007530615",
    "heart_unliked": "5346165399304055198",
    "headphones": "5330526830673831881",
    "dot": "5330193051585391352",
    "check": "5330273230034871570",
    "clock": "5330473500064912704",
    "chart": "5330382442463271794",
    "wifi": "5330160856510540663",
    "battery": "5330277593721644293",
    "status_active": "5345809221961167372",
    "status_inactive": "5345825096160291312",
    "status_warning": "5359432244043754958",
    "pulse_green": "5345809221961167372",
    "pulse_red": "5345825096160291312",
    "pulse_yellow": "5359432244043754958",
    "wave": "5330448799707998190",
    "linux": "5332695978136874502",
    "windows": "5332280534540264957",
    "app_telegram": "5251740431632344455",
    "app_yandex_music": "5251544533879006440",
    "app_happ": "5251339414830880848",
    "app_incy": "5319035276306196317",
    "app_chrome": "5251317871274929314",
    "app_antigravity": "5251292384938994108",
    "app_dolphin": "5278321743709124204",
}

def _load_canonical_icons():
    from pathlib import Path
    icons = dict(CANONICAL_ICONS)
    for candidate in [
        Path(__file__).resolve().parent / "resources" / "icons.json",
        Path(__file__).resolve().parent.parent / "resources" / "icons.json",
    ]:
        if candidate.exists():
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        icons.update(data)
                        return icons
            except Exception:
                pass
    return icons

ICONS = _load_canonical_icons()

EMOJI_BTN_POWER = ICONS.get("power", "5330077297921795474")
EMOJI_BTN_SCREENSHOT = ICONS.get("screenshot", "5330238646958204875")
EMOJI_BTN_APPS = ICONS.get("apps", "5330130731609929749")
EMOJI_BTN_SOUND = ICONS.get("sound", "5330140476890718558")
EMOJI_BTN_SETTINGS = ICONS.get("gear", "5330260680140431237")

DEFAULT_PLAYER_ICONS = {
    "btn_play": ICONS.get("play", "5330015871299527534"),
    "btn_pause": ICONS.get("pause", "5330570905628222348"),
    "btn_next": ICONS.get("next", "5330255053733276330"),
    "btn_prev": ICONS.get("prev", "5330363570376977789"),
    "btn_fwd10": ICONS.get("fwd10", "5330317725896059016"),
    "btn_back10": ICONS.get("back10", "5330294859490179153"),
    "btn_wave": ICONS.get("wave", "5330448799707998190"),
    "tag_note": ICONS.get("music", "5330477356945551054"),
    "tag_artist": ICONS.get("headphones", "5330526830673831881"),
    "tag_status_play": ICONS.get("play", "5330015871299527534"),
    "tag_status_pause": ICONS.get("pause", "5330570905628222348"),
    "tag_clock": ICONS.get("clock", "5330473500064912704"),
    "tag_slider_dot": ICONS.get("dot", "5330193051585391352"),
    "tag_heart_liked": ICONS.get("heart_liked", ICONS.get("heart", "5346267379007530615")),
    "tag_heart_unliked": ICONS.get("heart_unliked", "5346165399304055198"),
}

