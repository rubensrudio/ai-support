from api.llm.client import ChatMessage, SystemBlock, ToolCall
from api.llm.tokens import count_messages, count_tokens

_WORDS = (
    "the customer asked about the refund policy for an order that arrived late and damaged "
    "so the agent checked the knowledge base before answering with the correct steps"
).split()


def _english_text(n_words: int) -> str:
    return " ".join(_WORDS[i % len(_WORDS)] for i in range(n_words))


def test_count_tokens_empty_is_zero() -> None:
    assert count_tokens("") == 0


def test_count_tokens_2000_english_words_is_in_expected_range() -> None:
    tokens = count_tokens(_english_text(2000))
    assert 1500 <= tokens <= 3500


def test_count_messages_is_at_least_sum_of_parts() -> None:
    system = [SystemBlock("You are a support agent."), SystemBlock("Answer in English.")]
    messages = [
        ChatMessage(role="user", content="Where is my order 42?"),
        ChatMessage(
            role="assistant",
            content="Let me check.",
            tool_calls=[ToolCall(id="t1", name="get_order", arguments={"order_id": "42"})],
        ),
        ChatMessage(role="tool", content='{"status": "shipped"}', tool_call_id="t1"),
    ]
    parts = sum(count_tokens(b.text) for b in system) + sum(
        count_tokens(m.content) for m in messages
    )

    total = count_messages(system, messages)

    assert total >= parts
    assert total > count_messages(system, messages[:1])
    assert count_messages(system, []) >= sum(count_tokens(b.text) for b in system)


def test_count_messages_empty_is_zero() -> None:
    assert count_messages([], []) == 0
