"""POST /api/v1/gradients - the gradient refinement stage on its own.

``/vectorize`` runs this stage for you: every conversion goes through it
before the document is rendered, so nothing has to be called here to get
gradients out of the main endpoint. What this route adds is the ability to
run the same stage over a document you already hold -- to re-fit an SVG whose
gradients were flattened somewhere downstream, or to see what the stage makes
of one image without re-tracing it.

It is deliberately kept out of the published schema: it is the second half of
one conversion rather than a service in its own right, and calling it with a
vector that was never traced from the bitmap you send will produce a document
painted with colours from the wrong places.
"""

from __future__ import annotations

import base64

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.api.deps import SettingsDep, ThrottledPrincipalDep, VectorizerDep
from app.api.parsing import parse_gradient_request
from app.core.logging import current_request_id
from app.services.pipeline import safe_filename

router = APIRouter()


@router.post(
    "/gradients",
    summary="Re-fit an SVG's gradients against the bitmap it was traced from",
    response_class=Response,
    include_in_schema=False,
)
async def gradients(
    request: Request,
    settings: SettingsDep,
    principal: ThrottledPrincipalDep,
    vectorizer: VectorizerDep,
) -> Response:
    parsed = await parse_gradient_request(request, settings)
    outcome = await vectorizer.refine_gradients(
        parsed.svg, parsed.data, parsed.params
    )

    filename = safe_filename(parsed.filename, "svg")
    headers = {
        "X-Request-Id": current_request_id(),
        "X-Gradient-Count": str(outcome.report["gradients"]),
        "X-Gradient-Regions": str(outcome.report["regions"]),
        "X-Gradient-Merged": str(outcome.report["merged"]),
        "X-Gradient-Residual-Before": str(outcome.report["residual_before"]),
        "X-Gradient-Residual-After": str(outcome.report["residual_after"]),
        "X-Processing-Ms": str(outcome.report["ms"]),
        "Cache-Control": "no-store",
    }

    accept = request.headers.get("accept", "")
    if "application/json" in accept and "*/*" not in accept.split(",")[0]:
        return JSONResponse(
            {
                "image": {
                    "format": "svg",
                    "media_type": "image/svg+xml",
                    "filename": filename,
                    "base64": base64.b64encode(outcome.data).decode("ascii"),
                    "bytes": len(outcome.data),
                },
                "meta": {"shading": outcome.report},
            },
            headers=headers,
        )

    headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return Response(
        content=outcome.data, media_type="image/svg+xml", headers=headers
    )
