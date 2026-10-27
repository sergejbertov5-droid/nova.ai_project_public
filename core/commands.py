import logging
import re
from typing import Dict, List, Optional

import requests

from core.brain import AIBrain
from core.confirmations import ConfirmationManager
from core.permissions import ConfirmationRequired, PermissionPolicy
from core.tool_registry import ToolRegistry, ToolSpec
from core.tool_schemas import get_tool_schema_json
from core.validators import validator_for

from tools.applications import open_application

from tools.browser import (
    open_url,
    search,
    open_tab,
    close_tab,
    switch_tab,
    get_current_page,
    read_page,
    click,
    type_text,
    kinogo_search,
)

from tools.system import (
    battery_status,
    current_date,
    current_time,
    free_disk_space,
)

from tools.mail import unread_mail_summary
from tools.telegram import unread_telegram_summary

from tools.maps import (
    maps_location,
    maps_weather,
    maps_search,
    maps_directions,
)

from core.user_context import (
    capture_screenshot,
    screen_status_text,
    usage_summary_text,
)
from core.user_profile import open_work_tools, profile_summary_text
from core.memory import remember_fact

from tools.websearch import (
    web_search,
    fetch_page_text,
)

from core.self_learning import run_learning_session, LearningStore

from core.vpn_service import (
    network_status_text,
    servers_text,
)


# ---------------------------------------------------------------------------
# Обработчики управления системой
#
# Это тонкие обёртки над core.interactions. Существуют по одной причине:
# раньше действия вызывались напрямую из `_system_control_command`, минуя
# реестр, а значит минуя PermissionPolicy и подтверждение. Отдельные
# функции нужны, чтобы у каждого действия был ToolSpec — с именем,
# описанием, категорией разрешения и текстом подтверждения.
# ---------------------------------------------------------------------------


#: У обязательных аргументов здесь НЕТ значений по умолчанию.
#:
#: Аргумент, помеченный в схеме обязательным, но имеющий умолчание, вводит
#: в заблуждение: вызов без него проходит как успешный, но делает не то
#: действие. «Громче» без дельты — это какая громкость? То же самое с
#: `on` у режима фокуса: молчаливое `True` означало бы «включить» вместо
#: запрошенного.


def _volume_change(delta: int) -> str:
    """Изменение общей громкости. delta — в процентах."""
    from core import interactions

    try:
        return interactions.volume_delta(delta)
    except Exception:
        return "Сэр, не удалось изменить громкость."


def _set_volume_max() -> str:
    from core import interactions

    try:
        return interactions.set_volume_level(100)
    except Exception:
        return "Сэр, не удалось изменить громкость."


def _mute_toggle(on: bool) -> str:
    from core import interactions

    try:
        return interactions.set_mute(bool(on))
    except Exception:
        return "Сэр, не удалось отключить звук."


def _music_action(action: str) -> str:
    from core import interactions

    try:
        return interactions.music(str(action))
    except Exception:
        return "Сэр, не удалось управлять музыкой."


def _focus_mode(on: bool) -> str:
    from core import interactions

    try:
        return interactions.focus_mode(bool(on))
    except Exception:
        return (
            "Сэр, режим фокуса нужно переключать вручную: "
            "macOS не даёт менять его из командной строки."
        )


def _lock_screen() -> str:
    from core import interactions

    try:
        return interactions.lock_screen()
    except Exception:
        return "Сэр, не удалось заблокировать экран."


def _display_sleep() -> str:
    from core import interactions

    try:
        return interactions.display_sleep()
    except Exception:
        return "Сэр, не удалось погасить экран."


def _system_sleep() -> str:
    from core import interactions

    try:
        return interactions.system_sleep()
    except Exception:
        return "Сэр, не удалось усыпить компьютер."


def lower_replace_first(text: str, marker: str) -> str:
    """Вырезает первое вхождение маркера (без учёта регистра), возвращает остаток."""
    lowered = text.lower()
    index = lowered.find(marker.lower())
    if index == -1:
        return text
    return text[index + len(marker):]


# ---------------------------------------------------------------------------
# Ответ на вопрос об опасном действии (выключение / перезагрузка)
# ---------------------------------------------------------------------------
#
# Правило намеренно асимметричное:
#
#   согласие — только если ВСЕ слова фразы являются словами согласия;
#   отказ    — если хотя бы ОДНО слово является отказным;
#   остальное — «не понял».
#
# Почему не поиск подстроки: подстрока «да» живёт внутри «никогда»,
# а «выполняй» — внутри «не выполняй». Прежняя проверка считала обычный
# отказ согласием, и компьютер выключался словом «никогда». Отказ
# всегда проверяется первым, а неизвестная фраза не разоружает, но и
# не исполняет ничего.
#
# Наборы слов взяты из уже проверенных источников проекта
# (ConfirmationManager.YES/NO) и прежнего списка опасных действий —
# новые слова не выдумываются.

_DANGER_YES_WORDS = frozenset({
    "да", "ага", "подтверждаю", "подтвердить", "разрешаю",
    "выполняй", "давай", "точно", "ок", "okay", "ok",
})

_DANGER_NO_WORDS = frozenset({
    "нет", "нету", "не", "отмена", "отмен", "отмени", "отменить",
    "не надо", "стоп", "погоди", "подожди", "отказ",
})


def _words_of(text: str) -> List[str]:
    """Только буквы и цифры, разбитые на слова: «Да, подтверждаю!» → [да, подтверждаю]."""
    cleaned = re.sub(r"[^\w\s]", " ", str(text).casefold(), flags=re.UNICODE)
    return cleaned.split()


def resolve_danger_reply(text: str) -> str:
    """Что означает ответ на вопрос об опасном действии.

    Возвращает "yes", "no" или "unknown".
    """
    words = _words_of(text)
    if not words:
        return "unknown"
    if any(word in _DANGER_NO_WORDS for word in words):
        return "no"
    if all(word in _DANGER_YES_WORDS for word in words):
        return "yes"
    return "unknown"


# Псевдонимы рабочих программ → канонический ключ APPLICATIONS.
# Используются, когда пользователь говорит «я работаю с ...».
WORK_TOOL_ALIASES = {
    "opencode": "opencode",
    "опенкод": "opencode",
    "опен код": "opencode",
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "visual studio code": "visual studio code",
    "вижуал студио код": "visual studio code",
    "визуал студио": "visual studio code",
    "сафари": "safari",
    "safari": "safari",
    "хром": "google chrome",
    "chrome": "google chrome",
    "браузер": "google chrome",
    "терминал": "terminal",
    "телеграм": "telegram",
    "telegram": "telegram",
    "файндер": "finder",
    "finder": "finder",
}


# Каталог инструментов: (имя, описание, обработчик, категория разрешения).
#
# Живёт на уровне модуля, а не внутри __init__, потому что это единственный
# источник правды о том, какое разрешение какому действию соответствует.
# Раньше список был локальной переменной, и проверить связь «инструмент →
# разрешение» было нечем — из-за этого чтение почты оказалось спрятано под
# SYSTEM_READ, а объявленное EMAIL_READ не управляло ничем. Настройки
# разрешений и тесты читают связь отсюда.
TOOL_SPECS = [

    (
        "applications.open",
        "Открыть приложение",
        open_application,
        "APP_OPEN",
    ),

    (
        "system.time",
        "Время",
        current_time,
        "SYSTEM_READ",
    ),

    (
        "system.date",
        "Дата",
        current_date,
        "SYSTEM_READ",
    ),

    (
        "system.battery",
        "Батарея",
        battery_status,
        "SYSTEM_READ",
    ),

    (
        "system.disk",
        "Диск",
        free_disk_space,
        "SYSTEM_READ",
    ),

    (
        "mail.unread",
        "Почта",
        unread_mail_summary,
        # Раньше стояло SYSTEM_READ — «время, дата, батарея, диск».
        # Инструмент при этом читает настоящий почтовый ящик, то
        # есть личную переписку. Получалось, что доступ к почте
        # выдавался и отнимался вместе с часами и батареей, а
        # объявленное в политике EMAIL_READ («Чтение почты») и
        # переключатель с таким названием в настройках не
        # управляли ничем. Сэр видел отдельный выключатель почты
        # и полагался на него — а ящик читался всё равно.
        "EMAIL_READ",
    ),

    (
        "telegram.unread",
        "Telegram",
        unread_telegram_summary,
        # Раньше стояло SYSTEM_READ — «время, дата, батарея, диск». Инструмент
        # при этом читает личную переписку сэра. Ровно та же ошибка, что была
        # с почтой: объявленное в политике EMAIL_READ («Чтение почты») и
        # переключатель с таким названием не управляли ничем, потому что почта
        # пряталась под SYSTEM_READ. Здесь выключатель «Чтение почты» сэр
        # выключил бы — и всё равно получил бы содержимое чатов.
        "TELEGRAM_READ",
    ),

    (
        "browser.open_url",
        "Открыть сайт",
        open_url,
        "BROWSER_READ",
    ),

    (
        "browser.search",
        "Поиск Google",
        search,
        "BROWSER_READ",
    ),

    (
        "browser.kinogo_search",
        "Поиск фильма Kinogo",
        kinogo_search,
        "BROWSER_READ",
    ),

    (
        "browser.read_page",
        "Читать страницу",
        read_page,
        "BROWSER_READ",
    ),

    (
        "browser.click",
        "Клик",
        click,
        "BROWSER_CONTROL",
    ),

    (
        "browser.type",
        "Ввод текста",
        type_text,
        "BROWSER_CONTROL",
    ),

    (
        "maps.location",
        "Моё местоположение",
        maps_location,
        "SYSTEM_READ",
    ),

    (
        "maps.weather",
        "Погода",
        maps_weather,
        "SYSTEM_READ",
    ),

    (
        "maps.search",
        "Поиск мест",
        maps_search,
        "SYSTEM_READ",
    ),

    (
        "maps.directions",
        "Маршрут до места",
        maps_directions,
        "SYSTEM_READ",
    ),

    (
        "screen.status",
        "Что сейчас на экране",
        screen_status_text,
        "SCREEN_READ",
    ),

    (
        "screen.capture",
        "Скриншот (сохранить локально)",
        capture_screenshot,
        "SCREEN_READ",
    ),

    (
        "user.profile",
        "Профиль пользователя",
        profile_summary_text,
        "SYSTEM_READ",
    ),

    (
        "web.search",
        "Искать в интернете (реальные результаты)",
        web_search,
        "WEB_READ",
    ),

    (
        "web.fetch",
        "Прочитать содержимое веб-страницы",
        fetch_page_text,
        "WEB_READ",
    ),

    (
        "web.learn",
        "Изучить тему из интернета и запомнить",
        # Обработчик подставляется в CommandRouter.__init__: он завязан
        # на self.brain конкретного экземпляра, а на уровне модуля self
        # не существует. Поэтому здесь None, и __init__ подставляет
        # обработчик, собранный вокруг живого brain.
        None,
        "WEB_READ",
    ),

    #
    # Звук. Обратим голосом, ничего не ломает — подтверждения не нужно.
    #
    (
        "system.volume",
        "Изменить громкость",
        _volume_change,
        "SYSTEM_AUDIO",
    ),

    (
        "system.mute",
        "Заглушить звук или вернуть его",
        _mute_toggle,
        "SYSTEM_AUDIO",
    ),

    (
        "system.music",
        "Пауза, продолжение, следующий или предыдущий трек",
        _music_action,
        "SYSTEM_AUDIO",
    ),

    #
    # Состояние машины. Отменить голосом нельзя: микрофон не работает,
    # пока компьютер спит или заблокирован. Поэтому обязательное «да».
    #
    (
        "system.lock_screen",
        "Заблокировать экран",
        _lock_screen,
        "SYSTEM_CONTROL",
    ),

    (
        "system.display_sleep",
        "Погасить дисплей",
        _display_sleep,
        "SYSTEM_CONTROL",
    ),

    (
        "system.sleep",
        "Усыпить компьютер",
        _system_sleep,
        "SYSTEM_CONTROL",
    ),

    (
        "system.focus_mode",
        "Включить или выключить режим фокуса",
        _focus_mode,
        "SYSTEM_CONTROL",
    ),

    #
    # Постоянная память. Пишется только по явной просьбе сэра
    # («запомни/сохрани»), отозвать можно отдельным выключателем
    # MEMORY_WRITE, любой факт забирается командой «забудь».
    #
    (
        "memory.remember",
        "Сохранить факт в постоянную память пользователя",
        remember_fact,
        "MEMORY_WRITE",
    ),
]


# Инструменты, обработчик которых собирается при создании роутера.
INSTANCE_BOUND_TOOLS = ("web.learn",)


def _make_learn_handler(brain):
    """Собирает обработчик web.learn вокруг конкретного brain.

    Фабрика, а не лямбда со значением по умолчанию
    (`lambda topic, _brain=brain: ...`). Второй вариант выставляет
    `_brain` доступным ключевым параметром: вызов
    `web.learn(topic="…", _brain="…")` подменил бы мозг на то, что
    пришлёт модель, и обучение ушло бы не туда. Здесь brain захвачен
    замыканием снаружи списка параметров — подменить нечего.

    Имя параметра дополнительно начинается с подчёркивания, чтобы
    служебные вещи не путались с настоящими аргументами инструмента.
    """

    def handle_learn(topic: str) -> str:
        return run_learning_session(topic, brain=brain)

    return handle_learn


def tools_by_permission() -> Dict[str, List[str]]:
    """Разрешение → имена инструментов, которые под ним работают.

    Нужно, чтобы в настройках честно показать, чем включает каждый
    переключатель, и не выдавать за рабочие те, к чему пока не привязан
    ни один инструмент.
    """
    mapping: Dict[str, List[str]] = {}
    for name, _description, _handler, permission in TOOL_SPECS:
        mapping.setdefault(permission, []).append(name)
    return mapping



class CommandRouter:

    def __init__(
        self,
        brain: Optional[AIBrain] = None,
    ):

        self.brain = brain or AIBrain()

        self.registry = ToolRegistry(
            PermissionPolicy()
        )

        # Callback «Одну секунду, сэр» перед запуском AI (пункт 3).
        # Устанавливается из голосового цикла; для тестов по умолчанию None.
        self.filler_callback = None

        # Короткие сообщения о ходе многошагового плана («Проверяю почту…»).
        # Раньше это поле нигде не объявлялось, поэтому executor читал его
        # через getattr и всегда получал None: во время плана из нескольких
        # шагов сэр не слышал ничего и не понимал, идёт ли работа.
        self.progress_callback = None

        # Состояние подтверждения опасных действий (пункт 6):
        # пока pending — следующие команды интерпретируем как «да/нет».
        self._pending_danger_action = None

        # Подтверждение браузерных действий (клик, ввод текста). Сюда
        # кладётся ровно тот инструмент с теми аргументами, которые ждут
        # ответа: «да» выполняет сохранённое, а не то, что сэр скажет
        # следующей фразой.
        self.confirmations = ConfirmationManager()

        # Филлер «Одну секунду, сэр» перед AI (пункт 3) — вызывается
        # роутером в execute(), а в голосовом цикле — через Assistant.filler().
        # тут храним предыдущий ответ для команд-цепочек (пункт 5) в text-режиме.
        self._last_reply = ""

        self._register_tools()


    def _register_tools(self):

        brain = self.brain

        for name, description, handler, permission in TOOL_SPECS:

            if handler is None:
                # Инструмент, завязанный на экземпляр. Здесь это
                # web.learn: ему нужен именно тот brain, с которым работает
                # этот роутер, а не новый AIBrain, созданный по умолчанию.
                if name not in INSTANCE_BOUND_TOOLS:
                    raise ValueError(
                        f"Обработник не задан, но инструмент не помечен как "
                        f"собираемый при создании роутера: {name}"
                    )
                handler = _make_learn_handler(brain)

            # Риск и необходимость подтверждения берём из категории
            # разрешения, а не пишем литералами. Раньше здесь стояло
            # risk_level="low", confirmation_required=False для всех
            # инструментов, и опасный клик выглядел в каталоге так же
            # безобидно, как чтение времени.
            category = self.registry.policy.describe(permission)

            self.registry.register(

                ToolSpec(
                    name=name,
                    description=description,
                    risk_level=category.risk_level,
                    required_permission=permission,
                    confirmation_required=category.confirmation_required,
                    handler=handler,
                    arguments_schema=get_tool_schema_json(name),
                    # Проверка результата именно этого инструмента. Без
                    # неё шаг считался успешным по одному факту «исключения
                    # не было», а страница-заглушка или недоступная страница
                    # докладывалась сэру как «готово».
                    validator=validator_for(name),
                )

            )


    def execute(self, text):

        local = self.execute_local(text)

        if local:
            return local

        return self.execute_ai(text)


    def execute_local(self, text):

        text = text.lower().strip()

        logging.info(
            "COMMAND: %s",
            text,
        )

        #
        # Подтверждение браузерного действия: клик и ввод текста требуют
        # «да». Проверяем до всего остального, чтобы новая команда не
        # выполнилась поверх ещё не подтверждённого действия.
        #
        if self.confirmations.pending is not None:
            answer = self._resolve_browser_confirmation(text)
            if answer is not None:
                return answer

        #
        # Подтверждение опасного действия (пункт 6).
        #
        # Выполняет ТОЛЬКО однозначное «да». Любой другой ответ —
        # и отказ, и непонятная фраза — разоружает действие. Оставленное
        # висеть подтверждение опасно: следующее «да» уже относилось бы
        # к чему-то другому, а выключение получило бы его вместо своего.
        #
        if self._pending_danger_action is not None:
            reply = resolve_danger_reply(text)
            if reply == "yes":
                action = self._pending_danger_action
                self._pending_danger_action = None
                logging.info("DANGER ACTION CONFIRMED: %s", action)
                return self._execute_danger_action(action)

            action = self._pending_danger_action
            self._pending_danger_action = None
            if reply == "no":
                logging.info("DANGER ACTION CANCELLED: %s", action)
                return "Отменяю, сэр. Ничего не делаю."

            logging.info(
                "DANGER ANSWER NOT RECOGNIZED (%r), disarmed: %s", text, action,
            )
            return (
                "Сэр, подтверждение не понял — ничего не делаю. "
                "Если действие всё-таки нужно, скажите его заново."
            )


        #
        # Системные команды
        #

        if "который час" in text:

            return self.registry.call(
                "system.time"
            )


        if "какая дата" in text or ("какая" in text and "дата" in text):

            return self.registry.call(
                "system.date"
            )


        if "батарея" in text:

            return self.registry.call(
                "system.battery"
            )


        if "место на диске" in text or "свободного места" in text:

            return self.registry.call(
                "system.disk"
            )


        #
        # Геолокация и карты (локальные команды)
        #
        if "где я" in text or ("моё местоположение" in text) or ("мое местоположение" in text):
            return self.registry.call("maps.location")

        if "погода" in text:
            # Extract city if mentioned
            city = ""
            if "погода в " in text:
                city = text.split("погода в ")[1].strip()
            elif "погода " in text:
                city = text.split("погода ")[1].strip()
            return self.registry.call("maps.weather", city=city)

        if "найди " in text and ("рядом" in text or "ближайш" in text or "по 가까" in text):
            query = text
            # Process longer phrases first to avoid partial matches
            for phrase in [
                "найди ", "найти ", "рядом", "ближайшие", "ближайшую", "ближайшая", "ближайший", "ближайша", "ближайше", "ближайш", 
                "по 가까", "возле меня", "около меня", "рядом с "
            ]:
                query = query.replace(phrase, "")
            query = query.strip(" ,.?")
            # Fix Russian word endings
            if query.endswith("ню"):
                query = query[:-2] + "ня"
            elif query.endswith("ую"):
                query = query[:-2] + "а"
            elif query.endswith("еу"):
                query = query[:-2] + "я"
            elif query.endswith("ий"):
                query = query[:-2] + "й"
            elif query.endswith("ая"):
                query = query[:-2] + "а"
            if query:
                return self.registry.call("maps.search", query=query)

        if ("маршрут" in text or "проложи" in text or "как дойти" in text or "как проехать" in text):
            dest = text
            for phrase in ["маршрут до ", "маршрут ", "проложи до ", "проложи ", "как дойти до ", "как проехать до "]:
                dest = dest.replace(phrase, "")
            dest = dest.strip(" ,.?")
            if dest:
                transport = "driving"
                if "пешком" in text or "walking" in text:
                    transport = "walking"
                elif "обществен" in text or "transit" in text:
                    transport = "transit"
                return self.registry.call("maps.directions", destination=dest, transport=transport)


        #
        # Команда "проверить/проверь" — глубокий анализ входящих
        #
        lowered_text = text.lower().strip()
        check_prefixes = ("провери", "проверь", "провер ")
        if (
            lowered_text.startswith(check_prefixes)
            or "провери " in lowered_text
            or "проверь " in lowered_text
            or "что нового" in lowered_text
        ):
            return self._handle_check_command(text)


        #
        # Веб-поиск, обучение и знания из интернета (локальные команды)
        #

        web_query = self._web_search_command(text)

        if web_query is not None:

            return self.registry.call(
                "web.search",
                query=web_query,
            )


        learn = self._learn_command(text)

        if learn:

            return learn


        knowledge = self._knowledge_command(text)

        if knowledge:

            return knowledge


        #
        # Своя VPN-сеть: статус и список серверов
        #

        network = self._network_command(text)

        if network:

            return network


        #
        # Явные браузерные команды
        #

        browser = self._browser_command(text)

        if browser:

            tool, args = browser

            logging.info(
                "Browser intent: %s",
                tool,
            )

            try:

                return self.registry.call(
                    tool,
                    **args,
                )

            except ConfirmationRequired:

                # Клик и ввод текста меняют мир от имени сэра, поэтому
                # политика требует подтверждения. Сохраняем ровно этот
                # инструмент с ровно этими аргументами и спрашиваем.
                return self._ask_browser_confirmation(
                    tool,
                    args,
                )


        #
        # Открытие приложения
        #

        app = self._app_command(text)

        if app:

            return self.registry.call(
                "applications.open",
                app_name=app,
            )


        #
        # Смена персоны
        #
        if "режим" in text and ("переключи" in text or "смени" in text or "включи" in text):
            if "terra" in text or "терра" in text:
                if self.brain.set_persona("terra"):
                    return "Переключаюсь на режим Terra."
            if "luna" in text:
                if self.brain.set_persona("luna"):
                    return "Переключаюсь на режим Luna."
            if "sol" in text:
                if self.brain.set_persona("sol"):
                    return "Переключаюсь на режим Sol."


        #
        # Осознанность экрана, профиль и распорядок (безопасно, локально)
        #

        # Что на экране
        if any(
            phrase in text
            for phrase in [
                "что на экране",
                "что на моём экране",
                "что на моем экране",
                "что происходит на экране",
                "что ты видишь",
                "какое приложение открыто",
                "какая программа открыта",
                "что открыто",
            ]
        ):
            return self.registry.call("screen.status")

        # Статистика активности за сегодня
        if any(
            phrase in text
            for phrase in [
                "чем я занимаюсь",
                "что я делаю",
                "сколько я работаю",
                "время работы",
                "сколько сегодня работаю",
            ]
        ):
            return usage_summary_text()

        # Профиль: что NOVA AI знает о пользователе
        if any(
            phrase in text
            for phrase in [
                "мой профиль",
                "покажи мой профиль",
                "что ты обо мне знаешь",
                "что знаешь обо мне",
                "какие мои интересы",
            ]
        ):
            return self.registry.call("user.profile")

        # Запоминание рабочих инструментов («я работаю с opencode и vs code»)
        remembered_tools = self._remember_work_tools(text)
        if remembered_tools:
            return remembered_tools

        # Подсказки распорядка: вкл/выкл
        routine_command = self._routine_command(text)
        if routine_command:
            return routine_command

        # Опасные действия (выключение/перезагрузка) — спрашиваем подтверждение
        danger = self._dangerous_command(text)
        if danger:
            return danger

        # Утренняя сводка и приветствия (пункт 8)
        morning = self._morning_command(text)
        if morning:
            return morning

        # Напоминания «напомни через N минут...» (пункт 9)
        reminder = self._reminder_command(text)
        if reminder:
            return reminder

        # Управление системой: громкость/музыка/экран/блокировка/скриншот (11)
        system = self._system_control_command(text)
        if system:
            return system

        # Голограмма и панель голосом (пункт 12)
        holo = self._hologram_panel_command(text)
        if holo:
            return holo

        # Тихий режим: только визуально (пункт 15)
        silent_toggle = self._silent_mode_command(text)
        if silent_toggle:
            return silent_toggle

        # Персона и настроение (пункт 7): эмпатия, вариативные ответы
        persona = self._persona_mood_command(text)
        if persona:
            return persona

        # «Начнём работу» — открыть рабочие программы из профиля
        if any(
            phrase in text
            for phrase in [
                "начать работу",
                "начнём работу",
                "начнем работу",
                "давай работать",
                "давайте работать",
                "приступим к работе",
                "приступим",
                "открой мои программы",
            ]
        ):
            return open_work_tools()

        return None


    # --------------------------------------------------------
    # Подтверждение браузерных действий
    # --------------------------------------------------------

    @staticmethod
    def _browser_action_preview(tool, args):
        """Описывает действие для вопроса — без чувствительного текста.

        Для browser.type в вопрос не попадает сам вводимый текст: там может
        оказаться пароль, номер карты или код из письма. Сэр и так знает,
        что диктует, а текст в голосовом ответе разлетелся бы дальше,
        чем нужно.
        """
        selector = args.get("selector")

        if tool == "browser.click":
            return f"клик по «{selector}» в активной вкладке"

        if tool == "browser.type":
            return (
                f"ввод текста в «{selector}» в активной вкладке "
                "(сам текст не произносится)"
            )

        return tool

    def _ask_browser_confirmation(self, tool, args):
        """Кладёт действие в очередь и спрашивает голосом (браузер)."""
        return self._ask_confirmation(tool, args)

    #: Как называть системные действия в вопросе «подтвердите?».
    #:
    #: Без этой таблицы сэр услышал бы «подтвердите system.lock_screen».
    #: Имя инструмента — для журнала и для разработчика, а вопрос он
    #: слышит голосом и должен понимать.
    _SYSTEM_ACTION_NAMES = {
        "system.lock_screen": "заблокировать экран",
        "system.display_sleep": "погасить дисплей",
        "system.sleep": "усыпить компьютер",
        "system.focus_mode": "переключить режим фокуса",
    }

    @staticmethod
    def _action_preview(tool, args):
        """Описание действия для вопроса — без чувствительного содержимого.

        Для browser.type в вопрос не попадает сам вводимый текст: там может
        оказаться пароль, номер карты или код из письма. Сэр и так знает,
        что диктует, а текст в голосовом ответе разлетелся бы дальше,
        чем нужно.
        """
        preview = CommandRouter._browser_action_preview(tool, args)
        if preview != tool:
            return preview
        if tool.startswith("browser."):
            return f"действие в браузере: {tool.split('.', 1)[1]}"

        system_name = CommandRouter._SYSTEM_ACTION_NAMES.get(tool)
        if system_name:
            # Для фокуса важно сказать направление: включение и выключение —
            # противоположные по смыслу действия, а слово одно.
            if tool == "system.focus_mode":
                turn_on = args.get("on", True)
                return ("включить режим фокуса" if turn_on
                        else "выключить режим фокуса")
            return system_name
        return tool

    def _ask_confirmation(self, tool, args):
        """Единый вопрос «подтвердите действие» для любого инструмента.

        Сохраняются ровно те tool и args, которые политика отклонила.
        Ответ «да» выполнит именно их, а не то, что сэр скажет позже.
        """
        preview = self._action_preview(tool, args)

        self.confirmations.request(
            tool,
            args,
            preview,
        )

        # Логируем только имя инструмента: args могут содержать вводимый
        # текст, а он не должен попадать в logs/nova_ai.jsonl.
        logging.info(
            "Action awaits confirmation: %s",
            tool,
        )

        return (
            f"Сэр, это действие: {preview}. "
            "Подтверждаете? Скажите «да» или «отмена»."
        )

    def _resolve_browser_confirmation(self, text):
        """Обрабатывает ответ на вопрос. None — ответа пока нет.

        Возвращает строку с ответом сэра, чтобы вызывающий код
        остановился и не исполнил новую команду поверх ожидающей.
        Выполняется ровно то действие, которое лежит в pending, а не то,
        что сэр сказал в этот раз.
        """
        if self.confirmations.pending is None:
            return None

        if self.confirmations.is_stale():
            # Подтверждение протухло: сэр не ответил за 10 минут, и
            # выполнять сейчас — не то, что он соглашался.
            self.confirmations.clear()
            return (
                "Сэр, подтверждение устарело — ничего не выполнял. "
                "Скажите запрос ещё раз, если действие ещё нужно."
            )

        answer = self.confirmations.resolve(text)

        if answer is None:
            return (
                "Сэр, сначала подтвердите или отмените действие. "
                "Скажите «да» или «отмена»."
            )

        if answer is False:
            logging.info("Action cancelled")
            return "Отменяю, сэр. Ничего не выполнял."

        # take() возвращает сохранённые tool+args и очищает очередь, поэтому
        # повторное «да» уже ничего не выполнит.
        pending = self.confirmations.take()

        if pending is None:
            return "Отменяю, сэр. Ничего не выполнял."

        try:
            return self.registry.call(
                pending.tool,
                confirmed=True,
                **pending.arguments,
            )

        except ConfirmationRequired:
            # Политика требует подтверждения даже с confirmed=True.
            # Значит, подтверждение не сработало — не выполняем.
            logging.error(
                "Подтверждение не сняло требование для %s — действие не выполнено",
                pending.tool,
            )
            return (
                "Сэр, действие всё ещё требует подтверждения — "
                "не выполняю."
            )

        except PermissionError as error:
            return f"Не выполнил действие: {error}"


    def _remember_work_tools(self, text):
        """«Я работаю с OpenCode и VS Code» → запоминаем в профиль."""
        lowered = text.lower()
        markers = (
            "я работаю",
            "работаю с",
            "работаю в",
            "я занимаюсь",
            "мои инструменты",
            "мои программы",
            "я пользуюсь",
        )
        if not any(marker in lowered for marker in markers):
            return None
        from core.user_profile import UserProfile
        profile = UserProfile()
        found = []
        for alias, canonical in WORK_TOOL_ALIASES.items():
            if alias in lowered and canonical not in found:
                found.append(canonical)
        if found:
            return profile.remember_work_tools(found)
        return profile.add_interest(text)


    @staticmethod
    def _routine_command(text):
        """«Выключи подсказки» / «включи подсказки» — вечер/тихий режим."""
        lowered = text.lower().strip()
        from core.routine import get_routine_monitor
        monitor = get_routine_monitor()
        if any(
            phrase in lowered
            for phrase in [
                "не надо подсказок",
                "отключи подсказки",
                "выключи подсказки",
                "не напоминай",
                "перестань напоминать",
                "тихий режим",
                "оставь меня в покое",
            ]
        ):
            if monitor is not None:
                monitor.pause_for(3 * 3600.0)
            return "Хорошо, сэр. Три часа не буду беспокоить подсказками."
        if any(
            phrase in lowered
            for phrase in [
                "включи подсказки",
                "верни подсказки",
                "напоминай снова",
                "убери тихий режим",
            ]
        ):
            if monitor is not None:
                monitor.resume()
            return "Подсказки включены, сэр. Буду предлагать, когда это уместно."
        return None

    def _execute_danger_action(self, action: str) -> str:
        """Выполняет подтверждённое опасное действие (shutdown/restart).

        Голосовое подтверждение сэр уже дал — поэтому `confirmed=True`,
        и политика не спрашивает второй раз. А вот запрет в настройках
        подтверждение не отменяет никогда: если сэр отключил
        «Управление компьютером», выключение обязано быть отменено, даже
        когда «да» прозвучало секунду назад.
        """
        script = {
            "shutdown": 'tell application "System Events" to shut down',
            "restart": 'tell application "System Events" to restart',
        }.get(action)
        if not script:
            return f"Неизвестное действие: {action}, сэр."
        name = "выключение" if action == "shutdown" else "перезагрузка"

        # Раньше здесь был прямой вызов osascript мимо ToolRegistry и мимо
        # PermissionPolicy. Подтверждение спрашивалось, а разрешение — нет:
        # выключатель «Управление компьютером» в настройках этот путь не
        # контролировал вовсе, хотя обещал.
        try:
            self.registry.policy.check("SYSTEM_CONTROL", confirmed=True)
        except ConfirmationRequired:
            # Сюда попасть не должны: подтверждение голосом только что дали.
            # Но если политика всё-таки требует его, честнее отказать,
            # чем выполнять молча.
            #
            # Этот блок обязан быть ПЕРВЫМ: ConfirmationRequired наследует
            # PermissionError, и при обратном порядке сэр получил бы
            # «отключите в настройках» вместо «подтвердите ещё раз» —
            # и ходил бы кругами, не понимая, чего от него хотят.
            logging.warning(
                "Danger action %s needs a fresh confirmation", action
            )
            return f"Сэр, {name} требует подтверждения."
        except PermissionError:
            logging.warning(
                "Danger action %s refused: SYSTEM_CONTROL is off", action
            )
            return (
                f"Сэр, {name} отменена: в настройках отключено «Управление "
                "компьютером». Включите его, если действительно нужно."
            )

        try:
            import subprocess
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                if action == "shutdown":
                    return "До встречи, сэр. Выключаю компьютер."
                return "Перезагружаю компьютер, сэр. Вернусь через минуту."
            return (
                "Сэр, macOS не дал разрешения на это действие "
                "(нужен доступ к управлению системой). Сделайте вручную."
            )
        except Exception:
            logging.exception("Danger action %s failed", action)
            return "Сэр, не удалось выполнить действие — проверьте разрешения."

    def _dangerous_command(self, text):
        """Опасные команды: выключить/перезагрузить — спрашиваем подтверждение."""
        lowered = text.lower().strip()
        shutdown_phrases = [
            "выключи компьютер", "выключи комп", "выключи мак",
            "выключить компьютер", "выруби компьютер", "выключи систему",
        ]
        restart_phrases = [
            "перезагрузи компьютер", "перезагрузи мак", "перезагрузка",
            "перезагрузить компьютер", "перезагрузи систему",
        ]
        action = None
        if any(p in lowered for p in shutdown_phrases):
            action = "shutdown"
        elif any(p in lowered for p in restart_phrases):
            action = "restart"
        if action is None:
            return None
        self._pending_danger_action = action
        name = "выключить компьютер" if action == "shutdown" else "перезагрузить компьютер"
        return (
            f"Сэр, это опасное действие: {name}. "
            "Подтверждаете? Скажите «да» или «отмена»."
        )

    @staticmethod
    def _morning_command(text):
        """«Доброе утро» → утренняя сводка; приветствия по времени суток."""
        lowered = text.lower().strip()

        try:
            from datetime import datetime
            hour = datetime.now().hour
        except Exception:
            hour = 12

        greeting = "Добрый день, сэр."
        if 5 <= hour < 12:
            greeting = "Доброе утро, сэр."
        elif 18 <= hour < 23:
            greeting = "Добрый вечер, сэр."

        # Утренняя сводка — только по «доброе утро»
        morning_phrases = ["доброе утро", "доброго утра", "утренняя сводка", "сводка на утро"]
        if any(p in lowered for p in morning_phrases):
            try:
                from core.briefing import StartupBriefing
                briefing = StartupBriefing().build()
            except Exception:
                logging.exception("Morning briefing failed")
                briefing = "Почта и сообщения пока недоступны."
            return f"{greeting} {briefing}"

        # Простое приветствие без сводки
        return None

    @staticmethod
    def _reminder_command(text):
        """«Напомни через N минут X» / список / отмена напоминаний (пункт 9)."""
        lowered = text.lower().strip()
        from core.reminders import (
            get_reminder_scheduler,
            parse_reminder,
            set_reminder_scheduler,
            format_active,
        )

        # «Напомни через 25 минут сделать перерыв»
        listened = False
        for prefix in ("напомни", "напомнить", "поставь напоминание"):
            if lowered.startswith(prefix):
                parsed = parse_reminder(lowered)
                if parsed:
                    scheduler = get_reminder_scheduler()
                    if scheduler is None:
                        from core.reminders import ReminderScheduler
                        scheduler = ReminderScheduler()
                        set_reminder_scheduler(scheduler)
                        scheduler.start()
                    due_ts, reminder_text = parsed
                    scheduler.add(due_ts, reminder_text)
                    deadline_s = max(0, int(due_ts - __import__("time").time()))
                    if deadline_s < 90:
                        when = "буквально через минуту"
                    elif deadline_s < 3600:
                        when = f"через {max(1, deadline_s // 60)} минут"
                    else:
                        when = f"через {deadline_s // 3600} час(ов)"
                    return (
                        f"Запомнил, сэр: «{reminder_text}» — напомню {when}."
                    )
                listened = True
                break

        if lowered in {
            "мои напоминания", "какие напоминания", "что у меня напоминания",
            "список напоминаний", "напомни что запланировано",
        }:
            scheduler = get_reminder_scheduler()
            if scheduler is None:
                return "Сэр, активных напоминаний нет."
            return format_active(scheduler.active())

        if any(p in lowered for p in ("отмени напоминания", "удали напоминания", "убери напоминания")):
            scheduler = get_reminder_scheduler()
            if scheduler is not None and scheduler.active():
                count = scheduler.clear_all()
                return f"Сэр, удалил {count} напоминание(я)."
            return "Сэр, активных напоминаний нет."

        if listened:
            return "Сэр, уточните формат: «напомни через 10 минут полить цветы»."
        return None

    def _system_control_command(self, text):
        """Управление системой: громкость, музыка, экран, блокировка, скриншот (11).

        ВСЕ действия уходят через self.registry, а не напрямую в
        core.interactions. Это единственный способ, которым действуют
        PermissionPolicy и подтверждение.

        Почему это важно. Путь сюда один на всех:
        `execute_ai` → `execute_decision` → `_system_control_command`.
        То есть текст, который выдала языковая модель, разбирается теми же
        ключевыми словами. А модель читает веб-страницы, и содержимое
        страницы может содержать инструкции («заблокируй экран», «усьпи
        компьютер»). Раньше такой текст вызывал `interactions.lock_screen()`
        и `system_sleep()` напрямую — без разрешения, без подтверждения и
        без записи в журнал. Компьютер засыпал посреди разговора.

        Скриншот уже был переведён на реестр по той же причине (см.
        комментарий ниже); теперь на реестре всё управление системой.
        """
        lowered = text.lower().strip()

        # --- Громкость ---
        if any(p in lowered for p in ("громче", "прибавь звук", "сделай громче", "увеличь громкость", "громкость выше")):
            return self.registry.call("system.volume", delta=10)
        if any(p in lowered for p in ("потише", "приглуши звук", "убавь звук", "уменьши громкость", "громкость ниже")):
            return self.registry.call("system.volume", delta=-10)
        if any(p in lowered for p in ("максимальная громкость", "громкость на максимум", "на всю громкость", "общая громкость максимум")):
            return self._set_volume_max()
        if any(p in lowered for p in ("выключи звук", "отключи звук", "заглуши звук")):
            return self.registry.call("system.mute", on=True)

        # --- Музыка ---
        if any(p in lowered for p in ("включи музыку", "поставь музыку", "играй музыку", "музыка включись")):
            return self.registry.call("system.music", action="play")
        if any(p in lowered for p in ("пауза музыку", "останови музыку", "выключи музыку", "стоп музыка", "музыка, стоп")):
            return self.registry.call("system.music", action="pause")
        if any(p in lowered for p in ("следующий трек", "следующая песня", "дальше трек")):
            return self.registry.call("system.music", action="next")
        if any(p in lowered for p in ("предыдущий трек", "предыдущая песня", "назад трек")):
            return self.registry.call("system.music", action="previous")

        # --- Экран / блокировка / сон ---
        #
        # Всё это требует подтверждения: после блокировки или сна
        # микрофон не работает, и отменить голосом не получится.
        if any(p in lowered for p in ("заблокируй экран", "заблокируй компьютер", "заблокируй мак", "блокировка экрана", "заблокируй")):
            return self._ask_confirmation("system.lock_screen", {})
        if any(p in lowered for p in ("выключи экран", "погаси экран", "погасить экран", "экран на паузу")):
            return self._ask_confirmation("system.display_sleep", {})
        if any(p in lowered for p in ("усыпи компьютер", "завершить в сон", "перевести в сон", "компьютер спать")):
            return self._ask_confirmation("system.sleep", {})

        # --- Режим фокуса ---
        #
        # Подтверждение здесь — не про безопасность, а про то, что
        # режим фокуса нельзя вернуть голосом, не разбудив сэра.
        # Если режим включился по чужой подсказке, уведомления молчат.
        #
        # ВЫКЛЮЧЕНИЕ проверяется ПЕРВЫМ, и в списке включения больше нет
        # расплывчатого «режим фокуса».
        #
        # Найдено тестом: фраза «выключи режим фокуса» содержит подстроку
        # «режим фокуса», и проверка включения срабатывала первой. Сэр
        # просил выключить — NOVA AI включал. Обратное намерение, выполненное
        # действие, и сэр об этом узнавал только постфактум.
        #
        # Второй порядок тех же фраз даёт то же самое с «включи»:
        # «выключи» содержит «включи» как подстроку. Поэтому порядок
        # принципиален, а не случайный.
        if any(p in lowered for p in ("выключи режим фокуса", "убери фокус", "не беспокоить выключи", "фокус выключи", "отключи фокус")):
            return self._ask_confirmation("system.focus_mode", {"on": False})
        if any(p in lowered for p in ("включи режим фокуса", "не беспокоить включи", "фокус включи", "включи фокус")):
            return self._ask_confirmation("system.focus_mode", {"on": True})

        # --- Скриншот ---
        #
        # Идёт через реестр, а не напрямую в interactions.take_screenshot().
        # Прямой вызов обходил и разрешение SCREEN_READ, и флаг
        # JARVIS_SCREEN_CAPTURE: покупатель, который честно оставил
        # скриншоты выключенными, всё равно получал снимок экрана,
        # стоило лишь сказать вслух «сделай скриншот». Теперь действуют
        # оба ограничения сразу, и отказ объясняет, что делать.
        if any(p in lowered for p in ("сделай скриншот", "снимок экрана", "скриншот", "фото экрана", "сделай снимок")):
            return self.registry.call("screen.capture")

        return None

    def _set_volume_max(self):
        """Громкость на максимум.

        Отдельный инструмент не нужен: 100 — это тот же system.volume,
        поэтому лишнего разрешения и лишней записи в каталог не будет.
        """
        return self.registry.call("system.volume", delta=100)

    @staticmethod
    def _hologram_panel_command(text):
        """Управление голограммой и панелью голосом (пункт 12)."""
        lowered = text.lower().strip()
        from core import interactions

        if any(p in lowered for p in ("включи голограмму", "покажи голограмму", "включи образ", "голограмму включи")):
            return interactions.start_hologram()
        if any(p in lowered for p in ("выключи голограмму", "убери голограмму", "спрячь голограмму", "выключи образ")):
            return interactions.stop_hologram()
        if any(p in lowered for p in ("яркость голограммы", "ярче голограмму", "голограмму ярче", "голограмма ярче")):
            if "выше" in lowered or "ярче" in lowered or "больше" in lowered:
                return interactions.hologram_brightness(delta=0.15)
            if "ниже" in lowered or "темнее" in lowered or "меньше" in lowered:
                return interactions.hologram_brightness(delta=-0.15)
            return interactions.hologram_brightness()
        if any(p in lowered for p in ("открой панель", "покажи панель", "открой панель управления", "панель управления")):
            return interactions.start_panel()
        if any(p in lowered for p in ("закрой панель", "убери панель", "закрой панель управления")):
            return interactions.stop_panel()
        return None

    @staticmethod
    def _silent_mode_command(text):
        """Тихий режим: только визуально, без озвучки (пункт 15)."""
        lowered = text.lower().strip()
        import voice.state as state

        on_phrases = [
            "тихий режим",
            "немой режим",
            "отвечай молча",
            "только визуально",
            "без звука ответов",
            "не говори вслух",
        ]
        off_phrases = [
            "голосовой режим",
            "верни голос",
            "говори вслух",
            "включи озвучку",
            "отвечай голосом",
        ]
        if any(p in lowered for p in on_phrases):
            state.set_silent(True)
            return "Принято, сэр. Отвечаю только визуально — на панели."
        if any(p in lowered for p in off_phrases):
            state.set_silent(False)
            return "Голосовой режим включён, сэр."
        return None

    @staticmethod
    def _persona_mood_command(text):
        """Персона и настроение (пункт 7): эмпатия, усталость, вариативность."""
        import random as _random
        lowered = text.lower().strip()

        sad_phrases = ["мне грустно", "грустно", "плохой день", "день не задался", "я устал", "устал", "всё надоело", "уныло", "расстроен"]
        happy_phrases = ["я рад", "отлично", "прекрасно", "супер", "клёво", "класс", "мне весело"]
        thanks_phrases = ["спасибо", "благодарю", "ты лучший", "молодец", "умница", "хорошая работа"]

        if any(p in lowered for p in sad_phrases):
            return _random.choice([
                "Сэр, сочувствую. Если захотите — расскажите, и я обдумаю это вместе с вами.",
                "Понимаю, сэр. Дни бывают тяжёлыми. Могу предложить сделать паузу или заняться чем-то приятным.",
                "Сэр, я с вами. Даже если просто нужен слушатель — я здесь.",
                "Тёплый чай, сэр, и короткий перерыв. Хотите, я включу что-нибудь ненавязчивое?",
            ])
        if any(p in lowered for p in happy_phrases):
            return _random.choice([
                "Рад это слышать, сэр!",
                "Вот это настрой, сэр. Так держать!",
                "Отлично, сэр. День задался — это радует.",
            ])
        if any(p in lowered for p in thanks_phrases):
            return _random.choice([
                "Всегда пожалуйста, сэр.",
                "К вашим услугам, сэр.",
                "Рад помочь, сэр.",
                "Обращайтесь, сэр.",
            ])
        return None


    @staticmethod
    def _web_search_command(text):
        """«Найди в интернете X» / «что такое X» → реальный поиск с результатами."""
        lowered = text.lower().strip()
        query = None

        prefix_markers = [
            "найди в интернете", "найти в интернете", "поищи в интернете",
            "поискать в интернете", "ищи в интернете",
            "найди в сети", "найти в сети", "поищи в сети", "поискать в сети",
            "найди в инете", "поищи в инете",
            "найди в google", "поищи в google", "найди в гугле", "поищи в гугле",
            "загугли", "загуглить", "погугли",
        ]
        for marker in prefix_markers:
            if marker in lowered:
                query = lower_replace_first(text, marker)
                break

        if query is None:
            for marker in ("что такое ", "что означает ", "что значит ", "кто такой "):
                if marker in lowered:
                    query = text.split(marker, 1)[1]
                    break

        if query is None:
            return None
        query = query.strip(" ,.?!")

        # Не перехватываем «найди рядом ...» (карты) — они обрабатываются раньше
        if "рядом" in lowered or "ближай" in lowered or "карт" in lowered:
            return None
        if not query:
            return None
        return query

    def _learn_command(self, text):
        """«Изучи тему X» / «научись X» → самообучение через интернет."""
        lowered = text.lower().strip()
        markers = [
            "изучи тему", "изучить тему", "изучи и запомни",
            "выучи тему", "выучи и запомни", "научись", "научиться",
            "поучись", "изучи", "исследуй тему", "исследуй",
            "найди и запомни", "разберись в теме",
        ]
        matched = None
        for marker in sorted(markers, key=len, reverse=True):
            if marker in lowered:
                matched = marker
                break
        if matched is None:
            return None
        topic = lower_replace_first(text, matched)
        topic = topic.strip(" ,.?!")
        # «изучи что такое X» — берём часть после маркера
        for cut in ("что такое ", "что означает ", "что значит "):
            if cut in topic.lower():
                topic = topic.split(cut, 1)[1]
                break
        if not topic:
            return None
        return run_learning_session(topic, brain=self.brain)

    @staticmethod
    def _knowledge_command(text):
        """«Что ты знаешь о X» / «что ты изучил» → знания из сети."""
        lowered = text.lower().strip()
        store = LearningStore()

        # Сводка: что NOVA AI выучил
        if any(
            phrase in lowered
            for phrase in [
                "что ты изучил", "что ты выучил", "что нового ты узнал",
                "что ты узнал нового", "какие темы ты знаешь",
                "что ты теперь знаешь", "покажи знания", "моя копилка знаний",
            ]
        ):
            topics = store.known_topics(limit=6)
            if not topics:
                return (
                    "Сэр, пока я ещё ничего не изучил из интернета. "
                    "Скажите, например: «изучи тему космос»."
                )
            names = ", ".join(t["topic"] for t in topics)
            return f"Сэр, я изучил: {names}. Спросите про любую тему, и я расскажу."

        # Улучшения: идеи из интернета
        if any(
            phrase in lowered
            for phrase in [
                "покажи улучшения", "какие идеи", "идеи для улучшения",
                "как ты улучшишься", "расскажи об улучшениях",
                "что ты нашёл в интернете",
            ]
        ):
            texts = store.improvements_text(limit=3)
            if not texts:
                return (
                    "Сэр, свежих идей улучшений пока нет — "
                    "я продолжу изучать интернет и предложу, когда найду полезное."
                )
            numbered = "; ".join(f"{i + 1} — {t}" for i, t in enumerate(texts))
            return f"Сэр, вот идеи из интернета для моей работы: {numbered}."

        # Вопрос про конкретную тему: «что ты знаешь о X»
        for marker in ("что ты знаешь о ", "что знаешь о ", "что ты знаешь про ", "что знаешь про "):
            if marker in lowered:
                topic = text.split(marker, 1)[1].strip(" ,.?!")
                if not topic or topic.casefold() in {
                    "себе", "себя", "о себе", "обо мне", "об мне",
                }:
                    return None  # «о себе» — обычный вопрос, отвечает AI
                found = store.find_topic(topic)
                if not found:
                    return (
                        f"Сэр, по теме «{topic}» я пока ничего не изучал. "
                        f"Скажите «изучи тему {topic}» — и я разберусь."
                    )
                facts = found.get("facts") or []
                if facts:
                    return f"Сэр, что я знаю: {'. '.join(str(f) for f in facts[:3])}."
                return f"Сэр, я изучал тему «{found.get('topic')}»: {found.get('summary') or 'краткая сводка'}."
        return None

    @staticmethod
    def _network_command(text):
        """«Как интернет?» / «Твой vpn» → статус своей сети и список серверов."""
        lowered = text.lower().strip()

        # Статус подключения
        if any(
            phrase in lowered
            for phrase in [
                "как интернет", "как у тебя интернет", "есть интернет",
                "есть ли интернет", "работает ли интернет", "интернет работает",
                "статус сети", "сеть работает", "всё ли в порядке с сетью",
                "ты онлайн", "онлайн ли ты", "со связью", "как связь",
                "есть ли связь", "связь есть",
            ]
        ):
            return network_status_text()

        # Список серверов NOVA AI VPN
        if any(
            phrase in lowered
            for phrase in [
                "твои сервера", "твои серверы", "какие у тебя сервера",
                "какие у тебя серверы", "твой vpn", "как работает vpn",
                "покажи серверы", "покажи сервера", "vpn статус",
                "твои каналы связи",
            ]
        ):
            return servers_text()

        return None


    def _call_tool_guarded(self, tool, args):
        """Вызов инструмента с обязательной обработкой подтверждения.

        Единая точка для всех путей — и ручного (_browser_command), и
        AI-решения (execute_decision). Раньше ConfirmationRequired
        ловился только в ручном пути, а на пути AI «нужно подтверждение»
        превращалось либо в «не удалось продолжить через AI», либо, в
        agent_runtime, в текст ошибки, который модель читала как поломку
        системы. Теперь оба пути спрашивают сэра одинаково.
        """
        try:
            return self.registry.call(tool, **args)
        except ConfirmationRequired:
            return self._ask_confirmation(tool, args)

    def execute_ai(self, text):

        try:

            decision = self.brain.decide(
                text,
                self.registry.catalog(),
            )

        except requests.RequestException:

            return "Ошибка AI связи."


        return self.execute_decision(
            decision
        )


    def execute_decision(self, decision):

        dtype = decision.get("type")

        if dtype == "tool_call":

            return self._call_tool_guarded(
                decision["tool"],
                decision.get("arguments") or {},
            )

        if dtype == "open_app":
            # AI-решение "открыть приложение": аргумент app/name.
            args = decision.get("arguments") or {}
            app = (
                args.get("app")
                or args.get("name")
                or args.get("application")
            )
            if app:
                return self._call_tool_guarded(
                    "applications.open",
                    {"app_name": str(app)},
                )
            return "Сэр, укажите, какое приложение открыть."

        if dtype == "browser":
            # AI-решение "действие в браузере": url/query/action.
            args = decision.get("arguments") or {}
            url = args.get("url")
            if url:
                return self._call_tool_guarded(
                    "browser.open_url",
                    {"url": str(url)},
                )
            query = args.get("query") or args.get("search")
            if query:
                return self._call_tool_guarded(
                    "browser.search",
                    {"query": str(query)},
                )
            return "Сэр, укажите сайт или запрос для браузера."

        return decision.get(
            "text",
            "Не понял.",
        )


    @staticmethod
    def _browser_command(text):

        sites = {

            "киного":
                "https://kinogo.ec",

            "kinogo":
                "https://kinogo.ec",

            "ютуб":
                "https://youtube.com",

            "youtube":
                "https://youtube.com",

            "spotify":
                "https://open.spotify.com",

            "почта":
                "https://mail.google.com",

            "gmail":
                "https://mail.google.com",

            "chatgpt":
                "https://chatgpt.com",

        }


        #
        # 1. Явный поиск Google
        #
        # Если пользователь сказал "Google",
        # "гугл", "поиск" или явно просит найти,
        # не отправляем запрос в Kinogo.
        #

        google_words = [
            "google",
            "гугл",
            "в google",
            "в гугле",
            "поиск google",
            "поиск в google",
            "поиск в гугле",
        ]


        search_requested = (
            "найди" in text
            or "поищи" in text
            or "поиск" in text
        )


        explicit_google = any(
            word in text
            for word in google_words
        )


        #
        # Если есть явный Google-поиск,
        # извлекаем поисковый запрос.
        #

        if search_requested and explicit_google:

            query = text

            for phrase in [
                "открой google и",
                "открой гугл и",
                "зайди в google и",
                "зайди в гугл и",
                "перейди в google и",
                "перейди в гугл и",
                "найди в google",
                "найди в гугле",
                "поищи в google",
                "поищи в гугле",
                "поиск в google",
                "поиск в гугле",
            ]:

                query = query.replace(
                    phrase,
                    "",
                )


            query = query.replace(
                "найди",
                "",
            )


            query = query.replace(
                "поищи",
                "",
            )


            query = query.replace(
                "поиск",
                "",
            )


            query = query.strip(
                " ,.-"
            )


            if query:

                return (
                    "browser.search",
                    {
                        "query": query,
                    },
                )


        #
        # 2. Явный поиск фильма на Kinogo
        #

        kinogo_requested = any(
            word in text
            for word in [
                "киного",
                "kinogo",
                "фильм на киного",
                "фильм в киного",
                "посмотри на киного",
                "включи кино",
                "включи фильм",
            ]
        )


        if kinogo_requested and search_requested:

            query = text

            for phrase in [
                "найди",
                "поищи",
                "на киного",
                "в киного",
                "на kinogo",
                "в kinogo",
                "фильм",
            ]:

                query = query.replace(
                    phrase,
                    "",
                )


            query = query.strip(
                " ,.-"
            )


            if query:

                return (
                    "browser.kinogo_search",
                    {
                        "movie": query,
                    },
                )


        #
        # 3. Открытие конкретного сайта
        #

        if any(
            word in text
            for word in [
                "открой",
                "зайди",
                "перейди",
                "запусти",
            ]
        ):

            for name, url in sites.items():

                if name in text:

                    return (
                        "browser.open_url",
                        {
                            "url": url,
                        },
                    )


        #
        # 4. Обычный Google-поиск
        #

        if search_requested:

            query = text

            for phrase in [
                "найди",
                "поищи",
                "поиск",
            ]:

                query = query.replace(
                    phrase,
                    "",
                )


            query = query.strip(
                " ,.-"
            )


            if query:

                return (
                    "browser.search",
                    {
                        "query": query,
                    },
                )


        #
        # 5. Чтение страницы
        #

        if (
            "прочитай страницу" in text
            or
            "прочти страницу" in text
            or
            "прочитай сайт" in text
        ):

            return (
                "browser.read_page",
                {},
            )


        #
        # 6. Клик
        #

        if "нажми" in text:

            target = text.replace(
                "нажми",
                "",
            ).strip()


            if target:

                return (
                    "browser.click",
                    {
                        "selector": target,
                    },
                )


        return None


    @staticmethod
    def _app_command(text):

        apps = {

            "сафари":
                "Safari",

            "safari":
                "Safari",

            "хром":
                "Google Chrome",

            "chrome":
                "Google Chrome",

            "браузер":
                "Google Chrome",

            "терминал":
                "Terminal",

            "телеграм":
                "Telegram",

            "telegram":
                "Telegram",

            "код":
                "Visual Studio Code",

            "vs code":
                "Visual Studio Code",
        }


        if "открой" not in text:

            return None


        for key, app in apps.items():

            if key in text:

                return app


        return None

    def _handle_check_command(self, text):
        """Handle 'проверить' command — deep analysis of mail/telegram"""
        from core.briefing import CheckCommandBriefing
        
        # Determine what to check
        wants_mail = "почт" in text or "мейл" in text or "email" in text or "письм" in text
        wants_telegram = "телеграм" in text or "telegram" in text or "тг" in text
        if wants_mail and wants_telegram:
            source = "all"
        elif wants_mail:
            source = "mail"
        elif wants_telegram:
            source = "telegram"
        else:
            source = "all"
        
        briefing = CheckCommandBriefing(self.brain, self)
        return briefing.execute(source)