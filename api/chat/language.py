"""Message language detection (CT-21, DA-9).

`langdetect` with a fixed seed, so the same text always gets the same answer. The rule
(CHAT-10, CHAT-95):

- fewer than `MIN_ALPHA_CHARS` letters → `fallback` (short texts like "ok" are unreliable);
- main guess is `pt` or `en` → that language;
- main guess is another language with probability ≥ `OTHER_MIN_PROB` → `"other"`;
- anything else, including a detector error → `fallback`, so a message is never blocked by
  an uncertain guess.
"""

from typing import Literal

from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

from api.messages import Lang

DetectedLanguage = Literal["pt", "en", "other"]

MIN_ALPHA_CHARS = 20
OTHER_MIN_PROB = 0.90

DetectorFactory.seed = 0


def detect_language(text: str, fallback: Lang) -> DetectedLanguage:
    if sum(1 for char in text if char.isalpha()) < MIN_ALPHA_CHARS:
        return fallback
    try:
        guesses = detect_langs(text)
    except LangDetectException:
        return fallback
    if not guesses:
        return fallback
    top = guesses[0]
    lang = str(top.lang)
    if lang == "pt":
        return "pt"
    if lang == "en":
        return "en"
    if float(top.prob) >= OTHER_MIN_PROB:
        return "other"
    return fallback
