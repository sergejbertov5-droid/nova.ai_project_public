"""
Проверка, что приложение действительно запустилось (tools/applications.py).

`open -a` возвращает код 0 в момент приёма запроса, а не в момент
появления окна. Раньше это означало, что «Открываю X» звучало и тогда,
когда Launch Services зависли и процесса не появилось вовсе — ложный
«Готово» ровно по тому сценарию, который запрещён: действие не
состоялось, а NOVA AI докладывал об успехе.

Важно: сообщение при успехе НЕ меняется — его пинают роутинг-тесты.
Меняется только то, что происходит при неудаче.
"""
import unittest
from unittest.mock import patch

from tools.applications import open_application


class _Result:
    """Минимальный stand-in для subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _UncheckableResult:
    """У MagicMock returncode не int — значит, проверить не удалось."""

    returncode = object()


def _fake_run(open_code=0, pgrep_code=1, pgrep_result=None):
    """subprocess.run, различающий `open` и `pgrep` по имени команды."""
    calls = []

    def _run(args, **kwargs):
        calls.append(list(args))
        if args[0] == "pgrep":
            return pgrep_result if pgrep_result is not None else _Result(
                returncode=pgrep_code
            )
        return _Result(returncode=open_code)

    _run.calls = calls
    return _run


class LaunchVerificationTest(unittest.TestCase):
    def test_open_success_with_running_app_keeps_message(self):
        """Процесс найден — сообщение остаётся прежним, как его и пинают."""
        with patch("tools.applications.subprocess.run", side_effect=_fake_run(
            pgrep_code=0
        )):
            answer = open_application("safari")

        self.assertEqual(answer, "Открываю Safari.")

    def test_accepted_request_without_process_is_a_failure(self):
        """`open` сказал «ок», процесса нет — это провал, а не успех."""
        fake = _fake_run(pgrep_code=1)
        with patch("tools.applications.subprocess.run", side_effect=fake), \
                patch("tools.applications.LAUNCH_VERIFY_TIMEOUT", 0.0), \
                patch("tools.applications.time.sleep"):
            with self.assertRaises(RuntimeError) as caught:
                open_application("safari")

        message = str(caught.exception)
        self.assertIn("Не удалось открыть Safari", message)
        self.assertIn("не запустилось", message)
        # pgrep действительно спрашивался, а не пропустил проверку
        self.assertTrue(any(args[0] == "pgrep" for args in fake.calls))

    def test_failed_open_never_reaches_verification(self):
        """Есть честная ошибка `open` — она и остаётся главной причиной."""
        fake = _fake_run(open_code=1, pgrep_code=1)
        with patch("tools.applications.subprocess.run", side_effect=fake):
            with self.assertRaises(RuntimeError) as caught:
                open_application("safari")

        self.assertIn("Не удалось открыть Safari", str(caught.exception))
        self.assertFalse(any(args[0] == "pgrep" for args in fake.calls))

    def test_unverifiable_check_does_not_become_false_failure(self):
        """Не смог проверить ≠ не запустилось: врать в другую сторону тоже нельзя."""
        fake = _fake_run(pgrep_result=_UncheckableResult())
        with patch("tools.applications.subprocess.run", side_effect=fake):
            answer = open_application("safari")

        self.assertEqual(answer, "Открываю Safari.")

    def test_verification_pattern_targets_the_bundle(self):
        """Ищем по бандлу, а не по имени процесса — иначе мимо кассы VS Code."""
        fake = _fake_run(pgrep_code=0)
        with patch("tools.applications.subprocess.run", side_effect=fake):
            open_application("visual studio code")

        pgrep = [args for args in fake.calls if args[0] == "pgrep"]
        self.assertTrue(pgrep)
        self.assertEqual(pgrep[0][-1], "Visual Studio Code.app")


if __name__ == "__main__":
    unittest.main()
