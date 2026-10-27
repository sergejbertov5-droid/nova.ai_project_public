"""NOVA AI voice package.

Submodules are imported lazily so that text-only modes (AgentRuntime,
tests without audio hardware) work without optional dependencies such as
pyaudio.  Call `_ensure_speech_to_text()` or `_ensure_text_to_speech()`
before using those classes.
"""

from voice.state import (
    is_speaking,
    start_speaking,
    stop_speaking,
    get_is_speaking,
    get_last_spoken,
)

_SpeechRecognitionSTT = None
_quick_listen = None
_ElevenLabsTTS = None


def _ensure_speech_to_text():
    """Import speech_to_text on demand.  Raises ImportError if pyaudio is missing."""
    global _SpeechRecognitionSTT, _quick_listen
    if _SpeechRecognitionSTT is None:
        try:
            from voice.speech_to_text import SpeechRecognitionSTT, quick_listen
        except ImportError as exc:
            # Provide a clear message about the missing dependency
            raise ImportError(
                "voice.speech_to_text requires pyaudio. "
                "Install with: pip install pyaudio"
            ) from exc
        _SpeechRecognitionSTT = SpeechRecognitionSTT
        _quick_listen = quick_listen
    return _SpeechRecognitionSTT, _quick_listen


def _ensure_text_to_speech():
    """Import text_to_speech on demand.  Raises ImportError if dependencies are missing."""
    global _ElevenLabsTTS
    if _ElevenLabsTTS is None:
        try:
            from voice.text_to_speech import ElevenLabsTTS
        except ImportError as exc:
            raise ImportError(
                "voice.text_to_speech dependencies not available."
            ) from exc
        _ElevenLabsTTS = ElevenLabsTTS
    return _ElevenLabsTTS


# Backwards-compatible getters
def __getattr__(name: str):
    if name == "SpeechRecognitionSTT":
        return _ensure_speech_to_text()[0]
    if name == "quick_listen":
        return _ensure_speech_to_text()[1]
    if name == "ElevenLabsTTS":
        return _ensure_text_to_speech()
    raise AttributeError(f"module 'voice' has no attribute {name!r}")


__all__ = [
    "SpeechRecognitionSTT",
    "ElevenLabsTTS",
    "quick_listen",
    "is_speaking",
    "start_speaking",
    "stop_speaking",
    "get_is_speaking",
    "get_last_spoken",
    "_ensure_speech_to_text",
    "_ensure_text_to_speech",
]
