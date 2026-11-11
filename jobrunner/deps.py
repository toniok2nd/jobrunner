from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import ApiToken
from .security import SESSION_COOKIE, hash_token, read_session_cookie


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


def require_token(
    request: Request, db: Session = Depends(get_db)
) -> ApiToken:
    """Bearer-token auth for the job/run REST API."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token_hash = hash_token(auth.removeprefix("Bearer ").strip())
    row = db.scalar(
        select(ApiToken).where(
            ApiToken.token_hash == token_hash, ApiToken.revoked.is_(False)
        )
    )
    if row is None:
        raise HTTPException(status_code=401, detail="invalid or revoked token")
    return row
