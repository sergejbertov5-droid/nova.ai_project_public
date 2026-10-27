import pytest
import sys
import os
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.semantic_memory import (
    SemanticMemory,
    SemanticMemoryConfig,
    RecordNotFoundError,
    get_semantic_memory,
    reset_semantic_memory,
    reset_semantic_memory_config,
)


@pytest.fixture
def temp_memory():
    """Фикстура: временный экземпляр памяти с изолированными файлами."""
    test_dir = Path(tempfile.mkdtemp())
    config = SemanticMemoryConfig.instance()
    config.config["index_path"] = str(test_dir / "test.index")
    config.config["metadata_path"] = str(test_dir / "test.json")

    mem = SemanticMemory()
    yield mem

    shutil.rmtree(test_dir, ignore_errors=True)


def test_semantic_memory_initialization():
    """Тест инициализации семантической памяти."""
    # Сбросить singleton и конфиг, чтобы предыдущие тесты не оставляли
    # загрязнённое состояние (например, конфигурацию с путями к
    # предыдущим временным директориям).
    reset_semantic_memory(None)
    reset_semantic_memory_config(None)
    mem = SemanticMemory()
    assert mem._is_ready is True
    assert mem._model is not None
    assert mem._index is not None


def test_add_and_search(temp_memory):
    """Тест добавления и поиска текста."""
    mem = temp_memory

    mem.add_text("Отличная погода сегодня", {"category": "weather"})
    mem.add_text("Мне нравится программировать на Python", {"category": "hobby"})
    mem.add_text("Сегодня я ел пиццу на обед", {"category": "food"})

    stats = mem.get_semantic_stats()
    assert stats["vectors"] == 3

    results = mem.search("Как дела с программированием?", k=2)
    assert len(results) > 0
    found_python = any("Python" in r["text"] for r in results)
    assert found_python

    results = mem.search("Какая сегодня погода?", k=2)
    assert len(results) > 0
    found_weather = any("погода" in r["text"] for r in results)
    assert found_weather


def test_semantic_search_relevance(temp_memory):
    """Тест релевантности семантического поиска."""
    mem = temp_memory

    mem.add_text("Сегодня я планирую пойти в парк и покататься на велосипеде", {"activity": "outdoor"})
    mem.add_text("Мне нужно купить продукты в магазине на улице Ленина", {"activity": "shopping"})
    mem.add_text("У меня запланирована встреча с клиентом в офисе в 15:00", {"activity": "work"})
    mem.add_text("После работы я хочу посмотреть новый фильм в кинотеатре", {"activity": "entertainment"})

    results = mem.search("Где я могу активно провести время на свежем воздухе?", k=3)
    assert len(results) > 0

    if len(results) > 0:
        top_result = results[0]
        assert "парк" in top_result["text"] or "велосипед" in top_result["text"]
        assert top_result["similarity"] > 0.3


def test_convenience_functions(temp_memory):
    """Тест удобных функций: add_text + search + stats."""
    mem = temp_memory

    record_id = mem.add_text("Тестовый текст для удобной функции", {"test": True})
    assert record_id is not None

    stats = mem.get_semantic_stats()
    assert stats["vectors"] == 1

    results = mem.search("текст для функции", k=1)
    assert len(results) == 1
    assert "Тестовый текст" in results[0]["text"]


def test_tag_filtering(temp_memory):
    """Тест фильтрации по тегам."""
    mem = temp_memory

    mem.add_text("Программирование на Python", {"tags": ["code", "python"]})
    mem.add_text("Рецепт вкусной пиццы", {"tags": ["food", "cooking"]})
    mem.add_text("Погода в Москве", {"tags": ["weather"]})

    results = mem.search("пицца", tags=["food"])
    assert len(results) == 1
    assert "пиццы" in results[0]["text"]

    # Поиск с тегом, которого нет ни у одной записи — должен вернуть 0
    results = mem.search("пицца", tags=["nonexistent_tag"])
    assert len(results) == 0


def test_persistence(temp_memory):
    """Тест сохранения данных между инициализациями."""
    mem = temp_memory
    text = "Важное воспоминание"
    record_id = mem.add_text(text)

    mem2 = SemanticMemory()
    record = mem2.get_by_id(record_id)
    assert record["text"] == text


def test_invalid_id(temp_memory):
    """Тест обработки некорректных ID."""
    mem = temp_memory
    with pytest.raises(RecordNotFoundError):
        mem.get_by_id("non-existent-id")

    assert mem.delete("non-existent-id") is False


def test_singleton_accessor():
    """Тест глобального доступа к памяти."""
    sm = get_semantic_memory()
    assert isinstance(sm, SemanticMemory)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])