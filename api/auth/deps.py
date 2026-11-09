"""Authenticated principals and FastAPI dependencies (CT-16).

Skeleton for phase 02: types are final; the dependencies are implemented by TASK-025.
The customer identity comes only from the `atlas_session` cookie, never from the request body.
"""

import secrets
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.db.engine import get_db_session
from api.errors import AtlasError, ErrorCode
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
    customer = await _customer_from_cookie(request, session)
    if customer is None:
        raise AtlasError(ErrorCode.AUTH_REQUIRED, 401)
    return customer


async def require_customer_or_operator(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    x_operator_key: Annotated[str | None, Header(alias=OPERATOR_HEADER)] = None,
) -> Principal:
    """Customer cookie, or `X-Operator-Key` compared with `secrets.compare_digest`."""
    customer = await _customer_from_cookie(request, session)
    if customer is not None:
        return customer
    if x_operator_key is not None and _is_operator_key(x_operator_key):
        return OperatorPrincipal()
    raise AtlasError(ErrorCode.AUTH_REQUIRED, 401)


async def _customer_from_cookie(request: Request, session: AsyncSession) -> CurrentCustomer | None:
    # Imported here: `api.auth.service` imports `CurrentCustomer` from this module.
    from api.auth.service import resolve_session

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return await resolve_session(session, token)


def _is_operator_key(candidate: str) -> bool:
    """Constant-time check; with no configured key the operator path is disabled (LAC-35)."""
    configured = get_settings().operator_api_key
    expected = configured.get_secret_value() if configured is not None else ""
    if not expected:
        return False
    return secrets.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
