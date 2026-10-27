#!/usr/bin/env python3
"""
NOVA AI — Enhanced Voice AI Assistant
Main entry point with improved startup, monitoring, and error handling.
"""
import atexit
import logging
import os
import random
import sys
import tempfile
import time
from datetime import datetime

import voice.state as state

from config import (
    INPUT_MODE,
    JARVIS_PERSONA,
    JARVIS_VPN_GEO_AUTO,
    JARVIS_VPN_GEO_TIMEOUT,
    STT_LANGUAGE,
    STT_MIC_INDEX,
    STT_WAKE_WORD,
)
from core.agent_runtime import AgentRuntime
from core.assistant import Assistant
from core.briefing import StartupBriefing
from core.commands import CommandRouter
from core.health import run_all_checks
from core.lifecycle import LIFECYCLE
from core.single_instance import SingleInstanceGuard
from core.task_monitor import init_task_monitor, get_task_monitor
from voice.text_to_speech import ElevenLabsTTS, FishTTS

#: Бюджет остановки всех подсистем. Должен быть меньше времени, за
#: которое stop_nova_ai.sh переходит к kill -9 (10 секунд), иначе
#: супервизор убьёт процесс на середине аккуратной остановки.
SHUTDOWN_BUDGET = 8.0

#: Удержание единственного экземпляра. Объект живёт до конца main().
_INSTANCE_GUARD = SingleInstanceGuard()


def get_greeting() -> str:
    """Time-aware greeting — с вариантами, чтобы не звучать как запись."""
    hour = datetime.now().hour
    if 5 <= hour < 12:
        options = [
            "Доброе утро, сэр. С возвращением.",
            "Утро доброе, сэр. Рад снова слышать вас.",
            "Сэр, доброе утро. Чем начнём день?",
            "Доброе утро, сэр. Всё под контролем.",
        ]
    elif 12 <= hour < 18:
        options = [
            "Добрый день, сэр. С возвращением.",
            "Сэр, добрый день. Отличного продолжения дня.",
            "Добрый день, сэр. Готов к работе.",
            "Хорошего дня, сэр. С возвращением.",
        ]
    elif 18 <= hour < 23:
        options = [
            "Добрый вечер, сэр. С возвращением.",
            "Сэр, добрый вечер. Как прошёл день?",
            "Добрый вечер, сэр. У вас всё под контролем.",
            "Вечер добрый, сэр. Снова к делу?",
        ]
    else:
        options = [
            "Доброй ночи, сэр. С возвращением.",
            "Сэр, уже поздно. Чем могу помочь?",
            "Доброй ночи, сэр. Берегите себя.",
        ]
    return random.choice(options)


def is_interrupt_command(text: str) -> bool:
    """Check for speech interruption commands"""
    command = text.lower().strip()
    # «замолчи» ловим и целиком, и как подстроку слов «замолчите», «замолчать»
    if "замолчи" in command or "замолкни" in command:
        return True
    words = [
        "стоп", "хватит", "отмена", "пауза", "тише", "тихо",
        "достаточно", "прекрати", "перестань", "заткнись",
        "молчи", "стоп сэр", "хватит сэр",
    ]
    return any(word in command for word in words)


def is_task_cancel_command(text: str) -> bool:
    """Отмена идущей задачи, а не только озвучки.

    «стоп» глушит речь — это barge-in и он должен работать всегда.
    Здесь же phrases, которыми сэр обрывает именно работу: после них
    runtime перестаёт выполнять шаги, а не просто замолкает.
    """
    command = text.lower().strip()
    phrases = (
        "остановись", "остановись сэр", "прекрати задачу", "отмени задачу",
        "отмени всё", "отмени все", "хватит делать", "не надо делать",
        "стоп задача", "стоп задачу", "брось задачу", "забудь задачу",
    )
    return any(phrase in command for phrase in phrases)


def stop_speech(assistant: Assistant):
    """Interrupt current speech"""
    try:
        assistant.stop_speaking()
        state.stop_speaking()
        logging.info("NOVA AI speech interrupted by user")
    except Exception:
        logging.exception("Speech stop error")


def cancel_task(assistant: Assistant) -> None:
    """Останавливает текущую задачу агента, если она идёт.

    Отдельно от stop_speech: озвучка и работа — разные вещи. Если
    задачи не было, сэру не говорится ничего лишнего.
    """
    try:
        cancel = getattr(assistant.runtime, "cancel_task", None)
        if not cancel:
            return
        answer = cancel()
        if answer:
            assistant.speak(answer)
    except Exception:
        logging.exception("Отмена задачи не удалась")


def is_task_status_command(text: str) -> bool:
    """Вопрос о ходе дела, а не новая задача.

    Отдельная ветка нужна затем, что «что с задачей?» не должно
    уходить ни в AI, ни в планировщик: это вопрос о том, что уже
    происходит, и ответ на него — из реестра.
    """
    command = text.lower().strip()
    phrases = (
        "что с задачей", "что с задачами", "статус задачи", "статус задач",
        "как задача", "как дела с задачей", "как идёт задача",
        "как идут дела", "что делаешь", "чем занят", "чем занимаешься",
        "какая задача", "какие задачи", "сколько осталось", "сколько готово",
        "progress", "task status",
    )
    return any(phrase in command for phrase in phrases)


def answer_task_status(assistant: Assistant) -> None:
    """Отвечает сэру голосом, что происходит с задачами."""
    try:
        answer = getattr(assistant.runtime, "task_status_answer", None)
        if not answer:
            return
        assistant.speak(answer())
    except Exception:
        logging.exception("Не удалось сообщить о задачах")


def setup_logging():
    """Enhanced logging with structured output and rotation"""
    from logging.handlers import RotatingFileHandler

    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

    class JSONFormatter(logging.Formatter):
        def format(self, record):
            import json
            log_obj = {
                "ts": self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
                "module": record.module,
                "func": record.funcName,
                "line": record.lineno,
            }
            if record.exc_info:
                log_obj["exc"] = self.formatException(record.exc_info)
            return json.dumps(log_obj, ensure_ascii=False)

    # Console: human-readable
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))

    # File: JSONL with rotation (10MB, 5 files)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "nova_ai.jsonl"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(JSONFormatter())

    # Error file: only ERROR+
    error_handler = RotatingFileHandler(
        os.path.join(log_dir, "nova_ai-errors.jsonl"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(JSONFormatter())

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers = [console_handler, file_handler, error_handler]


def _probe_microphone() -> tuple:
    """Проверяет доступность микрофона, не оставляя занятых устройств.

    Возвращает (ok, сообщение).

    Почему нельзя просто создать распознаватель
    -------------------------------------------
    Так делалось раньше: `SpeechRecognitionSTT()` открывал поток CoreAudio на
    выбранном устройстве и **никогда не закрывался**. Дальше голосовой цикл
    создавал второй распознаватель на том же микрофоне, а на macOS два
    одновременных потока на устройстве дают «cannot do in current context» —
    то есть проверка готовности сама ломала то, что проверяла. Плюс панель и
    `probe_mic_level` тоже видели занятое устройство.

    Теперь поток гарантированно закрывается в `finally`, а если микрофон уже
    открыт кем-то ещё — новый поток не создаётся вовсе, берётся уровень из
    существующего.

    Как определяется «микрофон недоступен»
    --------------------------------------
    Через `startup_step`, который по замыслу **не выпускает исключение наружу**
    (это его главная задача: отказ подсистемы не должен ронять NOVA AI). Поэтому
    спрашивать `except` здесь бессмысленно — он не сработает никогда, и сводка
    готовности радостно написала бы «Микрофон OK» при отсутствующем доступе.
    Итог берём из статуса подсистемы: `startup_step` записал в него и факт
    отказа, и текст ошибки.
    """
    from core.lifecycle import STATUS_OK
    from voice.speech_to_text import active_mic_level

    # Уже открытый поток: не создаём второй, просто подтверждаем живость.
    _rms, mic_index = active_mic_level()
    if mic_index is not None:
        return True, f"режим голосового ввода (вход {mic_index})"

    recognizer = None
    try:
        with LIFECYCLE.startup_step(
            "Микрофон", description="готовность голосового ввода", quiet=True,
        ) as mic_step:
            from voice.speech_to_text import SpeechRecognitionSTT

            recognizer = SpeechRecognitionSTT(
                use_wake_word=STT_WAKE_WORD,
                language=STT_LANGUAGE,
                mic_index=STT_MIC_INDEX or None,
            )
        if mic_step.status != STATUS_OK:
            return False, mic_step.error or "микрофон недоступен"
        return True, "режим голосового ввода"
    finally:
        # Освобождаем устройство, даже если проверка упала на середине.
        if recognizer is not None:
            try:
                recognizer.close()
            except Exception:
                logging.debug("Не удалось закрыть проверочный поток микрофона",
                              exc_info=True)


def _print_readiness(health: dict) -> None:
    """Компактная сводка готовности к демонстрации (печатается при старте)."""
    def row(name, ok, msg):
        mark = "OK" if ok else "✗"
        return f"  {name:<14} {mark:<3} {msg}"

    print("\n" + "=" * 50)
    print("  ГОТОВНОСТЬ К ДЕМО")
    print("=" * 50)
    ai = health.get("ai", {})
    print(row("AI", ai.get("ok"), ai.get("message", "")))
    from tools.telegram_bot import is_configured as _bot_configured
    from tools.telegram_bot import BOT_ENABLED as _bot_enabled
    if _bot_enabled and _bot_configured():
        print(row("Telegram-бот", True, "автоответы от имени владельца"))
    elif _bot_enabled:
        print(row("Telegram-бот", False, "нет токена (вкладка «Доступы и данные»)"))
    else:
        print(row("Telegram-бот", False, "выключен в настройках"))
    el = health.get("elevenlabs", {})
    fish = health.get("fish", {})
    if fish.get("ok"):
        print(row("Голос", True, "Fish.audio (голос NOVA AI)"))
    else:
        print(row("Голос", el.get("ok"), el.get("message", "")))
    tg = health.get("telegram", {})
    print(row("Telegram", tg.get("ok"), tg.get("message", "")))
    ch = health.get("chrome", {})
    print(row("Chrome", ch.get("ok"), ch.get("message", "")))

    if INPUT_MODE == "voice":
        mic_ok, mic_msg = _probe_microphone()
        print(row("Микрофон", mic_ok, mic_msg))
        if not mic_ok:
            print(
                "  → при старте выполнится автоматический\n"
                "    переход на текстовый ввод"
            )
    else:
        print("  Микрофон         —   текстовый режим (JARVIS_INPUT_MODE=text)")

    if not ai.get("ok"):
        print("-" * 50)
        print(
            "  Главное для демо: AI_API_KEY в файле .env —\n"
            "  https://console.groq.com/keys (Create API Key)"
        )
    print("=" * 50 + "\n")


def _claim_single_instance() -> bool:
    """Забирает право быть единственным экземпляром NOVA AI.

    Второй экземпляр означал бы два микрофона на одном устройстве
    (второй получит -10863 и оглохнет), двойную запись в состояние
    Telegram и в логи, и два набора напоминаний. Возвращаем False —
    вызывающий код решит, что сказать.
    """
    if not _INSTANCE_GUARD.acquire():
        logging.error("Второй экземпляр NOVA AI: %s", _INSTANCE_GUARD.owner_summary())
        print(
            "\n  ⚠️  " + _INSTANCE_GUARD.owner_summary() + "\n"
            "     Второй NOVA AI запускать не нужно: два процесса делят\n"
            "     микрофон и пишут в одни и те же файлы состояния.\n"
            "     Остановите первый (./stop_nova_ai.sh) или закройте панель.\n",
            file=sys.stderr,
        )
        return False
    # Страховка на случай выхода в обход finally: блокировку снимет
    # ядро, но явная отмена делает порядок явным.
    atexit.register(_INSTANCE_GUARD.release)
    return True


def _bootstrap_tts():
    """Голос NOVA AI: Fish.audio → ElevenLabs → macOS say.

    Ошибка здесь недопустима: без голоса NOVA AI всё равно должен
    работать (текстовый режим, панель), поэтому последний рубеж —
    системный синтезатор, который не требует ни ключа, ни сети.
    """
    from voice.text_to_speech import MacOSTTS

    for factory, label in ((FishTTS, "Fish.audio"), (ElevenLabsTTS, "ElevenLabs")):
        try:
            return factory()
        except Exception as error:
            logging.warning("Голос %s недоступен (%s)", label, error)
    try:
        return MacOSTTS()
    except Exception as error:
        logging.error("Системный голос недоступен (%s)", error)
        return None


def main():
    # Initialize logging first: everything after this point is logged.
    setup_logging()
    logging.info("=== NOVA AI STARTING ===")

    # Один экземпляр на машину. Проверка идёт до всего, что занимает
    # ресурсы: второй процесс не должен даже открыть микрофон.
    if not _claim_single_instance():
        return 2

    # Страховка на любой выход, включая SystemExit из обработчика
    # сигнала: освобождаем микрофон, озвучку и фоновые подсистемы.
    atexit.register(_shutdown_everything)

    # Обработчики сигналов ставим ДО всего, что занимает ресурсы.
    #
    # Почему именно здесь, а не рядом с фоновыми подсистемами
    # -------------------------------------------------------
    # Раньше обработчики ставились поздно — уже после VPN, health-проверок и
    # подбора голоса. Замерено на реальном запуске: от старта процесса до
    # установки обработчика проходит **13 секунд**. Любой SIGTERM в это окно
    # обрабатывался ядром по умолчанию, то есть процесс умирал мгновенно:
    # без единой строки в логе, без раскрутки подсистем, без возврата
    # подменённых переменных прокси. Именно это окно — самое частое место
    # для Ctrl+C: пользователь ждёт приветствия и отменяет запуск.
    #
    # Теперь весь запуск защищён: SystemExit раскручивается через
    # `finally` в main() и через atexit-регистрацию `_shutdown_everything`.
    setup_signal_handlers()

    # Временные файлы — после логирования, чтобы уборка была видна.
    cleanup_temp_files()

    # Initialize task monitor
    init_task_monitor("logs/tasks")

    # Собственный VPN-сервис подключается РАНЬШЕ health-проверок:
    # иначе AI/поиск «не доступны» (а проверки часто видят сеть без туннеля,
    # а Gemini ещё и геоблокирует запросы не из США/EU).
    #
    # Важно: при сбое vpn обязан стать connected=False. Раньше стоял
    # дефолт {"connected": True}, который в except не менялся, — и при
    # упавшем VPN пользователю говорилось, что всё в порядке, хотя сети
    # не было.
    vpn = {"connected": False, "message": "Работаю в локальном режиме."}
    with LIFECYCLE.startup_step(
        "VPN", description="собственный канал связи",
    ) as vpn_step:
        from core.vpn_service import (
            discover_ai_channel,
            ensure_connectivity,
            get_geo_auto,
            restore_original_proxy_env,
            set_geo_auto,
        )
        # Авто-подбор канала для онлайн-AI (собственный VPN, страны вне СНГ):
        # если через обычные каналы Gemini гео-заблокирован — NOVA AI сам
        # найдёт рабочий прокси вне СНГ и применит его до приветствия.
        set_geo_auto(JARVIS_VPN_GEO_AUTO)
        connected = ensure_connectivity(timeout=None)
        if get_geo_auto():
            geo_vpn = discover_ai_channel(timeout=JARVIS_VPN_GEO_TIMEOUT)
            if geo_vpn is not None:
                logging.info("NOVA AI VPN | geo-канал: %s", geo_vpn.get("server_name"))
                connected = geo_vpn
        vpn = connected
        # Останавливать туннель не нужно: это переменные окружения, а не
        # процесс. Но вернуть их пользователю обязательно — иначе после выхода
        # из NOVA AI всё запущенное (панель, afplay, curl) продолжит ходить
        # через прокси, которого уже не существует.
        vpn_step.stop = restore_original_proxy_env
        logging.info("NOVA AI VPN | соединение=%s", vpn.get("connected"))

    # Health checks (теперь — через подключённый канал VPN).
    # Не фатальны: подсказывают состояние, но не решают, запускаться ли.
    logging.info("Running health checks...")
    health = _run_health_checks_safely()

    # Читаемая сводка готовности для демо
    _print_readiness(health)

    # Initialize TTS: голос NOVA AI (Fish.audio) → ElevenLabs → macOS say
    speaker = _bootstrap_tts()

    # Initialize Assistant with enhanced runtime
    router = CommandRouter()
    runtime = AgentRuntime(router)
    assistant = Assistant(speaker=speaker)
    # Replace runtime with enhanced version
    assistant.router = router
    assistant.runtime = runtime
    # Озвучка — первая подсистема в порядке остановки: её останавливаем
    # последней, иначе фоновые помощники будут говорить в закрытый канал.
    LIFECYCLE.register(
        "озвучка", stop=assistant.stop_speaking,
        description="Fish/ElevenLabs/системный голос",
    )

    # Филлер «Одну секунду, сэр» перед AI-ответом (пункт 3).
    # Хук вызывается внутри AgentRuntime перед brain.decide.
    try:
        router.filler_callback = assistant.filler
        # В голосовом режиме озвучиваем филлер; в текстовом — достаточно панели.
        assistant.say_filler = INPUT_MODE == "voice"
    except Exception:
        logging.exception("Не удалось подключить филлер")

    # Ход работы по многошаговому плану. Без него executor молчал, и сэр
    # во время плана не слышал ничего: не было понятно, завис NOVA AI или
    # работает.
    try:
        router.progress_callback = assistant.speak
    except Exception:
        logging.exception("Не удалось подключить сообщения о ходе работы")

    # Обработчики сигналов уже стоят (см. начало main): весь запуск,
    # а не только работа фоновых подсистем, защищён от SIGTERM/SIGHUP.

    # Напоминания (пункт 9): планировщик в фоне, озвучка через Assistant.
    with LIFECYCLE.startup_step(
        "Напоминания", description="по расписанию",
    ) as reminders_step:
        from core.reminders import (
            ReminderScheduler,
            set_reminder_scheduler,
        )
        reminder_scheduler = ReminderScheduler(speak=assistant.speak)
        set_reminder_scheduler(reminder_scheduler)
        reminder_scheduler.start()
        reminders_step.stop = reminder_scheduler.stop

    # Startup greeting + briefing
    startup = get_greeting()
    briefing = StartupBriefing().build()
    if briefing:
        startup += " " + briefing

    # Если AI недоступен — коротко сообщаем, чтобы не молчать в важных случаях
    try:
        ai_online = router.brain.is_available()
    except Exception:
        ai_online = False
    if not ai_online:
        startup += (
            " Внимание: сейчас работаю в локальном режиме без онлайн-AI "
            "и отвечу сам, но глубокий анализ займу. "
            "Собственный VPN уже автоматически ищет рабочий канал вне СНГ; "
            "если хотите ускорить — добавьте рабочий ключ Groq в Настройках."
        )

    # Если сеть не поднялась — честно говорим об этом сразу (локальный режим),
    # чтобы NOVA AI не молчал и пользователь понимал, как он сейчас работает.
    if not vpn.get("connected"):
        startup += " " + vpn.get("message", "Работаю в локальном режиме.")

    # Персональное предложение при старте — в интересах пользователя.
    # (Для этого пользователя: начать работу в OpenCode и VS Code.
    #  Для остальных — NOVA AI вежливо спросит, с чем они работают, и запомнит.)
    try:
        from config import STARTUP_SUGGEST
        if STARTUP_SUGGEST:
            from core.user_profile import UserProfile
            suggestion = UserProfile().startup_suggestion()
            if suggestion:
                startup += " " + suggestion
    except Exception:
        logging.exception("Стартовое предложение не удалось сформировать")

    assistant.speak(startup)
    logging.info("NOVA AI started | mode=%s | persona=%s | ai_online=%s", INPUT_MODE, JARVIS_PERSONA, ai_online)

    # Распорядок дня: вежливые подсказки по времени (безопасно, локально).
    with LIFECYCLE.startup_step(
        "Распорядок дня", description="подсказки по времени",
    ) as routine_step:
        from core.routine import RoutineMonitor, set_routine_monitor
        from core.user_context import get_tracker
        monitor = RoutineMonitor(
            tracker=get_tracker(),
            profile=UserProfile(),
            speak=assistant.speak,
        )
        set_routine_monitor(monitor)
        monitor.start()
        routine_step.stop = monitor.stop

    # Самообучение: фоновый поиск идей улучшений в интернете (безопасно, локально).
    with LIFECYCLE.startup_step(
        "Самообучение", description="фоновый поиск идей",
    ) as learning_step:
        from core.self_learning import SelfImprover, LearningStore, set_self_improver
        improver = SelfImprover(
            brain=router.brain,
            store=LearningStore(),
            speak=assistant.speak,
        )
        set_self_improver(improver)
        improver.start()
        learning_step.stop = improver.stop

    # VPN-монитор: автофейловер при потере/возврате сети (сам, тихо).
    with LIFECYCLE.startup_step(
        "VPN-монитор", description="автопереподключение",
    ) as vpn_monitor_step:
        from core.vpn_service import VPNMonitor, set_vpn_monitor, ensure_servers_file
        ensure_servers_file()
        monitor_vpn = VPNMonitor()
        set_vpn_monitor(monitor_vpn)
        monitor_vpn.start()
        vpn_monitor_step.stop = monitor_vpn.stop

    # Фоновый Telegram-бот (автоответы, когда владелец занят).
    # Недоступность Telegram не должна мешать основному NOVA AI.
    with LIFECYCLE.startup_step(
        "Telegram-бот", description="автоответы в фоне",
    ) as bot_step:
        from tools.telegram_bot import start_bot_if_configured, stop_bot
        start_bot_if_configured()
        bot_step.stop = stop_bot

    _report_degraded_subsystems(assistant)

    # Main loop
    try:
        if INPUT_MODE == "text":
            run_text_loop(assistant)
        else:
            run_voice_loop(assistant)
    finally:
        _shutdown_everything()


def _run_health_checks_safely() -> dict:
    """Проверки готовности. Никогда не роняют запуск.

    Проверки ходят в сеть и в систему, поэтому любая из них может
    зависнуть или упасть. Раньше они выполнялись прямо в main() без
    защиты: сбой одной проверки ронял весь NOVA AI, хотя проверки —
    чисто диагностические.
    """
    try:
        health = run_all_checks()
    except Exception:
        logging.exception("Проверки готовности не завершились — продолжаем")
        return {"_overall": False, "_error": True}

    if not health.get("_overall"):
        failed = [k for k, v in health.items() if k != "_overall" and not v.get("ok")]
        logging.warning("Health checks failed: %s", failed)
    else:
        logging.info("All health checks passed")
    return health


def _report_degraded_subsystems(assistant) -> None:
    """Говорит вслух о том, что не поднялось.

    Раньше ошибка старта подсистемы уходила только в лог, а сэр
    слышал обычное «Доброе утро» и не понимал, почему Telegram молчит
    или голос другой. Молчание об отказе подсистемы — это тоже отказ.
    """
    degraded = LIFECYCLE.degraded()
    if not degraded:
        return
    names = ", ".join(item["name"] for item in degraded)
    logging.warning("Запущены с ограничениями: %s", names)
    try:
        assistant.speak(
            f"Сэр, внимание: {names} сейчас недоступно. "
            "Остальное работает как обычно."
        )
    except Exception:
        logging.exception("Не удалось сообщить о деградации")


#: Остановка уже отработала — второй выход не пишет её заново.
_shutdown_done = False


def _shutdown_everything() -> None:
    """Останавливает все подсистемы. Идемпотентно.

    Вызывается ДВАЖДЫ: из `finally` в main() и из atexit. Путь выхода может
    быть любым — нормальный выход, SystemExit из обработчика сигнала,
    необработанное исключение, — и закрыть дыру одним `finally` нельзя.

    Сама остановка идемпотентна (защищена флагом в Lifecycle), но этого мало:
    строка `=== NOVA AI STOPPED ===` печаталась дважды, и по журналу было
    невозможно понять, какой из двух остановов настоящий. Поэтому весь вывод
    тоже защищён флагом — при любом выходе остановка видна ровно один раз.
    """
    global _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True

    report = LIFECYCLE.shutdown_all(timeout=SHUTDOWN_BUDGET)
    # Микрофон и флаг озвучки снимаем последними: они освобождают
    # устройство, которым пользуются и фоновые подсистемы.
    try:
        state.stop_speaking()
    except Exception:
        logging.exception("Не удалось снять флаг озвучки")
    if report:
        logging.info("Остановлено: %s", ", ".join(report))
    logging.info("=== NOVA AI STOPPED ===")


def run_text_loop(assistant: Assistant):
    """Text input loop with enhanced UX"""
    print("\n" + "=" * 50)
    print("NOVA AI Text Mode — Type 'выход' to quit")
    print("=" * 50 + "\n")

    while True:
        try:
            text = input("Вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not text:
            continue

        if state.is_speaking:
            if is_interrupt_command(text):
                stop_speech(assistant)
            continue

        command = text.lower()
        if command in {"выход", "exit", "quit", "завершить"}:
            assistant.speak("Завершаю работу, сэр.")
            break

        # Handle special commands
        if command in {"статус", "status", "метрики", "metrics"}:
            monitor = get_task_monitor()
            metrics = monitor.get_metrics()
            print(f"\n📊 Metrics: {metrics}\n")
            continue

        if command in {"история", "history"}:
            monitor = get_task_monitor()
            for t in monitor.get_history(10):
                print(f"  {t.task_id}: {t.goal[:50]}... [{t.status.value}] {t.total_duration_ms}ms")
            continue

        if command in {"отчет", "report"}:
            monitor = get_task_monitor()
            path = monitor.export_report()
            print(f"\n📄 Report saved to: {path}\n")
            continue

        # Тот же вопрос о задачах, что и в голосе, — ответ тот же.
        if is_task_status_command(text):
            answer = getattr(assistant.runtime, "task_status_answer", None)
            print(f"  {answer() if answer else 'Сэр, активных задач нет.'}")
            continue

        assistant.handle(text)


def _presence_phrase(rms: float) -> str:
    """Что сказать, когда NOVA AI долго не слышит речи.

    rms ниже порога «живого» микрофона — значит поток слышит ровно тишину:
    почти наверняка микрофон переключён в mute (клавиша на клавиатуре,
    Control Center) или отключилась гарнитура. Спрашивать в этом случае
    «вы ушли?» бессмысленно — подсказываем причину. Иначе (звук идёт, но
    фраза не разобралась) — вежливый вопрос «вы ещё со мной?».
    """
    from voice.speech_to_text import MIC_DEAD_LEVEL

    if rms < MIC_DEAD_LEVEL:
        # Ровно тишина бывает и просто потому, что сэр не говорит, поэтому
        # не ставим диагноз, а даём подсказку и оставляем выбор за ним.
        return random.choice(
            [
                "Сэр, я вас не слышу. Если говорите — проверьте, не "
                "отключён ли микрофон, пожалуйста.",
                "Сэр, у меня тихо. Если вы говорите, а я молчу — включите "
                "микрофон, и я снова готов.",
            ]
        )
    return random.choice(
        [
            "Сэр, вы ещё здесь?",
            "Сэр, вы тут? Или вы уже ушли?",
            "Сэр, я вас не разобрал — вы ещё со мной?",
        ]
    )


def run_voice_loop(assistant: Assistant):
    """Voice input loop with wake word support"""
    from voice.speech_to_text import (
        MIC_DEAD_LEVEL,
        MicUnavailable,
        SpeechRecognitionSTT,
    )

    # Страховка от «залипшей» озвучки: если флаг is_speaking не снимется
    # (упал TTS-поток), голосовой цикл слушал бы только перебивание и оглох
    # бы навсегда. Снимаем флаг принудительно.
    SPEAKING_WATCHDOG = 120.0
    # Пересоздание микрофона: пауза растёт, но не бесконечно (иначе
    # «микрофон недоступен» превращается в молчание без объяснений).
    mic_backoff = 5.0

    def _create_recognizer(attempts: int = 3, mic_index=None):
        """Создаёт распознаватель.

        mic_index передаётся только для первой попытки: если выбранный ранее
        вход исчез (отключилась гарнитура), следующие попытки ищут микрофон
        заново. Это и быстрее, и бережнее к CoreAudio: не перебираем все
        входы при каждом пересоздании.
        """
        nonlocal mic_backoff
        for attempt in range(1, attempts + 1):
            try:
                logging.info(
                    "Инициализация микрофона (попытка %s/%s, вход %s)",
                    attempt,
                    attempts,
                    mic_index if attempt == 1 else "авто",
                )
                stt = SpeechRecognitionSTT(
                    use_wake_word=STT_WAKE_WORD,
                    language=STT_LANGUAGE,
                    mic_index=mic_index if attempt == 1 else None,
                )
                mic_backoff = 5.0
                return stt
            except Exception as error:
                logging.error("Микрофон недоступен: %s", error)
                if attempt < attempts:
                    time.sleep(3)
        return None

    def _drop_recognizer(current):
        """Освобождает поток микрофона перед пересозданием.

        Поток микрофона теперь постоянный (без переподключения раз в 8 с),
        поэтому старый экземпляр обязательно нужно закрыть — иначе на том же
        устройстве останется висящий CoreAudio-поток.
        """
        if current is None:
            return None
        try:
            current.close()
        except Exception:
            logging.debug("Recognizer close failed", exc_info=True)
        return None

    recognizer = None
    printed_wake_hint = False
    silent_streak = 0
    # После долгого молчания NOVA AI не «жалуется» на микрофон, а вежливо
    # спрашивает, на месте ли сэр («Сэр, вы ещё здесь?»). Повторяем не чаще
    # раза в 30 минут, чтобы не надоедать: фраза уместна только когда микрофон
    # реально молчит (никто не говорит), а не когда звук идёт.
    # Отсчёт начинаем от старта, иначе вопрос «вы тут?» прозвучал бы уже
    # через минуту после запуска — нелепо и пугающе.
    PRESENCE_CHECK_COOLDOWN = 30 * 60
    last_presence_check = time.time()
    # Пункт 1: после presence-вопроса (или «разбуди» с панели) следующий
    # распознаватель должен принять ответ без слова-будильника.
    arm_after_recreate = False
    # Пункт 16: «Разбудить» с панели — не обрабатываем один и тот же файл повторно.
    last_processed_wake_ts = 0.0
    # Микрофон упал не из-за тишины — об этом уже сказали, молчащий цикл
    # не должен снова и снова «спрашивать, вы тут?».
    mic_broken_notified = False
    # Микрофон открыт, но молчит: предупреждаем один раз, а не на каждом
    # пересоздании, и отмечаем, когда звук вернулся.
    mic_was_silent = False
    # Сколько раз подряд пересоздавали поток из-за тишины: по этой счётке
    # растёт пауза между попытками (1 → 2 → 4 минуты).
    mic_dead_rounds = 0
    # Какой вход микрофона уже работает: переиспользуем его при
    # пересоздании, чтобы не трогать лишние устройства.
    last_mic_index = None

    from voice.ipc import last_wake_ts as ipc_last_wake_ts, read_silent_file
    from voice.state import is_sleeping, is_quiet_hour, set_sleeping, set_silent

    while True:
        try:
            # Микрофон может ломаться (занят другим приложением, спит,
            # разблокирован доступ) — не роняем цикл, а пересоздаём.
            if recognizer is None:
                recognizer = _create_recognizer(mic_index=last_mic_index)
                silent_streak = 0
                if recognizer is None:
                    if not mic_broken_notified:
                        mic_broken_notified = True
                        print("🎤 Микрофон недоступен.")
                        print(
                            "🔁 Проверьте доступ к микрофону: Системные настройки → "
                            "Конфиденциальность и безопасность → Микрофон. "
                            "Повторяю попытки автоматически."
                        )
                        assistant.speak(
                            "Сэр, микрофон мне недоступен. Разрешите доступ "
                            "к микрофону, и я продолжу."
                        )
                    # Пробуем снова позже, а не прыгаем в текстовый режим:
                    # сэр может просто временно занять микрофон другим
                    # приложением (звонок, видео).
                    time.sleep(mic_backoff)
                    mic_backoff = min(mic_backoff * 1.5, 60.0)
                    continue
                mic_broken_notified = False
                last_mic_index = recognizer.mic_index
                if arm_after_recreate:
                    # Пункт 1: после «Сэр, вы ещё здесь?» ждём ответ без wake word.
                    recognizer.arm_without_wake()
                    arm_after_recreate = False

            # Подсказка для демо: как пользоваться голосовым режимом
            if not printed_wake_hint:
                print(
                    "🎤 Голосовой режим: скажите «Джарвис, ...» — "
                    "например, «Джарвис, который час»."
                )
                printed_wake_hint = True

            # Пункт 15: тихий режим с панели (тумблер пишет jarvis_silent.ctl) —
            # применяем на каждом витке, и он живёт независимо от команд.
            try:
                silent_from_file = read_silent_file()
                if silent_from_file is not None:
                    set_silent(silent_from_file)
            except Exception:
                logging.exception("Silent-file poll failed")

            # Пока NOVA AI говорит — ловим перебивание голосом (barge-in).
            # Пользователь может сказать «стоп», «замолчи», «хватит» —
            # и NOVA AI немедленно замолчит, а следующую фразу примет
            # даже без слова-будильника.
            if state.is_speaking:
                # Озвучка не должна длиться вечно: иначе голосовой цикл
                # навсегда остался бы в режиме «слушаю только перебивание».
                talking_for = state.speaking_for()
                if talking_for > SPEAKING_WATCHDOG:
                    logging.warning(
                        "Озвучка длится %.0fс — останавливаю принудительно",
                        talking_for,
                    )
                    stop_speech(assistant)
                    state.stop_speaking()
                    continue
                spoken = recognizer.listen_for_interrupt()
                if spoken and is_interrupt_command(spoken):
                    logging.info("NOVA AI перебит голосом: «%s»", spoken)
                    stop_speech(assistant)
                    recognizer.arm_without_wake()
                continue

            text = recognizer.listen()
            if not text:
                # Всё, что ниже — самодиагностика «немого цикла». Если NOVA AI
                # молчит, но вы говорите — не должно выглядеть «он не отвечает»:
                # при серии пустых сессий проверяем реальный уровень звука,
                # пересоздаём распознаватель (оживляет «замёрзший» после сна
                # Mac аудио-поток) и ВСЛУХ сообщаем, что не слышим.
                silent_streak += 1
                if silent_streak >= 8:
                    # Уровень берём из уже открытого потока микрофона —
                    # второй параллельный поток на том же устройстве на
                    # macOS ломает CoreAudio («cannot do in current context»).
                    rms = recognizer.mic_level()
                    seconds = silent_streak * recognizer.wake_word_timeout
                    if rms >= MIC_DEAD_LEVEL:
                        # Звук идёт: вероятно, не расслышали фразу (шумный канал
                        # или слово-будильник не сказано). Тихо продолжаем.
                        if mic_was_silent:
                            logging.info(
                                "Микрофон ожил: звук пошёл (RMS=%.0f)", rms
                            )
                            mic_was_silent = False
                        silent_streak = 0
                        mic_dead_rounds = 0
                        continue
                    # Поток открыт, но звука нет. Первую попытку оживить его
                    # делаем быстро (через минуту) — вдруг поток «замёрз» после
                    # сна Mac. Если и после пересоздания тишина, значит микрофон
                    # действительно отключён/заглушен: дальше ждём всё реже
                    # (2, 4, 4… минут), чтобы не дёргать CoreAudio впустую.
                    if silent_streak < min(8 * 2 ** min(mic_dead_rounds, 2), 32):
                        continue
                    streak = silent_streak
                    silent_streak = 0
                    mic_dead_rounds += 1
                    if not mic_was_silent:
                        mic_was_silent = True
                        logging.warning(
                            "Звука нет (RMS=%.1f, ~%ds тишины) — освежаю "
                            "поток микрофона",
                            rms, seconds,
                        )
                    # Старый поток закрываем ДО создания нового, иначе
                    # на микрофоне останется висящий CoreAudio-поток.
                    recognizer = _drop_recognizer(recognizer)

                    now = time.time()
                    # В режиме сна («спокойной ночи») и в тихие часы не спрашиваем
                    # «Сэр, вы ещё здесь?» — не беспокоим.
                    if (
                        now - last_presence_check >= PRESENCE_CHECK_COOLDOWN
                        and not is_sleeping()
                        and not is_quiet_hour()
                    ):
                        last_presence_check = now
                        assistant.speak(_presence_phrase(rms))
                        # Пункт 1: следующий ответ примем без слова-будильника.
                        arm_after_recreate = True
                continue

            silent_streak = 0

            command = text.lower().strip()
            # «Спокойной ночи» — НЕ завершает цикл: NOVA AI уходит в спящий режим
            # и просыпается по «доброе утро» (пункт 2 и «доброе утро» → сводка).
            if command == "спокойной ночи":
                set_sleeping(True)
                assistant.speak(
                    "Спокойной ночи, сэр. Я на связи — "
                    "скажите «доброе утро», и я проснусь."
                )
                continue
            if command in {"выход", "завершить работу", "закройся"}:
                assistant.speak("Завершаю работу, сэр.")
                # Освобождаем микрофон: иначе процесс не даст завершиться
                # или следующий запуск столкнётся с занятым устройством.
                _drop_recognizer(recognizer)
                return

            # «Доброе утро» будит NOVA AI и даёт утреннюю сводку.
            if "доброе утро" in command or "доброго утра" in command:
                set_sleeping(False)
                assistant.handle(text)
                continue

            if is_interrupt_command(command):
                stop_speech(assistant)
                # «Стоп» глушит голос. Если при этом идёт задача —
                # она тоже останавливается, иначе сэр продолжал бы
                # слышать работу после команды «хватит».
                if is_task_cancel_command(command):
                    cancel_task(assistant)
                continue

            # «Что с задачей?» — вопрос о текущем деле. Отвечаем из
            # реестра, не отправляя фразу в AI: иначе сэру пришлось бы
            # ждать ответа провайдера ради сведений, которые уже есть.
            if is_task_status_command(command):
                answer_task_status(assistant)
                continue

            assistant.handle(text)

            # Пункт 16: «Разбудить» с панели (или горячая клавиша) — после
            # ответа можно сказать следующую фразу без слова-будильника.
            try:
                wake_ts = ipc_last_wake_ts()
                if wake_ts and wake_ts > last_processed_wake_ts:
                    last_processed_wake_ts = wake_ts
                    recognizer.arm_without_wake()
                    if not is_sleeping():
                        assistant.speak("Сэр, я слушаю.")
                    else:
                        set_sleeping(False)
                        assistant.speak(
                            "Сэр, с добрым утром. Я слушаю вас."
                        )
            except Exception:
                logging.exception("Wake-file poll failed")

        except MicUnavailable as error:
            # Поток микрофона отвалился (устройство усыпили/отобрали).
            # Это не «пользователь молчит» — молчащий цикл тут неуместен.
            logging.warning("Поток микрофона прерван: %s", error)
            recognizer = _drop_recognizer(recognizer)
            silent_streak = 0
            time.sleep(2)
        except Exception:
            logging.exception("Voice loop error")
            # Сбой распознавания — освобождаем микрофон и пересоздаём
            # распознаватель на следующем проходе.
            recognizer = _drop_recognizer(recognizer)
            time.sleep(3)


#: Сколько мегабайтов максимум держим каждый «ротящийся» лог. Старше и
#: крупнее — удаляем. Файлы самого NOVA AI не трогаем: сейчас пишет.
_LOG_MEGABYTES_MAX = 16.0
#: Сколько суток хранить журнал диалогов. Диалог — это личные данные сэра,
#: и копить их годами незачем; но свежий контекст нужен для отладки.
_DIALOG_RETENTION_DAYS = 30


def _rotate_logs_by_age(
    directory: str,
    pattern: str,
    max_megabytes: float,
    max_age_days: float = _DIALOG_RETENTION_DAYS,
) -> list:
    """Удаляет крупные и старые ротированные логи. Возвращает удалённые пути.

    Отдельная функция, а не три строки в `cleanup_temp_files`, потому что
    правило «что можно удалять» должно быть одно и проверяемое тестом.
    Удаляются ТОЛЬКО файлы по маске и ТОЛЬКО в указанной папке: журнал
    разговора пользователя — не наш файл, чтобы удалить.

    Файл удаляется, если он ЛИБО крупнее `max_megabytes`, ЛИБО старше
    `max_age_days`. Два независимых предела нужны потому, что лог может
    быть и старым, но мелким (редкий NOVA AI), и свежим, но огромным
    (цикл ошибок за одну ночь).
    """
    import glob

    cutoff = time.time() - max_age_days * 86400
    size_limit = max_megabytes * 1024 * 1024
    removed = []
    for path in glob.glob(os.path.join(directory, pattern)):
        if not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if not (stat.st_size > size_limit or stat.st_mtime < cutoff):
            continue
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            # Файл мог быть занят или уже удалён другим процессом.
            continue
    return removed


def cleanup_temp_files():
    """Уборка временных файлов при старте.

    Раньше здесь был один `glob("jarvis_mic.wav")` — файла с таким именем
    нигде не создаётся (микрофон пишет через `tempfile`), поэтому уборка
    всегда была пустой, а реальные временные файлы — mp3 озвучки, скриншоты,
    снимки экрана на рабочий стол — копились месяцами и занимали сотни
    мегабайт.

    Ничего чужого не удаляем: только файлы, которые NOVA AI сам создаёт и
    знает по имени.
    """
    import glob

    cleaned = 0

    # 1. Временные файлы озвучки и микрофона.
    #    Имена соответствуют tempfile-шаблонам в voice/ и tools/.
    for tmp in glob.glob(os.path.join(tempfile.gettempdir(), "jarvis_*.mp3")):
        cleaned += _remove_quietly(tmp, "временная озвучка")
    for tmp in glob.glob(os.path.join(tempfile.gettempdir(), "jarvis_tts_*.mp3")):
        cleaned += _remove_quietly(tmp, "временная озвучка")
    for tmp in glob.glob(os.path.join(tempfile.gettempdir(), "jarvis_mic*.wav")):
        cleaned += _remove_quietly(tmp, "временная запись микрофона")

    # 2. Скриншоты: наш каталог и рабочий стол (их делает tools/screenshot
    #    по команде, и пользователь часто забывает про них).
    for shot in glob.glob(os.path.join("data", "screenshots", "*.png")):
        cleaned += _remove_quietly(shot, "скриншот")
    desktop = os.path.expanduser("~/Desktop")
    if os.path.isdir(desktop):
        for shot in glob.glob(os.path.join(desktop, "jarvis_*.png")):
            cleaned += _remove_quietly(shot, "скриншот с рабочего стола")

    # 3. Служебные файлы дымового теста: результат одноразовый, старый —
    #    только вводит в заблуждение при разборе состояния системы.
    cleaned += _remove_quietly("data/_smoke_test.json", "результат дымового теста")

    # 4. Ротированные логи: крупные и старые. Текущие не трогаем.
    for pattern, name in (
        ("nova_ai.jsonl.*", "ротированный лог"),
        ("nova_ai-errors.jsonl.*", "ротированный лог ошибок"),
        ("nova_ai.log.*", "ротированный лог"),
        ("nova_ai-hidden.log.*", "ротированный скрытый лог"),
        ("nova_ai-launchd*.log.*", "ротированный лог launchd"),
        ("dialog.jsonl.*", "ротированный журнал диалогов"),
    ):
        for path in _rotate_logs_by_age("logs", pattern, _LOG_MEGABYTES_MAX):
            cleaned += 1
            logging.info("Удалён %s: %s", name, path)
    for path in _rotate_logs_by_age(
        os.path.join("logs", "tasks"), "tasks-*.jsonl.*", _LOG_MEGABYTES_MAX
    ):
        cleaned += 1
        logging.info("Удалён ротированный журнал задач: %s", path)

    if cleaned:
        logging.info("Уборка при старте: удалено файлов — %d", cleaned)


def _remove_quietly(path: str, what: str) -> int:
    """Удаляет файл, не поднимая ошибку. Возвращает 1, если файл был удалён."""
    try:
        os.remove(path)
        logging.info("Удалён %s: %s", what, path)
        return 1
    except FileNotFoundError:
        return 0
    except OSError as error:
        logging.debug("Не удалось удалить %s (%s): %s", what, path, error)
        return 0


def setup_signal_handlers(assistant: Assistant | None = None):
    """Обработчики остановки: SIGTERM, SIGINT и SIGHUP.

    В обработчике сигнала НЕЛЬЗЯ вызывать logging: если сигнал пришёл
    ровно во время записи в лог, обработчик попытается взять тот же
    нереентрантный блокировок и процесс зависнет навсегда — супервизор
    не сможет перезапустить NOVA AI, и он останется в прошлом состоянии.
    Поэтому здесь только SystemExit, а освобождение ресурсов (озвучка,
    микрофон) делает finally в main() и atexit-регистрация.

    `assistant` не используется и остаётся только для совместимости с
    прежними вызовами: обработчики ставятся до того, как ассистент
    существует, иначе они не защищали бы запуск.

    Почему SIGHUP важен
    ------------------
    SIGHUP приходит при закрытии терминала (`exit` из сессии, отключение
    ssh-консоли). По умолчанию Python его **игнорирует**: процесс-родитель
    умирает, NOVA AI остаётся жив, микрофон остаётся занятым, а панель и
    `launch_jarvis.sh` видят «работающий» экземпляр, который никто не может
    остановить. Раньше этот сигнал не обрабатывался — именно поэтому
    «после выхода из терминала NOVA AI оглох и не перезапускался».
    """
    import signal

    def _handle_shutdown(signum, _frame):
        # Никаких логов и блокировок: только требование завершиться.
        # Раскрутка происходит в finally/atexit.
        raise SystemExit(0)

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _handle_shutdown)

    # SIGHUP есть не на всех платформах (его нет в Windows) — ставим, только
    # если система его знает.
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle_shutdown)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Shutdown requested")
    except Exception:
        logging.exception("Fatal error")
    # `=== NOVA AI STOPPED ===` здесь не пишем намеренно: его уже пишет
    # `_shutdown_everything`, которая зарегистрирована через atexit и потому
    # срабатывает при любом выходе. Дублировать строку значило бы заставлять
    # сэра гадать, какой из двух остановов был настоящим.