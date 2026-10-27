import json
import os
import tempfile
import unittest
from unittest.mock import patch

from core.commands import CommandRouter
from core.permissions import PermissionPolicy
from core.tool_registry import ToolRegistry, ToolSpec


class Phase1ToolsTest(unittest.TestCase):
    def test_safe_router_commands_use_tools(self):
        router = CommandRouter()
        with patch("tools.applications.subprocess.run"):
            self.assertTrue(router.execute("Который час?").startswith("Сейчас "))
            self.assertTrue(router.execute("Какая сегодня дата?").startswith("Сегодня "))
            self.assertEqual(router.execute("Открой Safari"), "Открываю Safari.")

        self.assertIn("гигабайта", router.execute("Сколько свободного места на диске?"))
        self.assertIn("Батарея:", router.execute("Какая батарея?"))

    def test_unknown_tool_is_blocked(self):
        registry = ToolRegistry(PermissionPolicy())
        registry.register(
            ToolSpec("safe", "safe test", "low", "SYSTEM_READ", False, lambda: "ok")
        )

        self.assertEqual(registry.call("safe"), "ok")
        with self.assertRaises(KeyError):
            registry.call("missing")

    def test_denied_permission_is_blocked(self):
        with self.assertRaises(PermissionError):
            PermissionPolicy().check("EMAIL_SETTINGS")


class ScreenshotGateTest(unittest.TestCase):
    """Голосовой скриншот обязан уважать и флаг, и разрешение.

    Найдено на живом коде: ветка «сделай скриншот» вызывала
    interactions.take_screenshot() напрямую, минуя ToolRegistry,
    PermissionPolicy и флаг JARVIS_SCREEN_CAPTURE. Покупатель, который
    оставил скриншоты выключенными (так и стоит по умолчанию) и запретил
    SCREEN_READ, всё равно получал снимок экрана, стоило произнести
    фразу вслух. Обещание «по умолчанию выключено» было неправдой.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.policy_path = os.path.join(self._dir.name, "permissions.json")

        self.router = CommandRouter()
        self.router.registry.policy = PermissionPolicy(self.policy_path)

        # Считаем попытки снять экран вместо реального screencapture.
        self.shots = []
        patcher = patch(
            "core.interactions.take_screenshot",
            side_effect=lambda: self.shots.append(1) or "снимок сделан",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _deny_screen_read(self):
        with open(self.policy_path, "w", encoding="utf-8") as handle:
            json.dump({"SCREEN_READ": False}, handle)

    def test_denied_permission_stops_voice_screenshot(self):
        self._deny_screen_read()

        for phrase in ("сделай скриншот", "снимок экрана", "фото экрана"):
            with self.subTest(phrase=phrase):
                answer = self.router.execute_local(phrase)

                self.assertEqual(
                    self.shots, [],
                    f"«{phrase}» не должен снимать экран при запрете",
                )
                self.assertNotIn("снимок сделан", str(answer))

    def test_flag_off_gives_honest_refusal(self):
        """Выключенный флаг должен объяснять, а не молчать."""
        with patch("core.user_context.SCREEN_CAPTURE", False):
            answer = self.router.execute_local("сделай скриншот")

        self.assertEqual(
            self.shots, [],
            "при выключенном флаге снимка быть не должно",
        )
        self.assertIn("выключены", str(answer).lower())

    def test_permitted_and_enabled_takes_shot(self):
        """Разрешённое действие по-прежнему выполняется."""
        with patch("core.user_context.SCREEN_CAPTURE", True):
            with patch("core.user_context.os.makedirs"):
                with patch("core.user_context.os.path.exists", return_value=True):
                    with patch("core.user_context.os.path.getsize", return_value=2048):
                        with patch("core.user_context.subprocess.run") as run:
                            run.return_value.returncode = 0
                            run.return_value.stdout = ""
                            run.return_value.stderr = ""
                            answer = self.router.execute_local("сделай скриншот")

        self.assertNotIn("выключены", str(answer).lower())
        self.assertIn("Скриншот сохранён", str(answer))


if __name__ == "__main__":
    unittest.main()
