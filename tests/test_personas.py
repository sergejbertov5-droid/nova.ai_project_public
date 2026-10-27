import unittest

from core.commands import CommandRouter
from core.brain import AIBrain


class PersonaTest(unittest.TestCase):
    def test_persona_switching(self):
        router = CommandRouter(brain=AIBrain())

        self.assertEqual(
            router.execute("Переключись на режим Terra"),
            "Переключаюсь на режим Terra.",
        )
        self.assertEqual(router.brain.persona, "terra")

        self.assertEqual(
            router.execute("Переключись на режим Luna"),
            "Переключаюсь на режим Luna.",
        )
        self.assertEqual(router.brain.persona, "luna")


if __name__ == "__main__":
    unittest.main()
