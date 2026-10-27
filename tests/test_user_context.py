import os
import tempfile
import unittest
from unittest.mock import patch

from core.user_context import (
    CATEGORY_BACKGROUND,
    CATEGORY_COMMUNICATION,
    CATEGORY_ENTERTAINMENT,
    CATEGORY_SYSTEM,
    CATEGORY_WORK,
    CATEGORY_OTHER,
    UsageTracker,
    classify_app,
    classify_url,
)


class ClassifyAppTest(unittest.TestCase):
    def test_work_apps(self):
        for name in [
            "Visual Studio Code", "OpenCode", "Terminal", "PyCharm",
            "Xcode", "Notion", "Figma", "Microsoft Word",
        ]:
            self.assertEqual(classify_app(name), CATEGORY_WORK, name)

    def test_entertainment_apps(self):
        for name in ["YouTube", "Kinogo", "Netflix", "Steam", "Twitch"]:
            self.assertEqual(classify_app(name), CATEGORY_ENTERTAINMENT, name)

    def test_background_music_is_not_entertainment(self):
        for name in ["Яндекс Музыка", "Spotify", "Apple Music"]:
            self.assertEqual(classify_app(name), CATEGORY_BACKGROUND, name)

    def test_communication_apps(self):
        for name in ["Telegram", "Mail", "Slack", "Zoom", "Discord"]:
            self.assertEqual(classify_app(name), CATEGORY_COMMUNICATION, name)

    def test_system_apps(self):
        for name in ["Finder", "System Settings"]:
            self.assertEqual(classify_app(name), CATEGORY_SYSTEM, name)

    def test_unknown_is_other(self):
        self.assertEqual(classify_app("Google Chrome"), CATEGORY_OTHER)
        self.assertEqual(classify_app(""), CATEGORY_OTHER)
        self.assertEqual(classify_app(None), CATEGORY_OTHER)


class ClassifyUrlTest(unittest.TestCase):
    def test_entertainment_urls(self):
        for url in [
            "https://www.youtube.com/watch?v=abc",
            "https://youtu.be/abc",
            "https://kinogo.ec/film/1",
            "https://www.netflix.com/title/123",
            "https://www.twitch.tv/streamer",
        ]:
            self.assertEqual(classify_url(url), CATEGORY_ENTERTAINMENT, url)

    def test_work_urls(self):
        for url in [
            "https://github.com/user/repo",
            "https://stackoverflow.com/questions/1",
            "https://docs.google.com/document/d/1",
            "https://chat.openai.com/",
        ]:
            self.assertEqual(classify_url(url), CATEGORY_WORK, url)

    def test_other_urls(self):
        self.assertEqual(classify_url("https://example.com"), CATEGORY_OTHER)
        self.assertEqual(classify_url(""), CATEGORY_OTHER)


class UsageTrackerTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="jarvis_usage_test_")
        self.path = os.path.join(self.tmpdir, "usage_log.json")

    def test_accumulates_work_minutes(self):
        tracker = UsageTracker(self.path)
        base = 1_700_000_000.0
        tracker.observe("Visual Studio Code", CATEGORY_WORK, base)
        tracker.observe("Visual Studio Code", CATEGORY_WORK, base + 3600)
        self.assertAlmostEqual(tracker.today_work_minutes(base + 3600), 60.0, places=3)

    def test_category_change_accumulates_and_streak_resets(self):
        tracker = UsageTracker(self.path)
        base = 1_700_000_000.0
        tracker.observe("YouTube", CATEGORY_ENTERTAINMENT, base)
        streak = tracker.entertainment_streak(base + 3000)
        self.assertTrue(streak["active"])
        self.assertAlmostEqual(streak["minutes"], 50.0, places=3)
        # Переключились на работу — развлечения закончились
        tracker.observe("Visual Studio Code", CATEGORY_WORK, base + 3000)
        streak = tracker.entertainment_streak(base + 3000)
        self.assertFalse(streak["active"])
        self.assertAlmostEqual(tracker.today_entertainment_minutes(base + 3000), 50.0, places=3)
        self.assertAlmostEqual(tracker.today_work_minutes(base + 3600), 10.0, places=3)

    def test_persistence_survives_reload(self):
        tracker = UsageTracker(self.path)
        base = 1_700_000_000.0
        tracker.observe("Visual Studio Code", CATEGORY_WORK, base)
        tracker.observe("Visual Studio Code", CATEGORY_WORK, base + 3600)
        restored = UsageTracker(self.path)
        self.assertAlmostEqual(restored.today_work_minutes(base + 3600), 60.0, places=3)

    def test_day_change_resets(self):
        # Смена календарного дня: статистика вчерашнего дня не «перетекает»
        # в новый (всё локально, безопасно — приоритет текущему дню).
        tracker = UsageTracker(self.path)
        tracker.observe("Visual Studio Code", CATEGORY_WORK, 1000.0)
        tracker.observe("Visual Studio Code", CATEGORY_WORK, 2000.0)
        with patch("core.user_context._today", return_value="2099-12-31"):
            restored = UsageTracker(self.path)  # другой день → данные сброшены
        self.assertAlmostEqual(restored.today_work_minutes(2000.0), 0.0, places=3)


if __name__ == "__main__":
    unittest.main()