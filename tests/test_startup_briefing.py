import unittest
from unittest.mock import patch

from core.briefing import StartupBriefing


class StartupBriefingTest(unittest.TestCase):
    @patch("core.briefing.unread_notifications_summary", return_value=[])
    @patch("core.briefing.unread_mail_summary", return_value=[])
    @patch("core.briefing.unread_telegram_summary", return_value="В Telegram новых входящих сообщений нет.")
    def test_reports_when_sources_are_empty(self, mail, telegram, notifications):
        result = StartupBriefing().build()
        self.assertIn("Новых писем нет", result)
        self.assertIn("Новых уведомлений нет", result)

    @patch("core.briefing.unread_notifications_summary", return_value=None)
    @patch("core.briefing.unread_mail_summary", return_value=None)
    @patch("core.briefing.unread_telegram_summary", return_value="В Telegram новых входящих сообщений нет.")
    def test_reports_available_and_unconnected_sources(self, mail, telegram, notifications):
        result = StartupBriefing().build()
        self.assertIn("Почта пока недоступна", result)
        self.assertIn("Системные уведомления пока недоступны", result)


if __name__ == "__main__":
    unittest.main()
