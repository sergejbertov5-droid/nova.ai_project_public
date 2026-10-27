"""
envconfig.py — чтение и изменение .env проекта без потери остальных строк.

Используется панелью настроек (control_settings.py): просмотр/правка ключей,
а также кнопки удаления личных данных (на случай передачи проекта другому
человеку — чтобы ключи и пароли не попали в чужие руки).
"""
from pathlib import Path
from typing import Dict, List

from core.secure_write import secure_write_text

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"

# Личные/секретные ключи — очищаются кнопкой «удалить все ключи»
SENSITIVE_KEYS = (
    "AI_API_KEY",
    "AI_BASE_URL",
    "AI_MODEL",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "CEREBRAS_API_KEY",
    "OPENROUTER_API_KEY",
    "NIM_API_KEY",
    "ELEVENLABS_API_KEY",
    "FISH_API_KEY",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TG_BOT_TOKEN",
)


def read_env(path: Path = ENV_FILE) -> Dict[str, str]:
    """Возвращает {КЛЮЧ: значение} из .env (значения без кавычек и комментариев)."""
    result: Dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # inline-комментарий через " #" (не трогаем # внутри значения)
        if " #" in value:
            value = value.split(" #", 1)[0].strip()
        result[key] = value
    return result


def _atomic_write_text(path: Path, text: str) -> None:
    """Пишет файл целиком — либо не пишет ничего, и всегда приватно.

    В .env лежат все ключи проекта, а обычная запись сначала обрезает файл.
    Обрыв на середине (диск кончился, Mac выключился) оставил бы сэра
    вовсе без ключей и без возможности их восстановить. Поэтому сначала
    пишем во временный файл рядом и только потом переименовываем.

    Права 0600 заданы в secure_write: временный файл создаётся закрытым
    сразу, поэтому ключи не лежат в миллисекунды в читаемом файле.
    """
    secure_write_text(path, text)


def _quote_env_value(value: str) -> str:
    """Оборачивает значение в двойные кавычки, если оно содержит спецсимволы,
    опасные для `source .env` в bash (пробелы, кавычки, $, `, # и т.п.)."""
    if not value:
        return ""
    # Безопасные символы: буквы/цифры/точка/`_` и служебные для токенов
    if all(ch.isalnum() or ch in "-_@:./+%=" for ch in value):
        return value
    escaped = (
        value
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
    )
    return f'"{escaped}"'


def write_env(updates: Dict[str, str], path: Path = ENV_FILE) -> None:
    """Обновляет только указанные ключи, порядок и остальные строки сохраняются."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    out: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in pending:
                value = pending.pop(key)
                if value == "":
                    continue  # пустое значение = строка удаляется
                out.append(f"{key}={_quote_env_value(value)}")
                continue
        out.append(line)
    for key, value in pending.items():
        if value != "":
            out.append(f"{key}={_quote_env_value(value)}")
    _atomic_write_text(path, "\n".join(out) + "\n")


def delete_keys(keys, path: Path = ENV_FILE) -> List[str]:
    """Удаляет строки с указанными ключами из .env. Возвращает удалённые ключи."""
    if not path.exists():
        return []
    keys_set = set(keys)
    lines = path.read_text(encoding="utf-8").splitlines()
    out: List[str] = []
    deleted: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in keys_set:
                deleted.append(key)
                continue
        out.append(line)
    _atomic_write_text(path, "\n".join(out) + "\n")
    return deleted


def delete_all_sensitive(path: Path = ENV_FILE) -> List[str]:
    """Удаляет все личные ключи — для безопасной передачи проекта."""
    return delete_keys(SENSITIVE_KEYS, path=path)