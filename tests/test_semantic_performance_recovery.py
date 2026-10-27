"""
test_semantic_performance_recovery.py — тесты производительности и восстановления памяти.

Измеряет:
- добавление записей (1000, 10000)
- поиск без тегов
- поиск с тегами
- сохранение и загрузку после перезапуска
- восстановление после повреждённых/отсутствующих файлов

Все тесты выполняются во временных каталогах.
"""
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.semantic_memory import SemanticMemory, SemanticMemoryConfig


def make_random_text(prefix: str, i: int) -> str:
    """Генерирует разные тексты для бенчмарка."""
    topics = [
        "программирование", "погода", "еда", "путешествия", "спорт",
        "работа", "учеба", "музыка", "фильмы", "книги", "машинное обучение",
        "Python", "JavaScript", "Linux", "Docker", "базы данных"
    ]
    return f"{prefix} {topics[i % len(topics)]} номер {i} с уникальными деталями"


@pytest.fixture
def temp_config():
    """Конфиг для временного каталога."""
    test_dir = Path(tempfile.mkdtemp())
    config = SemanticMemoryConfig.instance()
    config.config["index_path"] = str(test_dir / "bench.index")
    config.config["metadata_path"] = str(test_dir / "bench.json")
    config.config["similarity_threshold"] = 0.3
    yield config, test_dir
    shutil.rmtree(test_dir, ignore_errors=True)


def benchmark_add(mem: SemanticMemory, num_records: int, batch_size: int = 100) -> dict:
    """Измеряет время добавления записей."""
    times = []
    for batch_start in range(0, num_records, batch_size):
        batch_end = min(batch_start + batch_size, num_records)
        start = time.perf_counter()
        for i in range(batch_start, batch_end):
            mem.add_text(
                make_random_text("Тест", i),
                {"tags": [f"tag_{i % 10}", "benchmark"]}
            )
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    return {
        "total": sum(times),
        "per_record_ms": (sum(times) / num_records) * 1000,
        "batches": len(times),
    }


def benchmark_search(mem: SemanticMemory, num_queries: int = 50) -> dict:
    """Измеряет время поиска."""
    queries = [
        "программирование на Python",
        "погода на неделю",
        "рецепт вкусного ужина",
        "планы на выходные",
        "машинное обучение",
        "работа в офисе",
        "спорт и тренировки",
        "путешествие за границу",
    ]
    times = []
    for i in range(num_queries):
        q = queries[i % len(queries)]
        start = time.perf_counter()
        mem.search(q, k=10)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    return {
        "total": sum(times),
        "per_query_ms": (sum(times) / num_queries) * 1000,
        "median_ms": statistics.median(times) * 1000,
        "p95_ms": sorted(times)[int(num_queries * 0.95)] * 1000,
    }


def benchmark_tag_search(mem: SemanticMemory, num_queries: int = 50) -> dict:
    """Измеряет время поиска с тегами."""
    queries = ["программирование", "погода", "еда", "работа"]
    tags_list = [["tag_0"], ["tag_1"], ["tag_2"], ["tag_3"]]
    times = []
    for i in range(num_queries):
        q = queries[i % len(queries)]
        tags = tags_list[i % len(tags_list)]
        start = time.perf_counter()
        mem.search(q, k=10, tags=tags)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    return {
        "total": sum(times),
        "per_query_ms": (sum(times) / num_queries) * 1000,
        "median_ms": statistics.median(times) * 1000,
        "p95_ms": sorted(times)[int(num_queries * 0.95)] * 1000,
    }


def test_performance_1000(temp_config):
    """Бенчмарк на 1000 записей."""
    config, _ = temp_config
    mem = SemanticMemory()

    # Добавление
    add_res = benchmark_add(mem, 1000)
    print(f"\n=== 1000 записей: ADD ===")
    print(f"  Total: {add_res['total']:.2f}s")
    print(f"  Per record: {add_res['per_record_ms']:.2f}ms")

    # Поиск
    search_res = benchmark_search(mem, 50)
    print(f"\n=== 1000 записей: SEARCH (50 queries) ===")
    print(f"  Per query: {search_res['per_query_ms']:.2f}ms")
    print(f"  Median: {search_res['median_ms']:.2f}ms, P95: {search_res['p95_ms']:.2f}ms")

    # Поиск с тегами
    tag_res = benchmark_tag_search(mem, 50)
    print(f"\n=== 1000 записей: TAG SEARCH (50 queries) ===")
    print(f"  Per query: {tag_res['per_query_ms']:.2f}ms")
    print(f"  Median: {tag_res['median_ms']:.2f}ms, P95: {tag_res['p95_ms']:.2f}ms")

    # Сохранение
    start = time.perf_counter()
    mem._save()
    save_time = time.perf_counter() - start
    print(f"\n=== SAVE ===")
    print(f"  Time: {save_time*1000:.2f}ms")

    # Проверяем вектор count
    assert mem.get_semantic_stats()["vectors"] == 1000

    # Пороговые значения (адаптивные, не строгие)
    assert add_res["per_record_ms"] < 200, "Слишком медленное добавление"
    assert search_res["median_ms"] < 100, "Слишком медленный поиск"
    assert tag_res["median_ms"] < 100, "Слишком медленный поиск с тегами"


@pytest.mark.slow
def test_performance_10000(temp_config):
    """Бенчмарк на 10000 записей (медленный, опциональный)."""
    config, _ = temp_config
    mem = SemanticMemory()

    add_res = benchmark_add(mem, 10000)
    print(f"\n=== 10000 записей: ADD ===")
    print(f"  Total: {add_res['total']:.2f}s")
    print(f"  Per record: {add_res['per_record_ms']:.2f}ms")

    search_res = benchmark_search(mem, 50)
    print(f"\n=== 10000 записей: SEARCH ===")
    print(f"  Median: {search_res['median_ms']:.2f}ms, P95: {search_res['p95_ms']:.2f}ms")

    tag_res = benchmark_tag_search(mem, 50)
    print(f"\n=== 10000 записей: TAG SEARCH ===")
    print(f"  Median: {tag_res['median_ms']:.2f}ms, P95: {tag_res['p95_ms']:.2f}ms")

    assert mem.get_semantic_stats()["vectors"] == 10000
    assert add_res["per_record_ms"] < 300
    assert search_res["median_ms"] < 200
    assert tag_res["median_ms"] < 200


# --- Тесты восстановления ---

def test_recovery_missing_index(temp_config):
    """Восстановление: индекс удалён, метаданные есть — индекс должен перестроиться."""
    config, test_dir = temp_config
    mem = SemanticMemory()
    mem.add_text("Тест восстановления 1", {"tags": ["test"]})
    mem.add_text("Тест восстановления 2", {"tags": ["test"]})
    assert mem.get_semantic_stats()["vectors"] == 2

    # Удаляем индекс
    os.remove(config.config["index_path"])
    assert not Path(config.config["index_path"]).exists()

    # Новый экземпляр должен перестроить индекс из метаданных
    mem2 = SemanticMemory()
    assert mem2.get_semantic_stats()["vectors"] == 2
    results = mem2.search("восстановления", k=5)
    assert len(results) == 2


def test_recovery_missing_metadata(temp_config):
    """Восстановление: метаданные удалены, индекс есть — память должна быть пустой."""
    config, test_dir = temp_config
    mem = SemanticMemory()
    mem.add_text("Тест 1", {"tags": ["test"]})
    mem.add_text("Тест 2", {"tags": ["test"]})

    # Удаляем метаданные
    os.remove(config.config["metadata_path"])
    assert not Path(config.config["metadata_path"]).exists()

    # Новый экземпляр — метаданных нет, индекс тоже не читается (нет метаданных)
    mem2 = SemanticMemory()
    assert mem2.get_semantic_stats()["vectors"] == 0
    results = mem2.search("тест", k=5)
    assert len(results) == 0


def test_recovery_corrupted_json(temp_config):
    """Восстановление: JSON метаданных повреждён — должен начаться пустым."""
    config, test_dir = temp_config
    mem = SemanticMemory()
    mem.add_text("Важные данные", {"tags": ["important"]})

    # Пишем некорректный JSON
    with open(config.config["metadata_path"], "w") as f:
        f.write("{ это не json }}")

    mem2 = SemanticMemory()
    # Должен начаться пустым (bad JSON → ValueError → migration to empty)
    assert mem2.get_semantic_stats()["vectors"] == 0


def test_recovery_index_vector_count_mismatch(temp_config):
    """Восстановление: количество векторов в индексе ≠ количеству метаданных."""
    config, test_dir = temp_config
    mem = SemanticMemory()
    mem.add_text("Запись 1", {})
    mem.add_text("Запись 2", {})
    mem.add_text("Запись 3", {})
    assert mem.get_semantic_stats()["vectors"] == 3

    # Вручную добавляем лишний вектор в индекс (имитация сбоя)
    import faiss
    import numpy as np
    extra_vec = np.random.random(mem._embedding_dim).astype("float32")
    mem._index.add(extra_vec.reshape(1, -1))
    mem._save()  # Сохраняем повреждённый индекс

    assert mem._index.ntotal == 4  # стало 4 вектора вместо 3 записей

    # При перезапуске: _load_index загрузит 4, _load_metadata загрузит 3
    # _rebuild_index_from_metadata перестроит правильно
    mem2 = SemanticMemory()
    assert mem2.get_semantic_stats()["vectors"] == 3


def test_restart_persistence(temp_config):
    """Полный цикл: добавление → перезапуск → поиск работает."""
    config, test_dir = temp_config
    mem = SemanticMemory()
    added_texts = [
        "Постоянная запись для проверки сохранения",
        "Вторая постоянная запись с тегами",
        "Третья запись без тегов",
    ]
    for t in added_texts:
        mem.add_text(t, {"tags": ["persist"]} if "тегами" in t else {})

    # Перезапуск
    mem2 = SemanticMemory()
    assert mem2.get_semantic_stats()["vectors"] == 3

    results = mem2.search("постоянная запись", k=5)
    assert len(results) >= 2
    assert any("Постоянная запись" in r["text"] for r in results)


def test_concurrent_access_safety(temp_config):
    """Безопасность конкурентного доступа (thread-safety smoke test)."""
    import threading
    config, _ = temp_config
    mem = SemanticMemory()
    errors = []

    def writer(start: int, count: int):
        try:
            for i in range(start, start + count):
                mem.add_text(f"Concurrent text {i}", {"tags": ["concurrent"]})
        except Exception as e:
            errors.append(e)

    def searcher(count: int):
        try:
            for _ in range(count):
                mem.search("concurrent", k=5)
        except Exception as e:
            errors.append(e)

    threads = []
    for i in range(3):
        threads.append(threading.Thread(target=writer, args=(i * 100, 100)))
    for _ in range(2):
        threads.append(threading.Thread(target=searcher, args=(50,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Ошибки в конкурентном доступе: {errors}"
    assert mem.get_semantic_stats()["vectors"] == 300


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short"])