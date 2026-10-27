#!/usr/bin/env python3
"""
Скрипт для тестирования производительности (benchmark) семантической памяти.
"""

import time
import tempfile
import shutil
import platform
import sys
from pathlib import Path

# Добавляем корень проекта в путь
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.semantic_memory import SemanticMemory, SemanticMemoryConfig

def run_benchmark(num_records=1000):
    print(f"=== Запуск Benchmark Семантической Памяти ({num_records} записей) ===")
    print(f"Платформа: {platform.platform()}")
    print(f"Python: {platform.python_version()}")

    # Подготовка временного окружения
    test_dir = Path(tempfile.mkdtemp())
    config = SemanticMemoryConfig.instance()
    config.config["index_path"] = str(test_dir / "bench.index")
    config.config["metadata_path"] = str(test_dir / "bench.json")

    mem = SemanticMemory()

    # 1. Измерение добавления
    start_time = time.time()
    added_ids = []
    for i in range(num_records):
        record_id = mem.add_text(
            f"Тестовая запись номер {i} для проверки скорости работы семантической памяти",
            {"tags": [f"tag_{i%10}", "benchmark"]}
        )
        added_ids.append(record_id)
    add_time = time.time() - start_time
    print(f"Добавление {num_records} записей: {add_time:.2f} сек ({add_time/num_records*1000:.2f} мс/запись)")

    # 2. Измерение поиска
    start_time = time.time()
    num_searches = 50
    for i in range(num_searches):
        results = sm_results = mem.search("проверка скорости работы", k=5)
    search_time = time.time() - start_time
    print(f"Поиск ({num_searches} запросов): {search_time:.2f} сек ({search_time/num_searches*1000:.2f} мс/запрос)")

    # 3. Измерение поиска с тегами
    start_time = time.time()
    for i in range(num_searches):
        results = mem.search("запись", k=5, tags=["tag_1"])
    tag_search_time = time.time() - start_time
    print(f"Поиск с тегами ({num_searches} запросов): {tag_search_time:.2f} сек ({tag_search_time/num_searches*1000:.2f} мс/запрос)")

    # 4. Измерение удаления (удалим 50 записей)
    start_time = time.time()
    num_deletes = min(50, num_records)
    for record_id in added_ids[:num_deletes]:
        mem.delete(record_id)
    delete_time = time.time() - start_time
    print(f"Удаление {num_deletes} записей: {delete_time:.2f} сек ({delete_time/num_deletes*1000:.2f} мс/удаление)")

    # Очистка
    shutil.rmtree(test_dir, ignore_errors=True)
    print("=== Benchmark Завершен ===")

if __name__ == "__main__":
    run_benchmark(1000)
