"""
test_system_control_permissions.py — управление компьютером через реестр.

Найденный дефект
----------------
`_system_control_command()` вызывал `core.interactions` НАПРЯМУЮ:

    if any(p in lowered for p in ("заблокируй экран", ...)):
        return interactions.lock_screen()
    if any(p in lowered for p in ("усыпи компьютер", ...)):
        return interactions.system_sleep()

То есть мимо реестра, а значит мимо PermissionPolicy и мимо подтверждения.
Скриншот к тому моменту уже был переведён на реестр по ровно той же
причине (это описано в комментарии в коде), а остальные действия — нет.

Почему это опасно, а не просто неаккуратно. До `_system_control_command`
ведёт единственный путь, и он общий для сэра и для модели:

    execute_ai() → execute_decision() → _system_control_command()

То есть ключевые слова разбираются прямо в том тексте, который выдала
языковая модель. А модель читает веб-страницы. Содержимое страницы может
содержать инструкцию («заблокируй экран», «усьпи компьютер»), и тогда
компьютер засыпал или блокировался посреди разговора — без вопроса,
без разрешения и без записи в журнал. Отменить голосом уже нельзя:
микрофон не работает, пока машина спит.

Теперь всё управление системой идёт через реестр:
  * звук и музыка  → SYSTEM_AUDIO      (обратимо голосом, «да» не нужно)
  * блокировка, сон, гашение экрана, режим фокуса → SYSTEM_CONTROL
    (высокий риск, обязательное «да»)
"""
import unittest
from unittest.mock import patch

from core import interactions
from core.commands import TOOL_SPECS, CommandRouter
from core.permissions import PERMISSION_META, PermissionPolicy
from core.tool_registry import ToolRegistry


def _permission_for(tool_name):
    for name, _label, _handler, permission in TOOL_SPECS:
        if name == tool_name:
            return permission
    raise AssertionError(f"инструмент {tool_name} не найден в TOOL_SPECS")


#: Что должно спрашивать подтверждения, а что нет.
NEEDS_CONFIRMATION = {
    "system.lock_screen": True,
    "system.display_sleep": True,
    "system.sleep": True,
    "system.focus_mode": True,
    "system.volume": False,
    "system.mute": False,
    "system.music": False,
}


class SystemToolRegistryTest(unittest.TestCase):
    """Каждое системное действие — зарегистрированный инструмент."""

    def test_all_system_tools_are_registered(self):
        names = {name for name, _d, _h, _p in TOOL_SPECS}
        for tool in NEEDS_CONFIRMATION:
            with self.subTest(tool=tool):
                self.assertIn(
                    tool, names,
                    f"{tool} не в реестре — значит действие идёт в обход "
                    f"разрешений",
                )

    def test_confirmation_matches_risk(self):
        for tool, needs in NEEDS_CONFIRMATION.items():
            with self.subTest(tool=tool):
                self.assertEqual(_permission_for(tool) == "SYSTEM_CONTROL", needs)

    def test_machine_state_actions_are_high_risk_with_confirmation(self):
        # Эти четыре меняют состояние машины так, что отменить голосом
        # уже нельзя: микрофон не работает, пока спит или заблокирован.
        for tool in ("system.lock_screen", "system.display_sleep",
                     "system.sleep", "system.focus_mode"):
            with self.subTest(tool=tool):
                permission = _permission_for(tool)
                self.assertEqual(permission, "SYSTEM_CONTROL")
                self.assertEqual(
                    PermissionPolicy().describe(permission).confirmation_required,
                    True,
                    f"{tool} не требует подтверждения",
                )
                self.assertEqual(
                    PermissionPolicy().describe(permission).risk_level, "high"
                )

    def test_audio_does_not_require_confirmation(self):
        # Громкость и музыка обратимы голосом и ничего не ломают.
        # Требовать «да» на «сделай громче» — значит научить сэра
        # не отвечать на вопросы вслепую.
        for tool in ("system.volume", "system.mute", "system.music"):
            with self.subTest(tool=tool):
                permission = _permission_for(tool)
                self.assertEqual(permission, "SYSTEM_AUDIO")
                self.assertFalse(
                    PermissionPolicy().describe(permission).confirmation_required
                )

    def test_permissions_are_declared_for_the_settings_panel(self):
        for name in ("SYSTEM_AUDIO", "SYSTEM_CONTROL"):
            with self.subTest(permission=name):
                self.assertIn(name, PERMISSION_META)

    def test_audio_and_control_are_separate_switches(self):
        # Отзыв звука не должен отключать блокировку и наоборот.
        policy = PermissionPolicy()
        policy.set_permission("SYSTEM_AUDIO", allowed=False)
        self.assertTrue(policy.perms()["SYSTEM_CONTROL"].allowed)

        policy = PermissionPolicy()
        policy.set_permission("SYSTEM_CONTROL", allowed=False)
        self.assertTrue(policy.perms()["SYSTEM_AUDIO"].allowed)

    def test_revoking_control_blocks_the_dangerous_tools(self):
        router = CommandRouter()
        router.registry.policy.set_permission("SYSTEM_CONTROL", allowed=False)
        for tool in ("system.lock_screen", "system.sleep",
                     "system.display_sleep", "system.focus_mode"):
            with self.subTest(tool=tool), patch.object(
                interactions, "lock_screen", return_value="ok"
            ), patch.object(interactions, "system_sleep", return_value="ok"), \
                 patch.object(interactions, "display_sleep", return_value="ok"), \
                 patch.object(interactions, "focus_mode", return_value="ok"):
                with self.assertRaises(PermissionError):
                    router.registry.call(tool)


class SystemControlGoesThroughRegistryTest(unittest.TestCase):
    """Голосовой путь обязан спрашивать, а не выполнять молча."""

    def setUp(self):
        self.calls = []
        self.router = CommandRouter()
        # Подменяем ВСЁ, что меняет состояние машины. Без этого тест
        # заблокировал бы экран или усыпил компьютер по-настоящему.
        for name, result in (
            ("lock_screen", "экран заблокирован"),
            ("system_sleep", "компьютер спит"),
            ("display_sleep", "дисплей погашен"),
            ("focus_mode", "режим фокуса включён"),
            ("volume_delta", "громкость +10"),
            ("set_volume_level", "громкость 100"),
            ("set_mute", "звук выключен"),
            ("music", "музыка играет"),
        ):
            self._patch(name, result)

    def _patch(self, name, result):
        def stub(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return result

        patcher = patch.object(interactions, name, side_effect=stub)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sleep_asks_and_does_not_execute(self):
        answer = self.router.execute_local("усыпи компьютер")

        self.assertEqual(
            self.calls, [],
            "компьютер усыплён без подтверждения",
        )
        self.assertIn("да", answer.lower(), f"сэр не спрошен: {answer!r}")
        self.assertIsNotNone(
            self.router.confirmations.pending,
            "вопрос задан, но действия нет в очереди — «да» не сработает",
        )
        self.assertEqual(self.router.confirmations.pending.tool, "system.sleep")

    def test_lock_screen_asks_and_does_not_execute(self):
        answer = self.router.execute_local("заблокируй экран")
        self.assertEqual(self.calls, [], "экран заблокирован без подтверждения")
        self.assertEqual(
            self.router.confirmations.pending.tool, "system.lock_screen"
        )
        self.assertIn("заблокировать экран", answer.lower())

    def test_focus_mode_asks(self):
        self.router.execute_local("включи режим фокуса")
        self.assertEqual(self.calls, [])
        pending = self.router.confirmations.pending
        self.assertEqual(pending.tool, "system.focus_mode")
        self.assertEqual(pending.arguments, {"on": True})

    def test_focus_mode_off_asks_separately(self):
        # Включение и выключение — противоположные действия, и вопрос
        # обязан их различать, иначе сэр подтвердит не то.
        answer = self.router.execute_local("выключи режим фокуса")
        pending = self.router.confirmations.pending
        self.assertEqual(pending.arguments, {"on": False})
        self.assertIn("выключить", answer.lower())

    def test_confirmation_executes_exactly_that_action(self):
        self.router.execute_local("усыпи компьютер")
        self.router.execute_local("да")

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "system_sleep")
        self.assertIsNone(
            self.router.confirmations.pending,
            "очередь не очищена — повторное «да» выполнит действие дважды",
        )

    def test_cancelling_does_not_execute(self):
        self.router.execute_local("заблокируй экран")
        answer = self.router.execute_local("отмена")
        self.assertEqual(self.calls, [], "действие выполнилось после отмены")
        self.assertIsNone(self.router.confirmations.pending)

    def test_stale_confirmation_does_not_fire_on_random_yes(self):
        # Подтверждение протухает. Иначе «да» в разговоре о погоде через
        # полчаса усыпило бы компьютер.
        self.router.execute_local("усыпи компьютер")
        pending = self.router.confirmations.pending
        pending.created_at -= 700.0  # TTL 600 секунд

        self.router.execute_local("да")
        self.assertEqual(self.calls, [], "протухшее подтверждение сработало")

    def test_volume_needs_no_confirmation(self):
        # Не требовать «да» на громкость — иначе сэр перестанет
        # вслушаться в вопросы, и они перестанут его защищать.
        answer = self.router.execute_local("сделай громче")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "volume_delta")
        self.assertIsNone(self.router.confirmations.pending)
        self.assertNotIn("да", answer.lower() or "да")

    def test_mute_needs_no_confirmation(self):
        self.router.execute_local("выключи звук")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "set_mute")
        self.assertIsNone(self.router.confirmations.pending)

    def test_music_pause_needs_no_confirmation(self):
        self.router.execute_local("пауза музыку")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "music")
        self.assertEqual(self.calls[0][1], ("pause",))
        self.assertIsNone(self.router.confirmations.pending)

    def test_max_volume_goes_through_volume_tool(self):
        # 100 — это тот же system.volume с дельтой 100. Проверяем, что
        # дошла именно дельта, а не отдельный вызов с двумя путями.
        self.router.execute_local("максимальная громкость")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "volume_delta")
        self.assertEqual(self.calls[0][1], (100,))

    def test_screenshot_still_goes_through_the_registry(self):
        """Прежнее поведение скриншота не должно было сломаться.

        Проверяем не вызов подмены, а совпадение пути: команда голосом
        обязана давать ровно тот же ответ, что и прямой вызов
        `registry.call("screen.capture")`. Если бы она ушла мимо реестра,
        ответ отличался бы (и, скорее всего, скриншот был бы сделан в
        обход JARVIS_SCREEN_CAPTURE — именно тот дефект, который уже
        чинили в этом же месте).
        """
        voice = self.router.execute_local("сделай скриншот")
        direct = self.router.registry.call("screen.capture")
        self.assertEqual(
            voice, direct,
            "голосовая команда и реестр разошлись — скриншот идёт в обход",
        )

    def test_screenshot_respects_the_flag_and_explains_how_to_enable(self):
        # По умолчанию скриншоты выключены. Отказ обязан объяснять, что
        # делать, а не просто молчать.
        answer = self.router.execute_local("сделай скриншот")
        self.assertIn("JARVIS_SCREEN_CAPTURE", answer)

    def test_revoking_screen_read_stops_the_screenshot(self):
        router = CommandRouter()
        router.registry.policy.set_permission("SCREEN_READ", allowed=False)
        with self.assertRaises(PermissionError):
            router.registry.call("screen.capture")


class NoDirectInteractionsCallsTest(unittest.TestCase):
    """Структурная проверка: в коде маршрутизации нет прямых вызовов.

    Тесты выше проверяют поведение, но поведение можно случайно
    «починить», вернув прямой вызов. Эта проверка читает исходник и
    запрещает сам способ, которым дефект появился.
    """

    def test_system_command_has_no_direct_interactions_calls(self):
        """Проверка по ДЕРЕВУ, а не по тексту.

        Прошлый вариант искал подстроку в исходнике и срабатывал на
        собственном же docstring, где `interactions.lock_screen()`
        упоминается как описание старого дефекта. Разбираем AST и смотрим
        только настоящие вызовы.
        """
        import ast
        import inspect
        import textwrap

        from core.commands import CommandRouter

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(CommandRouter._system_control_command))
        )

        called = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id == "interactions":
                    called.add(func.attr)

        self.assertEqual(
            called, set(),
            f"прямые вызовы core.interactions в обход реестра: {sorted(called)}",
        )

    def test_system_command_calls_only_through_the_registry(self):
        """Каждое системное действие обязано попасть под политику.

        Допустимы ровно два пути:

        * `registry.call(...)` — действие выполняется сразу. Так идёт звук:
          разрешение проверяется, подтверждение не требуется.
        * `_ask_confirmation(tool, args)` — действие кладётся в очередь и
          выполняется через `registry.call(..., confirmed=True)`, когда сэр
          ответит «да». Так идёт всё, что меняет состояние машины.

        Третьего пути быть не должно: именно он и был дефектом.
        """
        import ast
        import inspect
        import textwrap

        from core.commands import CommandRouter

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(CommandRouter._system_control_command))
        )

        direct = set()
        deferred = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            # self.registry.call("system.x", ...)
            if (func.attr == "call"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "registry"):
                direct.add(first.value)
            # self._ask_confirmation("system.x", ...)
            elif (func.attr == "_ask_confirmation"
                  and isinstance(func.value, ast.Name)
                  and func.value.id == "self"):
                deferred.add(first.value)

        covered = direct | deferred
        for tool in ("system.volume", "system.mute", "system.music",
                     "system.lock_screen", "system.display_sleep",
                     "system.sleep", "system.focus_mode"):
            with self.subTest(tool=tool):
                self.assertIn(
                    tool, covered,
                    f"{tool} не идёт ни через реестр, ни через "
                    f"подтверждение — разрешение не проверяется",
                )

        # Аудито должно быть ровно там, где нужно: звук — сразу,
        # состояние машины — только после «да».
        self.assertEqual(
            deferred,
            {"system.lock_screen", "system.display_sleep", "system.sleep",
             "system.focus_mode"},
            "набор действий, требующих подтверждения, изменился",
        )
        self.assertNotIn("system.lock_screen", direct)
        self.assertNotIn("system.sleep", direct)
        self.assertEqual(
            direct & deferred, set(),
            "действие одновременно выполняется сразу и ждёт подтверждения",
        )

    def test_confirmed_action_really_goes_through_registry(self):
        # Отложенное действие обязано при подтверждении выполниться через
        # реестр с confirmed=True, иначе политика обходится целиком.
        import ast
        import inspect
        import textwrap

        from core.commands import CommandRouter

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(CommandRouter._resolve_browser_confirmation))
        )
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and func.attr == "call"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "registry"):
                keywords = {kw.arg for kw in node.keywords}
                if "confirmed" in keywords:
                    found = True
        self.assertTrue(
            found,
            "подтверждённое действие выполняется не через "
            "registry.call(confirmed=True)",
        )

    def test_every_interaction_call_goes_through_a_tool(self):
        # Набор инструментов должен покрывать все обращения к
        # interactions из маршрутизации, иначе появятся новые обходы.
        names = {name for name, _d, _h, _p in TOOL_SPECS}
        for tool in ("system.lock_screen", "system.sleep", "system.display_sleep",
                     "system.focus_mode", "system.volume", "system.mute",
                     "system.music"):
            self.assertIn(tool, names)

    def test_tool_schemas_exist_for_new_tools(self):
        # Без схемы модель не знает, какие аргументы передавать,
        # и инструмент для неё бесполезен.
        from core.tool_schemas import validate_tool_arguments

        self.assertEqual(
            validate_tool_arguments("system.volume", {"delta": 10}), {"delta": 10}
        )
        self.assertEqual(
            validate_tool_arguments("system.mute", {"on": True}), {"on": True}
        )
        self.assertEqual(
            validate_tool_arguments("system.music", {"action": "play"}),
            {"action": "play"},
        )
        self.assertEqual(
            validate_tool_arguments("system.focus_mode", {"on": False}),
            {"on": False},
        )

    def test_invalid_music_action_is_rejected(self):
        # Схема ограничивает действие перечислением. Без этого модель
        # могла бы передать «stop» или пустую строку и положить текст
        # в музыкальный плеер.
        from pydantic import ValidationError

        from core.tool_schemas import validate_tool_arguments

        for bad in ("stop", "", "PLAY; rm -rf /", "перезагрузка"):
            with self.subTest(action=bad), self.assertRaises(ValidationError):
                validate_tool_arguments("system.music", {"action": bad})


class FocusModeIntentTest(unittest.TestCase):
    """Намерение сэра не должно переворачиваться на противоположное.

    Найдено тестом на этой же правке, и баг был не в моём коде, а в
    исходном: фраза «выключи режим фокуса» содержит подстроку «режим
    фокуса», а проверка включения стояла первой. Сэр просил выключить —
    NOVA AI включал, то есть делал обратное тому, о чём его просили.
    """

    def setUp(self):
        self.calls = []

        def stub(enable=True):
            self.calls.append(bool(enable))
            return "режим фокуса переключён"

        patcher = patch.object(interactions, "focus_mode", side_effect=stub)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.router = CommandRouter()

    def _pending_on(self, phrase):
        answer = self.router.execute_local(phrase)
        self.assertEqual(
            self.calls, [],
            f"«{phrase}» выполнилась без подтверждения",
        )
        pending = self.router.confirmations.pending
        self.assertIsNotNone(pending, f"«{phrase}» не спросила подтверждения")
        return pending.arguments.get("on"), answer

    def test_turn_off_asks_to_turn_off(self):
        value, answer = self._pending_on("выключи режим фокуса")
        self.assertIs(value, False)
        self.assertIn("выключить", answer.lower())

    def test_turn_on_asks_to_turn_on(self):
        value, answer = self._pending_on("включи режим фокуса")
        self.assertIs(value, True)
        self.assertIn("включить", answer.lower())

    def test_variants_of_turn_off(self):
        for phrase in ("выключи режим фокуса", "убери фокус",
                       "не беспокоить выключи", "фокус выключи",
                       "отключи фокус"):
            with self.subTest(phrase=phrase):
                self.router.confirmations.clear()
                self.calls.clear()
                value, _answer = self._pending_on(phrase)
                self.assertIs(
                    value, False,
                    f"«{phrase}» спросила про включение — намерение перевёрнуто",
                )

    def test_variants_of_turn_on(self):
        for phrase in ("включи режим фокуса", "не беспокоить включи",
                       "фокус включи", "включи фокус"):
            with self.subTest(phrase=phrase):
                self.router.confirmations.clear()
                self.calls.clear()
                value, _answer = self._pending_on(phrase)
                self.assertIs(
                    value, True,
                    f"«{phrase}» спросила про выключение — намерение перевёрнуто",
                )

    def test_bare_phrase_asks_rather_than_guesses(self):
        # «режим фокуса» без указания направления. Раньше эта подстрока
        # означала «включить» и перехватывала всё, включая «выключи».
        # Теперь фраза неоднозначна — и это честнее, чем угадать наоборот.
        answer = self.router.execute_local("режим фокуса")
        self.assertIsNone(
            self.router.confirmations.pending,
            f"неоднозначная фраза что-то предложила: {answer!r}",
        )

    def test_confirmed_turn_off_really_turns_off(self):
        self.router.execute_local("выключи режим фокуса")
        self.router.execute_local("да")
        self.assertEqual(self.calls, [False])


class ConfirmationQuestionReadableTest(unittest.TestCase):

    def test_question_does_not_leak_internal_tool_names(self):
        router = CommandRouter()
        router.execute_local("усыпи компьютер")
        pending = router.confirmations.pending
        self.assertEqual(pending.preview, "усыпить компьютер")
        self.assertNotIn("system.", pending.preview)
        self.assertNotIn("SYSTEM_CONTROL", pending.preview)

    def test_lock_screen_preview_is_plain_russian(self):
        router = CommandRouter()
        router.execute_local("заблокируй экран")
        self.assertEqual(
            router.confirmations.pending.preview, "заблокировать экран"
        )


if __name__ == "__main__":
    unittest.main()
