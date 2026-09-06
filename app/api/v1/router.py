"""v1 API surface."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import meta, vectorize

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(vectorize.router, tags=["vectorize"])
api_router.include_router(meta.router)
