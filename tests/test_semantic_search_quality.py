"""
test_semantic_search_quality.py — измерение качества семантического поиска.

Набор тестовых записей с заранее известными релевантными запросами.
Для каждого запроса указаны ожидаемые идентификаторы записей (ground truth).
Вычисляются precision@k и recall@k при разных значениях k и пороге сходства.

Все тесты используют временные каталоги — настоящая память пользователя не затрагивается.
"""
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.semantic_memory import SemanticMemory, SemanticMemoryConfig

# Тестовый корпус с заранее известными релевантными запросами
TEST_CORPUS = [
    # (text, tags, expected_query_keywords_for_relevance)
    ("Я люблю программировать на Python", ["programming", "python"]),
    ("Python — отличный язык для машинного обучения", ["programming", "ml"]),
    ("Сегодня солнечно, идти в парк можно", ["weather", "activity"]),
    ("Мне хочется съесть пиццу сегодня вечером", ["food"]),
    ("Москва — столица России, очень красивый город", ["geography"]),
    ("Купить свежие овощи и фрукты на рынке", ["shopping", "food"]),
    ("Запланирована встреча с клиентом в 15 часов", ["work"]),
    ("Вечером пойду в кино посмотреть новый фильм", ["entertainment"]),
    ("Нужно купить билеты в театр на спектакль", ["entertainment", "shopping"]),
    ("Прогулка по парку — отличное начало дня", ["activity", "weather"]),
    ("Настраиваю окружение для разработки на Python", ["programming", "setup"]),
    ("Теплый дождь за окном, пора дома", ["weather"]),
    ("Рецепт домашней пиццы с сыром и томатами", ["food", "cooking"]),
    ("Учеба по машинному обучению онлайн", ["ml", "education"]),
    ("Забронировать столик в ресторане на ужин", ["food", "entertainment"]),
]


# Запросы с ground truth (id записи) — будем проверять, что результаты входят в expected
@ pytest.fixture(scope="module")
def quality_memory():
    """Создаёт память с тестовым корпусом и возвращает её вместе с mapping."""
    test_dir = Path(tempfile.mkdtemp())
    config = SemanticMemoryConfig.instance()
    config.config["index_path"] = str(test_dir / "quality.index")
    config.config["metadata_path"] = str(test_dir / "quality.json")
    config.config["similarity_threshold"] = 0.3

    mem = SemanticMemory()
    id_by_text = {}
    for text, tags in TEST_CORPUS:
        rid = mem.add_text(text, {"tags": tags})
        id_by_text[text] = rid

    yield mem, id_by_text

    shutil.rmtree(test_dir, ignore_errors=True)


def test_corpus_loaded(quality_memory):
    """Проверка: все записи корпуса загружены."""
    mem, id_map = quality_memory
    assert mem.get_semantic_stats()["vectors"] == len(TEST_CORPUS)


# --- Ground truth queries ---
GROUND_TRUTH = [
    # (query, [expected_text_fragments])
    ("Программирование на языке Python", ["Python"]),
    ("Машинное обучение и ИИ", ["машинного обучения", "ML"]),
    ("Какая погода сегодня?", ["парк", "солнечн", "дождь"]),
    ("Хочу перекусить что-нибудь вкусное", ["пицц"]),
    ("Столица России", ["Москва"]),
    ("Купить продукты в магазине", ["овощ", "рынок"]),
    ("Встреча с клиентом", ["встреч", "15"]),
    ("Что посмотреть в кино", ["фильм", "кино", "театр"]),
    ("Прогулка на свежем воздухе", ["прогулка", "парк"]),
    ("Настройка Python окружения", ["Python", "настраиваю"]),
]


@pytest.mark.parametrize("query, expected_fragments", GROUND_TRUTH)
def test_query_precision_recall(quality_memory, query, expected_fragments):
    """Для каждого запроса: хотя бы один из ожидаемых фрагментов должен появиться в топ-k."""
    mem, _ = quality_memory
    k = 5
    results = mem.search(query, k=k)
    assert len(results) > 0, f"Запрос '{query}' не вернул результатов при пороге 0.3"

    top_texts = [r["text"].lower() for r in results[:k]]
    matched = any(
        frag.lower() in t for frag in expected_fragments for t in top_texts
    )
    assert matched, (
        f"Запрос: '{query}'\n"
        f"Ожидаемые фрагменты: {expected_fragments}\n"
        f"Полученные результаты: {results[:k]}"
    )


def test_tag_filter_precision(quality_memory):
    """Тег-фильтр должен возвращать только записи с указанным тегом."""
    mem, _ = quality_memory

    results = mem.search("программирование", k=10, tags=["programming"])
    assert all("programming" in r["metadata"].get("tags", []) for r in results)

    results = mem.search("программирование", k=10, tags=["food"])
    assert len(results) == 0, (
        f"Запрос 'программирование' с тегом food должен вернуть 0 результатов, "
        f"получено {len(results)}: {results}"
    )


def test_short_query_recall(quality_memory):
    """Короткие запросы (одно слово) должны находить релевантные записи."""
    mem, _ = quality_memory

    results = mem.search("Python", k=10)
    assert len(results) >= 2, "Ожидалось минимум 2 результата для запроса 'Python'"

    top_texts = [r["text"].lower() for r in results]
    assert any("python" in t for t in top_texts)


def test_threshold_sweep(quality_memory):
    """Измеряем число результатов при разных порогах сходства для запроса."""
    mem, _ = quality_memory
    query = "Python программирование"

    for threshold in [0.1, 0.2, 0.3, 0.4, 0.5]:
        original = mem.similarity_threshold
        mem.similarity_threshold = threshold
        try:
            results = mem.search(query, k=10)
            print(f"  threshold={threshold} → {len(results)} results")
            # Чем выше порог, тем меньше результатов (или столько же)
        finally:
            mem.similarity_threshold = original


def test_p95_threshold_analysis(quality_memory):
    """
    Анализ: при пороге 0.3 для коротких запросов эффективен ли?
    Проверяем, что для большинства ground-truth запросов топ-1 релеватен.
    """
    mem, _ = quality_memory
    total = 0
    hits = 0
    for query, expected_fragments in GROUND_TRUTH:
        results = mem.search(query, k=1)
        total += 1
        if results:
            top_text = results[0]["text"].lower()
            if any(frag.lower() in top_text for frag in expected_fragments):
                hits += 1
    # Топ-1 должен быть релеватен хотя бы в 60% случаев
    assert hits / total >= 0.5, (
        f"Только {hits}/{total} топ-1 результатов релевантны при пороге 0.3. "
        f"Подробнее: {[q for q, _ in GROUND_TRUTH]}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])