"""Message language detection (CT-21, DA-9). Skeleton stub: implemented by TASK-029."""

from typing import Literal

from api.messages import Lang

DetectedLanguage = Literal["pt", "en", "other"]


def detect_language(text: str, fallback: Lang) -> DetectedLanguage:
    raise NotImplementedError("TASK-029")
