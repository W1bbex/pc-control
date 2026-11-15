import json
import time
from pathlib import Path
from core.premium_checker import PremiumChecker


def test_premium_checker_event_driven(tmp_path):
    cache_file = tmp_path / "premium_cache.json"
    checker = PremiumChecker(cache_path=cache_file, ttl_seconds=10)

    # Initially unknown user -> False
    assert checker.is_user_premium(12345) is False

    # Event arrives with is_premium: True
    res = checker.update_from_user_obj(12345, {"id": 12345, "is_premium": True})
    assert res is True
    assert checker.is_user_premium(12345) is True

    # Check persistence
    assert cache_file.exists()
    with open(cache_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["12345"]["is_premium"] is True

    # New instance loads from file
    checker2 = PremiumChecker(cache_path=cache_file, ttl_seconds=10)
    assert checker2.is_user_premium(12345) is True


def test_premium_checker_api_fallback(tmp_path):
    cache_file = tmp_path / "premium_cache.json"
    checker = PremiumChecker(cache_path=cache_file, ttl_seconds=10)

    class MockBotApi:
        def __init__(self, response):
            self.response = response
            self.calls = []

        def call(self, method, payload=None, timeout=25):
            self.calls.append((method, payload))
            return self.response

    # Test detection via emoji_status_custom_emoji_id
    mock_api = MockBotApi({"id": 999, "emoji_status_custom_emoji_id": "5240204600446818776"})
    is_prem = checker.is_user_premium(999, bot_api=mock_api)
    assert is_prem is True
    assert len(mock_api.calls) == 1

    # Second call should use cache, not call API
    is_prem_cached = checker.is_user_premium(999, bot_api=mock_api)
    assert is_prem_cached is True
    assert len(mock_api.calls) == 1  # No additional API call


def test_premium_checker_expiration(tmp_path):
    cache_file = tmp_path / "premium_cache.json"
    checker = PremiumChecker(cache_path=cache_file, ttl_seconds=1)

    checker.update_from_user_obj(888, {"id": 888, "is_premium": True})
    assert checker.get_cached_status(888) is True

    # Manually expire
    checker._memory_cache[888]["expires_at"] = time.time() - 10
    checker._save_cache()

    class MockBotApi:
        def __init__(self):
            self.called = False

        def call(self, method, payload=None, timeout=25):
            self.called = True
            return {"id": 888, "is_premium": False}

    api = MockBotApi()
    res = checker.is_user_premium(888, bot_api=api)
    assert api.called is True
    assert res is False


def test_premium_checker_record_user(tmp_path):
    cache_file = tmp_path / "premium_cache.json"
    checker = PremiumChecker(cache_path=cache_file, ttl_seconds=10)

    assert checker.record_user({"id": 555, "is_premium": True}) is True
    assert checker.is_user_premium(555) is True

    assert checker.record_user(None) is False
    assert checker.record_user({}) is False
    assert checker.record_user("not-a-dict") is False
