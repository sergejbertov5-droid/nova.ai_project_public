import pytest
import sys
import os
import tempfile
import shutil
from pathlib import Path

# Добавляем путь к проекту в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.semantic_memory import (
    SemanticMemory,
    SemanticMemoryConfig,
    RecordNotFoundError,
    get_semantic_memory
)

@pytest.fixture
def temp_memory():
    """Фикстура для создания временного экземпляра семантической памяти."""
    test_dir = Path(tempfile.mkdtemp())
    index_path = test_dir / "test.index"
    metadata_path = test_dir / "test.json"
    config_path = test_dir / "config.json"

    # Создаем конфиг
    config = SemanticMemoryConfig.instance()
    config.config["index_path"] = str(index_path)
    config.config["metadata_path"] = str(metadata_path)
    # Отключаем порог сходства — в этих тестах проверяется фильтрация по тегам,
    # а не рематическая релевантность: короткие запросы вроде «код» или «пицца»
    # могут иметь сходство ниже порога с текстами, которым по тегу не принадлежат.
    config.config["similarity_threshold"] = 0.0

    mem = SemanticMemory()
    yield mem

    # Очистка
    shutil.rmtree(test_dir, ignore_errors=True)

def test_crud_operations(temp_memory):
    """Тест базовых CRUD операций."""
    mem = temp_memory

    # 1. Добавление
    text = "Тестовая запись для NOVA AI"
    tags = ["test", "nova_ai"]
    record_id = mem.add_text(text, {"tags": tags})
    assert record_id is not None

    # 2. Получение по ID
    record = mem.get_by_id(record_id)
    assert record["text"] == text
    assert record["tags"] == tags

    # 3. Обновление
    new_text = "Обновленная тестовая запись"
    new_tags = ["updated"]
    updated_id = mem.update_text(record_id, new_text, {"tags": new_tags})
    assert updated_id == record_id

    record = mem.get_by_id(record_id)
    assert record["text"] == new_text
    assert record["tags"] == new_tags

    # 4. Удаление
    success = mem.delete(record_id)
    assert success is True

    with pytest.raises(RecordNotFoundError):
        mem.get_by_id(record_id)

def test_tag_filtering(temp_memory):
    """Тест поиска с фильтрацией по тегам."""
    mem = temp_memory

    mem.add_text("Программирование на Python", {"tags": ["code", "python"]})
    mem.add_text("Рецепт вкусной пиццы", {"tags": ["food", "cooking"]})
    mem.add_text("Погода в Москве", {"tags": ["weather"]})

    # Поиск без тегов
    results = mem.search("код", k=10)
    assert len(results) >= 1

    # Поиск с тегом (совпадение)
    results = mem.search("пицца", tags=["food"])
    assert len(results) == 1
    assert "пиццы" in results[0]["text"]

    # Поиск с тегом weather — возвращается запись с этим тегом,
    # даже если запрос «пицца» к ней семантически нерелевантен.
    results = mem.search("пицца", tags=["weather"])
    assert len(results) == 1
    assert "погода" in results[0]["text"].lower()

def test_persistence(temp_memory):
    """Тест сохранения данных между инициализациями."""
    mem = temp_memory
    text = "Важное воспоминание"
    record_id = mem.add_text(text)

    # Создаем новый экземпляр, имитируя перезапуск
    # (в реальности нужно было бы подменить пути в конфиге,
    # но наш temp_memory уже настроен на временные файлы)
    mem2 = SemanticMemory()

    record = mem2.get_by_id(record_id)
    assert record["text"] == text

def test_invalid_id(temp_memory):
    """Тест обработки некорректных ID."""
    mem = temp_memory
    with pytest.raises(RecordNotFoundError):
        mem.get_by_id("non-existent-id")

    success = mem.delete("non-existent-id")
    assert success is False

def test_update_preserves_added_at_and_id(temp_memory):
    """Тест: update_text() сохраняет исходный added_at и id, меняя только text."""
    mem = temp_memory

    # 1. add_text()
    original_text = "Первоначальная запись"
    record_id = mem.add_text(original_text, {"tags": ["note"]})

    # 2. сохраняем исходный added_at
    record = mem.get_by_id(record_id)
    original_added_at = record["added_at"]

    # 3. update_text()
    new_text = "Обновлённая запись"
    updated_id = mem.update_text(record_id, new_text)

    # 4. проверяем новый text
    record = mem.get_by_id(record_id)
    assert record["text"] == new_text

    # 5. проверяем прежний added_at
    assert record["added_at"] == original_added_at

    # 6. проверяем неизменный id
    assert updated_id == record_id
    assert record["id"] == record_id


def test_update_metadata_does_not_clobber_added_at(temp_memory):
    """Тест: metadata в update_text() не перезаписывает исторический added_at."""
    mem = temp_memory

    record_id = mem.add_text("Запись", {"tags": ["note"]})
    original_added_at = mem.get_by_id(record_id)["added_at"]

    # metadata пытается задать свой added_at — он должен быть проигнорирован.
    mem.update_text(
        record_id,
        "Обновлённая запись",
        {"tags": ["updated"], "added_at": original_added_at + 999},
    )

    record = mem.get_by_id(record_id)
    assert record["added_at"] == original_added_at
    assert record["id"] == record_id
    assert record["text"] == "Обновлённая запись"
    assert record["tags"] == ["updated"]


if __name__ == "__main__":
    pytest.main([__file__])
