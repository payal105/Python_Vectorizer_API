"""Turning an inbound HTTP request into (image bytes, validated params).

Clients may send multipart/form-data (the usual case, with a file part),
urlencoded form fields, or a flat JSON object. All three use the same
dotted parameter names, so they share one code path here.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.requests import Request

from app.config import Settings
from app.core.errors import (
    BadImageData,
    BadParameter,
    FileTooLarge,
    MultipleImagesSupplied,
    NoImageSupplied,
)
from app.schemas.params import VectorizeParams
from app.services.fetch import fetch_image

IMAGE_FIELDS = ("image", "image.base64", "image.url")


@dataclass(slots=True)
class ParsedRequest:
    data: bytes
    filename: str | None
    params: VectorizeParams
    source: str


def _friendly_validation_error(exc: ValidationError) -> BadParameter:
    """Render pydantic's first complaint as a single actionable sentence."""
    errors = exc.errors()
    if not errors:
        return BadParameter()
    first = errors[0]
    kind = first.get("type", "")
    loc = ".".join(str(p) for p in first.get("loc", ()))

    if kind == "extra_forbidden":
        return BadParameter(
            f"Unknown parameter {loc!r}. Check the spelling against GET /api/v1/parameters."
        )
    message = first.get("msg", "is invalid")
    message = message.removeprefix("Value error, ")
    return BadParameter(f"{loc}: {message}" if loc else message)


def _build_params(raw: dict[str, Any]) -> VectorizeParams:
    try:
        return VectorizeParams.model_validate(raw)
    except ValidationError as exc:
        raise _friendly_validation_error(exc) from exc


def _decode_base64(value: str) -> bytes:
    payload = value.strip()
    # Tolerate data URLs: data:image/png;base64,AAAA...
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise BadImageData("image.base64 is not valid base64 data.") from exc


def _check_size(data: bytes, settings: Settings) -> bytes:
    if len(data) > settings.max_upload_bytes:
        raise FileTooLarge(
            f"The image is {len(data):,} bytes; the limit is "
            f"{settings.max_upload_bytes:,}."
        )
    return data


async def _read_json_body(request: Request) -> dict[str, Any]:
    body = await request.body()
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise BadParameter(f"Request body is not valid JSON: {exc.msg}.") from exc
    if not isinstance(parsed, dict):
        raise BadParameter("The JSON body must be an object of parameters.")
    return parsed


async def parse_vectorize_request(
    request: Request, settings: Settings
) -> ParsedRequest:
    """Extract the input image and validated parameters from *request*."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()

    upload: UploadFile | None = None
    fields: dict[str, Any] = {}

    if content_type in ("multipart/form-data", "application/x-www-form-urlencoded"):
        form = await request.form()
        try:
            for key, value in form.multi_items():
                if isinstance(value, UploadFile):
                    if key != "image":
                        raise BadParameter(
                            f"Unexpected file part {key!r}; the image part must "
                            "be named 'image'."
                        )
                    if upload is not None:
                        raise MultipleImagesSupplied()
                    upload = value
                else:
                    fields[key] = value
            data, filename, source = await _resolve_image(
                upload, fields, settings
            )
        finally:
            await form.close()
    elif content_type == "application/json":
        fields = await _read_json_body(request)
        data, filename, source = await _resolve_image(None, fields, settings)
    else:
        raise BadParameter(
            "Content-Type must be multipart/form-data, "
            "application/x-www-form-urlencoded or application/json."
        )

    # Query-string parameters are accepted too, so a URL-driven conversion can
    # be expressed entirely in the URL.
    for key, value in request.query_params.items():
        fields.setdefault(key, value)

    for field in IMAGE_FIELDS:
        fields.pop(field, None)

    params = _build_params(fields)
    return ParsedRequest(data=data, filename=filename, params=params, source=source)


async def _resolve_image(
    upload: UploadFile | None,
    fields: dict[str, Any],
    settings: Settings,
) -> tuple[bytes, str | None, str]:
    """Pick exactly one of the three ways to supply an image."""
    b64 = fields.get("image.base64")
    url = fields.get("image.url")

    supplied = [
        name
        for name, present in (
            ("image", upload is not None),
            ("image.base64", bool(b64)),
            ("image.url", bool(url)),
        )
        if present
    ]

    if not supplied:
        raise NoImageSupplied()
    if len(supplied) > 1:
        raise MultipleImagesSupplied(
            "Supplied " + " and ".join(supplied) + "; provide exactly one."
        )

    if upload is not None:
        data = _check_size(await upload.read(), settings)
        return data, upload.filename, "upload"

    if b64:
        if not isinstance(b64, str):
            raise BadParameter("image.base64 must be a string.")
        return _check_size(_decode_base64(b64), settings), None, "base64"

    if not isinstance(url, str):
        raise BadParameter("image.url must be a string.")
    data = _check_size(await fetch_image(url, settings), settings)
    filename = url.rsplit("/", 1)[-1].split("?")[0] or None
    return data, filename, "url"
