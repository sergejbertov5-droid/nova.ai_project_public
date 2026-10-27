"""
vault.py — мастер-пароль на личные данные NOVA AI.

Пароль защищает секции с чувствительными данными (Telegram-бот/аккаунт,
ключи AI, озвучка) в панели настроек: без него личные ключи нельзя
посмотреть или изменить. Сам пароль не хранится — только PBKDF2-хэш
с солью в data/vault.json.
"""
import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from core.secure_write import secure_write_json

VAULT_FILE = Path(__file__).resolve().parent.parent / "data" / "vault.json"

_ITERATIONS = 240_000
_ALGO = "pbkdf2_sha256"

#: Минимальная длина мастер-пароля. 4 символа — это не защита: при
#: PBKDF2-HMAC-SHA256 и 240 000 итераций четырёхсимвольный пароль
#: перебирается офлайн за секунды, а пароль защищает ключи API.
_MIN_LENGTH = 8

#: То же значение публично: панель настроек подсказывает минимум,
#: иначе она предлагала бы 4 символа, а vault такой пароль отверг бы —
#: пользователь получил бы отказ без причины.
MIN_PASSWORD_LENGTH = _MIN_LENGTH

#: Защищён ли доступ к секретам. Читается панелью настроек:
#: True — секреты за мастер-паролем, False — пароль не задан.
#: Третье состояние (файл повреждён) означает «заблокировано», и
#: панель обязана сказать об этом, а не открыть ключи.
_ACCESS_STATE_UNSET = "unset"
_ACCESS_STATE_SET = "set"
_ACCESS_STATE_CORRUPT = "corrupt"

_access_state = _ACCESS_STATE_UNSET
_corrupt_reported = False


def _mark_corrupt(reason: str) -> None:
    # global нужен для обоих имён: без него Python считает присваивание
    # локальным, и чтение _corrupt_reported падало с UnboundLocalError
    # ровно в момент поломки — то есть вместо внятной блокировки
    # сэр получал невнятное исключение.
    global _access_state, _corrupt_reported
    _access_state = _ACCESS_STATE_CORRUPT
    if _corrupt_reported:
        return
    _corrupt_reported = True
    import logging

    logging.error(
        "Файл мастер-пароля %s не читается (%s). Секреты заблокированы: "
        "восстановите файл из резервной копии или удалите его, чтобы "
        "задать пароль заново. До этого NOVA AI не покажет и не позволит "
        "изменить ключи.",
        VAULT_FILE, reason,
    )


def _read_vault() -> dict:
    """Читает хранилище хэша.

    ВАЖНО: при любой ошибке чтения возвращается «нечитаемое»
    состояние, а не пустой словарь. Пустой словарь выглядел бы как
    «пароль не задан», и панель настроек открывала бы все ключи
    вообще без пароля — то есть повреждённый файл снимал бы защиту.
    Fail-closed здесь обязателен: система хранения секретов не имеет
    права открываться при неисправности.
    """
    global _access_state

    if not VAULT_FILE.exists():
        return {}
    try:
        raw = VAULT_FILE.read_text(encoding="utf-8")
    except OSError as error:
        _mark_corrupt(f"файл недоступен: {error}")
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        _mark_corrupt(f"повреждён JSON: {error}")
        return {}

    if not isinstance(data, dict):
        _mark_corrupt("ожидался JSON-объект")
        return {}
    if not (data.get("hash") and data.get("salt")):
        # Запись есть, но хэша в ней нет — это не «пароль не задан»,
        # а сломанная запись. Иначе клюши открылись бы.
        _mark_corrupt("в файле нет хэша и соли")
        return {}
    return data


def access_state() -> str:
    """Состояние доступа: unset / set / corrupt."""
    global _access_state
    if _access_state != _ACCESS_STATE_CORRUPT:
        _read_vault()
    return _access_state


def is_corrupt() -> bool:
    """Повреждён ли файл мастер-пароля (секреты заблокированы)."""
    return access_state() == _ACCESS_STATE_CORRUPT


def has_password_broken() -> bool:
    """Повреждён ли файл мастер-пароля.

    Отдельное имя, а не флаг в UI: панель настроек обязана отличать
    «пароля нет» (секреты можно смотреть) от «файл сломан» (секреты
    заблокированы до восстановления). Смешать эти состояния — значит
    открыть ключи при неисправности.
    """
    return is_corrupt()


def reset_access_state() -> None:
    """Сбрасывает закэшированное состояние (для тестов и после починки)."""
    global _access_state, _corrupt_reported
    _access_state = _ACCESS_STATE_UNSET
    _corrupt_reported = False


def _write_vault(vault: dict) -> None:
    VAULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Атомарно и приватно. Здесь цена ошибки не только в потере хэша: если
    # файл окажется нечитаемым, _read_vault вернёт пустой словарь,
    # has_password() даст False, и панель настроек откроет доступ к ключам
    # вообще без пароля. Поэтому файл заменяется целиком и никогда не
    # остаётся наполовину записанным. Права 0600: временный файл создаётся
    # закрытым сразу, хэш пароля не лежит в читаемом файле.
    secure_write_json(VAULT_FILE, vault)


def _hash_password(password: str, salt: bytes, iterations: int = _ITERATIONS) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return digest.hex()


def has_password() -> bool:
    """Установлен ли мастер-пароль.

    При повреждённом файле возвращаем True, а не False: файл есть,
    значит пароль когда-то был, и система обязана остаться
    заблокированной. Иначе сломанный vault.json снимал бы пароль.
    """
    if is_corrupt():
        return True
    vault = _read_vault()
    return bool(vault.get("hash") and vault.get("salt"))


def set_password(password: str) -> bool:
    """Создаёт (или заменяет) мастер-пароль.

    Заменой старого пароля считается только успешная проверка старого
    либо явное решение владельца «забыл пароль» (см. reset_password).
    """
    if not password or len(password) < _MIN_LENGTH:
        return False
    salt = secrets.token_bytes(16)
    _write_vault({
        "algo": _ALGO,
        "iterations": _ITERATIONS,
        "salt": salt.hex(),
        "hash": _hash_password(password, salt),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    global _access_state
    _access_state = _ACCESS_STATE_SET
    return True


def reset_password(new_password: str) -> bool:
    """Задаёт пароль заново, не спрашивая старый.

    Нужен ровно для повреждённого vault.json: там пароль проверить
    нечем, но и оставить систему заблокированной навсегда нельзя.
    Действие явное и доступно только из панели с подтверждением;
    сам по себе этот вызов защиты не отменяет — её снимает решение
    владельца.
    """
    return set_password(new_password)


def change_password(old_password: str, new_password: str) -> bool:
    """Меняет пароль после проверки старого."""
    if not verify_password(old_password):
        return False
    return set_password(new_password)


def verify_password(password: str) -> bool:
    """Проверяет пароль по хранимому хэшу."""
    if not password:
        return False
    if is_corrupt():
        # Повреждённый файл не подтверждает ни один пароль: проверять
        # нечего, и «повезло» означало бы угадать хэш вслепую.
        return False
    vault = _read_vault()
    salt_hex = vault.get("salt")
    stored = vault.get("hash")
    if not salt_hex or not stored:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    computed = _hash_password(
        password, salt, int(vault.get("iterations", _ITERATIONS))
    )
    return secrets.compare_digest(computed, stored)


def delete_password(password: str) -> bool:
    """Удаляет мастер-пароль (только при верном пароле)."""
    if not verify_password(password):
        return False
    try:
        VAULT_FILE.unlink(missing_ok=True)
    except OSError:
        return False
    global _access_state
    _access_state = _ACCESS_STATE_UNSET
    return True