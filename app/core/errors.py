"""Error taxonomy and JSON envelope.

Every failure leaves the API as::

    {"error": {"status": 400, "code": 1006, "message": "..."}}

mirroring the shape vectorizer.ai clients already parse. Numeric codes are
stable contract: append new ones, never renumber.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode:
    """Stable numeric error codes."""

    # 1xxx - request / input problems
    BAD_REQUEST = 1000
    NO_IMAGE = 1001
    MULTIPLE_IMAGES = 1002
    BAD_IMAGE_DATA = 1003
    IMAGE_TOO_MANY_PIXELS = 1004
    FILE_TOO_LARGE = 1005
    BAD_PARAMETER = 1006
    UNSUPPORTED_OUTPUT_FORMAT = 1007
    URL_FETCH_FAILED = 1008
    URL_NOT_ALLOWED = 1009
    OUTPUT_TOO_MANY_PIXELS = 1010

    # 2xxx - authentication / authorisation / billing
    UNAUTHORIZED = 2000
    INVALID_CREDENTIALS = 2001
    INSUFFICIENT_CREDITS = 2002

    # 3xxx - throttling
    RATE_LIMITED = 3000
    SERVER_BUSY = 3001

    # 5xxx - server side
    INTERNAL_ERROR = 5000
    VECTORIZATION_FAILED = 5001
    RENDER_FAILED = 5002
    JOB_TIMEOUT = 5003


class APIError(Exception):
    """Base class for every error deliberately raised by the application."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: int = ErrorCode.BAD_REQUEST
    message: str = "Bad request."

    def __init__(
        self,
        message: str | None = None,
        *,
        status_code: int | None = None,
        code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.status_code = status_code or self.status_code
        self.code = code or self.code
        self.headers = headers or {}
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "status": self.status_code,
                "code": self.code,
                "message": self.message,
            }
        }


# --- Concrete errors ---------------------------------------------------------


class BadParameter(APIError):
    code = ErrorCode.BAD_PARAMETER
    message = "One or more parameters are invalid."


class NoImageSupplied(APIError):
    code = ErrorCode.NO_IMAGE
    message = "No input image supplied. Provide 'image', 'image.base64' or 'image.url'."


class MultipleImagesSupplied(APIError):
    code = ErrorCode.MULTIPLE_IMAGES
    message = "Supply exactly one of 'image', 'image.base64' or 'image.url'."


class BadImageData(APIError):
    code = ErrorCode.BAD_IMAGE_DATA
    message = "The input could not be decoded as a supported raster image."


class ImageTooLarge(APIError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = ErrorCode.IMAGE_TOO_MANY_PIXELS
    message = "The input image has too many pixels."


class FileTooLarge(APIError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = ErrorCode.FILE_TOO_LARGE
    message = "The uploaded file is too large."


class OutputTooLarge(APIError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = ErrorCode.OUTPUT_TOO_MANY_PIXELS
    message = "The requested output size has too many pixels."


class UnsupportedOutputFormat(APIError):
    code = ErrorCode.UNSUPPORTED_OUTPUT_FORMAT
    message = "The requested output format is not supported."


class UrlFetchFailed(APIError):
    code = ErrorCode.URL_FETCH_FAILED
    message = "The image URL could not be retrieved."


class UrlNotAllowed(APIError):
    status_code = status.HTTP_403_FORBIDDEN
    code = ErrorCode.URL_NOT_ALLOWED
    message = "The image URL points at a disallowed host."


class Unauthorized(APIError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.UNAUTHORIZED
    message = "Authentication required."

    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        headers = kwargs.pop("headers", None) or {}
        headers.setdefault("WWW-Authenticate", 'Basic realm="vector-api"')
        super().__init__(message, headers=headers, **kwargs)


class InvalidCredentials(Unauthorized):
    code = ErrorCode.INVALID_CREDENTIALS
    message = "Invalid API credentials."


class InsufficientCredits(APIError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = ErrorCode.INSUFFICIENT_CREDITS
    message = "Insufficient credits for this request."


class RateLimited(APIError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = ErrorCode.RATE_LIMITED
    message = "Rate limit exceeded. Slow down."


class ServerBusy(APIError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = ErrorCode.SERVER_BUSY
    message = "The server is at capacity. Retry shortly."


class VectorizationFailed(APIError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = ErrorCode.VECTORIZATION_FAILED
    message = "The image could not be vectorized."


class RenderFailed(APIError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = ErrorCode.RENDER_FAILED
    message = "The vector result could not be written in the requested format."


class JobTimeout(APIError):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = ErrorCode.JOB_TIMEOUT
    message = "The vectorization job exceeded the time limit."


# --- Handlers ----------------------------------------------------------------


def _json(error: APIError, request: Request) -> JSONResponse:
    payload = error.to_payload()
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        payload["error"]["request_id"] = request_id
    return JSONResponse(payload, status_code=error.status_code, headers=error.headers)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers so *every* error path emits the same envelope."""

    @app.exception_handler(APIError)
    async def _handle_api_error(request: Request, exc: APIError) -> JSONResponse:
        return _json(exc, request)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        detail = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in detail.get("loc", ()) if p not in ("body", "query"))
        msg = detail.get("msg", "Invalid request.")
        text = f"{loc}: {msg}" if loc else msg
        return _json(BadParameter(text), request)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        mapped = APIError(
            str(exc.detail),
            status_code=exc.status_code,
            code=ErrorCode.BAD_REQUEST
            if exc.status_code < 500
            else ErrorCode.INTERNAL_ERROR,
            headers=dict(getattr(exc, "headers", None) or {}),
        )
        return _json(mapped, request)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        from app.core.logging import get_logger

        get_logger().exception("unhandled error: %s", exc)
        return _json(
            APIError(
                "An unexpected error occurred.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                code=ErrorCode.INTERNAL_ERROR,
            ),
            request,
        )
