"""Local token counting (CT-8, DA-9).

Uses `tiktoken` with the `o200k_base` encoding. It is an estimate for context budgets, chunking
and the context log; cost always comes from the `usage` the provider returns.
"""

import json
from functools import lru_cache

import tiktoken

from api.llm.client import ChatMessage, SystemBlock

ENCODING_NAME = "o200k_base"
# Rough per-message framing overhead (role markers and separators), as in chat formats.
MESSAGE_OVERHEAD_TOKENS = 4


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(ENCODING_NAME)


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_encoding().encode(text, disallowed_special=()))


def count_messages(system: list[SystemBlock], messages: list[ChatMessage]) -> int:
    """Estimated prompt tokens: system blocks plus every message with its framing overhead."""
    total = sum(count_tokens(block.text) for block in system)
    for message in messages:
        total += MESSAGE_OVERHEAD_TOKENS + count_tokens(message.content)
        for call in message.tool_calls:
            total += count_tokens(call.name) + count_tokens(
                json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
            )
    return total
