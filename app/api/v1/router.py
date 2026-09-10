"""v1 API surface."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import gradients, meta, vectorize

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(vectorize.router, tags=["vectorize"])
# The gradient stage, which /vectorize already runs for every conversion,
# exposed on its own for re-fitting a document that already exists.
api_router.include_router(gradients.router, tags=["vectorize"])
api_router.include_router(meta.router)
