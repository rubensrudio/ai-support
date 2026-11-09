"""Login, logout and me endpoints (CT-17, DA-10, AS-1).

The session token travels only in the `atlas_session` cookie (`HttpOnly`, `SameSite=Strict`,
`Path=/`, `Secure` when `ATLAS_COOKIE_SECURE`). Unknown user and wrong password get the same
401 `INVALID_CREDENTIALS` response.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import service
from api.auth.deps import SESSION_COOKIE, CurrentCustomer, require_customer
from api.config import get_settings
from api.db.engine import get_db_session
from api.messages import Lang

auth_router = APIRouter(prefix="/auth", tags=["auth"])

_MAX_CREDENTIAL_CHARS = 256


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=_MAX_CREDENTIAL_CHARS)
    password: str = Field(min_length=1, max_length=_MAX_CREDENTIAL_CHARS)


class CustomerProfile(BaseModel):
    display_name: str
    language: Lang


class LoginResponse(BaseModel):
    customer: CustomerProfile


def _profile(customer: CurrentCustomer) -> CustomerProfile:
    return CustomerProfile(display_name=customer.display_name, language=customer.language)


@auth_router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> LoginResponse:
    customer = await service.authenticate(session, body.username, body.password)
    token = await service.create_session(session, customer.id)
    # Commit before the cookie leaves, so the client never holds a token that was not stored.
    await session.commit()
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return LoginResponse(customer=_profile(customer))


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await service.revoke_session(session, token)
        await session.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=get_settings().cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@auth_router.get("/me", response_model=CustomerProfile)
async def me(customer: Annotated[CurrentCustomer, Depends(require_customer)]) -> CustomerProfile:
    return _profile(customer)
