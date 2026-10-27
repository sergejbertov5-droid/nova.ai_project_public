# Прогресс: Semantic Memory (фаза 2.1)

## Текущий статус

Семантическая память реализована в `core/semantic_memory.py`.
Работает в двух режимах:

- **Text-only ( degraded )** — при отсутствии `numpy` / `faiss-cpu` /
  `sentence-transformers` приложение запускается, `add_text` /
  `update_text` / `delete` / `clear` / `get_by_id` работают, сохраняют
  метаданные на диск и честно сообщают, что семантический поиск
  отключён (`search()` возвращает `[]`).  Предупреждения выводятся один
  раз, не повторяются на каждый запрос.
- **Full backend** — при наличии всех зависимостей и локально
  кэшированной модели `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
  работает векторный FAISS-индекс, семантический поиск, персистентность
  индекса и метаданных.

## История тестовых прогонов

### Старый полный прогон `.venv` (прерван SIGINT)

**Прерван SIGINT; на момент остановки 575 passed / 22 failed. Это не итог
полного набора и не результат после последних изменений.**

Причина прерывания — ручной `SIGINT` (не завершение прогона). 22 failure
приходятся на тесты, требующие `sentence-transformers`, который отсутствует
в `.venv`.  Остальные тесты проходили.

### Текущие целевые прогоны

| Набор | Окружение | Результат |
|-------|-----------|-----------|
| `tests/test_semantic_memory_degraded.py` (4 теста) | `.venv` | 4 passed |
| `tests/test_semantic_memory_degraded.py` (4 теста) | `.venv2` | 4 passed |
| `tests/test_semantic_memory_full_backend.py` | `.venv2` | 1 passed |
| `tests/test_semantic_memory_transition.py` | `.venv2` | 1 passed |

Все прогоны выполнены после последних изменений в `core/semantic_memory.py`.

### Регрессия после изменений `core/semantic_memory.py`

Объединённый запуск всех трёх файлов в одном процессе (`.venv2`):

```
.venv2/bin/python -m pytest -q tests/test_semantic_memory_degraded.py \
    tests/test_semantic_memory_full_backend.py \
    tests/test_semantic_memory_transition.py
# 6 passed, 6 warnings in 78.50s (0:01:18)
```

**Ограничение устранено.** Ранее degraded-тесты блокировали `sys.modules`
в родительском процессе, из-за чего full-тесты пропускались или вызывали
segfault. Теперь:

- Блокировка `numpy` / `faiss` / `sentence_transformers` выполняется
  **только внутри subprocess**, до импорта кода проекта.
- Родительский pytest не меняет `sys.modules`.
- `test_semantic_memory_transition.py` degraded-фаза также запускается в
  отдельном subprocess.
- `test_semantic_memory_full_backend.py` больше не содержит обходного
  skip « зависимости заблокированы другим тестом »; реальное отсутствие
  пакетов или локальной модели — отдельное обоснованное ограничение.

## Проверенные сценарии

- Degraded-инициализация: `_is_ready == True`, `_model is None`,
  `_index is None`.
- Сохранение/чтение через `add_text` / `get_by_id` в отдельном subprocess
  с блокировкой `sys.modules`.
- `update_text` + `delete` в degraded mode, проверка через публичный API
  в свежем процессе.
- `clear()` + повторный `add_text` + перезагрузка в новом процессе.
- Full backend: `add` → `clear` → `add` → reload в новом процессе.
- Переход full → degraded → full: индекс обнаруживается устаревшим по
  сохраняемому `_index_revision` (хеш метаданных) и перестраивается,
  несмотря на равенство `ntotal == len(metadata)`.

## Ограничения

- `.venv2` — локальное окружение, не переносимый способ установки.
  Восстановление среды описано в `README.md`.
- Модель `paraphrase-multilingual-MiniLM-L12-v2` должна быть локально
  кэширована; тесты не/download.
- `clear()` сохраняет пустой `metadata_path` вместо его удаления.

## Следующий этап

Интеграция с `core/memory.py` (`MemoryStore.relevant()`) не начата.
Новые подсистемы (Goal Decomposition, Reflection Loop, Self-Learning)
не реализованы.