"""
Emotional Intelligence модуль для NOVA AI.
Анализирует тон пользователя, адаптирует персону и предоставляет эмпатичные ответы.
"""
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import (
    USER_PROFILE_PATH,
)
from core.secure_write import secure_write_json

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
EMOTION_STATE_FILE = PROJECT_DIR / "data" / "emotional_state.json"


@dataclass
class EmotionReading:
    """Результат анализа эмоций."""
    timestamp: float
    valence: float  # -1.0 (negative) to +1.0 (positive)
    arousal: float  # 0.0 (calm) to 1.0 (excited)
    confidence: float  # 0.0 to 1.0
    detected_emotions: List[str]  # e.g., ["happy", "frustrated", "neutral"]
    text_sample: str  # образец текста для отладки


class EmotionalState:
    """Хранилище эмоционального состояния пользователя."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or EMOTION_STATE_FILE
        self._lock = threading.RLock()
        self._history: deque = deque(maxlen=50)  # последние 50 измерений
        self._current: Optional[EmotionReading] = None
        self._ensure_file()

    def _ensure_file(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if not os.path.exists(self.path):
            self._write({})

    def _read(self) -> Dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: Dict) -> None:
        secure_write_json(self.path, data)

    def add_reading(self, reading: EmotionReading) -> None:
        """Добавляет новое измерение эмоций."""
        with self._lock:
            self._history.append(reading)
            self._current = reading
            # Также сохраняем в файл для персистентности
            data = self._read()
            data["history"] = [
                {
                    "timestamp": r.timestamp,
                    "valence": r.valence,
                    "arousal": r.arousal,
                    "confidence": r.confidence,
                    "detected_emotions": r.detected_emotions,
                    "text_sample": r.text_sample[:100]  # обрезаем для экономии места
                }
                for r in self._history
            ]
            data["current"] = {
                "timestamp": reading.timestamp,
                "valence": reading.valence,
                "arousal": reading.arousal,
                "confidence": reading.confidence,
                "detected_emotions": reading.detected_emotions,
                "text_sample": reading.text_sample[:100]
            }
            self._write(data)

    def get_current(self) -> Optional[EmotionReading]:
        """Возвращает текущее эмоциональное состояние."""
        with self._lock:
            return self._current

    def get_recent_average(self, window_seconds: int = 300) -> Optional[EmotionReading]:
        """Возвращает среднее эмоциональное состояние за последние N секунд."""
        with self._lock:
            if not self._history:
                return None

            cutoff = time.time() - window_seconds
            recent = [r for r in self._history if r.timestamp >= cutoff]
            if not recent:
                return None

            # Вычисляем средние значения
            avg_valence = sum(r.valence for r in recent) / len(recent)
            avg_arousal = sum(r.arousal for r in recent) / len(recent)
            avg_confidence = sum(r.confidence for r in recent) / len(recent)

            # Объединяем обнаруженные эмоции
            all_emotions = []
            for r in recent:
                all_emotions.extend(r.detected_emotions)

            # Находим самые частые эмоции
            from collections import Counter
            emotion_counts = Counter(all_emotions)
            top_emotions = [emotion for emotion, _ in emotion_counts.most_common(3)]

            # Берем последний текстовый образец
            latest_text = recent[-1].text_sample if recent else ""

            return EmotionReading(
                timestamp=time.time(),
                valence=avg_valence,
                arousal=avg_arousal,
                confidence=avg_confidence,
                detected_emotions=top_emotions,
                text_sample=latest_text
            )

    def get_trend(self, window_seconds: int = 600) -> str:
        """Определяет тренд эмоционального состояния."""
        with self._lock:
            if len(self._history) < 2:
                return "stable"

            cutoff = time.time() - window_seconds
            recent = [r for r in self._history if r.timestamp >= cutoff]
            if len(recent) < 2:
                return "stable"

            # Сравниваем первую и вторую половины периода
            mid_point = len(recent) // 2
            first_half = recent[:mid_point]
            second_half = recent[mid_point:]

            if not first_half or not second_half:
                return "stable"

            first_avg = sum(r.valence for r in first_half) / len(first_half)
            second_avg = sum(r.valence for r in second_half) / len(second_half)

            diff = second_avg - first_avg
            if diff > 0.1:
                return "improving"
            elif diff < -0.1:
                return "declining"
            else:
                return "stable"


class EmotionAnalyzer:
    """Анализатор эмоций из текста и аудио сигналов."""

    def __init__(self) -> None:
        # Простые лексиконы для начала (можно расширить)
        self.positive_words = {
            "хорошо", "отлично", "замечательно", "прекрасно", "великолепно",
            "радует", "обрадовал", "счастлив", "весело", "радость",
            "благодарю", "спасибо", "превосходно", "идеально", "супер",
            "круто", "класс", "ловко", "умница", "молодец", " bravo",
            "wonderful", "great", "excellent", "fantastic", "amazing",
            "awesome", "brilliant", "splendid", "marvelous", "terrific",
            "delighted", "pleased", "happy", "joy", "glad", "thank",
            "thanks", "grateful", "appreciate"
        }

        self.negative_words = {
            "плохо", "ужасно", "отвратительно", "кошмар", "беда",
            "грустно", "печаль", "тоска", "разочарован", "зол", "гнев",
            "ярость", "ненависть", "отвращение", "боюсь", "страшно",
            "тревога", "встревожен", "нервничаю", "стресс", "усталость",
            "устал", "измотан", "раздражает", "бесит", " ненавижу",
            "bad", "terrible", "awful", "horrible", "hate", "angry",
            "furious", "mad", "sad", "unhappy", "depressed", "miserable",
            "worried", "anxious", "nervous", "stressed", "tired", "exhausted",
            "frustrated", "irritated", "annoyed", "bothered"
        }

        self.calm_indicators = {
            "спокойно", "успокаивает", "relax", "relaxing", "calm",
            "peaceful", "serene", "tranquil", "meditate", "meditation",
            "deep breath", "дышать", "дыхание"
        }

        self.excited_indicators = {
            "взволнован", "восторг", "энтузиазм", "возбуждён",
            "excited", "thrilled", "pumped", "amped", "energetic",
            "enthusiastic", "eager", "keen", "psyched", "stoked"
        }

    def analyze_text(self, text: str) -> EmotionReading:
        """Анализирует эмоциональную окраску текста."""
        if not text or not text.strip():
            return EmotionReading(
                timestamp=time.time(),
                valence=0.0,
                arousal=0.5,
                confidence=0.1,
                detected_emotions=["neutral"],
                text_sample=""
            )

        text_lower = text.lower()
        words = set(text_lower.split())

        # Простой подсчет совпадений
        pos_matches = len(words & self.positive_words)
        neg_matches = len(words & self.negative_words)
        calm_matches = len(words & self.calm_indicators)
        excited_matches = len(words & self.excited_indicators)

        total_words = len(words)
        if total_words == 0:
            total_words = 1  # избегаем деления на ноль

        # Нормализуем по длине текста
        pos_score = min(pos_matches / max(total_words * 0.1, 1), 1.0)
        neg_score = min(neg_matches / max(total_words * 0.1, 1), 1.0)
        calm_score = min(calm_matches / max(total_words * 0.1, 1), 1.0)
        excited_score = min(excited_matches / max(total_words * 0.1, 1), 1.0)

        # Вычисляем валенс (позитив/негатив)
        valence = pos_score - neg_score
        valence = max(-1.0, min(1.0, valence))  # зажимаем в [-1, 1]

        # Вычисляем arousal (возбуждение/спокойствие)
        arousal = (excited_score + (1.0 - calm_score)) / 2.0
        arousal = max(0.0, min(1.0, arousal))

        # Определяем уверенность на основе количества совпадений
        total_matches = pos_matches + neg_matches + calm_matches + excited_matches
        confidence = min(total_matches / max(len(text.split()) * 0.3, 1), 1.0)
        confidence = max(0.1, confidence)  # минимум 10% уверенности

        # Определяем доминирующие эмоции
        emotions = []
        if pos_score > 0.3:
            emotions.append("positive")
        if neg_score > 0.3:
            emotions.append("negative")
        if calm_score > 0.3:
            emotions.append("calm")
        if excited_score > 0.3:
            emotions.append("excited")
        if not emotions:
            emotions.append("neutral")

        return EmotionReading(
            timestamp=time.time(),
            valence=valence,
            arousal=arousal,
            confidence=confidence,
            detected_emotions=emotions,
            text_sample=text[:100]
        )

    def analyze_voice_features(self, pitch_variance: float, speaking_rate: float,
                              volume_level: float) -> EmotionReading:
        """Анализирует эмоции по аудио особенностям (заглушка для будущего расширения)."""
        # Это будет расширено при интеграции с реальным аудио анализом
        # Пока возвращаем нейтральное состояние
        return EmotionReading(
            timestamp=time.time(),
            valence=0.0,
            arousal=0.5,
            confidence=0.2,
            detected_emotions=["unknown"],
            text_sample=""
        )


class PersonaAdapter:
    """Адаптирует персону NOVA AI в зависимости от эмоционального состояния пользователя."""

    PERSONA_MODIFIERS = {
        "sol": {  # базовая персонa
            "distressed": "terra",   # переключаем на более заботливую
            "excited": "luna",       # переключаем на более игривую
            "calm": "sol",           # оставляем как есть
            "default": "sol"
        },
        "luna": {  # игривая персонa
            "distressed": "terra",   # в стрессе становится более серьезной
            "excited": "luna",       # остается игривой
            "calm": "sol",           # в спокойном состоянии становится более сбалансированной
            "default": "luna"
        },
        "terra": {  # заботливая персонa
            "distressed": "terra",   # остается заботливой
            "excited": "luna",       # в восторге становится более игривой
            "calm": "sol",           # в спокойном состоянии становится более сбалансированной
            "default": "terra"
        }
    }

    @staticmethod
    def adapt_persona(current_persona: str, emotional_state: EmotionReading) -> str:
        """Возвращает адаптированную персону на основе эмоционального состояния."""
        # Определяем общее эмоциональное состояние
        if emotional_state.valence < -0.3 and emotional_state.confidence > 0.5:
            mood_key = "distressed"
        elif emotional_state.valence > 0.3 and emotional_state.arousal > 0.6 and emotional_state.confidence > 0.5:
            mood_key = "excited"
        elif emotional_state.arousal < 0.4 and emotional_state.confidence > 0.5:
            mood_key = "calm"
        else:
            mood_key = "default"

        modifiers = PersonaAdapter.PERSONA_MODIFIERS.get(
            current_persona,
            PersonaAdapter.PERSONA_MODIFIERS["sol"]
        )

        return modifiers.get(mood_key, modifiers["default"])


# Глобальные экземпляры
_emotional_state: Optional[EmotionalState] = None
_emotional_state_lock = threading.Lock()
_emotion_analyzer = EmotionAnalyzer()
_persona_adapter = PersonaAdapter()


def get_emotional_state() -> EmotionalState:
    """Возвращает глобальный экземпляр эмоционального состояния."""
    global _emotional_state
    if _emotional_state is None:
        with _emotional_state_lock:
            if _emotional_state is None:
                _emotional_state = EmotionalState()
    return _emotional_state


def analyze_user_emotion(text: str = "",
                        pitch_variance: float = 0.0,
                        speaking_rate: float = 0.0,
                        volume_level: float = 0.5) -> EmotionReading:
    """Анализирует эмоции пользователя и обновляет глобальное состояние."""
    # Сначала пробуем текстовый анализ
    if text and text.strip():
        reading = _emotion_analyzer.analyze_text(text)
    else:
        # Если нет текста, пробуем аудио анализ
        reading = _emotion_analyzer.analyze_voice_features(
            pitch_variance, speaking_rate, volume_level
        )

    # Обновляем глобальное состояние
    state = get_emotional_state()
    state.add_reading(reading)

    return reading


def get_adapted_persona(current_persona: str) -> str:
    """Возвращает персону, адаптированную под текущее эмоциональное состояние пользователя."""
    state = get_emotional_state()
    current = state.get_current()
    if current is None:
        return current_persona  # нет данных, возвращаем исходную персону

    return _persona_adapter.adapt_persona(current_persona, current)


def get_empathy_response(base_response: str, emotional_state: EmotionReading) -> str:
    """Добавляет эмпатичный префикс или суффикс к базовому ответу."""
    if emotional_state.confidence < 0.3:
        return base_response  # низкая уверенность - не модифицируем

    valence = emotional_state.valence
    arousal = emotional_state.arousal

    # Добавляем эмпатичные элементы в зависимости от эмоций
    if valence < -0.5:  # сильно негативные эмоции
        empathy_prefixes = [
            "Понимаю, что это непросто, сэр. ",
            "Сочувствую вашему состоянию, сэр. ",
            "Вижу, что вы переживаете, сэр. ",
            "Сначала позвольте выразить поддержку, сэр. "
        ]
        import random
        prefix = random.choice(empathy_prefixes)
        return prefix + base_response

    elif valence > 0.5:  # сильно позитивные эмоции
        empathy_suffixes = [
            " Рад за вас, сэр!",
            " Как приятно это слышать, сэр!",
            " Прекрасные новости, сэр!",
            " Восхитительно, сэр!"
        ]
        import random
        suffix = random.choice(empathy_suffixes)
        return base_response + suffix

    elif arousal > 0.7:  # высокое возбуждение
        calming_suffixes = [
            " Давайте действовать спокойно и методично, сэр.",
            " Сохраним хладнокровие и сосредоточимся, сэр.",
            " Постепенно и уверенно движемся вперёд, сэр."
        ]
        import random
        suffix = random.choice(calming_suffixes)
        return base_response + suffix

    elif arousal < 0.3:  # низкое возбуждение (апатия/усталость)
        energizing_suffixes = [
            " Давайте добавим немного энергии в наши действия, сэр.",
            " Постепенно набираем темп, сэр.",
            " Маленькие шаги приведут к большому результату, сэр."
        ]
        import random
        suffix = random.choice(energizing_suffixes)
        return base_response + suffix

    return base_response  # нейтральное состояние - без изменений


# Инструмент для доступа из AI
def get_emotion_summary() -> str:
    """Возвращает краткое резюме текущего эмоционального состояния для использования в промптах."""
    state = get_emotional_state()
    current = state.get_current()
    if current is None:
        return "Эмоциональное состояние: данные недоступны"

    valence_desc = ""
    if current.valence > 0.3:
        valence_desc = "пользователь в позитивном настроении"
    elif current.valence < -0.3:
        valence_desc = "пользователь в негативном настроении"
    else:
        valence_desc = "пользователь в нейтральном настроении"

    arousal_desc = ""
    if current.arousal > 0.6:
        arousal_desc = ", высокий уровень энергии"
    elif current.arousal < 0.4:
        arousal_desc = ", низкий уровень энергии (спокоен/устал)"
    else:
        arousal_desc = ", умеренный уровень энергии"

    emotions_str = ", ".join(current.detected_emotions) if current.detected_emotions else "нейтральное"

    return f"Эмоциональное состояние: {valence_desc}{arousal_desc}. Обнаружены эмоции: {emotions_str}. Уверенность: {int(current.confidence * 100)}%"


def record_emotion_from_text(text: str) -> str:
    """Инструмент для записи эмоций из текста (вызывается AI)."""
    try:
        reading = analyze_user_emotion(text=text)
        state = get_emotional_state()
        state.add_reading(reading)

        valence = reading.valence
        if valence > 0.3:
            return f"Записал позитивное настроение (валенс: {valence:.2f})."
        elif valence < -0.3:
            return f"Зафиксировал негативное настроение (валенс: {valence:.2f})."
        else:
            return f"Зарегистрировано нейтральное состояние."
    except Exception as e:
        logger.error(f"Ошибка записи эмоций: {e}")
        return "Не удалось проанализировать эмоции из текста."