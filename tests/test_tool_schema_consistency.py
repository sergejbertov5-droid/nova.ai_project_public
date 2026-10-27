"""
Регрессия: схема аргументов должна совпадать с сигнатурой обработчика.

Почему это важно. Модель выбирает инструмент и формирует аргументы по
схеме из `arguments_schema`. Реестр вызывает `handler(**arguments)`.
Если схема и сигнатура разошлись, вызов падает с `TypeError` в момент,
когда сэр ждёт ответа, и выглядит как «инструмент не работает».

До появления этого теста расхождения никто не искал, и одна из них уже
была настоящей: после выноса каталога инструментов на уровень модуля
`web.learn` собирался как `lambda topic, _brain=brain`. Значение по
умолчанию в аргументах делает `_brain` доступным ключевым параметром,
то есть модель могла подменить себе brain одним лишним аргументом.
Тест ловит и это: он требует, чтобы в сигнатуре не осталось ничего,
кроме объявленного в схеме.

Проверяются три класса расхождений:
  * свойство схемы, которого нет в сигнатуре  → TypeError при вызове;
  * параметр сигнатуры без значения по умолчанию, которого нет в схеме
    → вызов модели упадёт, хотя инструмент вроде бы всё умеет;
  * обязательный по схеме аргумент, которого в схеме нет → модель не
    сможет собрать корректный вызов.
"""
import inspect
import unittest

from core.commands import CommandRouter, TOOL_SPECS
from core.permissions import PermissionPolicy
from core.tool_registry import ToolRegistry


def _router_with_live_brain():
    """Роутер с зарегистрированными инструментами, без голоса и демона."""
    router = CommandRouter.__new__(CommandRouter)
    router.brain = "ЖИВОЙ-BRAIN"
    router.confirmations = None
    router.filler_callback = None
    router._pending_danger_action = None
    router._last_reply = ""
    router.registry = ToolRegistry(PermissionPolicy())
    router._register_tools()
    return router


class SchemaMatchesSignatureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = _router_with_live_brain()

    def _handler_params(self, spec):
        sig = inspect.signature(spec.handler)
        accepts_extra = any(
            p.kind == p.VAR_KEYWORD for p in sig.parameters.values()
        )
        named = {
            name: p
            for name, p in sig.parameters.items()
            if name != "self"
            and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        }
        return named, accepts_extra

    def test_schema_properties_are_accepted(self):
        # Свойство, которого нет в сигнатуре, даёт TypeError на вызове.
        for name, *_ in TOOL_SPECS:
            with self.subTest(tool=name):
                spec = self.router.registry.get(name)
                params, accepts_extra = self._handler_params(spec)
                props = set((spec.arguments_schema or {}).get("properties") or {})
                if accepts_extra:
                    continue
                self.assertEqual(
                    props - set(params), set(),
                    f"{name}: модель может прислать эти аргументы, "
                    f"а обработчик их не примет",
                )

    def test_required_arguments_are_always_accepted(self):
        # Обязательный по схеме аргумент должен быть и в сигнатуре, и
        # без значения по умолчанию — иначе вызов не составится.
        for name, *_ in TOOL_SPECS:
            with self.subTest(tool=name):
                spec = self.router.registry.get(name)
                params, accepts_extra = self._handler_params(spec)
                required = set((spec.arguments_schema or {}).get("required") or [])
                if accepts_extra:
                    continue
                for argument in required:
                    self.assertIn(argument, params)
                    self.assertIs(
                        params[argument].default,
                        inspect.Parameter.empty,
                        f"{name}.{argument} помечен обязательным в схеме, "
                        f"но имеет значение по умолчанию — схема врёт",
                    )

    def test_mandatory_params_are_declared_in_schema(self):
        # Параметр без умолчания, которого нет в схеме, модель не сможет
        # передать: инструмент выглядит рабочим, а не вызывается.
        for name, *_ in TOOL_SPECS:
            with self.subTest(tool=name):
                spec = self.router.registry.get(name)
                params, accepts_extra = self._handler_params(spec)
                props = set((spec.arguments_schema or {}).get("properties") or {})
                if accepts_extra:
                    continue
                for argument, param in params.items():
                    if param.default is inspect.Parameter.empty:
                        with self.subTest(argument=argument):
                            self.assertIn(
                                argument, props,
                                f"{name} требует {argument}, но его нет "
                                f"в схеме — модель о нём не узнает",
                            )

    def test_no_internal_leaks_in_signature(self):
        # Регрессия на _brain: значение по умолчанию в аргументах
        # лямбды делает переменную доступным ключевым параметром.
        for name, *_ in TOOL_SPECS:
            with self.subTest(tool=name):
                spec = self.router.registry.get(name)
                params, _ = self._handler_params(spec)
                for argument in params:
                    self.assertFalse(
                        argument.startswith("_"),
                        f"{name}: во внешней сигнатуре остался служебный "
                        f"параметр {argument}",
                    )


class LiveBrainNotOverridableTest(unittest.TestCase):
    """web.learn обязан учиться на том brain, с которым работает роутер."""

    def setUp(self):
        import core.commands as commands

        self.commands = commands
        self.router = _router_with_live_brain()
        self.spec = self.router.registry.get("web.learn")
        self.seen = {}
        self.original = commands.run_learning_session
        commands.run_learning_session = (
            lambda topic, brain=None: self.seen.update(
                {"topic": topic, "brain": brain}
            ) or "выучено"
        )

    def tearDown(self):
        self.commands.run_learning_session = self.original

    def test_uses_live_brain(self):
        self.spec.handler(topic="квантовая физика")
        self.assertEqual(self.seen.get("brain"), "ЖИВОЙ-BRAIN")
        self.assertEqual(self.seen.get("topic"), "квантовая физика")

    def test_cannot_be_overridden_by_extra_argument(self):
        with self.assertRaises(TypeError):
            self.spec.handler("тема", _brain="ЧУЖОЙ")
        self.assertNotEqual(self.seen.get("brain"), "ЧУЖОЙ")

    def test_signature_is_exactly_topic(self):
        params = list(inspect.signature(self.spec.handler).parameters)
        self.assertEqual(params, ["topic"])


if __name__ == "__main__":
    unittest.main()
