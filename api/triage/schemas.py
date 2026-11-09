"""Ticket triage schemas (CT-48, TRI-01, TRI-05, TRI-06, TRI-08, TRI-90, LAC-11).

Closed enums and `extra='forbid'` on both models. The request carries only the text: no
`conversation_id` (TRI-05). The text must have 1..`ATLAS_MAX_MESSAGE_CHARS` characters and
cannot be only whitespace (TRI-90). `resumo` is written in the language of the input (TRI-08);
the enum values are always the fixed Portuguese codes below.

`triage_output_schema()` is the schema sent to providers as native structured output: the
`TriageResult` JSON schema without the keywords that providers' strict modes reject
(`UNSUPPORTED_SCHEMA_KEYWORDS`, e.g. `minLength`). Those rules still hold because every answer
is validated with `TriageResult` afterwards.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.config import get_settings

Categoria = Literal["pedido", "assinatura", "reembolso", "entrega", "conta", "outro"]
Prioridade = Literal["baixa", "media", "alta", "urgente"]
Sentimento = Literal["positivo", "neutro", "negativo"]


class TriageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _text_rules(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        limit = get_settings().max_message_chars
        if len(value) > limit:
            raise ValueError(f"text must have at most {limit} characters")
        return value


class TriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categoria: Categoria
    prioridade: Prioridade
    sentimento: Sentimento
    precisa_humano: bool
    resumo: str = Field(min_length=1)


# Conservative union of the keywords rejected by Anthropic structured outputs and OpenAI strict
# mode (some, like `pattern`, are accepted by one of them; dropping them is harmless because
# `TriageResult` validates every answer anyway).
UNSUPPORTED_SCHEMA_KEYWORDS: frozenset[str] = frozenset(
    {
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)


def _strip_unsupported(node: Any) -> Any:
    """Drop unsupported keywords from schema nodes; never from the field names of `properties`
    or `$defs`, which are names, not keywords."""
    if isinstance(node, dict):
        stripped: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("properties", "$defs") and isinstance(value, dict):
                stripped[key] = {name: _strip_unsupported(sub) for name, sub in value.items()}
            elif key not in UNSUPPORTED_SCHEMA_KEYWORDS:
                stripped[key] = _strip_unsupported(value)
        return stripped
    if isinstance(node, list):
        return [_strip_unsupported(item) for item in node]
    return node


def triage_output_schema() -> dict[str, Any]:
    """`TriageResult` JSON schema safe for providers' native structured output."""
    schema: dict[str, Any] = _strip_unsupported(TriageResult.model_json_schema())
    return schema
