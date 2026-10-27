"""Тесты взаимодействия с сэром: реплики при молчании и страховка озвучки.

Здесь нет железа и сети — проверяется чистая логика, из-за которой сэр
получал нелепые «вы тут?» и мог оглохнуть навсегда.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as jarvis_main  # noqa: E402
import voice.state as state  # noqa: E402
from voice.speech_to_text import MIC_DEAD_LEVEL  # noqa: E402


class PresencePhraseTest(unittest.TestCase):
    """Молчащий микрофон и «молчащий сэр» — это разные вещи."""

    def test_dead_mic_hints_at_mute(self):
        """Поток открыт, но слышит тишину — говорим про микрофон, а не «вы ушли»."""
        for _ in range(20):
            phrase = jarvis_main._presence_phrase(0.5)
            self.assertIn("микрофон", phrase.lower())

    def test_live_mic_asks_presence(self):
        """Звук идёт, но фраза не разобралась — вежливый вопрос, без упрёков."""
        for _ in range(20):
            phrase = jarvis_main._presence_phrase(150.0)
            self.assertIn("сэр", phrase.lower())
            self.assertNotIn("микрофон", phrase.lower())

    def test_dead_threshold_is_low(self):
        """Порог не должен путать «тихий микрофон» с «мёртвым»."""
        self.assertLess(MIC_DEAD_LEVEL, 40.0)
        self.assertGreater(MIC_DEAD_LEVEL, 0.5)


class InterruptCommandTest(unittest.TestCase):
    """Перебивание голосом должно работать по любой фразе остановки."""

    def test_known_stop_words(self):
        for text in ("стоп", "замолчи", "хватит", "стоп стоп", "замолчите"):
            self.assertTrue(jarvis_main.is_interrupt_command(text), text)

    def test_ordinary_command_is_not_interrupt(self):
        for text in ("открой почту", "который час", "расскажи анекдот"):
            self.assertFalse(jarvis_main.is_interrupt_command(text), text)


class SpeakingWatchdogTest(unittest.TestCase):
    """Страховка от «залипшей» озвучки: флаг не должен висеть вечно."""

    def setUp(self):
        state.stop_speaking()

    def tearDown(self):
        state.stop_speaking()

    def test_not_speaking_is_zero(self):
        self.assertEqual(state.speaking_for(), 0.0)

    def test_speaking_timer_grows(self):
        state.start_speaking("Доброй ночи")
        time.sleep(0.2)
        self.assertGreaterEqual(state.speaking_for(), 0.15)
        self.assertLess(state.speaking_for(), 5.0)

    def test_stop_resets_timer(self):
        state.start_speaking("тест")
        time.sleep(0.1)
        state.stop_speaking()
        self.assertEqual(state.speaking_for(), 0.0)
        self.assertFalse(state.is_speaking)

    def test_restart_refreshes_timer(self):
        """Новая фраза сбрасывает отсчёт — watchdog не сработает зря."""
        state.start_speaking("первая")
        time.sleep(0.2)
        state.start_speaking("вторая")
        self.assertLess(state.speaking_for(), 0.2)


class TtsBreakerTest(unittest.TestCase):
    """Предохранитель озвучки: отказавший сервис не должен тормозить каждую фразу.

    Без него каждая реплика сначала ждала 2-7 секунд отказа Fish (402) и
    ElevenLabs (таймаут) — ассистент казался «задумчивым» на ровном месте.
    """

    def setUp(self):
        from voice import text_to_speech as tts

        self.tts = tts
        tts.reset_tts_breakers()

    def tearDown(self):
        self.tts.reset_tts_breakers()

    def test_healthy_provider_is_used(self):
        self.assertFalse(self.tts.tts_provider_is_down("fish"))

    def test_down_provider_is_skipped(self):
        self.tts.mark_tts_down("fish", "HTTP 402")
        self.assertTrue(self.tts.tts_provider_is_down("fish"))
        self.assertEqual(self.tts.tts_provider_down_reason("fish"), "HTTP 402")

    def test_recovery_after_cooldown(self):
        self.tts.mark_tts_down("fish", "HTTP 402")
        self.tts.mark_tts_up("fish")            # сервис ожил
        self.assertFalse(self.tts.tts_provider_is_down("fish"))

    def test_speak_skips_dead_fish_without_delay(self):
        """Отказавший Fish не вызывается — идём сразу в запасной голос."""
        from voice.text_to_speech import FishTTS

        spoken = []
        fish = FishTTS()
        fish.stop = lambda: None
        fish._speak_fish = lambda text: spoken.append("fish")
        fish.fallback.speak = lambda text: spoken.append("fallback")
        self.tts.mark_tts_down("fish", "HTTP 402")

        started = time.monotonic()
        fish.speak("проверка")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(spoken, ["fallback"])

    def test_speak_still_uses_live_fish(self):
        """Настроенный Fish.audio должен побеждать фолбэк.

        Раньше тест опирался на FISH_VOICE_ID, захардкоженный в config.py.
        Его убрали: клон голоса персонажа из фильма нельзя раздавать
        покупателям, а reference-модель fish.audio всё равно привязана к
        одному аккаунту. Теперь голос задаёт пользователь, поэтому тест
        задаёт его сам — и проверяет то, что реально важно: при живом
        Fish говорит Fish, а не ElevenLabs.
        """
        from voice.text_to_speech import FishTTS

        spoken = []
        with mock.patch(
            "voice.text_to_speech.FISH_VOICE_ID", "тестовый-голос"
        ):
            fish = FishTTS()
        fish.stop = lambda: None
        fish._speak_fish = lambda text: spoken.append("fish")
        fish.fallback.speak = lambda text: spoken.append("fallback")

        fish.speak("проверка")
        self.assertEqual(spoken, ["fish"])

    def test_fish_without_voice_id_falls_back(self):
        """Без своего голоса Fish не должен считаться готовым.

        Раньше это была неявная зависимость от чужого голоса. Теперь
        голос пустой → Fish выключен, речь уходит в ElevenLabs, и это
        должен быть штатный путь, а не ошибка.
        """
        from voice.text_to_speech import FishTTS

        spoken = []
        with mock.patch("voice.text_to_speech.FISH_VOICE_ID", ""):
            fish = FishTTS()
        fish.stop = lambda: None
        fish._speak_fish = lambda text: spoken.append("fish")
        fish.fallback.speak = lambda text: spoken.append("fallback")

        fish.speak("проверка")
        self.assertEqual(spoken, ["fallback"])


class ElevenLabsAvailabilityTest(unittest.TestCase):
    """Отвечающий сервис не должен объявляться мёртвым из-за тела ответа.

    Раньше не-JSON (заглушка прокси) ронял проверку в except, и ElevenLabs
    выключался на 15 минут, хотя синтез через него прекрасно работал бы.
    """

    def _check_with_response(self, response):
        from unittest import mock
        from voice import text_to_speech as tts

        tts.reset_tts_breakers()
        with mock.patch.object(tts, "ELEVENLABS_API_KEY", "test-key"), \
             mock.patch.object(tts.requests, "get", return_value=response):
            speaker = tts.ElevenLabsTTS()
        return speaker

    def test_http_200_with_non_json_body_is_available(self):
        class Resp:
            ok = True
            status_code = 200

            def json(self):
                raise ValueError("Expecting value: line 1 column 1")

        speaker = self._check_with_response(Resp())
        self.assertTrue(speaker._api_available)

    def test_http_403_is_unavailable(self):
        class Resp:
            ok = False
            status_code = 403

            def json(self):
                return {}

        speaker = self._check_with_response(Resp())
        self.assertFalse(speaker._api_available)


if __name__ == "__main__":
    unittest.main()
