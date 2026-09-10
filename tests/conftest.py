from __future__ import annotations

import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.config import Settings
from app.main import create_app

DEMO_ID = "test_id"
DEMO_SECRET = "test_secret"
AUTH = (DEMO_ID, DEMO_SECRET)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        require_auth=True,
        api_keys=f"{DEMO_ID}:{DEMO_SECRET}",
        rate_limit_per_minute=0,  # off by default; enabled per-test
        credits_enabled=True,
        default_credit_balance=100.0,
        allow_url_fetch=True,
        max_input_pixels=4_000_000,
    )


@pytest.fixture()
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def single_trace_client(settings: Settings):
    """A client that traces once, for tests that measure one stage in isolation.

    The pipeline normally traces an image several ways and keeps whichever
    lands closest to the source (app/services/adaptive.py). That is right for
    callers and wrong for a test asking what one setting does, because the
    search is free to answer with a different one — and it cannot tell that a
    caller who asked for a setting's *default* value meant it, since
    asked_for() compares against that default.
    """
    app = create_app(settings.model_copy(update={"adaptive_enabled": False}))
    with TestClient(app) as test_client:
        yield test_client


def make_image(width: int = 240, height: int = 180, mode: str = "RGB") -> Image.Image:
    """A few flat-coloured shapes: the easy, high-signal case for a tracer."""
    image = Image.new(mode, (width, height), "white" if mode == "RGB" else (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse([10, 10, 120, 120], fill=(220, 40, 60))
    draw.rectangle([110, 70, 230, 160], fill=(30, 90, 200))
    draw.polygon([(120, 8), (232, 20), (180, 70)], fill=(20, 160, 90))
    return image


def png_bytes(image: Image.Image | None = None) -> bytes:
    buffer = io.BytesIO()
    (image or make_image()).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(image: Image.Image | None = None) -> bytes:
    buffer = io.BytesIO()
    (image or make_image()).convert("RGB").save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


@pytest.fixture()
def sample_png() -> bytes:
    return png_bytes()


@pytest.fixture()
def sample_png_b64(sample_png: bytes) -> str:
    return base64.b64encode(sample_png).decode("ascii")


def upload(data: bytes = b"", name: str = "sample.png", content_type: str = "image/png"):
    return {"image": (name, data or png_bytes(), content_type)}
