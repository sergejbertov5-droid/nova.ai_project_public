import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from core.secure_write import secure_write_json

logger = logging.getLogger(__name__)

# Файл с пользовательскими настройками разрешений (прав доступа)
DEFAULT_PERMISSIONS_FILE = Path(__file__).resolve().parent.parent / "data" / "permissions.json"

# Человекочитаемые описания для интерфейса настроек:
# название → (название для интерфейса, пояснение)
PERMISSION_META: Dict[str, tuple[str, str]] = {
    "APP_OPEN": (
        "Открытие приложений",
        "Открывать программы на компьютере (например, «открой браузер»).",
    ),
    "BROWSER_CONTROL": (
        "Действия в браузере (клик, ввод текста)",
        "Кликать и вводить текст в Chrome. Это действия от вашего имени: "
        "так можно отправить форму, написать сообщение, изменить настройку "
        "или оплатить покупку. Каждое такое действие NOVA AI сначала "
        "проговаривает вслух и ждёт вашего «да».",
    ),
    "BROWSER_READ": (
        "Чтение страниц браузера",
        "Открывать сайты, искать в Google, читать содержимое вкладок Chrome. "
        "Ничего не нажимается и не вводится — только просмотр.",
    ),
    "WEB_READ": (
        "Веб-поиск и чтение страниц",
        "Искать в интернете и читать веб-страницы — по запросу, безопасно.",
    ),
    "SYSTEM_READ": (
        "Системная информация",
        "Время, дата, батарея, место на диске, погода, карты.",
    ),
    "SYSTEM_AUDIO": (
        "Звук и музыка",
        "Громкость, отключение звука, пауза и переключение треков. "
        "Всё это обратимо голосом и ничего не ломает, поэтому отдельное "
        "подтверждение не требуется.",
    ),
    "SYSTEM_CONTROL": (
        "Управление компьютером",
        "Заблокировать экран, усыпить компьютер, погасить дисплей, "
        "переключить режим фокуса, а также выключить и перезагрузить "
        "компьютер. Каждое такое действие NOVA AI сначала проговаривает "
        "вслух и ждёт вашего «да»: если машина заснёт, заблокируется "
        "или выключится, голосом отменить это уже не получится.",
    ),
    "SCREEN_READ": (
        "Наблюдение за экраном",
        "Видеть, какие приложения открыты и что на переднем плане "
        "(только метаданные; содержимое окон не читается, скриншоты — "
        "отдельная настройка, по умолчанию выключены).",
    ),
    "FILE_READ": (
        "Чтение файлов",
        "Читать файлы по запросу пользователя.",
    ),
    "FILE_WRITE": (
        "Запись файлов",
        "Создавать и изменять файлы (запрос подтверждения).",
    ),
    "FILE_DELETE": (
        "Удаление файлов",
        "Удалять файлы (высокий риск, запрос подтверждения).",
    ),
    "TERMINAL_EXECUTE": (
        "Команды терминала",
        "Выполнять команды в терминале (высокий риск, запрос подтверждения).",
    ),
    "EMAIL_READ": (
        "Чтение почты",
        "Читать письма и составлять краткие выводы по важным.",
    ),
    "TELEGRAM_READ": (
        "Чтение Telegram",
        "Читать непрочитанные сообщения в Telegram и составлять краткие "
        "выводы. Это ваша личная переписка — тот же класс данных, что почта, "
        "поэтому и выключатель здесь отдельный.",
    ),
    "TELEGRAM_REPLY": (
        "Автоответы в Telegram от вашего имени",
        "Бот NOVA AI в Telegram сам отвечает на «не особо важные» "
        "сообщения, пока вы заняты. Он пишет от вашего имени, поэтому "
        "собеседник видит ваш голос — и это легко принять за вас "
        "настоящего. Ответы генерирует модель, то есть это не дословно "
        "ваши слова. Отключите, если автоответы не нужны.",
    ),
    "EMAIL_SETTINGS": (
        "Изменение почтовых настроек",
        "Менять настройки почты — по умолчанию ЗАПРЕЩЕНО (критично).",
    ),
    "MEMORY_WRITE": (
        "Запись в постоянную память",
        "Сохранять факты в постоянную память — только по вашей просьбе "
        "«запомни/сохрани». Любой факт можно попросить забыть в любой "
        "момент, поэтому запись обратима и не требует подтверждения.",
    ),
}

RISK_LABELS = {
    "low": "низкий",
    "medium": "средний",
    "high": "высокий",
    "critical": "критичный",
}


@dataclass(frozen=True)
class Permission:
    name: str
    risk_level: str
    confirmation_required: bool
    allowed: bool = True


class ConfirmationRequired(PermissionError):
    """Действие разрешено политикой, но требует подтверждения пользователя.

    Отдельный класс нужен, чтобы «нужно подтверждение» и «запрещено» не
    были одним и тем же отказом. Раньше check() при confirmation_required=True
    бросал PermissionError, и инструмент с таким флагом становился просто
    неисполнимым: подтвердить его было нечем. Наследуем PermissionError,
    чтобы прежняя обработка отказа продолжала работать как раньше.
    """


def _default_permissions() -> Dict[str, Permission]:
    return {
        "APP_OPEN": Permission("APP_OPEN", "low", False),
        # Клик и ввод текста действуют от имени пользователя: форма, сообщение,
        # настройка, покупка. Поэтому высокий риск и обязательное подтверждение.
        "BROWSER_CONTROL": Permission("BROWSER_CONTROL", "high", True),
        # Открытие сайта, поиск и чтение страницы ничего не меняют.
        "BROWSER_READ": Permission("BROWSER_READ", "low", False),
        "WEB_READ": Permission("WEB_READ", "low", False),
        "SYSTEM_READ": Permission("SYSTEM_READ", "low", False),
        # Звук обратим голосом и ничего не ломает — отдельного подтверждения
        # не нужно, но и под SYSTEM_READ его прятать нельзя: иначе
        # отзыв «Системная информация» отключал бы ещё и громкость.
        "SYSTEM_AUDIO": Permission("SYSTEM_AUDIO", "low", False),
        # А вот здесь READ не работает. Блокировка экрана и сон меняют
        # состояние машины так, что отменить это голосом уже нельзя —
        # микрофон не работает, пока компьютер спит. Поэтому высокий риск
        # и обязательное подтверждение.
        #
        # Раньше эти действия вызывались напрямую из `_system_control_command`
        # и вообще не проходили через реестр: ни разрешения, ни подтверждения.
        # Путь был один — `execute_ai` → `execute_decision`, то есть любая
        # фраза, которую выдала модель (в том числе подсказанная содержимым
        # прочитанной веб-страницы), могла усыпить компьютер без вопроса.
        "SYSTEM_CONTROL": Permission("SYSTEM_CONTROL", "high", True),
        "SCREEN_READ": Permission("SCREEN_READ", "low", False),
        "FILE_READ": Permission("FILE_READ", "low", False),
        "FILE_WRITE": Permission("FILE_WRITE", "medium", True),
        "FILE_DELETE": Permission("FILE_DELETE", "high", True),
        "TERMINAL_EXECUTE": Permission("TERMINAL_EXECUTE", "high", True),
        "EMAIL_READ": Permission("EMAIL_READ", "low", False),
        # Переписка в Telegram — те же личные данные, что почта. Отдельное
        # разрешение нужно, чтобы выключатель «Чтение Telegram» действительно
        # им управлял: при общем SYSTEM_READ сэр не мог отозвать доступ
        # к сообщениям, не отбирая заодно время, дату и батарею.
        "TELEGRAM_READ": Permission("TELEGRAM_READ", "low", False),
        # Бот в Telegram пишет ОТ ИМЕНИ владельца. Раньше он работал
        # вообще вне политики: отзыв TELEGRAM_READ в настройках не
        # останавливал его, и NOVA AI продолжал читать переписку.
        #
        # Подтверждения здесь нет — и это осознанно. Подтверждение
        # спрашивается голосом у сэра, а бот работает в фоне, когда сэр
        # занят и не слышит вопрос. Спросить и ждать ответа там некого,
        # поэтому требование подтверждения сделало бы автоответы
        # неисполнимыми навсегда.
        #
        # Решение владельца принимается один раз и явно: разрешил или
        # запретил это разрешение в настройках. Поэтому риск высокий
        # (пишет от лица человека), а подтверждения нет.
        "TELEGRAM_REPLY": Permission("TELEGRAM_REPLY", "high", False),
        "EMAIL_SETTINGS": Permission(
            "EMAIL_SETTINGS", "critical", True, allowed=False
        ),
        # Память пишется только по явной просьбе сэра, и запись
        # обратима («забудь ...»), поэтому риск низкий и подтверждение
        # не нужно. Но выключатель отдельный: право модели сохранять
        # факты о пользователе должно быть можно отозвать, не отбирая
        # чтение почты или время.
        "MEMORY_WRITE": Permission("MEMORY_WRITE", "low", False),
    }


class PermissionPolicy:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path: Path = Path(path) if path else DEFAULT_PERMISSIONS_FILE
        self._permissions: Dict[str, Permission] = _default_permissions()
        # Повреждённый файл откладываем не чаще раза за экземпляр: политика
        # создаётся при каждом старте и в каждом открытии панели настроек.
        self._corrupt_reported = False
        self._load()

    # ----------------------------------------------------------
    # Хранение в data/permissions.json (правки из настроек GUI)
    # ----------------------------------------------------------
    def _preserve_corrupt(self, reason: str) -> None:
        """Откладывает нечитаемый файл разрешений в сторону, чтобы не потерять его.

        Важно не только ради сохранности настроек: при повреждении политика
        молча откатывается к значениям по умолчанию, где почти всё разрешено.
        """
        if self._corrupt_reported:
            return
        self._corrupt_reported = True
        preserved_path = Path(f"{self.path}.corrupt")
        try:
            if preserved_path.exists():
                # Не затираем предыдущий повреждённый файл — оставляем оба.
                preserved_path = Path(f"{self.path}.corrupt.{int(time.time())}")
            os.replace(self.path, preserved_path)
        except OSError as error:
            logger.error(
                "Файл разрешений %s не читается (%s), но сохранить его не удалось: %s. "
                "Проверьте файл вручную — возможно, NOVA AI придётся сбросить.",
                self.path, reason, error,
            )
            return
        logger.warning(
            "Файл разрешений %s не читается (%s). Сохранён как %s — NOVA AI работает "
            "со стандартными разрешениями, ваши настройки можно вернуть из этого файла.",
            self.path, reason, preserved_path,
        )

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as error:
            logger.warning("Не удалось прочитать разрешения: %s", error)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            self._preserve_corrupt(str(error))
            return
        if not isinstance(data, dict):
            # Раньше data.items() стоял вне try, поэтому валидный по синтаксису,
            # но неверный по форме JSON ронял PermissionPolicy(), а значит —
            # весь NOVA AI на старте и панель настроек. Теперь форма проверяется
            # явно, и неверный файл откладывается, а не переписывается.
            self._preserve_corrupt("ожидался JSON-объект с разрешениями")
            return
        for name, cfg in data.items():
            if name not in self._permissions:
                continue
            if not isinstance(cfg, dict):
                # Неизвестная форма записи — пропускаем, как и раньше.
                continue
            perm = self._permissions[name]
            allowed = bool(cfg.get("allowed", perm.allowed))
            confirm = bool(cfg.get("confirmation_required", perm.confirmation_required))
            self._permissions[name] = Permission(
                name, perm.risk_level, confirm, allowed
            )

    def save(self, path: Optional[str] = None) -> Path:
        target = Path(path) if path else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        data = {
            name: {
                "allowed": perm.allowed,
                "confirmation_required": perm.confirmation_required,
                "risk_level": perm.risk_level,
            }
            for name, perm in self._permissions.items()
        }
        # Атомарно и приватно. Прямая запись обрезала файл до первой строки,
        # и обрыв на середине оставлял нечитаемый JSON. Файл описывает, что
        # NOVA AI разрешено делать, поэтому закрываем его от посторонних.
        secure_write_json(target, data)
        return target

    def set_permission(
        self,
        name: str,
        allowed: Optional[bool] = None,
        confirmation_required: Optional[bool] = None,
    ) -> None:
        perm = self._permissions.get(name)
        if perm is None:
            raise PermissionError(f"Unknown permission: {name}")
        self._permissions[name] = Permission(
            name,
            perm.risk_level,
            perm.confirmation_required if confirmation_required is None else confirmation_required,
            perm.allowed if allowed is None else allowed,
        )

    # ----------------------------------------------------------
    # Интерфейс для GUI
    # ----------------------------------------------------------
    def perms(self) -> Dict[str, Permission]:
        """Все разрешения (копия)."""
        return dict(self._permissions)

    def describe_all(self):
        """Список для отображения: имя, название, описание, риск, значения."""
        result = []
        for name, perm in self._permissions.items():
            meta = PERMISSION_META.get(name, (name, ""))
            result.append({
                "name": name,
                "title": meta[0],
                "description": meta[1],
                "risk_level": perm.risk_level,
                "risk_label": RISK_LABELS.get(perm.risk_level, perm.risk_level),
                "allowed": perm.allowed,
                "confirmation_required": perm.confirmation_required,
            })
        return result

    def check(
        self,
        required_permission: str,
        confirmed: bool = False,
    ) -> Permission:
        """Проверяет разрешение и решает, можно ли выполнять действие.

        Два разных исхода, и их важно не путать:

        * allowed=False — действие запрещено. PermissionError в любом случае,
          подтверждение его не отменяет.
        * allowed=True и confirmation_required=True — действие разрешено, но
          ждёт подтверждения. Бросаем ConfirmationRequired: политика говорит
          «можно, если сэр скажет да». Вызывающий код ловит это, спрашивает
          и выполняет действие с confirmed=True.

        confirmed=True пропускает только требование подтверждения. Запрет
        (allowed=False) проверяется раньше и не обходится никогда.
        """
        permission = self._permissions.get(required_permission)
        if permission is None:
            raise PermissionError(f"Unknown permission: {required_permission}")
        if not permission.allowed:
            raise PermissionError(f"Permission denied: {required_permission}")
        if permission.confirmation_required and not confirmed:
            raise ConfirmationRequired(f"Confirmation required: {required_permission}")
        return permission

    def describe(self, required_permission: str) -> Permission:
        permission = self._permissions.get(required_permission)
        if permission is None:
            raise PermissionError(f"Unknown permission: {required_permission}")
        return permission
