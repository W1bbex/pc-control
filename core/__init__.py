"""PCControlAgent core modules."""
from .activity_db import ActivityDB, get_activity_db
from .activity_logger import ActivityTracker
from .antigravity_crypto import AntigravityCrypto, ProtobufHelper
from .antigravity_process import IDE_PROCESS_NAMES, is_ide_running, kill_ide, launch_ide
from .antigravity_switcher import AntigravityAccountManager
from .app_emoji_manager import AppEmojiManager
from .premium_checker import PremiumChecker
try:
    from .icon_extractor import extract_app_icon_png
except (ImportError, AttributeError, OSError):
    extract_app_icon_png = None

try:
    from .media_manager import MediaManager
except (ImportError, AttributeError, OSError):
    MediaManager = None

__all__ = [
    "ActivityDB",
    "get_activity_db",
    "ActivityTracker",
    "AntigravityAccountManager",
    "AntigravityCrypto",
    "ProtobufHelper",
    "IDE_PROCESS_NAMES",
    "is_ide_running",
    "kill_ide",
    "launch_ide",
    "AppEmojiManager",
    "PremiumChecker",
    "extract_app_icon_png",
    "MediaManager",
]

