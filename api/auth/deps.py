"""Authenticated principals (CT-16).

Phase 01 skeleton: only the `CurrentCustomer` value type, so the chat service signature
(CT-15) is final from the start. TASK-025 adds `require_customer`, `OperatorPrincipal`,
`Principal` and `require_customer_or_operator` here.
"""

from dataclasses import dataclass
from uuid import UUID

from api.messages import Lang


@dataclass(frozen=True)
class CurrentCustomer:
    id: UUID
    email: str
    display_name: str
    language: Lang
