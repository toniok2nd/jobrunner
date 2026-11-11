import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import get_settings

_ph = PasswordHasher()

TOKEN_PREFIX = "jr_"
SESSION_COOKIE = "jobrunner_session"


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="jobrunner-session")


def create_session_cookie(username: str) -> str:
    return _signer().dumps(username)


def read_session_cookie(value: str) -> str | None:
    try:
        return _signer().loads(
            value, max_age=get_settings().session_max_age_seconds
        )
    except (BadSignature, SignatureExpired):
        return None


def generate_token() -> str:
    """Plaintext API token, shown once. Only its SHA-256 is stored."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
