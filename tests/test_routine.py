import unittest

from core.routine import decide_nudge


class FakeTracker:
    """Минимальная имитация UsageTracker для чистой функции decide_nudge."""

    def __init__(self, work_minutes=0.0, streak=None):
        self._work = work_minutes
        self._streak = streak or {"active": False, "minutes": 0.0, "app": "X"}

    def today_work_minutes(self, now):
        return self._work

    def entertainment_streak(self, now):
        return self._streak


class FakeProfile:
    def __init__(self, tools=None):
        self._tools = tools or []

    def work_tools(self):
        return list(self._tools)


class DecideNudgeTest(unittest.TestCase):
    NOW = 1_800_000_000.0
    START = NOW - 60 * 30  # 30 минут назад

    def nudge(self, tracker, last=None, profile=None):
        return decide_nudge(
            tracker,
            now=self.NOW,
            hour=15,
            last_nudges=last or {},
            started_at=self.START,
            profile=profile,
        )

    def test_no_nudge_during_start_delay(self):
        tracker = FakeTracker(work_minutes=600.0)
        result = decide_nudge(
            tracker,
            now=self.NOW,
            hour=15,
            last_nudges={},
            started_at=self.NOW,  # монитор только что запущен
            profile=None,
        )
        self.assertIsNone(result)

    def test_overwork_triggers(self):
        # 9 часов работы при норме 8 — подсказка об отдыхе
        tracker = FakeTracker(work_minutes=9 * 60)
        result = self.nudge(tracker)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "overwork")
        self.assertIn("отдохнуть", result["text"].lower())
        # Завершается вопросом — выбор за пользователем
        self.assertTrue(result["text"].strip().endswith("?"))

    def test_no_overwork_below_threshold(self):
        tracker = FakeTracker(work_minutes=3 * 60)
        self.assertIsNone(self.nudge(tracker))

    def test_entertainment_streak_triggers(self):
        tracker = FakeTracker(
            streak={"active": True, "minutes": 60.0, "app": "YouTube"}
        )
        result = self.nudge(tracker, profile=FakeProfile(["opencode"]))
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "entertainment")
        self.assertIn("развлечениях", result["text"].lower())
        self.assertIn("OpenCode", result["text"])
        self.assertTrue(result["text"].strip().endswith("?"))

    def test_short_entertainment_streak_no_nudge(self):
        tracker = FakeTracker(
            streak={"active": True, "minutes": 5.0, "app": "YouTube"}
        )
        self.assertIsNone(self.nudge(tracker))

    def test_cooldown_suppresses_repeat(self):
        tracker = FakeTracker(work_minutes=9 * 60)
        last = {"overwork": self.NOW - 60 * 5}  # подсказывали 5 минут назад
        self.assertIsNone(self.nudge(tracker, last=last))
        last = {"overwork": self.NOW - 60 * 35}  # прошло 35 минут — можно снова
        result = self.nudge(tracker, last=last)
        self.assertIsNotNone(result)

    def test_no_nudge_when_idle(self):
        tracker = FakeTracker(
            streak={"active": False, "minutes": 0.0, "app": "X"}
        )
        self.assertIsNone(self.nudge(tracker))


if __name__ == "__main__":
    unittest.main()