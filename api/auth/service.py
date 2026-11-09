"""Password and session service (CT-16, DA-10). Skeleton stub: implemented by TASK-025.

Passwords use argon2 (`argon2-cffi`). A session token is `secrets.token_urlsafe(32)`; only its
SHA-256 hex digest is stored (`customer_sessions.token_hash`). Sessions expire after
`Settings.session_ttl_hours`.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import CurrentCustomer


def hash_password(raw: str) -> str:
    raise NotImplementedError("TASK-025")


def hash_token(token: str) -> str:
    """SHA-256 hex digest (64 chars) of a session token."""
    raise NotImplementedError("TASK-025")


async def authenticate(session: AsyncSession, username: str, password: str) -> CurrentCustomer:
    """Raise `AtlasError(INVALID_CREDENTIALS, 401)` alike for unknown user and wrong password."""
    raise NotImplementedError("TASK-025")


async def create_session(session: AsyncSession, customer_id: UUID) -> str:
    """Create a session row and return the clear-text token (only its hash is stored)."""
    raise NotImplementedError("TASK-025")


async def resolve_session(session: AsyncSession, token: str) -> CurrentCustomer | None:
    """Customer for a valid, unexpired token; `None` otherwise."""
    raise NotImplementedError("TASK-025")


async def revoke_session(session: AsyncSession, token: str) -> None:
    raise NotImplementedError("TASK-025")
