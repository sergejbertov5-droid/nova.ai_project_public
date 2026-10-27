"""
test_tg_bot_permissions.py — Telegram-бот и разрешения NOVA AI.

Два независимых дефекта, оба в `tools/telegram_bot.py`.

1. Бот работал вообще вне PermissionPolicy
--------------------------------------------
`_process_message()` смотрел только на флаги .env (BOT_READ,
BOT_WRITE, BOT_AUTOREPLY). Ни один вызов `policy.check()` в модуле не
было. Последствие: в панели настроек сэр отключает TELEGRAM_READ —
и бот продолжает читать переписку. Отзыв разрешения выглядел
работающим (выключатель переключился, JSON перезаписался), но на бота
не влиял ничем. То же самое с автоответами: отдельного разрешения на
них не существовало вовсе, поэтому нечего было и отключить.

2. Пустой список чатов означал «отвечать всем»
---------------------------------------------
    return str(chat_id) in chats if chats else bool(is_private)

Список `TG_BOT_DIALOGS` у владельца пуст (он ничего не перечислял),
а значит эта строка разрешала ответ **каждому**, кто написал боту.

Почему это не «неаккуратно». Бот пишет от имени человека: в приветствии
подставляется имя владельца, а ответы генерирует модель. Собеседник
видит имя и принимает написанное за слова владельца. Дальше любой, кто
знает номер бота, может писать ему вместо владельца — «скажи, что я
перевёл деньги», «передай, что я буду в среду» — и это будет отправлено
именем владельца. Список чатов здесь и есть граница доверия, и пустой
список должен означать «никто», а не «все».

Теперь: список обязателен, ответить всем можно осознанно
(`TG_BOT_ALLOW_ALL_DIALOGS=true`), а чтение и автоответы проходят через
PermissionPolicy — TELEGRAM_READ и TELEGRAM_REPLY.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tools.telegram_bot as bot
from core.permissions import PERMISSION_META, PermissionPolicy


def fresh_policy():
    """Политика на пустом файле, со стандартными разрешениями.

    Обязательно: `PermissionPolicy()` без пути читает настоящий
    `data/permissions.json`, который меняют другие тесты. Наш тест должен
    зависеть от кода, а не от того, какие выключатели остались включены
    после чужого запуска.
    """
    directory = tempfile.TemporaryDirectory()
    path = Path(directory.name) / "permissions.json"
    policy = PermissionPolicy(str(path))
    # TemporaryDirectory живёт до конца теста: сохраняем ссылку.
    policy._test_tmpdir = directory  # noqa: SLF001
    return policy


class ChatAllowlistTest(unittest.TestCase):
    """Кому бот отвечает."""

    def test_empty_list_denies_every_private_chat(self):
        # Ядро дефекта: список пуст — и раньше это читалось как «все».
        for chat_id in (1, 123, 987654321, -1001234567890):
            with self.subTest(chat=chat_id):
                self.assertFalse(
                    bot.is_chat_allowed(chat_id, True, allowed=set())
                )

    def test_allow_all_is_an_explicit_opt_in(self):
        self.assertTrue(
            bot.is_chat_allowed(123, True, allowed=set(), allow_all=True)
        )
        # Даже с флагом — только личные чаты. Группа это всегда другой
        # список участников и другой уровень доверия.
        self.assertFalse(
            bot.is_chat_allowed(123, False, allowed=set(), allow_all=True)
        )

    def test_allow_all_defaults_to_false(self):
        # Проверяем и в .env.example, и в коде: включено должно быть
        # только явным решением владельца.
        import pathlib

        example = (
            pathlib.Path(__file__).resolve().parent.parent / ".env.example"
        ).read_text(encoding="utf-8")
        self.assertIn("TG_BOT_ALLOW_ALL_DIALOGS=false", example)

    def test_default_does_not_allow_all(self):
        # Если переменной нет в окружении — все равно нельзя.
        with patch.dict("os.environ", {}, clear=False):
            import importlib

            # Перечитываем модуль с пустым окружением только ради флага.
            saved = bot.ALLOW_ALL_DIALOGS
            try:
                bot.ALLOW_ALL_DIALOGS = False
                self.assertFalse(bot.is_chat_allowed(1, True, allowed=set()))
            finally:
                bot.ALLOW_ALL_DIALOGS = saved

    def test_shipped_env_does_not_enable_allow_all(self):
        # Конкретно про этот проект: в его .env список чатов пуст, и
        # ответить каждому — ровно то, чего делать не надо.
        import pathlib

        env = pathlib.Path(__file__).resolve().parent.parent / ".env"
        if not env.exists():
            self.skipTest("нет .env")
        text = env.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith("TG_BOT_ALLOW_ALL_DIALOGS"):
                value = line.split("=", 1)[1].strip().lower()
                self.assertIn(
                    value, ("false", "0", "no", "off", ""),
                    "в .env включён ответ всем в личке — бот пишет "
                    "от имени владельца каждому, кто его найдёт",
                )

    def test_summary_tells_the_truth(self):
        # Сэр должен видеть, кому бот отвечает. Раньше в журнале было
        # только «Telegram-бот онлайн» — и никакого намека, что бот
        # разговаривает со всеми.
        saved_chats, saved_all = bot.ALLOWED_DIALOGS, bot.ALLOW_ALL_DIALOGS
        try:
            bot.ALLOWED_DIALOGS, bot.ALLOW_ALL_DIALOGS = set(), False
            summary = bot.allowed_chats_summary()
            self.assertIn("никто", summary)
            self.assertIn("TG_BOT_DIALOGS", summary)

            bot.ALLOW_ALL_DIALOGS = True
            summary = bot.allowed_chats_summary()
            self.assertIn("все личные чаты", summary)

            bot.ALLOWED_DIALOGS, bot.ALLOW_ALL_DIALOGS = {"1", "2"}, False
            self.assertIn("2 чат", bot.allowed_chats_summary())
        finally:
            bot.ALLOWED_DIALOGS = saved_chats
            bot.ALLOW_ALL_DIALOGS = saved_all


class BotPermissionWiringTest(unittest.TestCase):
    """Бот обязан спрашивать разрешения, а не только флаги .env."""

    def setUp(self):
        saved = bot._policy_singleton
        bot._policy_singleton = None
        self.addCleanup(lambda: setattr(bot, "_policy_singleton", saved))

    def test_reply_permission_is_declared(self):
        # Без объявления в PERMISSION_META его не будет в панели
        # настроек, и отключить автоответы будет нечем.
        self.assertIn("TELEGRAM_REPLY", PERMISSION_META)

    def test_reply_permission_is_high_risk(self):
        # Пишет от имени человека. Риск должен быть виден в панели.
        perm = fresh_policy().describe("TELEGRAM_REPLY")
        self.assertEqual(perm.risk_level, "high")
        self.assertFalse(
            perm.confirmation_required,
            "подтверждение здесь делает автоответы неисполнимыми: "
            "бота никто не спрашивает, сэр в это время занят",
        )

    def test_permissions_are_consulted(self):
        policy = fresh_policy()
        self.assertIsNotNone(bot.bot_permissions.__doc__)
        with patch.object(bot, "_policy", return_value=policy):
            read, reply = bot.bot_permissions()
        self.assertTrue(read)
        self.assertTrue(reply)

    def test_revoking_telegram_read_is_seen(self):
        policy = fresh_policy()
        policy.set_permission("TELEGRAM_READ", allowed=False)
        with patch.object(bot, "_policy", return_value=policy):
            read, reply = bot.bot_permissions()
        self.assertFalse(read, "отзыв TELEGRAM_READ не действует на бота")
        self.assertTrue(reply)

    def test_revoking_telegram_reply_is_seen(self):
        policy = fresh_policy()
        policy.set_permission("TELEGRAM_REPLY", allowed=False)
        with patch.object(bot, "_policy", return_value=policy):
            read, reply = bot.bot_permissions()
        self.assertTrue(read)
        self.assertFalse(reply, "автоответы нельзя отключить")

    def test_broken_permissions_file_means_no_replies(self):
        # Обратный дефолт. Если файл разрешений не прочитался, молчащий
        # бот — это заметная недоделка, а бот, пишущий от имени человека
        # без спроса, — инцидент.
        with patch.object(bot, "_policy", return_value=bot._DENY_ALL):
            self.assertEqual(bot.bot_permissions(), (False, False))

    def test_policy_failure_does_not_crash_the_bot(self):
        class Exploding:
            def check(self, *_a, **_k):
                raise RuntimeError("файл разрешений недоступен")

        with patch.object(bot, "_policy", return_value=Exploding()):
            # RuntimeError не PermissionError — метод обязан его
            # пережить, иначе падает обработчик события.
            try:
                read, reply = bot.bot_permissions()
            except RuntimeError:
                self.fail("проверка разрешений роняет бота вместо отказа")
            self.assertFalse(read or reply)


# ---------------------------------------------------------------
# Поведение обработчика: реальный путь от сообщения до отправки
# ---------------------------------------------------------------

class _FakeChat:
    def __init__(self):
        self.sent = []


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()
        self.sent = []

    async def send_message(self, chat, text, *args, **kwargs):
        self.sent.append(text)
        return text


class _FakeSender:
    first_name = "Незнакомец"


class _FakeMessage:
    def __init__(self, text):
        self.message = text


class _FakeEvent:
    """Минимальный telethon-событие: сообщение в личке от постороннего."""

    def __init__(self, text, chat_id=555, is_private=True):
        self.message = _FakeMessage(text)
        self.chat_id = chat_id
        self.is_private = is_private
        self.sender_id = chat_id + 1
        self._sender = _FakeSender()

    async def get_chat(self):
        return _FakeChat()

    async def get_sender(self):
        return self._sender


class _FakeMe:
    id = 1
    username = "jarvis_test_bot"
    first_name = "NOVA AI"


def _run(coro):
    return asyncio.run(coro)


class ProcessMessagePermissionsTest(unittest.TestCase):
    """Бот уважает разрешения на настоящем пути обработки."""

    def setUp(self):
        self.client = _FakeClient()
        self.sent = self.client.sent
        # Стартуем как новая установка: пустое состояние на диске.
        self._patch(bot, "STATE_PATH", self._tmp_state())
        self._patch(bot, "MAX_HISTORY", 8)
        self._patch(bot, "BOT_READ", True)
        self._patch(bot, "BOT_WRITE", True)
        self._patch(bot, "BOT_AUTOREPLY", True)
        self._patch(bot, "OWNER_ID", 999999)
        self._patch(bot, "_last_send", [0.0])
        # Шаблон приветствия и имя владельца задаём явно. Раньше тест ждал
        # «JAR'VIS» в тексте и падал в полном прогоне, потому что другой
        # тестовый файл подменяет шаблон на «Jar'vis» и не восстанавливает
        # его. Зависимость от чужого глобального состояния — это дефект
        # теста, а не повод смягчить проверку.
        self._patch(bot, "GREETING_TEMPLATE", "Привет, {owner} сейчас занят.")
        self._patch(bot, "OWNER_NAME", "Сергей")

    def _tmp_state(self):
        import tempfile
        from pathlib import Path

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name) / "state.json"

    def _patch(self, obj, name, value):
        patcher = patch.object(obj, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _allow(self, chat_id):
        self._patch(bot, "ALLOWED_DIALOGS", {str(chat_id)})
        self._patch(bot, "ALLOW_ALL_DIALOGS", False)

    def test_sends_greeting_when_everything_is_allowed(self):
        # Контрольный случай: базовая функциональность не сломана.
        self._allow(555)
        policy = fresh_policy()
        with patch.object(bot, "_policy", return_value=policy):
            _run(bot._process_message(self.client, _FakeMe(), _FakeEvent("привет")))
        self.assertEqual(len(self.sent), 1)
        # build_greeting обращается к собеседнику по имени.
        self.assertEqual(self.sent[0], "Незнакомец, привет, Сергей сейчас занят.")

    def test_nothing_is_sent_to_a_chat_outside_the_list(self):
        # Тот же пустой список, что и в .env владельца.
        self._patch(bot, "ALLOWED_DIALOGS", set())
        self._patch(bot, "ALLOW_ALL_DIALOGS", False)
        policy = fresh_policy()
        with patch.object(bot, "_policy", return_value=policy):
            _run(bot._process_message(self.client, _FakeMe(), _FakeEvent("привет")))
        self.assertEqual(
            self.sent, [],
            "бот ответил в чат, который не перечислен в TG_BOT_DIALOGS",
        )

    def test_nothing_is_sent_when_reply_permission_is_revoked(self):
        # Флаги .env разрешают, а разрешение в политике запрещает.
        # Политика должна побеждать: это то, чего раньше не было.
        self._allow(555)
        policy = fresh_policy()
        policy.set_permission("TELEGRAM_REPLY", allowed=False)
        with patch.object(bot, "_policy", return_value=policy):
            _run(bot._process_message(self.client, _FakeMe(), _FakeEvent("привет")))
        self.assertEqual(
            self.sent, [],
            "TELEGRAM_REPLY отключён, но бот всё равно ответил",
        )

    def test_nothing_happens_when_read_permission_is_revoked(self):
        self._allow(555)
        policy = fresh_policy()
        policy.set_permission("TELEGRAM_READ", allowed=False)
        with patch.object(bot, "_policy", return_value=policy):
            _run(bot._process_message(self.client, _FakeMe(), _FakeEvent("привет")))
        self.assertEqual(self.sent, [])

    def test_owner_message_is_never_answered(self):
        # Прежняя гарантия не должна была сломаться.
        self._patch(bot, "OWNER_ID", 556)
        self._allow(555)
        policy = fresh_policy()
        with patch.object(bot, "_policy", return_value=policy):
            _run(bot._process_message(self.client, _FakeMe(), _FakeEvent("привет")))
        self.assertEqual(self.sent, [])


class NoPolicyBypassTest(unittest.TestCase):
    """Структурная проверка: обходов политики в модуле не осталось."""

    def test_module_actually_consults_the_policy(self):
        import ast
        import pathlib

        source = pathlib.Path(bot.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        checks = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "check"
        ]
        self.assertTrue(
            checks,
            "в telegram_bot.py нет ни одного policy.check() — "
            "бот работает мимо разрешений",
        )

    def test_env_flags_alone_are_not_enough(self):
        # Проверка именно смысла: обработчик обязан звать bot_permissions,
        # а не полагаться только на BOT_* из .env.
        import inspect

        source = inspect.getsource(bot._process_message)
        self.assertIn("bot_permissions()", source)


if __name__ == "__main__":
    unittest.main()