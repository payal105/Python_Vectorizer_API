"""Application factory and ASGI entrypoint."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.requests import Request

from app.api.v1.router import api_router
from app.config import Settings, get_settings
from app.core.credits import InMemoryCreditStore
from app.core.errors import FileTooLarge, register_exception_handlers
from app.core.logging import configure_logging, get_logger, set_request_id
from app.core.ratelimit import RateLimiter
from app.services.pipeline import Vectorizer

DESCRIPTION = """
Convert raster images (PNG, JPEG, WebP, GIF, BMP, TIFF) into clean,
resolution-independent vector artwork.

* **Vector output** - SVG, PDF and EPS are true vector files: the traced
  shapes become native path operators, not an embedded bitmap.
* **Raster preview** - PNG rasterizes the vector result so you can eyeball it.
* **Colour control** - cap the palette with `processing.max_colors`, or pin it
  exactly with `processing.palette`.
* **Print-ready sizing** - ask for `output.size.width=5&output.size.unit=in`
  and the PDF page comes out exactly five inches wide.

Authenticate with HTTP Basic (API id as the username, secret as the password)
or an `X-Api-Key: id:secret` header.
"""


def _strip_security_from_openapi(app: FastAPI) -> None:
    """Hide Swagger's Authorize button when the server takes no credentials.

    Leaving it on show for an open server invites people to fill it in and
    wonder why nothing changes.
    """
    from fastapi.openapi.utils import get_openapi

    def openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.get("components", {}).pop("securitySchemes", None)
        if not schema.get("components"):
            schema.pop("components", None)
        for path in schema.get("paths", {}).values():
            for operation in path.values():
                if isinstance(operation, dict):
                    operation.pop("security", None)
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger = get_logger()
    app.state.vectorizer = Vectorizer(settings)
    app.state.rate_limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.credit_store = InMemoryCreditStore(
        default_balance=settings.default_credit_balance,
        enabled=settings.credits_enabled,
    )
    logger.info(
        "%s v%s ready (env=%s, workers=%d, auth=%s)",
        settings.app_name,
        settings.version,
        settings.environment,
        settings.worker_slots,
        "on" if settings.require_auth else "off",
    )
    yield
    logger.info("shutting down")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(logging.DEBUG if settings.debug else logging.INFO)
    logger = get_logger("http")

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    # Keep the injected Settings and app.state.settings the same object, so
    # tests (and any embedder) can pass a custom configuration in.
    app.dependency_overrides[get_settings] = lambda: settings

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=[
                "X-Request-Id",
                "X-Receipt",
                "X-Credits-Calculated",
                "X-Credits-Charged",
                "X-Credits-Balance",
                "X-Image-Width",
                "X-Image-Height",
                "X-Path-Count",
                "X-Processing-Ms",
            ],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        set_request_id(request_id)

        # Reject oversized bodies from the header before reading a single byte.
        declared = request.headers.get("content-length")
        if declared and declared.isdigit():
            if int(declared) > settings.max_upload_bytes:
                error = FileTooLarge(
                    f"Request body is {int(declared):,} bytes; the limit is "
                    f"{settings.max_upload_bytes:,}."
                )
                payload = error.to_payload()
                payload["error"]["request_id"] = request_id
                return JSONResponse(payload, status_code=error.status_code)

        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers.setdefault("X-Request-Id", request_id)
        response.headers.setdefault("X-Response-Ms", f"{elapsed_ms:.1f}")
        logger.info(
            "%s %s -> %s in %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    register_exception_handlers(app)
    app.include_router(api_router)

    if not settings.require_auth:
        _strip_security_from_openapi(app)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/docs" if settings.docs_enabled else "/api/v1/health")

    return app


app = create_app()
