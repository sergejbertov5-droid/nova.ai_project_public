"""
Живые приёмочные тесты многошагового исполнения.

Запускаются вручную, не входят в обычный прогон pytest: часть из них
открывает приложения, ходит в сеть и управляет браузером.

  .venv/bin/python -m tests.live_acceptance

Почему вместо настоящего AI — подставной планировщик. Живой AI сейчас
недоступен: Groq отвечает 403, Gemini — 400 FAILED_PRECONDITION
«User location is not supported». Проверять нужно механизм исполнения,
а не доступность провайдера. Все инструменты при этом настоящие:
приложения открываются, сеть работает, браузер управляется.
"""
import json
import subprocess
import time

from core.agent_runtime import AgentRuntime
from core.commands import CommandRouter
from core.execution_plan import StepStatus


# ----------------------------------------------------------------------
# Подставной AI: строит заранее заданный план.
# ----------------------------------------------------------------------
class ScriptedBrain:
    """Отдаёт заготовленные шаги плана и настойчиво их повторяет.

    Настоящий AI сейчас недоступен по региону, поэтому проверяем
    исполнитель, а не провайдера.
    """

    persona = "sol"

    def __init__(self, decision, plans, summary="Готово, сэр.",
                 break_after_plan=False):
        self.decision = decision
        self.plans = list(plans)
        self.summary = summary
        self.break_after_plan = break_after_plan
        self.summary_available = True
        self.ai_calls = 0
        self.tool_results = []
        self.task_history = []
        self.prompts = []

    def start_task(self, goal):
        self.task_history.clear()

    def is_available(self, force=False):
        return self.summary_available

    def decide(self, text, catalog, memory_context="", new_task=True, task_context=""):
        self.ai_calls += 1
        return self.decision

    def decide_next(self, task_goal=None, task_history=None):
        return {"type": "answer", "text": "Продолжил по прежнему пути."}

    def generate_text(self, system_prompt, user_text, **kwargs):
        self.ai_calls += 1
        self.prompts.append(system_prompt)
        if self.plans:
            plan = self.plans.pop(0)
            if self.break_after_plan:
                # План построен, дальше провайдер отвалился — как сейчас
                # по региону. Итоговый вывод собирать будет нечем.
                self.summary_available = False
            return json.dumps({"steps": plan})
        return self.summary

    def record_tool_result(self, result):
        self.tool_results.append(result)

    def record_action(self, action):
        self.task_history.append(action)


class RefuseAiBrain(ScriptedBrain):
    """AI, который падает при любом обращении.

    Нужен, чтобы доказать: простая команда не требует AI вообще.
    """

    def decide(self, *args, **kwargs):
        raise AssertionError("простая команда не должна доходить до AI")

    def generate_text(self, *args, **kwargs):
        raise AssertionError("простая команда не должна доходить до AI")


# ----------------------------------------------------------------------
# Отчёт
# ----------------------------------------------------------------------
_results = []


def check(name, condition, detail=""):
    _results.append((name, bool(condition), detail))
    mark = "ПРОШЁЛ" if condition else "НЕ ПРОШЁЛ"
    print(f"    [{mark}] {name}")
    if detail:
        print(f"            {detail}")


def apps_running(app):
    out = subprocess.run(
        ["pgrep", "-x", app], capture_output=True, text=True
    ).stdout.strip()
    return len([line for line in out.splitlines() if line])


def build(brain, deterministic=True):
    """Роутер и исполнитель.

    deterministic=False отключает разбор фраз на лету. Он жадно
    перехватывает почти любую фразу с «найди» (диагностика в конце
    это показывает), и без отключения до планировщика не доходит
    ни одна составная цель. Здесь он отключён, чтобы измерять сам
    исполнитель, а не маршрутизацию.

    Все вызовы записываются на настоящем шлюзе реестра: иначе
    не видно ни отказов политики, ни попыток пройти в обход неё.
    """
    router = CommandRouter(brain=brain)
    if not deterministic:
        router.execute_local = lambda text: None

    calls = []
    real = router.registry.call_result

    def _recording(tool, confirmed=False, **kwargs):
        result = real(tool, confirmed=confirmed, **kwargs)
        calls.append((tool, result))
        return result

    router.registry.call_result = _recording
    router.call_log = calls
    return router, AgentRuntime(router)


# ----------------------------------------------------------------------
# TEST 6 — план выполнен, а итоговый вывод недоступен
# ----------------------------------------------------------------------
def test_6_answer_without_ai():
    """Сейчас именно такая страна: план есть, говорить некому.

    Голос всё равно обязан сообщить результат. Счётчик «выполнено 2
    из 2 шагов» без единого факта — это молчание, а не ответ.
    """
    print("\n─── TEST 6: итог без AI — результат всё равно звучит ───")
    brain = ScriptedBrain(
        decision={"type": "tool_call", "tool": "web.search", "arguments": {}},
        plans=[[
            {
                "step_id": "s1",
                "description": "открыть Safari",
                "required_tool": "applications.open",
                "arguments": {"app_name": "Safari"},
                "expected": "приложение запущено",
            },
            {
                "step_id": "s2",
                "description": "найти официальный сайт Python",
                "required_tool": "web.search",
                "arguments": {"query": "официальный сайт Python", "limit": 3},
                "dependencies": ["s1"],
                "expected": "есть результаты со ссылкой на python.org",
            },
        ]],
        break_after_plan=True,
    )
    router, runtime = build(brain, deterministic=False)

    answer = runtime.handle("открой Safari и найди официальный сайт Python")

    print(f"    AI для итога: {'есть' if brain.summary_available else 'недоступен'}")
    print(f"    ответ: {str(answer)[:200]!r}")
    check("оба шага выполнены", tools_ran(router) == ["applications.open", "web.search"])
    check(
        "ответ содержит результат последнего шага",
        "Результаты по запросу" in str(answer),
        "результат обязан звучать, а не считаться",
    )
    check("счётчик шагов остался в ответе", "2 из 2" in str(answer))
    check("короткий ответ, пригодный для голоса", len(str(answer)) < 400)
    return "Результаты по запросу" in str(answer)


def tools_ran(router):
    """Названия инструментов, дошедших до шлюза, по порядку."""
    return [tool for tool, _ in router.call_log]


# ----------------------------------------------------------------------
# TEST 1 — простая команда: план не нужен
# ----------------------------------------------------------------------
def test_1_simple():
    print("\n─── TEST 1: простая команда обходится без AI ───")
    before = apps_running("Safari")
    brain = RefuseAiBrain(decision=None, plans=[])
    router, runtime = build(brain)

    answer = runtime.handle("открой Safari")
    time.sleep(2.5)
    after = apps_running("Safari")

    print(f"    ответ: {answer!r}")
    check("Safari запущен", after > 0, f"процессов было {before}, стало {after}")
    check(
        "AI не вызывался",
        brain.ai_calls == 0,
        "AI бросил бы исключение, если бы его позвали",
    )
    return after > 0


# ----------------------------------------------------------------------
# TEST 2 — многошаговая цель с настоящими инструментами
# ----------------------------------------------------------------------
def test_2_multistep():
    print("\n─── TEST 2: многошаговая цель «открой Safari, найди сайт Python» ───")
    brain = ScriptedBrain(
        decision={"type": "tool_call", "tool": "web.search", "arguments": {}},
        plans=[[
            {
                "step_id": "s1",
                "description": "открыть Safari",
                "required_tool": "applications.open",
                "arguments": {"app_name": "Safari"},
                "expected": "приложение запущено",
            },
            {
                "step_id": "s2",
                "description": "найти официальный сайт Python",
                "required_tool": "web.search",
                "arguments": {"query": "официальный сайт Python", "limit": 3},
                "dependencies": ["s1"],
                "expected": "есть результаты со ссылкой на python.org",
            },
        ]],
    )
    router, runtime = build(brain, deterministic=False)

    answer = runtime.handle("открой Safari и найди официальный сайт Python")

    tools = tools_ran(router)
    print(f"    ответ: {str(answer)[:150]!r}")
    print(f"    инструменты: {tools}")
    check("выполнены оба шага", tools == ["applications.open", "web.search"], str(tools))
    check(
        "все шаги успешны",
        bool(tools) and all(r.ok for _, r in router.call_log),
        f"результатов: {len(router.call_log)}",
    )
    check("сэр получил ответ", bool(answer), f"ответ: {str(answer)[:120]!r}")
    # Содержание ответа без AI проверяет TEST 6, здесь подставной brain
    # возвращает заготовку, и проверять тут нечего.
    return tools == ["applications.open", "web.search"]


# ----------------------------------------------------------------------
# TEST 3 — восстановление после постоянного сбоя
# ----------------------------------------------------------------------
def test_3_recovery():
    print("\n─── TEST 3: шаг падает постоянно — исполнитель перепланирует ───")
    brain = ScriptedBrain(
        decision={"type": "tool_call", "tool": "web.search", "arguments": {}},
        plans=[
            # Первая попытка: приложения не существует.
            [{
                "step_id": "s1",
                "description": "открыть несуществующее приложение",
                "required_tool": "applications.open",
                "arguments": {"app_name": "НетТакогоПриложенияХа"},
                "expected": "приложение запущено",
            }],
            # Перепланирование: сэр хотел браузер, а не это приложение.
            [{
                "step_id": "r1",
                "description": "открыть Safari вместо несуществующего приложения",
                "required_tool": "applications.open",
                "arguments": {"app_name": "Safari"},
                "expected": "приложение запущено",
            }],
        ],
    )
    router, runtime = build(brain, deterministic=False)

    answer = runtime.handle("открой то приложение, которого нет")

    tools = tools_ran(router)
    classes = [getattr(r, "error_class", None) for _, r in router.call_log]
    print(f"    ответ: {str(answer)[:150]!r}")
    print(f"    инструменты: {tools}")
    print(f"    классы ошибок: {classes}")
    print(f"    запросов к AI: {brain.ai_calls}")
    for index, prompt in enumerate(brain.prompts, 1):
        head = prompt.strip().splitlines()[0]
        print(f"      промпт {index}: {head[:70]!r}")
    check(
        "сбойный шаг выполнен и распознан как постоянный",
        tools[:1] == ["applications.open"] and classes[0] == "permanent",
        str(classes),
    )
    check(
        "после перепланирования задача доведена до конца",
        tools == ["applications.open", "applications.open"],
        "второй запуск того же инструмента = новый план",
    )
    check("сэр получил ответ, а не тишину", bool(answer))
    return tools == ["applications.open", "applications.open"]


# ----------------------------------------------------------------------
# TEST 4 — отмена задачи на полпути
# ----------------------------------------------------------------------
def test_4_cancel():
    print("\n─── TEST 4: «стоп» на полпути — остальные шаги не выполняются ───")
    brain = ScriptedBrain(
        decision={"type": "tool_call", "tool": "web.search", "arguments": {}},
        plans=[[
            {
                "step_id": "s1",
                "description": "открыть Safari",
                "required_tool": "applications.open",
                "arguments": {"app_name": "Safari"},
                "expected": "приложение запущено",
            },
            {
                "step_id": "s2",
                "description": "найти сайт Python",
                "required_tool": "web.search",
                "arguments": {"query": "официальный сайт Python", "limit": 2},
                "dependencies": ["s1"],
                "expected": "есть результаты",
            },
            {
                "step_id": "s3",
                "description": "узнать время",
                "required_tool": "system.time",
                "arguments": {},
                "dependencies": ["s2"],
                "expected": "время известно",
            },
        ]],
    )
    router, runtime = build(brain, deterministic=False)

    def _stop(message):
        runtime.cancel_task("сэр сказал стоп")

    router.progress_callback = _stop
    answer = runtime.handle("открой Safari, найди сайт Python, скажи время")

    tools = tools_ran(router)
    print(f"    ответ: {answer!r}")
    print(f"    выполнено инструментов: {tools}")
    check(
        "выполнен только первый шаг",
        tools == ["applications.open"],
        "остальные шаги не должны запускаться после отмены",
    )
    check("задача снята, отменять больше нечего", runtime.cancel_task() is None)
    return tools == ["applications.open"]


# ----------------------------------------------------------------------
# TEST 5 — подтверждение относится к конкретному действию
# ----------------------------------------------------------------------
def test_5_confirmation():
    print("\n─── TEST 5: опасное действие ждёт «да», и только своего ───")
    brain = ScriptedBrain(
        decision={"type": "tool_call", "tool": "browser.click", "arguments": {}},
        plans=[[
            {
                "step_id": "s1",
                "description": "нажать кнопку на странице",
                "required_tool": "browser.click",
                "arguments": {"selector": "#p"},
                "expected": "страница изменилась",
            },
        ]],
    )
    router, runtime = build(brain, deterministic=False)

    asked = runtime.handle("нажми кнопку на странице")
    print(f"    вопрос: {asked!r}")
    check(
        "сэр спрошен до выполнения",
        tools_ran(router) == [],
        f"инструментов: {tools_ran(router)}",
    )
    check("вопрос содержит просьбу подтвердить", "подтверж" in str(asked).lower())

    # Посторонняя фраза не должна ни выполнить действие, ни забыть вопрос.
    again = runtime.handle("а сколько сейчас времени?")
    check(
        "посторонняя фраза не выполняет действие",
        tools_ran(router) == [],
        "ждать ответа всё ещё нужно",
    )
    check("вопрос повторён, а не проигнорирован", again == asked)

    approved = runtime.handle("да")
    check(
        "после «да» действие действительно выполнено",
        tools_ran(router) == ["browser.click"],
        f"инструментов: {tools_ran(router)}",
    )
    print(f"    результат действия: {str(approved)[:120]!r}")

    # Новое действие того же типа обязано спросить заново.
    brain.plans = [[
        {
            "step_id": "s2",
            "description": "нажать ещё одну кнопку",
            "required_tool": "browser.click",
            "arguments": {"selector": "#q"},
            "expected": "страница изменилась",
        },
    ]]
    again = runtime.handle("теперь нажми вот эту")
    check(
        "старое «да» не разрешает новое действие",
        tools_ran(router) == ["browser.click"],
        "второе действие снова ждёт подтверждения",
    )
    check("снова задан вопрос", "подтверж" in str(again).lower())
    return tools_ran(router) == ["browser.click"]


# ----------------------------------------------------------------------
# ДИАГНОСТИКА — не проверка, а измерение. На результат не влияет.
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# TEST 7 — голосовой вопрос о задаче
# ----------------------------------------------------------------------
def test_7_task_status_voice():
    """«Джарвис, что с задачей?» — ответ голосом из реестра.

    Проверяется настоящий голосовой цикл: команда должна распознаться
    раньше, чем фраза уйдёт в AI, и ответ должен прозвучать, а не
    раствориться в молчании.
    """
    print("\n─── TEST 7: голосовой вопрос «что с задачей?» ───")
    from core.assistant import Assistant
    from main import answer_task_status, is_task_status_command

    spoken = []

    class _Speaker:
        def speak(self, text):
            spoken.append(text)

        def stop(self):
            pass

    brain = ScriptedBrain(
        decision={"type": "tool_call", "tool": "web.search", "arguments": {}},
        plans=[[
            {
                "step_id": "s1",
                "description": "найти официальный сайт Python",
                "required_tool": "web.search",
                "arguments": {"query": "официальный сайт Python", "limit": 2},
                "expected": "есть результаты",
            },
        ]],
    )
    router, runtime = build(brain, deterministic=False)
    assistant = Assistant(speaker=_Speaker())
    assistant.router = router
    assistant.runtime = runtime

    check(
        "фраза распознана как вопрос о задаче",
        is_task_status_command("джарвис, что с задачей?"),
    )
    check(
        "обычная задача не принята за вопрос о задаче",
        not is_task_status_command("открой Safari"),
    )

    answer_task_status(assistant)
    empty_answer = spoken[-1] if spoken else ""
    print(f"    без задач: {empty_answer!r}")
    check("сэр получил ответ даже без задач", bool(empty_answer))
    check("ответ честно говорит, что задач нет", "нет" in empty_answer.lower())

    # Теперь задача идёт — отвечаем во время работы.
    # answer_task_status произносит ответ, а не возвращает его, поэтому
    # смотрим, что именно прозвучало.
    spoken_before = len(spoken)

    def _ask_during(message):
        answer_task_status(assistant)

    router.progress_callback = _ask_during
    runtime.handle("найди официальный сайт Python")

    live = spoken[spoken_before] if len(spoken) > spoken_before else ""
    print(f"    во время работы: {live!r}")

    answer_task_status(assistant)
    final = spoken[-1] if spoken else ""
    print(f"    после задачи: {final!r}")

    check("во время работы ответ прозвучал", bool(live))
    check(
        "ответ во время работы называл задачу",
        "сайт Python" in live,
        "сэр должен понимать, о какой задаче речь",
    )
    check("после задачи сказано «выполнена»", "выполнена" in final.lower())
    return "выполнена" in final.lower()


def diagnose_routing():
    """Доходит ли составная цель до планировщика в бою.

    Отдельная функция, а не TEST 6, потому что это не дефект
    исполнения, а вопрос маршрутизации: слой разбора фраз на лету
    отвечает на «найди…» сам, и планировщик не вызывается вовсе.
    Решение о маршрутизации — за сэром, тест лишь показывает факт.
    """
    print("\n─── ДИАГНОСТИКА: доходит ли составная цель до планировщика ───")

    goals = [
        "открой Safari и найди официальный сайт Python",
        "открой Safari, найди сайт Python и скажи время",
        "найди официальный сайт Python",
    ]
    for goal in goals:
        brain = ScriptedBrain(
            decision={"type": "tool_call", "tool": "web.search", "arguments": {}},
            plans=[[{
                "description": "найти официальный сайт Python",
                "required_tool": "web.search",
                "arguments": {"query": "официальный сайт Python", "limit": 2},
                "expected": "есть результаты",
            }]],
        )
        router = CommandRouter(brain=brain)
        runtime = AgentRuntime(router)
        answer = runtime.handle(goal)
        reached = [r.tool for r in brain.tool_results]
        print(f"    цель: {goal!r}")
        print(f"      слой фраз ответил: {str(answer)[:70]!r}")
        print(f"      планировщик выполнял: {reached or 'нет — цель не дошла'}")

    print("\n    Вывод: слой разбора фраз на лету отвечает на составные цели сам,")
    print("    поэтому в бою до планировщика они не доходят. Пока AI недоступен")
    print("    по региону, менять это нельзя — иначе вместо рабочего поиска")
    print("    сэр получит отказ AI.")


def main():
    print("=" * 66)
    print("ЖИВЫЕ ПРИЁМОЧНЫЕ ТЕСТЫ")
    print("=" * 66)
    print("  Живой AI недоступен (регион): Groq 403, Gemini FAILED_PRECONDITION.")
    print("  Планирование подставляется, инструменты — настоящие.")
    print("  Слой разбора фраз на лету отключён: см. ДИАГНОСТИКУ в конце.")

    outcomes = {
        "TEST 1 простая команда": test_1_simple(),
        "TEST 2 многошаговая цель": test_2_multistep(),
        "TEST 3 восстановление": test_3_recovery(),
        "TEST 4 отмена": test_4_cancel(),
        "TEST 5 подтверждение": test_5_confirmation(),
        "TEST 6 ответ без AI": test_6_answer_without_ai(),
        "TEST 7 вопрос о задаче": test_7_task_status_voice(),
    }

    diagnose_routing()

    print("\n" + "=" * 66)
    print("ИТОГ")
    print("=" * 66)
    failed = 0
    for name, passed in outcomes.items():
        if not passed:
            failed += 1
        print(f"  {'ПРОШЁЛ' if passed else 'НЕ ПРОШЁЛ':10s} {name}")
    checks = len(_results)
    good = sum(1 for _, ok, _ in _results if ok)
    print(f"\n  проверок: {good} из {checks}")
    print(f"  тестов не прошли: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
