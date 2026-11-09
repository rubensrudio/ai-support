"""Message language detection (TASK-029, CHAT-10, CHAT-95)."""

import pytest
from langdetect.lang_detect_exception import LangDetectException

from api.chat import language as language_module
from api.chat.language import detect_language

GERMAN = "Ich möchte meine Bestellung von gestern stornieren, bitte helfen Sie mir dabei."


def test_portuguese_sentence_is_pt() -> None:
    assert detect_language("Quero cancelar meu pedido de ontem", fallback="en") == "pt"


def test_english_sentence_is_en() -> None:
    assert detect_language("I want to cancel my order from yesterday", fallback="pt") == "en"


def test_long_german_sentence_is_other() -> None:
    assert detect_language(GERMAN, fallback="pt") == "other"


@pytest.mark.parametrize("fallback", ["pt", "en"])
def test_short_text_returns_fallback(fallback: str) -> None:
    assert detect_language("ok", fallback=fallback) == fallback  # type: ignore[arg-type]


def test_short_foreign_text_returns_fallback() -> None:
    # Fewer than 20 alphabetic characters: never "other", even if it looks foreign.
    assert detect_language("Guten Tag, danke!", fallback="en") == "en"


def test_text_without_letters_returns_fallback() -> None:
    assert detect_language("12345 !!! ??? 67890 ... 1234567890", fallback="pt") == "pt"


def test_detector_exception_returns_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(text: str) -> list[object]:
        raise LangDetectException(0, "no features")

    monkeypatch.setattr(language_module, "detect_langs", boom)

    assert detect_language(GERMAN, fallback="en") == "en"


def test_low_confidence_foreign_returns_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Guess:
        def __init__(self, lang: str, prob: float) -> None:
            self.lang = lang
            self.prob = prob

    monkeypatch.setattr(
        language_module, "detect_langs", lambda text: [_Guess("de", 0.89), _Guess("nl", 0.11)]
    )

    assert detect_language(GERMAN, fallback="pt") == "pt"


def test_detection_is_deterministic() -> None:
    results = {detect_language(GERMAN, fallback="pt") for _ in range(5)}
    assert results == {"other"}
