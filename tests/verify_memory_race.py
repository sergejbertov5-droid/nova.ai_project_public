"""Проверка блокировки памяти на реальной гонке записи.

Генерируем 60 ЗАВЕДОМО разных фактов: различаться должны слова, а не
цифры — иначе нормализация памяти (она отбрасывает короткие токены)
сделает все факты одинаковыми и дедупликация склеит их, а гонка
не проявится.
"""
import json
import os
import string
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.memory import MemoryStore

ALPHABET = "абвгдежзиклмнопрстуфхцчшщэюя"


def distinct_facts(count):
    """count фактов, у которых нет НИ ОДНОГО общего значимого слова."""
    facts = []
    for i in range(count):
        a = ALPHABET[i % len(ALPHABET)]
        b = ALPHABET[(i // len(ALPHABET)) % len(ALPHABET)]
        c = ALPHABET[(i // (len(ALPHABET) ** 2)) % len(ALPHABET)]
        facts.append(f"факт{a}{b}{c}")
    return facts


class NoLockMemoryStore(MemoryStore):
    """Старое поведение: чтение-модификация-запись без блокировки."""

    def remember(self, fact, important=False, category="general"):
        fact = fact.strip()
        now = time.time()
        memories = self._read()
        idx = self._find_existing(memories, fact)
        if idx is not None:
            memories[idx].update({
                "updated_at": now,
                "important": memories[idx]["important"] or important,
                "uses": memories[idx]["uses"] + 1,
            })
        else:
            memories.append({
                "fact": fact, "created_at": now, "updated_at": now,
                "category": category, "important": important, "uses": 1,
            })
        self._write(memories)
        return "Запомнила."


def hammer(store, facts, threads=6):
    def worker(start):
        for fact in facts[start::threads]:
            store.remember(fact)
    workers = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()


def count(path):
    with open(path, encoding="utf-8") as handle:
        return len(json.load(handle))


def scenario(name, factory):
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "memory.json")
    store = factory(path)
    facts = distinct_facts(60)
    hammer(store, facts)
    stored = count(path)
    print(f"  {name}: записано {stored} из {len(facts)}"
          f"  ->  {'OK' if stored == len(facts) else f'ПОТЕРЯНО {len(facts) - stored}'}")
    return stored


if __name__ == "__main__":
    print("Гонка записи в память (6 потоков, 60 различных фактов):")
    lost_without = 60 - scenario("без блокировки (старый код)", NoLockMemoryStore)
    gained = scenario("с блокировкой (новый код)", MemoryStore)
    print()
    print("Вывод:", "гонка была реальной и теперь закрыта"
          if lost_without > 0 and gained == 60
          else "проверить вручную")
