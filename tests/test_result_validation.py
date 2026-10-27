"""
Тесты проверки результата (core/validators.py).

Отдельный файл, потому что здесь проверяется само правило, а не его
применение в цикле: что считается успехом, а что — провалом шага при
формально «успешном» вызове инструмента.
"""
import unittest

from core.execution_plan import ValidationRecord
from core.results import ToolResult
from core.validators import (
    TOOL_VALIDATORS,
    looks_like_failure,
    require_page_content,
    validate_result,
    validator_for,
)


def _ok(text: str) -> ToolResult:
    return ToolResult(tool="t", ok=True, result=text)


def _fail(text: str) -> ToolResult:
    return ToolResult(tool="t", ok=False, error=text)


class GeneralValidationTest(unittest.TestCase):
    def test_normal_result_passes(self):
        record = validate_result(_ok("Открыл Safari"), expected="приложение запущено")
        self.assertTrue(record.ok)
        self.assertEqual(record.expected, "приложение запущено")
        self.assertIn("Safari", record.actual)

    def test_error_result_fails(self):
        record = validate_result(_fail("соединение оборвалось"))
        self.assertFalse(record.ok)
        self.assertIn("ошибкой", record.reason)

    def test_empty_result_fails(self):
        record = validate_result(_ok(""))
        self.assertFalse(record.ok)
        self.assertIn("не вернул данных", record.reason)

    def test_whitespace_only_result_fails(self):
        self.assertFalse(validate_result(_ok("   \n\t  ")).ok)

    def test_empty_collections_fail(self):
        for value in ("[]", "{}", "null", "None", "0", "нет данных"):
            with self.subTest(value=value):
                self.assertFalse(validate_result(_ok(value)).ok, f"{value} не должно считаться данными")

    def test_failure_phrase_inside_success_fails(self):
        record = validate_result(_ok("Не удалось открыть страницу"))
        self.assertFalse(record.ok)
        self.assertIn("отказе", record.reason)

    def test_honest_negative_answers_pass(self):
        for text in (
            "Новых писем нет",
            "Ничего не найдено по вашему запросу",
            "Результатов нет",
            "Пусто",
        ):
            with self.subTest(text=text):
                self.assertTrue(validate_result(_ok(text)).ok, f"{text!r} — честный ответ, а не сбой")

    def test_failure_word_inside_content_is_not_treated_as_failure(self):
        """Страница может содержать слово «ошибка» как данные."""
        text = "Документация: код ошибка 404 обрабатывается так"
        self.assertTrue(validate_result(_ok(text)).ok)

    def test_actual_is_truncated(self):
        record = validate_result(_ok("x" * 5000))
        self.assertLessEqual(len(record.actual), 400)


class FailureMarkerTest(unittest.TestCase):
    def test_detects_ru_prefixes(self):
        for text in ("не удалось", "Ошибка сети", "сбой соединения", "отказано"):
            with self.subTest(text=text):
                self.assertIsNotNone(looks_like_failure(text))

    def test_detects_en_prefixes(self):
        for text in ("Not found", "Failed to load", "Unable to reach", "Denied"):
            with self.subTest(text=text):
                self.assertIsNotNone(looks_like_failure(text))

    def test_ignores_clean_text(self):
        for text in ("Сейчас 14:32", "Вот три ссылки", "Safari открыт"):
            with self.subTest(text=text):
                self.assertIsNone(looks_like_failure(text))

    def test_detects_browser_action_refusals(self):
        """Отказ инструмента браузера — это отказ, а не результат.

        Такой текст приходит, когда действие не состоялось: элемента
        нет на странице, окна нет вовсе. Раньше он проходил как успех,
        и шаг плана помечался выполненным после несделанного клика.
        """
        for text in (
            "Браузер не запущен",
            "Не нашёл на странице элемент: #submit",
            "Не нашёл на странице поле: #input",
            "Не удалось кликнуть: браузер не запущен (нет открытого окна)",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(looks_like_failure(text), text)

    def test_action_success_texts_are_not_failures(self):
        """Успешное действие не должно ловиться как отказ."""
        for text in ("Кликнул #submit", "Текст введён", "Открываю Safari"):
            with self.subTest(text=text):
                self.assertIsNone(looks_like_failure(text), text)

    def test_empty_text_is_not_a_failure_marker(self):
        self.assertIsNone(looks_like_failure(""))


class ToolValidatorTest(unittest.TestCase):
    def test_specific_validator_can_fail(self):
        def _strict(result):
            return ValidationRecord(ok=False, reason="страница не та")

        record = validate_result(_ok("открыто"), validator=_strict)
        self.assertFalse(record.ok)
        self.assertEqual(record.reason, "страница не та")

    def test_specific_validator_returning_none_defers(self):
        record = validate_result(_ok("открыто"), validator=lambda r: None)
        self.assertTrue(record.ok)

    def test_specific_validator_fills_missing_fields(self):
        def _partial(result):
            return ValidationRecord(ok=False, reason="пусто")

        record = validate_result(_ok("открыто"), expected="ожидание", validator=_partial)
        self.assertEqual(record.expected, "ожидание")
        self.assertIn("открыто", record.actual)

    def test_specific_validator_wins_over_general_pass(self):
        """Общая проверка прошла, инструмент говорит «нет» — решает инструмент."""
        def _strict(result):
            return ValidationRecord(ok=False, reason="не та страница")

        record = validate_result(_ok("открыто"), validator=_strict)
        self.assertFalse(record.ok)

    def test_broken_validator_does_not_break_step(self):
        def _boom(result):
            raise RuntimeError("валидатор сломан")

        record = validate_result(_ok("открыто"), validator=_boom)
        self.assertTrue(record.ok, "падение валидатора не должно рушить шаг")

    def test_empty_output_is_caught_for_every_tool(self):
        """Правило «пусто = провал» общее, а не отдельный валидатор.

        Раньше для этого был свой require_non_empty, но он не был
        подключён ни к одному инструменту, а общая проверка и так
        ловит и пустую строку, и пустую коллекцию.
        """
        for text in ("", "   ", "[]", "{}", "null", "0", "-1"):
            with self.subTest(text=text):
                self.assertFalse(validate_result(_ok(text)).ok, text)


class PolitePrefixTest(unittest.TestCase):
    """Обращение «Сэр,» не должно прятать отказ.

    Инструменты отвечают вежливо, и отказ почти всегда приходит с обращением:
    «Сэр, не удалось прочитать страницу». Раньше такой ответ не начинался ни
    с одного признака отказа и уезжал сэру как «готово».
    """

    def test_failure_behind_salutation_is_detected(self):
        for text in (
            "Сэр, не удалось прочитать страницу: страница недоступна.",
            "Сэр, на странице не оказалось читаемого текста.",
            "NOVA AI: не удалось открыть Safari",
            "Сэр! Не получилось выполнить запрос.",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(looks_like_failure(text), text)
                self.assertFalse(validate_result(_ok(text)).ok, text)

    def test_honest_negatives_survive_salutation(self):
        """Вежливость не должна ломать честный ответ."""
        for text in (
            "Сэр, новых писем нет.",
            "Сэр, ничего не найдено по вашему запросу.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(looks_like_failure(text), text)
                self.assertTrue(validate_result(_ok(text)).ok, text)

    def test_ordinary_content_unaffected(self):
        for text in (
            "Погода в Москве: +5 и дождь, ветер 3 м/с.",
            "Документация: код ошибка 404 обрабатывается так",
            "Счёт 42 оплачен",
            "Открыл Safari",
        ):
            with self.subTest(text=text):
                self.assertIsNone(looks_like_failure(text), text)


class BlockPageTest(unittest.TestCase):
    """Страница-заглушка приходит без ошибки — её надо узнать."""

    def test_captcha_page_is_rejected(self):
        for text in (
            "Just a moment...",
            "Checking your browser before accessing example.com",
            "Verify you are human",
            "Подтвердите, что вы не робот",
            "Включите JavaScript, чтобы продолжить",
        ):
            with self.subTest(text=text):
                record = validate_result(
                    _ok(text), validator=require_page_content
                )
                self.assertFalse(record.ok, text)
                self.assertIn("заглушка", record.reason)

    def test_real_article_mentioning_marker_passes(self):
        """Настоящая страница длинная — маркер в ней данными, а не отказом.

        Иначе проверка запретила бы читать любой текст про CAPTCHA.
        """
        text = (
            "Cloudflare проверяет пользователя по капче. Страница описывает, "
            "как настроить проверку: verify you are human отключается в "
            "настройках сайта, enable javascript должен быть разрешён. "
        ) + "Дополнительный текст статьи. " * 20
        self.assertGreater(len(text), 600, "настоящая статья длиннее порога")
        record = validate_result(_ok(text), validator=require_page_content)
        self.assertTrue(record.ok)

    def test_empty_defers_to_general_check(self):
        """Пустоту ловит общая проверка — точнее и с понятной причиной."""
        self.assertIsNone(require_page_content(_ok("")))
        self.assertFalse(validate_result(_ok(""), validator=require_page_content).ok)

    def test_short_useful_text_passes(self):
        record = validate_result(
            _ok("Время: 14:32"), validator=require_page_content
        )
        self.assertTrue(record.ok)


class ValidatorRegistryTest(unittest.TestCase):
    """Валидаторы подключаются к инструментам, а не лежат мёртвыми."""

    def test_external_readers_have_a_validator(self):
        for name in (
            "browser.read_page",
            "browser.search",
            "browser.kinogo_search",
            "web.fetch",
            "web.search",
            "web.learn",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(validator_for(name), name)

    def test_other_tools_have_none(self):
        """Проверка не должна висеть на всём подряд."""
        for name in ("system.time", "mail.unread", "browser.click"):
            with self.subTest(name=name):
                self.assertIsNone(validator_for(name), name)

    def test_no_validator_for_unknown_tool(self):
        self.assertIsNone(validator_for("нет.такого"))

    def test_registered_names_are_real_tools(self):
        """Имя в таблице валидаторов обязано быть настоящим инструментом.

        Иначе проверка молча не сработает: инструмент переименуют, а
        валидатор останется висеть на старом имени.
        """
        from core.commands import TOOL_SPECS

        declared = {name for name, _d, _h, _p in TOOL_SPECS}
        self.assertEqual(
            set(TOOL_VALIDATORS) - declared,
            set(),
            "в таблице валидаторов есть имена, которых нет среди инструментов",
        )


class ValidationRecordTest(unittest.TestCase):
    def test_to_dict(self):
        record = ValidationRecord(ok=False, expected="e", actual="a", reason="r")
        self.assertEqual(
            record.to_dict(),
            {"ok": False, "expected": "e", "actual": "a", "reason": "r"},
        )


if __name__ == "__main__":
    unittest.main()
