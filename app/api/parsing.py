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
from app.schemas.gradients import GradientParams
from app.schemas.params import VectorizeParams
from app.services.fetch import fetch_image

IMAGE_FIELDS = ("image", "image.base64", "image.url")
VECTOR_FIELDS = ("vector", "vector.base64", "vector.svg")


@dataclass(slots=True)
class ParsedRequest:
    data: bytes
    filename: str | None
    params: VectorizeParams
    source: str


@dataclass(slots=True)
class ParsedGradientRequest:
    """An original bitmap and the vector traced from it, for /gradients."""

    data: bytes
    svg: bytes
    filename: str | None
    params: GradientParams


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


async def _read_form(
    request: Request, allowed_parts: tuple[str, ...]
) -> tuple[dict[str, tuple[bytes, str | None]], dict[str, Any]]:
    """Split a request body into its file parts and its plain fields."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    uploads: dict[str, Any] = {}
    fields: dict[str, Any] = {}

    if content_type in ("multipart/form-data", "application/x-www-form-urlencoded"):
        form = await request.form()
        try:
            for key, value in form.multi_items():
                if isinstance(value, UploadFile):
                    if key not in allowed_parts:
                        raise BadParameter(
                            f"Unexpected file part {key!r}; expected one of "
                            + ", ".join(repr(name) for name in allowed_parts)
                            + "."
                        )
                    if key in uploads:
                        raise BadParameter(f"More than one {key!r} part supplied.")
                    uploads[key] = value
                else:
                    fields[key] = value
            for key, upload in list(uploads.items()):
                # The form is closed below and its parts go with it, so every
                # one that matters has to be read while it is still open.
                uploads[key] = (await upload.read(), upload.filename)
        finally:
            await form.close()
    elif content_type == "application/json":
        fields = await _read_json_body(request)
    else:
        raise BadParameter(
            "Content-Type must be multipart/form-data, "
            "application/x-www-form-urlencoded or application/json."
        )
    return uploads, fields


async def parse_gradient_request(
    request: Request, settings: Settings
) -> ParsedGradientRequest:
    """Extract the original bitmap, the SVG to refine, and the parameters.

    The stage compares two things, so both have to arrive: the raster the
    artwork came from, supplied exactly as ``/vectorize`` takes it, and the
    vector that was traced from it.
    """
    uploads, fields = await _read_form(request, ("image", "vector"))

    raw_image = uploads.get("image")
    data, filename, _ = await _resolve_image(raw_image, fields, settings)

    part = uploads.get("vector")
    svg = part[0] if part else None
    if svg is None:
        text = fields.get("vector.svg") or ""
        encoded = fields.get("vector.base64") or ""
        if text and encoded:
            raise BadParameter(
                "Supplied vector.svg and vector.base64; provide exactly one."
            )
        if text:
            if not isinstance(text, str):
                raise BadParameter("vector.svg must be a string.")
            svg = text.encode("utf-8")
        elif encoded:
            if not isinstance(encoded, str):
                raise BadParameter("vector.base64 must be a string.")
            svg = _decode_base64(encoded)
    if not svg:
        raise BadParameter(
            "No vector supplied. Send the SVG as a file part named 'vector', "
            "or as vector.svg / vector.base64."
        )
    _check_size(svg, settings)

    for key, value in request.query_params.items():
        fields.setdefault(key, value)
    for field in IMAGE_FIELDS + VECTOR_FIELDS:
        fields.pop(field, None)

    try:
        params = GradientParams.model_validate(fields)
    except ValidationError as exc:
        raise _friendly_validation_error(exc) from exc
    return ParsedGradientRequest(
        data=data, svg=svg, filename=filename, params=params
    )


async def _resolve_image(
    upload: "UploadFile | tuple[bytes, str | None] | None",
    fields: dict[str, Any],
    settings: Settings,
) -> tuple[bytes, str | None, str]:
    """Pick exactly one of the three ways to supply an image.

    An upload arrives either as the live UploadFile -- which must be read
    before the form is closed -- or as the bytes and filename already taken
    off one, which is how the endpoints that read several parts hand it over.
    """
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
        if isinstance(upload, tuple):
            content, name = upload
        else:
            content, name = await upload.read(), upload.filename
        return _check_size(content, settings), name, "upload"

    if b64:
        if not isinstance(b64, str):
            raise BadParameter("image.base64 must be a string.")
        return _check_size(_decode_base64(b64), settings), None, "base64"

    if not isinstance(url, str):
        raise BadParameter("image.url must be a string.")
    data = _check_size(await fetch_image(url, settings), settings)
    filename = url.rsplit("/", 1)[-1].split("?")[0] or None
    return data, filename, "url"
