# Credential-shaped values in this file are synthetic TESTONLY fixtures.
"""
test_permission_wiring_privacy.py — личные данные и доступ к ним.

Два дефекта, найденные разбором каталога инструментов:

1. `telegram.unread` был привязан к `SYSTEM_READ` («время, дата, батарея,
   диск»), хотя читает личную переписку. Ровно та же ошибка уже была с почтой
   и исправлена на `EMAIL_READ`, но Telegram остался в хвосте: сэр мог
   выключить «Чтение почты» и всё равно получать содержимое чатов — и не мог
   отозвать доступ к сообщениям, не отобрав заодно время и батарею.

2. `core/brain.py` писал в лог тело ответа провайдера целиком при пустом
   content. В этом теле обычно лежит эхо запроса — то есть то, что сэр только
   что сказал вслух, — а иногда и данные авторизации.

Оба дефекта одного класса: чужой доступ и личные данные в журнале.
"""
import json
import unittest

from core import brain
from core.ai_providers import redact_secrets
from core.brain import PROVIDER_BODY_LOG_LIMIT, _safe_provider_body
from core.commands import TOOL_SPECS
from core.permissions import PERMISSION_META, PermissionPolicy


def _permission_for(tool_name):
    for name, _label, _handler, permission in TOOL_SPECS:
        if name == tool_name:
            return permission
    raise AssertionError(f"инструмент {tool_name} не найден в TOOL_SPECS")


class PrivateCorrespondencePermissionTest(unittest.TestCase):
    """Личная переписка управляется своим разрешением, а не системным."""

    def test_mail_reads_use_email_permission(self):
        self.assertEqual(_permission_for("mail.unread"), "EMAIL_READ")

    def test_telegram_reads_use_telegram_permission(self):
        # Главная проверка: Telegram не должен прятаться под SYSTEM_READ.
        self.assertEqual(_permission_for("telegram.unread"), "TELEGRAM_READ")

    def test_system_info_keeps_system_permission(self):
        # Обратная сторона: обычные системные вопросы не должны начать
        # требовать разрешения на чтение переписки.
        for tool in ("system.time", "system.date", "system.battery", "system.disk"):
            with self.subTest(tool=tool):
                self.assertEqual(_permission_for(tool), "SYSTEM_READ")

    def test_telegram_permission_is_declared_in_policy(self):
        policy = PermissionPolicy()
        perms = policy.perms()
        self.assertIn("TELEGRAM_READ", perms)
        self.assertIn("TELEGRAM_READ", PERMISSION_META)

    def test_telegram_permission_can_be_revoked_independently(self):
        # Смысл самого исправления: сэр отзывает доступ к чатам, не теряя
        # при этом время, дату, батарею и почту.
        policy = PermissionPolicy()
        policy.set_permission("TELEGRAM_READ", allowed=False)
        with self.assertRaises(PermissionError):
            policy.check(_permission_for("telegram.unread"))
        # Остальное продолжает работать.
        policy.check(_permission_for("system.time"))
        policy.check(_permission_for("mail.unread"))

    def test_email_and_telegram_are_separate_switches(self):
        policy = PermissionPolicy()
        policy.set_permission("TELEGRAM_READ", allowed=False)
        # Почта не должна пострадать от отключения Telegram.
        policy.check(_permission_for("mail.unread"))

    def test_telegram_tool_actually_hits_the_revoked_permission(self):
        # Сквозная проверка цепочки «инструмент → разрешение → политика»
        # на настоящем роутере. Раньше инструмент ходил в SYSTEM_READ,
        # и отзыв доступа к переписке на него никак не влиял.
        from core.commands import CommandRouter

        router = CommandRouter()
        policy = router.registry.policy
        policy.set_permission("TELEGRAM_READ", allowed=False)

        spec = router.registry.get("telegram.unread")
        self.assertEqual(spec.required_permission, "TELEGRAM_READ")
        with self.assertRaises(PermissionError):
            router.registry.call("telegram.unread")

    def test_revoking_system_read_does_not_silently_allow_telegram(self):
        # Обратная проверка на старой ошибке: пока telegram.unread ходил в
        # SYSTEM_READ, отзыв SYSTEM_READ блокировал и почту, и время, и
        # сообщения — то есть один выключатель управлял всем сразу.
        from core.commands import CommandRouter

        router = CommandRouter()
        router.registry.policy.set_permission("SYSTEM_READ", allowed=False)
        with self.assertRaises(PermissionError):
            router.registry.call("system.time")
        # Чаты при этом остаются доступны: у них своё разрешение.
        self.assertEqual(router.registry.get("telegram.unread").required_permission,
                         "TELEGRAM_READ")

    def test_every_tool_permission_is_declared(self):
        # Реестр — единственный источник правды. Инструмент, ссылающийся на
        # несуществующее разрешение, тихо работал бы в обход политики.
        known = set(PermissionPolicy().perms())
        for name, _label, _handler, permission in TOOL_SPECS:
            with self.subTest(tool=name):
                self.assertIn(permission, known,
                              f"{name} ссылается на неизвестное разрешение {permission}")

    def test_new_permission_survives_saved_policy_file(self):
        # У существующих пользователей уже есть data/permissions.json без
        # нового ключа. Он обязан появиться со значением по умолчанию, а не
        # пропасть или превратиться в отказ.
        import os
        import shutil
        import tempfile

        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "permissions.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"SYSTEM_READ": {"allowed": False}}, handle)

        policy = PermissionPolicy(path=path)
        # Унаследованное значение сохранилось…
        self.assertFalse(policy.perms()["SYSTEM_READ"].allowed)
        # …а новое разрешение доступно по умолчанию.
        self.assertTrue(policy.perms()["TELEGRAM_READ"].allowed)
        policy.check("TELEGRAM_READ")


class ProviderBodyLogSafetyTest(unittest.TestCase):
    """Тело ответа провайдера не должно попадать в журнал как есть."""

    def test_known_key_is_masked(self):
        body = {"error": {"message": "invalid api_key: gsk_TESTONLY1XXXXXXXXXXXX"}}
        out = _safe_provider_body(body)
        self.assertNotIn("gsk_TESTONLY1XXXXXXXXXXXX", out)
        self.assertIn("***", out)

    def test_gemini_style_key_is_masked(self):
        body = {"message": "AIzaTESTONLY2XXXXXXXXXXXXXXXXXXXXXXXXX"}
        out = _safe_provider_body(body)
        self.assertNotIn("AIzaTESTONLY2XXXXXXXXXXXXXXXXXXXXXXXXX", out)

    def test_body_is_truncated(self):
        # Эхо запроса — это то, что сэр только что сказал вслух. В журнал
        # попадает форма ответа, а не вся переписка.
        body = {"echo": "а" * 5000}
        out = _safe_provider_body(body)
        self.assertLessEqual(len(out), PROVIDER_BODY_LOG_LIMIT)

    def test_shape_is_preserved_for_diagnosis(self):
        # Обрезка не должна делать запись бесполезной: по логу нужно понять,
        # ЧТО прислал провайдер.
        out = _safe_provider_body({"choices": [], "finish_reason": "length"})
        self.assertIn("choices", out)
        self.assertIn("length", out)

    def test_unserialisable_body_does_not_raise(self):
        # Диагностика не должна ронять разбор ответа из-за странного объекта.
        class Weird:
            def __repr__(self):
                return "<weird>"

        out = _safe_provider_body({"x": Weird()})
        self.assertIn("weird", out)

    def test_empty_content_logs_redacted_body(self):
        # Проверяем не только помощника, но и реальную точку вызова:
        # раньше здесь стояло logging.error("...", data).
        import inspect

        source = inspect.getsource(brain)
        call_at = source.find("_safe_provider_body(data)")
        self.assertNotEqual(call_at, -1, "точка вызова не найдена")
        bare_at = source.find('"AI returned empty content: %s"')
        self.assertEqual(bare_at, -1, "сырой %s тела ответа снова в логе")

    def test_redact_helper_is_actually_used(self):
        # Модуль импортирует redact_secrets — проверяем, что маскирование
        # не отвалилось после правок импортов.
        self.assertEqual(redact_secrets("key: gsk_TESTONLY1XXXXXXXXXXXX").count("***"), 1)


if __name__ == "__main__":
    unittest.main()