"""
test_browser_permissions.py — браузерные действия делятся на READ и ACTION.

Смысл разделения: чтение страницы не меняет мир, а клик и ввод текста
меняют его от имени сэра — отправляют форму, пишут сообщение, оплачивают
покупку. Поэтому клик и ввод обязаны спрашивать подтверждения, а чтение
не должно.

Тесты идут через настоящий CommandRouter: команда → PermissionPolicy →
подтверждение → pending → ответ «да» → вызов обработчика. Настоящий Chrome
не открывается: подменяется только _run_applescript, то есть сама граница
с браузером.
"""
import json
import logging
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import tools.browser as browser_module

from core.commands import CommandRouter, resolve_danger_reply
from core.permissions import (
    PERMISSION_META,
    ConfirmationRequired,
    PermissionPolicy,
)


class BrowserPermissionBase(unittest.TestCase):
    """Роутер с отдельным файлом политики и безопасной заглушкой браузера."""

    def setUp(self):
        # Свой файл политики: тесты не трогают data/permissions.json.
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.policy_path = os.path.join(self._dir.name, "permissions.json")

        self.router = CommandRouter()
        self.router.registry.policy = PermissionPolicy(self.policy_path)

        # Считаем реальные вызовы браузера вместо того, чтобы дёргать Chrome.
        self.browser_calls = []

        def fake_applescript(script):
            self.browser_calls.append(script)
            return ("", "")

        patcher = mock.patch.object(
            browser_module, "_run_applescript", fake_applescript
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.addCleanup(self.router.confirmations.take)

    def click_commands(self, selector="#buy"):
        return f"нажми {selector}"


class ReadIsReadTest(BrowserPermissionBase):
    """READ-инструменты работают как раньше — без вопроса."""

    def test_search_asks_nothing(self):
        answer = self.router.execute_local("найди котиков")

        self.assertNotIn("Подтверждаете", str(answer))
        self.assertIsNone(self.router.confirmations.pending)

    def test_read_page_asks_nothing(self):
        answer = self.router.execute_local("прочитай страницу")

        self.assertNotIn("Подтверждаете", str(answer))
        self.assertIsNone(self.router.confirmations.pending)

    def test_open_url_asks_nothing(self):
        # open_url достигается через AI-решение, локального триггера нет.
        answer = self.router.execute_decision({
            "type": "tool_call",
            "tool": "browser.open_url",
            "arguments": {"url": "https://example.com"},
        })

        self.assertNotIn("Подтверждаете", str(answer))
        self.assertIsNone(self.router.confirmations.pending)

    def test_read_tools_are_risk_low_without_confirmation(self):
        for name in ("browser.search", "browser.open_url", "browser.read_page"):
            spec = self.router.registry.get(name)
            self.assertEqual(spec.risk_level, "low", name)
            self.assertFalse(spec.confirmation_required, name)
            self.assertEqual(spec.required_permission, "BROWSER_READ", name)


class ActionNeedsConfirmationTest(BrowserPermissionBase):
    """Клик и ввод текста не выполняются, пока сэр не сказал «да»."""

    def test_click_handler_not_called_before_confirmation(self):
        answer = self.router.execute_local(self.click_commands())

        self.assertIn("Подтверждаете", str(answer))
        self.assertEqual(
            self.browser_calls, [], "клик не должен был дойти до браузера"
        )
        self.assertIsNotNone(self.router.confirmations.pending)

    def test_type_handler_not_called_before_confirmation(self):
        pending = self.router.confirmations
        pending.request(
            "browser.type", {"selector": "#pass", "text": "тихий"}, "x"
        )
        # Напрямую через реестр: локального триггера у ввода текста нет.
        from core.permissions import ConfirmationRequired as CR

        with self.assertRaises(CR):
            self.router.registry.call(
                "browser.type", selector="#pass", text="тихий"
            )

        self.assertEqual(self.browser_calls, [])

    def test_click_executes_exactly_once_after_yes(self):
        self.router.execute_local(self.click_commands())
        self.assertEqual(self.browser_calls, [])

        answer = self.router.execute_local("да")

        self.assertEqual(
            len(self.browser_calls), 1, "клик должен выполниться ровно один раз"
        )
        self.assertIn("#buy", self.browser_calls[0])
        self.assertNotIn("Подтверждаете", str(answer))

    def test_click_not_executed_after_cancel(self):
        self.router.execute_local(self.click_commands())

        answer = self.router.execute_local("отмена")

        self.assertEqual(self.browser_calls, [], "после отмены клика быть не может")
        self.assertIsNone(self.router.confirmations.pending)
        self.assertNotIn("Кликнул", str(answer))

    def test_type_executes_once_with_original_selector_and_text(self):
        # Текст латиницей: кириллица в AppleScript уходит doubly-экранированной
        # (\\uXXXX), и проверять её в сыром скрипте нечитаемо. Суть теста —
        # до браузера доходят именно сохранённые selector и text.
        text = "hello-from-sir"
        self.router.confirmations.request(
            "browser.type",
            {"selector": "#message", "text": text},
            "ввод текста",
        )

        self.router.execute_local("да")

        self.assertEqual(len(self.browser_calls), 1)
        script = self.browser_calls[0]
        self.assertIn("#message", script)
        self.assertIn(text, script)

    def test_tool_spec_marks_action_tools_high_risk(self):
        for name in ("browser.click", "browser.type"):
            spec = self.router.registry.get(name)
            self.assertEqual(spec.risk_level, "high", name)
            self.assertTrue(spec.confirmation_required, name)
            self.assertEqual(spec.required_permission, "BROWSER_CONTROL", name)


class ConfirmationIsBoundToActionTest(BrowserPermissionBase):
    """Подтверждение исполняет сохранённое действие, а не новое."""

    def test_confirmation_cannot_substitute_arguments(self):
        self.router.execute_local(self.click_commands("#original"))
        saved = self.router.confirmations.pending
        self.assertEqual(saved.tool, "browser.click")
        self.assertEqual(saved.arguments["selector"], "#original")

        # Сэр смешивает подтверждение с новым требованием. Ответ принимается
        # только точным словом, поэтому смешанная фраза ничего не выполняет
        # и не подменяет ожидающее действие.
        self.router.execute_local("да, и нажми #other")

        self.assertEqual(
            self.browser_calls, [], "смешанная фраза не должна ничего выполнять"
        )
        self.assertEqual(
            self.router.confirmations.pending.arguments["selector"],
            "#original",
            "ожидающее действие не должно подмениться",
        )

        # А вот чистое «да» выполняет именно сохранённое действие.
        self.router.execute_local("да")

        self.assertEqual(len(self.browser_calls), 1)
        self.assertIn("#original", self.browser_calls[0])
        self.assertNotIn("#other", self.browser_calls[0])

    def test_repeated_yes_does_not_execute_twice(self):
        self.router.execute_local(self.click_commands())
        self.router.execute_local("да")
        self.assertEqual(len(self.browser_calls), 1)

        self.router.execute_local("да")

        self.assertEqual(
            len(self.browser_calls), 1, "повторное «да» не должно кликать снова"
        )

    def test_pending_cleared_after_yes(self):
        self.router.execute_local(self.click_commands())
        self.assertIsNotNone(self.router.confirmations.pending)

        self.router.execute_local("да")

        self.assertIsNone(self.router.confirmations.pending)

    def test_pending_cleared_after_cancel(self):
        self.router.execute_local(self.click_commands())
        self.assertIsNotNone(self.router.confirmations.pending)

        self.router.execute_local("отмена")

        self.assertIsNone(self.router.confirmations.pending)

    def test_unrelated_command_does_not_execute_pending_action(self):
        self.router.execute_local(self.click_commands())

        answer = self.router.execute_local("который час")

        self.assertEqual(
            self.browser_calls, [], "ожидающее действие не должно выполниться"
        )
        self.assertIn("подтвердите", str(answer).lower())


class DenyBeatsConfirmationTest(BrowserPermissionBase):
    """Запрет политики нельзя обойти подтверждением."""

    def _deny(self):
        self.router.registry.policy.set_permission(
            "BROWSER_CONTROL", allowed=False
        )

    def test_click_blocked_when_denied(self):
        self._deny()

        with self.assertRaises(PermissionError):
            self.router.execute_local(self.click_commands())

        self.assertEqual(self.browser_calls, [])
        self.assertIsNone(
            self.router.confirmations.pending,
            "запрещённое действие не должно попадать в очередь на подтверждение",
        )

    def test_type_blocked_when_denied(self):
        self._deny()

        with self.assertRaises(PermissionError):
            self.router.registry.call(
                "browser.type", selector="#x", text="y"
            )

        self.assertEqual(self.browser_calls, [])

    def test_confirmation_cannot_override_deny(self):
        # Ставим действие в очередь, затем запрещаем категорию.
        self.router.confirmations.request(
            "browser.click", {"selector": "#x"}, "клик"
        )
        self._deny()

        answer = self.router.execute_local("да")

        self.assertEqual(self.browser_calls, [], "запрет должен победить")
        self.assertIn("Не выполнил", str(answer))


class SensitiveTextTest(BrowserPermissionBase):
    """Вводимый текст не должен попадать в вопрос и в логи."""

    SECRET = "моя-карта-4111-1111-1111-1111"

    def test_secret_not_in_confirmation_prompt(self):
        self.router.confirmations.request(
            "browser.type",
            {"selector": "#card", "text": self.SECRET},
            "ввод текста",
        )
        pending = self.router.confirmations.pending

        # Вопрос строит роутер заново из сохранённых аргументов.
        prompt = self.router._ask_browser_confirmation(
            pending.tool, pending.arguments
        )

        self.assertNotIn(self.SECRET, prompt)
        self.assertIn("#card", prompt)

    def test_secret_not_written_to_log(self):
        with self.assertLogs(level="INFO") as captured:
            self.router.confirmations.request(
                "browser.type",
                {"selector": "#card", "text": self.SECRET},
                "ввод текста",
            )
            pending = self.router.confirmations.pending
            self.router._ask_browser_confirmation(
                pending.tool, pending.arguments
            )

        joined = "\n".join(captured.output)
        self.assertNotIn(self.SECRET, joined)
        self.assertIn("browser.type", joined)


class ExistingDangerFlowTest(BrowserPermissionBase):
    """Прежний вопрос для выключения/перезагрузки не должен сломаться."""

    def test_shutdown_still_asks(self):
        answer = self.router.execute_local("выключи компьютер")

        self.assertIn("Подтверждаете", str(answer))
        self.assertEqual(self.router._pending_danger_action, "shutdown")

    def test_shutdown_confirm_executes_once(self):
        fake_run = mock.MagicMock(
            returncode=0, stdout="", stderr=""
        )
        with mock.patch.object(subprocess, "run", fake_run):
            self.router.execute_local("выключи компьютер")
            self.router.execute_local("да")

        self.assertEqual(fake_run.call_count, 1)
        self.assertIsNone(self.router._pending_danger_action)

    def test_shutdown_cancel_executes_nothing(self):
        fake_run = mock.MagicMock(returncode=0, stdout="", stderr="")
        with mock.patch.object(subprocess, "run", fake_run):
            self.router.execute_local("выключи компьютер")
            self.router.execute_local("не надо")

        self.assertEqual(fake_run.call_count, 0)
        self.assertIsNone(self.router._pending_danger_action)

    def test_every_promised_cancel_word_still_cancels(self):
        """Слова, которые NOVA AI обещает в вопросе, и прежние.

        В вопросе сказано «Скажите «да» или «отмена»», поэтому «отмена»
        обязана работать. Соседние «отмени»/«отменить»/«не надо» — из
        прежнего списка, их регрессия тоже недопустима.
        """
        for cancel_word in ("отмена", "отмени", "отменить", "не надо"):
            with self.subTest(cancel_word=cancel_word):
                self.router._pending_danger_action = "shutdown"
                fake_run = mock.MagicMock(
                    returncode=0, stdout="", stderr=""
                )

                with mock.patch.object(subprocess, "run", fake_run):
                    answer = self.router.execute_local(cancel_word)

                self.assertEqual(
                    fake_run.call_count,
                    0,
                    f"после «{cancel_word}» ничего выполняться не должно",
                )
                self.assertIsNone(
                    self.router._pending_danger_action,
                    f"«{cancel_word}» должна очищать ожидающее действие",
                )
                self.assertNotIn("Подтверждаете", str(answer))

    def test_otmena_cancels_restart_too(self):
        """То же слово отменяет перезагрузку, не только выключение."""
        self.assertIsNotNone(
            self.router.execute_local("перезагрузи компьютер")
        )
        self.assertEqual(self.router._pending_danger_action, "restart")

        fake_run = mock.MagicMock(returncode=0, stdout="", stderr="")
        with mock.patch.object(subprocess, "run", fake_run):
            self.router.execute_local("отмена")

        self.assertEqual(fake_run.call_count, 0)
        self.assertIsNone(self.router._pending_danger_action)


class DangerRefusalTest(BrowserPermissionBase):
    """Отказ не должен превращаться в согласие.

    Найдено на живом коде: подтверждение выключения проверялось поиском
    подстроки, а «да» живёт внутри «никогда», а «выполняй» — внутри
    «не выполняй». Обычный отказ выключал компьютер — необратимое
    действие, достижимое одним словом.
    """

    def test_refusal_phrases_never_confirm_shutdown(self):
        for phrase in ("никогда", "не выполняй", "не подтверждаю",
                       "не надо", "нет", "отмена", "стоп", "погоди"):
            with self.subTest(phrase=phrase):
                self.router._pending_danger_action = "shutdown"
                fake_run = mock.MagicMock(returncode=0, stdout="", stderr="")

                with mock.patch.object(subprocess, "run", fake_run):
                    self.router.execute_local(phrase)

                self.assertEqual(
                    fake_run.call_count, 0,
                    f"«{phrase}» не может привести к выключению",
                )
                self.assertIsNone(self.router._pending_danger_action)

    def test_refusal_phrases_never_confirm_restart(self):
        for phrase in ("никогда", "не выполняй", "не подтверждаю"):
            with self.subTest(phrase=phrase):
                self.router._pending_danger_action = "restart"
                fake_run = mock.MagicMock(returncode=0, stdout="", stderr="")

                with mock.patch.object(subprocess, "run", fake_run):
                    self.router.execute_local(phrase)

                self.assertEqual(fake_run.call_count, 0)

    def test_unclear_answer_disarms_pending_action(self):
        """Незафиксированный ответ обязан снять действие с боевого взвода.

        Иначе оно «висит» и следующее «да» — уже про другую фразу —
        выключит компьютер.
        """
        self.router._pending_danger_action = "shutdown"
        fake_run = mock.MagicMock(returncode=0, stdout="", stderr="")

        with mock.patch.object(subprocess, "run", fake_run):
            answer = self.router.execute_local("ну я не знаю")

        self.assertEqual(fake_run.call_count, 0)
        self.assertIsNone(
            self.router._pending_danger_action,
            "непонятный ответ обязан разоружать действие",
        )
        self.assertIn("ничего не делаю", str(answer).lower())

    def test_agreement_still_works(self):
        """Правка не должна сломать обычное подтверждение."""
        for phrase in ("да", "да, подтверждаю", "подтверждаю",
                       "выполняй", "давай", "точно", "ок", "ok"):
            with self.subTest(phrase=phrase):
                self.router._pending_danger_action = "shutdown"
                fake_run = mock.MagicMock(returncode=0, stdout="", stderr="")

                with mock.patch.object(subprocess, "run", fake_run):
                    self.router.execute_local(phrase)

                self.assertEqual(
                    fake_run.call_count, 1,
                    f"«{phrase}» обязано подтверждать выключение",
                )


class DangerActionPermissionTest(BrowserPermissionBase):
    """Выключение обязано подчиняться выключателю в настройках.

    Раньше этот путь вызывал osascript напрямую: голосовое подтверждение
    спрашивалось, а разрешение — нет. «Управление компьютером» обещало
    контролировать состояние машины, но выключение компьютера из-под
    этого обещания уходило.
    """

    def _turn_system_control_off(self):
        # Штатный API: Permission — неизменяемый dataclass, и лезть в
        # внутренний словарь здесь нельзя.
        self.router.registry.policy.set_permission("SYSTEM_CONTROL", allowed=False)

    def test_permission_off_refuses_even_after_yes(self):
        """Прозвучавшее «да» не отменяет запрет из настроек."""
        self._turn_system_control_off()
        self.router.execute_local("выключи компьютер")
        fake_run = mock.MagicMock(returncode=0, stdout="", stderr="")

        with mock.patch.object(subprocess, "run", fake_run):
            answer = self.router.execute_local("да")

        self.assertEqual(fake_run.call_count, 0, "osascript запускать нельзя")
        self.assertIn("Управление компьютером", str(answer))

    def test_permission_off_refuses_restart_too(self):
        self._turn_system_control_off()
        self.router.execute_local("перезагрузи компьютер")
        fake_run = mock.MagicMock(returncode=0, stdout="", stderr="")

        with mock.patch.object(subprocess, "run", fake_run):
            answer = self.router.execute_local("да")

        self.assertEqual(fake_run.call_count, 0)
        self.assertIn("отменена", str(answer))

    def test_permission_on_still_executes(self):
        """Обычное поведение не сломано: по умолчанию разрешено."""
        fake_run = mock.MagicMock(returncode=0, stdout="", stderr="")
        self.router.execute_local("выключи компьютер")

        with mock.patch.object(subprocess, "run", fake_run):
            self.router.execute_local("да")

        self.assertEqual(fake_run.call_count, 1)

    def test_no_second_confirmation_question(self):
        """Голос уже подтвердил — второй раз спрашивать нельзя."""
        fake_run = mock.MagicMock(returncode=0, stdout="", stderr="")
        self.router.execute_local("выключи компьютер")

        with mock.patch.object(subprocess, "run", fake_run):
            answer = self.router.execute_local("да")

        self.assertNotIn("Подтверждаете", str(answer))
        self.assertEqual(fake_run.call_count, 1)

    def test_description_mentions_shutdown(self):
        """Описание в настройках обязано совпадать с тем, что он делает."""
        name, description = PERMISSION_META["SYSTEM_CONTROL"]
        self.assertIn("выключить", description.casefold())
        self.assertIn("перезагрузить", description.casefold())


class ResolveDangerReplyTest(unittest.TestCase):
    """Сама функция разбора ответа — без роутера и без macOS."""

    def test_clear_agreement(self):
        for phrase in ("да", "Да.", "да, подтверждаю", "ДА", "да да"):
            with self.subTest(phrase=phrase):
                self.assertEqual(resolve_danger_reply(phrase), "yes")

    def test_clear_refusal(self):
        for phrase in ("нет", "отмена", "не надо", "Не подтверждаю",
                       "не выполняй", "стоп", "погоди"):
            with self.subTest(phrase=phrase):
                self.assertEqual(resolve_danger_reply(phrase), "no")

    def test_never_is_not_agreement(self):
        """Самая опасная фраза: содержит «да», но согласия не значит."""
        self.assertNotEqual(resolve_danger_reply("никогда"), "yes")

    def test_punctuation_and_case_do_not_matter(self):
        self.assertEqual(resolve_danger_reply("  ДА!  "), "yes")
        self.assertEqual(resolve_danger_reply("Нет."), "no")

    def test_empty_and_noise_are_unknown(self):
        for phrase in ("", "   ", "...", "?!", "хмм"):
            with self.subTest(phrase=phrase):
                self.assertEqual(resolve_danger_reply(phrase), "unknown")

    def test_refusal_wins_over_agreement(self):
        """Смешанная фраза — отказ, а не согласие."""
        self.assertEqual(resolve_danger_reply("да нет"), "no")
        self.assertEqual(resolve_danger_reply("да, не надо"), "no")


class PolicySplitTest(unittest.TestCase):
    """Проверка самой политики, без роутера."""

    def test_check_raises_confirmation_required_not_plain_deny(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = PermissionPolicy(
                os.path.join(directory, "permissions.json")
            )

            with self.assertRaises(ConfirmationRequired):
                policy.check("BROWSER_CONTROL")

            # Подтверждённое действие проходит.
            resolved = policy.check("BROWSER_CONTROL", confirmed=True)
            self.assertEqual(resolved.name, "BROWSER_CONTROL")

    def test_deny_is_plain_permission_error_and_not_confirmable(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = PermissionPolicy(
                os.path.join(directory, "permissions.json")
            )
            policy.set_permission("BROWSER_CONTROL", allowed=False)

            with self.assertRaises(PermissionError) as caught:
                policy.check("BROWSER_CONTROL", confirmed=True)

            self.assertNotIsInstance(caught.exception, ConfirmationRequired)

    def test_read_category_needs_no_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = PermissionPolicy(
                os.path.join(directory, "permissions.json")
            )

            resolved = policy.check("BROWSER_READ")

            self.assertEqual(resolved.risk_level, "low")
            self.assertFalse(resolved.confirmation_required)


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.CRITICAL)
    unittest.main()
