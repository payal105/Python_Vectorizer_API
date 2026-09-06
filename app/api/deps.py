"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.requests import Request

from app.config import Settings, get_settings
from app.core.credits import CreditStore
from app.core.errors import RateLimited
from app.core.ratelimit import RateLimiter
from app.core.security import Principal, authenticate
from app.services.pipeline import Vectorizer

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


def get_credit_store(request: Request) -> CreditStore:
    return request.app.state.credit_store


def get_vectorizer(request: Request) -> Vectorizer:
    return request.app.state.vectorizer


# Declared purely so OpenAPI advertises Basic auth and Swagger UI renders an
# Authorize button. It never raises (auto_error=False); authenticate() reads
# the raw request and remains the single source of truth, so the X-Api-Key and
# Bearer forms keep working too.
_basic_scheme = HTTPBasic(auto_error=False, description="API id and secret.")


def get_principal(
    request: Request,
    settings: SettingsDep,
    _credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic_scheme)] = None,
) -> Principal:
    principal = authenticate(request, settings)
    request.state.principal = principal
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
CreditStoreDep = Annotated[CreditStore, Depends(get_credit_store)]
VectorizerDep = Annotated[Vectorizer, Depends(get_vectorizer)]


def enforce_rate_limit(
    principal: PrincipalDep,
    limiter: RateLimiterDep,
) -> Principal:
    """Throttle per API key. Depend on this from every billable endpoint."""
    allowed, remaining, retry_after = limiter.check(principal.key_id)
    if not allowed:
        raise RateLimited(
            f"Rate limit exceeded. Retry in {retry_after:.1f}s.",
            headers={
                "Retry-After": str(max(1, int(retry_after) + 1)),
                "X-RateLimit-Remaining": "0",
            },
        )
    return principal


ThrottledPrincipalDep = Annotated[Principal, Depends(enforce_rate_limit)]
