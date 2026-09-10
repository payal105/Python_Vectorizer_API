"""POST /api/v1/vectorize - the conversion endpoint."""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.api.deps import CreditStoreDep, SettingsDep, ThrottledPrincipalDep, VectorizerDep
from app.api.parsing import parse_vectorize_request
from app.core.credits import cost_for_mode
from app.core.logging import current_request_id
from app.schemas.params import VectorizeParams

router = APIRouter()


def _describe_optional(spec: dict[str, Any]) -> str:
    """Keep the field's real type visible once it renders as a text input."""
    description = spec.get("description", "")
    concrete = [
        option
        for option in spec.get("anyOf", [])
        if option.get("type") not in (None, "null")
    ]
    if not concrete:
        return description

    option = concrete[0]
    kind = option.get("type")
    if kind == "array":
        hint = "comma-separated list"
    elif kind in ("number", "integer"):
        bounds = []
        for key, label in (
            ("minimum", ">="),
            ("exclusiveMinimum", ">"),
            ("maximum", "<="),
        ):
            if key in option:
                bounds.append(f"{label} {option[key]}")
        hint = kind + (f", {', '.join(bounds)}" if bounds else "")
    else:
        hint = str(kind)

    prefix = f"[{hint}] Optional - leave blank to omit."
    return f"{prefix} {description}".strip()


def _multipart_schema() -> dict[str, Any]:
    """Describe the dotted form fields for the OpenAPI docs."""
    schema = VectorizeParams.model_json_schema(by_alias=True)
    properties: dict[str, Any] = {
        "image": {
            "type": "string",
            "format": "binary",
            "description": "The raster image to vectorize.",
        },
        # The empty default matters: without one, Swagger UI's "Try it out"
        # form pre-fills string inputs with the literal text "string" and
        # submits it, which reads as a second image and trips the
        # "provide exactly one" check. An empty value is treated as absent.
        "image.base64": {
            "type": "string",
            "default": "",
            "description": "The image as base64, or a data: URL. Alternative to 'image'.",
        },
        "image.url": {
            "type": "string",
            "default": "",
            "description": "A public URL to fetch the image from. Alternative to 'image'.",
        },
    }
    properties.update(schema.get("properties", {}))

    # Optional parameters arrive from pydantic as `anyOf: [<type>, null]` with
    # a null default. Two problems for Swagger UI: a null default is dropped
    # during JSON serialization, so the field renders as a string input
    # pre-filled with the literal text "string" and submits it; and giving
    # such a field an empty default instead makes the array/number widget fail
    # to render at all ("Could not render Parameters").
    #
    # Flattening them to plain string inputs fixes both. Multipart values are
    # strings on the wire regardless, the parser coerces them, and an empty
    # value is already treated as "not supplied".
    for name, spec in list(properties.items()):
        if name == "image" or spec.get("default") is not None:
            continue
        properties[name] = {
            "type": "string",
            "default": "",
            "title": spec.get("title", name),
            "description": _describe_optional(spec),
        }

    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }


OPENAPI_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {"schema": _multipart_schema()},
            "application/json": {"schema": _multipart_schema()},
        },
    }
}


def _headers(outcome: Any, receipt: Any, params: VectorizeParams) -> dict[str, str]:
    meta = outcome.meta
    timings = meta.get("timings_ms", {})
    headers = {
        "X-Request-Id": current_request_id(),
        "X-Engine": str(meta.get("engine", "")),
        "X-Source-Width": str(meta.get("source_width", "")),
        "X-Source-Height": str(meta.get("source_height", "")),
        "X-Image-Width": str(round(float(meta.get("output_width", 0)), 2)),
        "X-Image-Height": str(round(float(meta.get("output_height", 0)), 2)),
        "X-Path-Count": str(meta.get("paths", "")),
        "X-Shape-Count": str(meta.get("shapes", "")),
        "X-Combined-Paths": str(meta.get("combined") or "none"),
        # Which candidate settings won, and how many were tried. Without this
        # an adaptive result is unreproducible: the same upload can be traced
        # under different settings and nothing in the file says which.
        "X-Settings-Used": str(meta.get("settings_used") or "default"),
        "X-Settings-Considered": str(meta.get("settings_considered", 1)),
        "X-Processing-Ms": str(timings.get("total", "")),
        "X-Vectorize-Mode": params.mode,
        "Cache-Control": "no-store",
    }
    if receipt is not None:
        headers.update(
            {
                "X-Receipt": receipt.receipt_id,
                "X-Credits-Calculated": f"{receipt.calculated:.2f}",
                "X-Credits-Charged": f"{receipt.charged:.2f}",
                "X-Credits-Balance": f"{receipt.balance_after:.2f}",
            }
        )
    return headers


@router.post(
    "/vectorize",
    summary="Convert a raster image into vector artwork",
    description=(
        "Traces a bitmap into resolution-independent vector shapes and returns "
        "the result as SVG, PDF, EPS or a rasterized PNG preview.\n\n"
        "Supply the image as a multipart file part named `image`, as "
        "`image.base64`, or as `image.url`. Send `Accept: application/json` to "
        "receive a JSON envelope with the payload base64-encoded plus timing "
        "and geometry metadata instead of the raw file."
    ),
    response_class=Response,
    responses={
        200: {
            "description": "The vectorized image.",
            "content": {
                "image/svg+xml": {"schema": {"type": "string", "format": "binary"}},
                "application/pdf": {"schema": {"type": "string", "format": "binary"}},
                "application/postscript": {
                    "schema": {"type": "string", "format": "binary"}
                },
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "application/json": {"schema": {"type": "object"}},
            },
        },
        400: {"description": "Invalid parameters or undecodable image."},
        401: {"description": "Missing or invalid credentials."},
        402: {"description": "Insufficient credits."},
        413: {"description": "Input or requested output is too large."},
        429: {"description": "Rate limited."},
        504: {"description": "The job exceeded the time limit."},
    },
    openapi_extra=OPENAPI_BODY,
)
async def vectorize(
    request: Request,
    settings: SettingsDep,
    principal: ThrottledPrincipalDep,
    credits: CreditStoreDep,
    vectorizer: VectorizerDep,
) -> Response:
    parsed = await parse_vectorize_request(request, settings)
    params = parsed.params

    # Refuse up front if the caller cannot pay, rather than after burning CPU.
    cost = cost_for_mode(params.mode)
    credits.reserve(principal.key_id, cost)

    outcome = await vectorizer.run(parsed.data, params, parsed.filename)

    # Only bill once there is actually a result to hand back.
    receipt = credits.commit(principal.key_id, params.mode, cost)
    headers = _headers(outcome, receipt, params)

    accept = request.headers.get("accept", "")
    if "application/json" in accept and "*/*" not in accept.split(",")[0]:
        return JSONResponse(
            {
                "image": {
                    "format": params.output_file_format,
                    "media_type": outcome.media_type,
                    "filename": outcome.filename,
                    "base64": base64.b64encode(outcome.data).decode("ascii"),
                    "bytes": len(outcome.data),
                },
                "meta": outcome.meta,
                "credits": {
                    "receipt": receipt.receipt_id,
                    "calculated": receipt.calculated,
                    "charged": receipt.charged,
                    "balance": receipt.balance_after,
                },
            },
            headers=headers,
        )

    # "attachment" rather than "inline": this is a file to save, and Swagger
    # UI only offers its "Download file" link for attachments -- inline tells
    # the browser to try rendering it in place instead.
    headers["Content-Disposition"] = f'attachment; filename="{outcome.filename}"'
    return Response(
        content=outcome.data,
        media_type=outcome.media_type,
        headers=headers,
    )
