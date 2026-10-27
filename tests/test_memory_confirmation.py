import os
import tempfile
import unittest

from core.confirmations import ConfirmationManager
from core.memory import MemoryStore


class MemoryConfirmationTest(unittest.TestCase):
    def test_explicit_memory_is_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "memory.json")
            first = MemoryStore(path)
            self.assertEqual(first.remember("мой график начинается в 7 утра"), "Запомнила.")
            second = MemoryStore(path)
            self.assertIn("7 утра", second.summary())
            self.assertEqual(second.forget("график"), "Забыла указанную информацию.")

    def test_confirmation_requires_explicit_answer(self):
        manager = ConfirmationManager()
        self.assertIn("Подтвердить", manager.request("file.delete", {"path": "x"}, "Удалить x."))
        self.assertIsNone(manager.resolve("может быть"))
        self.assertTrue(manager.resolve("да"))
        # After confirmation, take() should be called to consume the pending
        self.assertIsNotNone(manager.take())
        self.assertIsNone(manager.pending)


if __name__ == "__main__":
    unittest.main()
