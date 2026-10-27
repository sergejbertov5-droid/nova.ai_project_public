import os
import tempfile
import unittest
from unittest.mock import patch

from core.user_profile import UserProfile, open_work_tools


class UserProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="jarvis_profile_test_")
        self.path = os.path.join(self.tmpdir, "user_profile.json")

    def make(self, **prefill):
        profile = UserProfile(self.path)
        for key, value in prefill.items():
            profile._data[key] = value
        profile._save()
        return UserProfile(self.path)

    def test_save_and_load_roundtrip(self):
        profile = UserProfile(self.path)
        profile.remember_work_tools(["opencode", "visual studio code"])
        restored = UserProfile(self.path)
        self.assertEqual(restored.work_tools(), ["opencode", "visual studio code"])
        self.assertTrue(restored.get("asked_about_tools"))

    def test_remember_work_tools_dedupes(self):
        profile = self.make(work_tools=["opencode"])
        reply = profile.remember_work_tools(["opencode", "visual studio code"])
        self.assertIn("Запомнил", reply)
        self.assertIn("OpenCode", reply)
        self.assertEqual(profile.work_tools(), ["opencode", "visual studio code"])

    def test_remember_work_tools_empty_asks_for_clarification(self):
        profile = UserProfile(self.path)
        reply = profile.remember_work_tools([])
        self.assertIn("уточните", reply.lower())
        self.assertEqual(profile.work_tools(), [])

    def test_startup_suggestion_when_tools_known(self):
        profile = self.make(work_tools=["opencode", "visual studio code"])
        suggestion = profile.startup_suggestion()
        self.assertIsNotNone(suggestion)
        self.assertIn("OpenCode", suggestion)
        self.assertIn("открыть", suggestion.lower())

    def test_startup_suggestion_asks_once_when_unknown(self):
        profile = UserProfile(self.path)
        first = profile.startup_suggestion()
        self.assertIsNotNone(first)
        self.assertIn("подскажите", first.lower())
        # второй вызов — уже не спрашиваем (не надоедаем)
        self.assertIsNone(profile.startup_suggestion())

    def test_add_interest(self):
        profile = UserProfile(self.path)
        reply = profile.add_interest("мне интересна автоматизация дома")
        self.assertIn("Запомнил", reply)
        self.assertIn("автоматизация", profile.get("interests", [])[0])

    def test_summary_text_empty(self):
        profile = UserProfile(self.path)
        self.assertIn("профиль пока пуст", profile.summary_text())

    def test_open_work_tools_opens_allowlisted(self):
        """Разрешённая программа открыта через `open -a` с её именем.

        Путь пошёл через общий обработчик applications.open (он после
        кода 0 ждёт появления процесса), поэтому мокаются уже его
        subprocess и ожидание процесса; проверки argv сохранены.
        """
        profile = self.make(work_tools=["opencode"])
        with patch("tools.applications.subprocess.run") as mock_run, \
                patch("tools.applications._await_running", return_value=True):
            reply = open_work_tools(profile=profile)
        self.assertIn("Открываю", reply)
        self.assertIn("OpenCode", reply)
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(args[:2], ["open", "-a"])
        self.assertEqual(args[2], "OpenCode")

    def test_open_work_tools_reports_unlaunched_app(self):
        """Система приняла запрос, но приложение не поднялось — не врать.

        Exit 0 от `open` не доказывает запуска: раньше такой случай
        всё равно попадал в «Открываю, сэр» — тот же ложный успех,
        что чинили в applications.open.
        """
        profile = self.make(work_tools=["opencode"])
        with patch("tools.applications.subprocess.run"), \
                patch("tools.applications._await_running", return_value=False):
            reply = open_work_tools(profile=profile)
        self.assertIn("не удалось открыть", reply.lower())
        self.assertNotIn("Открываю", reply)

    def test_open_work_tools_unknown_tool_skipped(self):
        profile = self.make(work_tools=["совсем-незнакомая-программа"])
        reply = open_work_tools(profile=profile)
        self.assertIn("не удалось открыть", reply)


if __name__ == "__main__":
    unittest.main()