"""Shared helpers for the auth contract tests (cookie parsing, error comparison, fake answers)."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from api.auth.deps import SESSION_COOKIE
from api.llm.client import LLMResponse, Usage

PERSONA_PASSWORD = "contract-persona-pass-7f3a"  # noqa: S105 - fictitious test credential


@dataclass(frozen=True)
class SetCookie:
    """A parsed `Set-Cookie` header: the value plus its lower-cased attribute names/values."""

    name: str
    value: str
    attributes: dict[str, str]


def parse_set_cookie(header: str) -> SetCookie:
    first, *rest = (part.strip() for part in header.split(";"))
    name, _, value = first.partition("=")
    attributes: dict[str, str] = {}
    for part in rest:
        if not part:
            continue
        key, _, attr_value = part.partition("=")
        attributes[key.strip().lower()] = attr_value.strip()
    return SetCookie(name=name.strip(), value=value.strip(), attributes=attributes)


def session_cookie_from(response: httpx.Response) -> SetCookie:
    parsed = [parse_set_cookie(header) for header in response.headers.get_list("set-cookie")]
    cookies = [cookie for cookie in parsed if cookie.name == SESSION_COOKIE]
    assert len(cookies) == 1, f"expected one {SESSION_COOKIE} Set-Cookie, got {cookies}"
    return cookies[0]


def cookie_header(token: str) -> dict[str, str]:
    """Explicit `Cookie` header, so tests do not depend on the client cookie jar."""
    return {"Cookie": f"{SESSION_COOKIE}={token}"}


def without_trace_id(body: dict[str, dict[str, str]]) -> dict[str, str]:
    error = dict(body["error"])
    error.pop("trace_id", None)
    return error


def fake_answer(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


LoginFn = Callable[[str], Awaitable[str]]
