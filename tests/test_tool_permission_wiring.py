"""
Регрессия: каждый инструмент привязан к тому разрешению, которое описывает
его суть, и каждый переключатель в настройках чем-то управляет.

Найдено на живом проекте. Инструмент `mail.unread` — он читает настоящий
почтовый ящик — был привязан к `SYSTEM_READ`, то есть к категории
«Время, дата, батарея, место на диске, погода, карты».

Последствия:
  * доступ к личной переписке выдавался и отнимался вместе с часами;
  * объявленное в политике `EMAIL_READ` («Чтение почты») и отдельный
    переключатель с таким названием в настройках не управляли ничем —
    сэр видел его и полагался на него, а почта читалась всё равно;
  * из шести неиспользуемых разрешений четыре выглядели как рабочие.

Почему это не поймали тесты: список инструментов жил локальной
переменной внутри `CommandRouter.__init__`. Связь «инструмент →
разрешение» было негде прочитать и негде проверить. Каталог вынесен на
уровень модуля (`TOOL_SPECS`), и эти тесты сверяют его с политикой.
"""
import unittest

from core.commands import (
    INSTANCE_BOUND_TOOLS,
    TOOL_SPECS,
    tools_by_permission,
)
from core.permissions import PermissionPolicy

# Инструменты, которые обязаны быть привязаны именно к этим категориям.
# Смысл каждой строки — в комментарии: проверяется не «совпадает ли
# строка», а «не спрятан ли личный доступ под безобидную категорию».
EXPECTED = {
    "mail.unread": "EMAIL_READ",            # личная переписка
    # Telegram — та же личная переписка. Добавил в ту же таблицу после того,
    # как telegram.unread остался привязан к SYSTEM_READ: почту к EMAIL_READ
    # перевели, а Telegram забыли, и один выключатель «Системная информация»
    # управлял и батареей, и содержимым чатов.
    "telegram.unread": "TELEGRAM_READ",
    "browser.click": "BROWSER_CONTROL",     # действие от имени сэра
    "browser.type": "BROWSER_CONTROL",      # ввод текста от имени сэра
    "browser.read_page": "BROWSER_READ",    # только чтение
    "browser.open_url": "BROWSER_READ",     # только чтение
    "user.profile": "SYSTEM_READ",
    "screen.capture": "SCREEN_READ",
    "web.fetch": "WEB_READ",
    "web.learn": "WEB_READ",
}


class ToolPermissionWiringTest(unittest.TestCase):
    def setUp(self):
        self.permissions = set(PermissionPolicy().perms())
        self.by_name = {
            name: permission for name, _d, _h, permission in TOOL_SPECS
        }

    def test_every_permission_exists_in_policy(self):
        # Разрешение, которого нет в политике, роняет describe() при
        # регистрации — то есть весь роутер, а не один инструмент.
        for name, permission in self.by_name.items():
            with self.subTest(tool=name):
                self.assertIn(permission, self.permissions)

    def test_expected_wiring(self):
        for name, expected in EXPECTED.items():
            with self.subTest(tool=name):
                self.assertIn(name, self.by_name)
                self.assertEqual(
                    self.by_name[name], expected,
                    f"{name} привязан не к тому разрешению",
                )

    def test_mail_is_not_under_system_read(self):
        # Отдельный тест на исходный дефект: чтение почты не должно
        # прятаться в «время, дата, батарея».
        self.assertNotEqual(self.by_name.get("mail.unread"), "SYSTEM_READ")

    def test_private_correspondence_is_not_under_system_read(self):
        # Тот же дефект для Telegram. Починили почту, а переписку в чатах
        # оставили под SYSTEM_READ — и выключатель «Системная информация»
        # молча управлял личными сообщениями.
        for tool in ("mail.unread", "telegram.unread"):
            with self.subTest(tool=tool):
                self.assertNotEqual(self.by_name.get(tool), "SYSTEM_READ")

    def test_reading_permissions_are_separate_per_data_kind(self):
        # Каждый вид личных данных — свой выключатель. Иначе сэр не может
        # отозвать доступ к одному виду, не отобрав другой.
        kinds = {
            "mail.unread": "EMAIL_READ",
            "telegram.unread": "TELEGRAM_READ",
        }
        self.assertEqual(len(set(kinds.values())), len(kinds),
                         "личные данные делят один выключатель")

    def test_no_tool_claims_a_read_permission_while_acting(self):
        # Клик и ввод — единственные инструменты, которые меняют мир
        # от имени сэра. Они обязаны быть под BROWSER_CONTROL.
        acting = ("click", "type", "submit", "delete", "write", "execute")
        for name, permission in self.by_name.items():
            if any(word in name.lower() for word in acting):
                with self.subTest(tool=name):
                    self.assertNotIn(
                        permission, ("BROWSER_READ", "WEB_READ", "SYSTEM_READ"),
                        f"{name} меняет мир, но привязан к категории чтения",
                    )

    def test_no_duplicate_tool_names(self):
        names = [name for name, *_ in TOOL_SPECS]
        self.assertEqual(
            len(names), len(set(names)),
            "дубликат имени: ToolRegistry.register() упал бы на старте",
        )

    def test_every_tool_has_description(self):
        for name, description, _handler, _permission in TOOL_SPECS:
            with self.subTest(tool=name):
                self.assertTrue(
                    (description or "").strip(),
                    "инструмент без описания не попадёт в каталог для модели",
                )


class InstanceBoundToolsTest(unittest.TestCase):
    """Инструменты, завязанные на экземпляр, должны быть собраны при старте."""

    def test_placeholders_are_declared(self):
        # Обработчик None в TOOL_SPECS допустим только для инструментов из
        # INSTANCE_BOUND_TOOLS: иначе он зарегистрируется как None и упадёт
        # при первом же вызове — в момент, когда сэр ждёт ответа.
        for name, _description, handler, _permission in TOOL_SPECS:
            if handler is None:
                with self.subTest(tool=name):
                    self.assertIn(name, INSTANCE_BOUND_TOOLS)

    def test_declared_are_actually_placeholders(self):
        for name, _description, handler, _permission in TOOL_SPECS:
            if name in INSTANCE_BOUND_TOOLS:
                with self.subTest(tool=name):
                    self.assertIsNone(handler)

    def test_learn_uses_the_live_brain(self):
        # Регрессия на вынос каталога: web.learn когда-то был лямбдой,
        # захватывающей self.brain прямо в списке. После выноса на уровень
        # модуля self там не существует, и инструмент молча получил бы
        # новый AIBrain вместо того, с которым работает роутер.
        from core.commands import CommandRouter
        from core.permissions import PermissionPolicy as PP
        from core.tool_registry import ToolRegistry

        router = CommandRouter.__new__(CommandRouter)
        router.brain = "ЖИВОЙ"
        router.confirmations = None
        router.filler_callback = None
        router._pending_danger_action = None
        router._last_reply = ""
        router.registry = ToolRegistry(PP())
        router._register_tools()

        spec = router.registry.get("web.learn")
        self.assertTrue(callable(spec.handler), "обработчик не собран")

        import core.commands as C
        seen = {}
        original = C.run_learning_session
        C.run_learning_session = lambda topic, brain=None: seen.update(
            {"topic": topic, "brain": brain}
        ) or "выучено"
        try:
            spec.handler("квантовая физика")
        finally:
            C.run_learning_session = original
        self.assertEqual(seen.get("brain"), "ЖИВОЙ")


class ToolsByPermissionTest(unittest.TestCase):
    def test_mapping_covers_every_tool(self):
        mapping = tools_by_permission()
        listed = {name for names in mapping.values() for name in names}
        for name, *_ in TOOL_SPECS:
            self.assertIn(name, listed)

    def test_unwired_permissions_are_detectable(self):
        # Настройки показывают предупреждение там, где переключатель
        # ничем не управляет. Если это перестаёт быть правдой, тест
        # упадёт и предупреждение перестанет показываться зря.
        mapping = tools_by_permission()
        declared = set(PermissionPolicy().perms())
        unwired = declared - set(mapping)
        self.assertIsInstance(unwired, set)
        for name in unwired:
            self.assertNotIn(name, mapping)

    def test_email_read_is_wired(self):
        self.assertIn("mail.unread", tools_by_permission().get("EMAIL_READ", []))


if __name__ == "__main__":
    unittest.main()
