"""Ticket triage schemas (CT-48, TRI-01, TRI-05, TRI-06, TRI-08, TRI-90, LAC-11).

Closed enums and `extra='forbid'` on both models. The request carries only the text: no
`conversation_id` (TRI-05). The text must have 1..`ATLAS_MAX_MESSAGE_CHARS` characters and
cannot be only whitespace (TRI-90). `resumo` is written in the language of the input (TRI-08);
the enum values are always the fixed Portuguese codes below.
"""

from typing import Literal

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
