"""Message language detection (CT-21, DA-9).

`langdetect` with a fixed seed, so the same text always gets the same answer. The rule
(CHAT-10, CHAT-95):

- fewer than `MIN_ALPHA_CHARS` letters → `fallback` (short texts like "ok" are unreliable);
- main guess is `pt` or `en` → that language;
- main guess is another language with probability ≥ `OTHER_MIN_PROB` → `"other"`, except a
  short text (fewer than `SHORT_TEXT_ALPHA_CHARS` letters) guessed as a language close to
  Portuguese (`PT_CONFUSABLE`: es, gl, it) that also carries Portuguese evidence
  (`_looks_portuguese`: ã/õ/ç or words absent from Spanish and Italian, such as "você", "não",
  "custa", "meu"):
  short Portuguese questions are often detected as Spanish with high confidence ("Quanto custa
  o plano Plus?" → es 0.99), so they get `"pt"`. Short French, Italian or Spanish without that
  evidence still get `"other"` (CHAT-95);
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
SHORT_TEXT_ALPHA_CHARS = 40
PT_CONFUSABLE = frozenset({"es", "gl", "it"})
_PT_CHARS = frozenset("ãõç")
# Portuguese words that do not exist (as written) in Spanish or Italian. A few are shared with
# Galician ("meu", "minha"), which is close enough to Portuguese to be answered in it.
_PT_WORDS = frozenset(
    {
        "você",
        "voce",
        "vocês",
        "voces",
        "não",
        "nao",
        "uma",
        "um",
        "já",
        "também",
        "tambem",
        "isso",
        "ainda",
        "quero",
        "pra",
        "obrigado",
        "obrigada",
        "assinatura",
        "custa",
        "custo",
        "meu",
        "minha",
        "meus",
        "minhas",
        "cadê",
        "cade",
        "tá",
    }
)

DetectorFactory.seed = 0


def detect_language(text: str, fallback: Lang) -> DetectedLanguage:
    letters = sum(1 for char in text if char.isalpha())
    if letters < MIN_ALPHA_CHARS:
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
    if lang in PT_CONFUSABLE and letters < SHORT_TEXT_ALPHA_CHARS and _looks_portuguese(text):
        return "pt"
    if float(top.prob) >= OTHER_MIN_PROB:
        return "other"
    return fallback


def _looks_portuguese(text: str) -> bool:
    lowered = text.lower()
    if any(char in _PT_CHARS for char in lowered):
        return True
    words = {word.strip(".,!?;:\"'()") for word in lowered.split()}
    return bool(words & _PT_WORDS)
