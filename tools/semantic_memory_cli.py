#!/usr/bin/env python3
"""
CLI для управления семантической памятью NOVA AI.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# Добавляем корневой каталог в путь
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.semantic_memory import get_semantic_memory, RecordNotFoundError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("semantic_memory_cli")

def main():
    parser = argparse.ArgumentParser(description="NOVA AI Semantic Memory CLI")
    subparsers = parser.add_subparsers(dest="command", help="Команда для выполнения")

    # Add
    add_parser = subparsers.add_parser("add", help="Добавить запись")
    add_parser.add_argument("text", help="Текст для добавления")
    add_parser.add_argument("--tags", nargs="+", help="Список тегов")
    add_parser.add_argument("--meta", help="JSON-строка с метаданными")

    # Search
    search_parser = subparsers.add_parser("search", help="Поиск записей")
    search_parser.add_argument("query", help="Поисковый запрос")
    search_parser.add_argument("-k", type=int, default=5, help="Количество результатов")
    search_parser.add_argument("--tags", nargs="+", help="Фильтр по тегам")

    # Get
    get_parser = subparsers.add_parser("get", help="Получить запись по ID")
    get_parser.add_argument("id", help="ID записи")

    # Update
    update_parser = subparsers.add_parser("update", help="Обновить запись")
    update_parser.add_argument("id", help="ID записи")
    update_parser.add_argument("text", help="Новый текст")
    update_parser.add_argument("--tags", nargs="+", help="Новые теги")

    # Delete
    delete_parser = subparsers.add_parser("delete", help="Удалить запись")
    delete_parser.add_argument("id", help="ID записи")

    # Stats
    subparsers.add_parser("stats", help="Статистика памяти")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    sm = get_semantic_memory()

    try:
        if args.command == "add":
            metadata = {}
            if args.meta:
                metadata = json.loads(args.meta)
            if args.tags:
                metadata["tags"] = args.tags
            record_id = sm.add_text(args.text, metadata)
            print(f"Запись успешно добавлена. ID: {record_id}")

        elif args.command == "search":
            results = sm.search(args.query, k=args.k, tags=args.tags)
            print(f"Найдено результатов: {len(results)}")
            for r in results:
                print(f"- [{r['similarity']:.2f}] (ID: {r['id']}) {r['text']}")
                if r['metadata']:
                    print(f"  Метаданные: {r['metadata']}")

        elif args.command == "get":
            record = sm.get_by_id(args.id)
            print(json.dumps(record, ensure_ascii=False, indent=2))

        elif args.command == "update":
            metadata = None
            if args.tags:
                metadata = {"tags": args.tags}
            sm.update_text(args.id, args.text, metadata)
            print(f"Запись {args.id} успешно обновлена.")

        elif args.command == "delete":
            success = sm.delete(args.id)
            if success:
                print(f"Запись {args.id} успешно удалена.")
            else:
                print(f"Запись {args.id} не найдена.")

        elif args.command == "stats":
            stats = sm.get_semantic_stats()
            print(json.dumps(stats, ensure_ascii=False, indent=2))

    except RecordNotFoundError:
        print(f"Ошибка: Запись с ID {args.id} не найдена.")
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды {args.command}: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
