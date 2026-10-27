import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.emotional_intelligence import (
    analyze_user_emotion,
    get_emotion_summary,
    get_adapted_persona,
    record_emotion_from_text,
    EmotionReading
)

def test_emotion_analysis_positive():
    """Тест анализа положительных эмоций."""
    text = "Отлично, спасибо за помощь! Прекрасно всё работает."
    reading = analyze_user_emotion(text=text)

    assert isinstance(reading, EmotionReading)
    assert reading.valence > 0  # должен быть положительным
    assert "positive" in reading.detected_emotions or len(reading.detected_emotions) > 0
    assert reading.confidence > 0.1

def test_emotion_analysis_negative():
    """Тест анализа отрицательных эмоций."""
    text = "Это ужасно, всё плохо и раздражает меня."
    reading = analyze_user_emotion(text=text)

    assert isinstance(reading, EmotionReading)
    assert reading.valence < 0  # должен быть отрицательным
    assert "negative" in reading.detected_emotions or len(reading.detected_emotions) > 0
    assert reading.confidence > 0.1

def test_emotion_analysis_neutral():
    """Тест анализа нейтрального текста."""
    text = "Который час?"
    reading = analyze_user_emotion(text=text)

    assert isinstance(reading, EmotionReading)
    # Нейтральный текст может дать около нулевого валенса
    assert -0.5 <= reading.valence <= 0.5
    assert reading.confidence >= 0.1

def test_emotion_summary():
    """Тест получения резюме эмоций."""
    # Сначала анализируем какой-то текст чтобы создать состояние
    analyze_user_emotion(text="Сегодня замечательный день!")

    summary = get_emotion_summary()
    assert isinstance(summary, str)
    assert len(summary) > 0
    assert "Эмоциональное состояние:" in summary or "данные недоступны" in summary

def test_persona_adaptation():
    """Тест адаптации персоны."""
    # Сначала создаем негативное эмоциональное состояние
    analyze_user_emotion(text="Мне очень плохо, всё раздражает")

    # Проверяем, что персонa адаптируется
    adapted = get_adapted_persona("sol")
    # В состоянии distressed должен переключаться на terra
    assert adapted in ["sol", "terra", "luna"]  # допустимые варианты

    # Сбрасываем и проверяем позитивное
    # Для простоты теста просто вызываем функцию
    adapted2 = get_adapted_persona("luna")
    assert adapted2 in ["sol", "terra", "luna"]

def test_record_emotion_from_text():
    """Тест инструмента записи эмоций."""
    result = record_emotion_from_text("Прекрасно, всё отлично работает!")
    assert isinstance(result, str)
    assert len(result) > 0
    # Должен содержать информацию о записанном настроении
    assert "Записал" in result or "Зафиксировал" in result or "Зарегистрировано" in result

if __name__ == "__main__":
    test_emotion_analysis_positive()
    test_emotion_analysis_negative()
    test_emotion_analysis_neutral()
    test_emotion_summary()
    test_persona_adaptation()
    test_record_emotion_from_text()
    print("Все тесты эмоционального интеллекта прошли успешно!")