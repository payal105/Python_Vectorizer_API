"""API credential verification.

Three equivalent ways for a client to authenticate, so existing
vectorizer.ai integrations (which use HTTP Basic) work unchanged:

* ``Authorization: Basic base64(id:secret)``
* ``Authorization: Bearer <id>:<secret>``
* ``X-Api-Key: <id>:<secret>``
"""

from __future__ import annotations

import base64
import binascii
import hmac
from dataclasses import dataclass

from starlette.requests import Request

from app.config import Settings
from app.core.errors import InvalidCredentials, Unauthorized

ANONYMOUS = "anonymous"


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    key_id: str
    authenticated: bool = True


def _split_pair(raw: str) -> tuple[str, str] | None:
    key_id, sep, secret = raw.partition(":")
    if not sep:
        return None
    return key_id.strip(), secret.strip()


def _extract_credentials(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "").strip()
    if header:
        scheme, _, value = header.partition(" ")
        scheme = scheme.lower()
        value = value.strip()
        if scheme == "basic":
            try:
                decoded = base64.b64decode(value, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                raise InvalidCredentials("Malformed Basic authorization header.")
            return _split_pair(decoded)
        if scheme == "bearer":
            return _split_pair(value)

    api_key = request.headers.get("x-api-key", "").strip()
    if api_key:
        return _split_pair(api_key)
    return None


def authenticate(request: Request, settings: Settings) -> Principal:
    """Resolve the caller, or raise if credentials are missing/wrong."""
    credentials = _extract_credentials(request)

    if credentials is None:
        if settings.require_auth:
            raise Unauthorized(
                "Provide API credentials via HTTP Basic auth or the X-Api-Key header."
            )
        return Principal(key_id=ANONYMOUS, authenticated=False)

    key_id, secret = credentials
    expected = settings.credentials.get(key_id)
    # Compare even on a miss so a bad key id and a bad secret take the same time.
    reference = expected if expected is not None else "\x00" * max(len(secret), 1)
    ok = hmac.compare_digest(secret, reference) and expected is not None
    if not ok:
        raise InvalidCredentials()
    return Principal(key_id=key_id)
