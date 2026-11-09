"""Password and session service (CT-16, DA-10, AS-1, AS-7).

Passwords use argon2 (`argon2-cffi`). A session token is `secrets.token_urlsafe(32)`; only its
SHA-256 hex digest is stored (`customer_sessions.token_hash`). Sessions expire after
`Settings.session_ttl_hours`.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import cast
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import CurrentCustomer
from api.auth.models import Customer, CustomerSession
from api.config import get_settings
from api.errors import AtlasError, ErrorCode
from api.messages import Lang

_hasher = PasswordHasher()


@cache
def _dummy_hash() -> str:
    """Fixed argon2 hash checked for unknown usernames, so both failures cost the same."""
    return _hasher.hash("atlas-dummy-password-never-valid")


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def hash_token(token: str) -> str:
    """SHA-256 hex digest (64 chars) of a session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def _to_current(customer: Customer) -> CurrentCustomer:
    return CurrentCustomer(
        id=customer.id,
        email=customer.email,
        display_name=customer.display_name,
        language=cast(Lang, customer.language),
    )


async def authenticate(session: AsyncSession, username: str, password: str) -> CurrentCustomer:
    """Raise `AtlasError(INVALID_CREDENTIALS, 401)` alike for unknown user and wrong password."""
    customer = await session.scalar(select(Customer).where(Customer.username == username))
    if customer is None:
        _verify(_dummy_hash(), password)
        raise AtlasError(ErrorCode.INVALID_CREDENTIALS, 401)
    if not _verify(customer.password_hash, password):
        raise AtlasError(ErrorCode.INVALID_CREDENTIALS, 401)
    return _to_current(customer)


async def create_session(session: AsyncSession, customer_id: UUID) -> str:
    """Create a session row and return the clear-text token (only its hash is stored)."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(hours=get_settings().session_ttl_hours)
    session.add(
        CustomerSession(
            customer_id=customer_id, token_hash=hash_token(token), expires_at=expires_at
        )
    )
    await session.flush()
    return token


async def resolve_session(session: AsyncSession, token: str) -> CurrentCustomer | None:
    """Customer for a valid, unexpired token; `None` otherwise."""
    if not token:
        return None
    customer = await session.scalar(
        select(Customer)
        .join(CustomerSession, CustomerSession.customer_id == Customer.id)
        .where(
            CustomerSession.token_hash == hash_token(token),
            CustomerSession.expires_at > datetime.now(UTC),
        )
    )
    return None if customer is None else _to_current(customer)


async def revoke_session(session: AsyncSession, token: str) -> None:
    await session.execute(
        delete(CustomerSession).where(CustomerSession.token_hash == hash_token(token))
    )
