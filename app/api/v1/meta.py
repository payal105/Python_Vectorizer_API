"""Introspection endpoints: health, capability discovery and account state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import CreditStoreDep, PrincipalDep, SettingsDep, VectorizerDep
from app.core.credits import COST_BY_MODE
from app.schemas.params import MEDIA_TYPES, RASTER_FORMATS, VECTOR_FORMATS
from app.services.engine import ENGINE_NAME
from app.services.preprocess import SUPPORTED_INPUT_FORMATS

router = APIRouter()


@router.get("/health", summary="Liveness probe", tags=["meta"])
async def health(settings: SettingsDep) -> dict[str, Any]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.version,
        "environment": settings.environment,
    }


@router.get("/ready", summary="Readiness probe", tags=["meta"])
async def ready(vectorizer: VectorizerDep) -> dict[str, Any]:
    return {
        "status": "ready",
        "engine": ENGINE_NAME,
        "workers": {
            "capacity": vectorizer.capacity,
            "in_flight": vectorizer.in_flight,
        },
    }


@router.get(
    "/formats",
    summary="Supported input and output formats",
    tags=["meta"],
)
async def formats() -> dict[str, Any]:
    return {
        "input": sorted(SUPPORTED_INPUT_FORMATS),
        "output": {
            "vector": sorted(VECTOR_FORMATS),
            "raster": sorted(RASTER_FORMATS),
            "media_types": MEDIA_TYPES,
        },
        "modes": {name: {"credits": cost} for name, cost in COST_BY_MODE.items()},
    }


@router.get(
    "/parameters",
    summary="Machine-readable schema of every vectorize parameter",
    tags=["meta"],
)
async def parameters() -> dict[str, Any]:
    """The authoritative parameter list, including ranges and defaults."""
    from app.schemas.params import VectorizeParams

    schema = VectorizeParams.model_json_schema(by_alias=True)
    return {
        "image_inputs": {
            "image": "multipart file part",
            "image.base64": "base64 string or data: URL",
            "image.url": "http(s) URL to fetch",
        },
        "parameters": schema.get("properties", {}),
        "definitions": schema.get("$defs", {}),
    }


@router.get(
    "/account",
    summary="Credit balance and usage for the calling key",
    tags=["meta"],
)
async def account(
    principal: PrincipalDep,
    credits: CreditStoreDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    snapshot = credits.snapshot(principal.key_id)
    return {
        "key_id": snapshot.key_id,
        "authenticated": principal.authenticated,
        "credits": {
            "enabled": settings.credits_enabled,
            "balance": round(snapshot.balance, 2),
            "charged_total": round(snapshot.charged_total, 2),
            "cost_by_mode": COST_BY_MODE,
        },
        "requests": snapshot.requests,
        "limits": {
            "max_upload_bytes": settings.max_upload_bytes,
            "max_input_pixels": settings.max_input_pixels,
            "max_output_pixels": settings.max_output_pixels,
            "rate_limit_per_minute": settings.rate_limit_per_minute,
            "job_timeout_seconds": settings.job_timeout_seconds,
        },
    }
