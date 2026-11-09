"""Authenticated principals and FastAPI dependencies (CT-16).

Skeleton for phase 02: types are final; the dependencies are implemented by TASK-025.
The customer identity comes only from the `atlas_session` cookie, never from the request body.
"""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.engine import get_db_session
from api.messages import Lang

SESSION_COOKIE = "atlas_session"
OPERATOR_HEADER = "X-Operator-Key"


@dataclass(frozen=True)
class CurrentCustomer:
    id: UUID
    email: str
    display_name: str
    language: Lang


@dataclass(frozen=True)
class OperatorPrincipal:
    """The Operator, authenticated by `X-Operator-Key` (LAC-35). Disabled when no key is set."""

    name: str = "operator"


Principal = CurrentCustomer | OperatorPrincipal


async def require_customer(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CurrentCustomer:
    """Resolve the `atlas_session` cookie; 401 `AUTH_REQUIRED` when missing, unknown or expired."""
    raise NotImplementedError("TASK-025")


async def require_customer_or_operator(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    x_operator_key: Annotated[str | None, Header(alias=OPERATOR_HEADER)] = None,
) -> Principal:
    """Customer cookie, or `X-Operator-Key` compared with `secrets.compare_digest`."""
    raise NotImplementedError("TASK-025")
