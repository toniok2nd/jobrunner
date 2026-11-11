from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import ApiToken
from .security import SESSION_COOKIE, hash_token, read_session_cookie

_api_key_header = APIKeyHeader(name="Authorization", auto_error=False)


class AuthRedirect(Exception):
    """Raised by UI routes when there is no valid session.

    Handled in main.py with a redirect to /login.
    """

    def __init__(self, url: str = "/login"):
        self.url = url


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def session_username(request: Request) -> str | None:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    return read_session_cookie(cookie)


def require_user(request: Request) -> str:
    """Session-cookie auth for HTML pages and the token-management API."""
    username = session_username(request)
    if not username:
        raise AuthRedirect()
    return username


def _lookup_token(db: Session, authorization: str | None) -> ApiToken | None:
    if not authorization:
        return None
    value = authorization.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return db.scalar(
        select(ApiToken).where(
            ApiToken.token_hash == hash_token(value), ApiToken.revoked.is_(False)
        )
    )


def require_token(
    db: Session = Depends(get_db),
    authorization: str | None = Security(_api_key_header),
) -> ApiToken:
    """Bearer-token auth for the job/run REST API.

    Accepts "Bearer jr_…" or a bare "jr_…" (Swagger UI's Authorize button).
    The APIKeyHeader security scheme makes the OpenAPI spec declare auth, so
    /api/docs gets a working "Authorize" + "Try it out" flow.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="missing bearer token")
    row = _lookup_token(db, authorization)
    if row is None:
        raise HTTPException(status_code=401, detail="invalid or revoked token")
    return row


def optional_token(
    db: Session = Depends(get_db),
    authorization: str | None = Security(_api_key_header),
) -> ApiToken | None:
    """Same lookup, but returns None instead of 401 — for public endpoints."""
    return _lookup_token(db, authorization)
