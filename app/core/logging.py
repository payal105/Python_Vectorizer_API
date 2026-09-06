"""Minimal structured logging with a per-request correlation id."""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("request_id", default="-")

LOGGER_NAME = "vector_api"


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


def configure_logging(level: str | int = logging.INFO) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:  # already configured (e.g. uvicorn reload)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler.addFilter(_RequestIdFilter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def set_request_id(value: str) -> None:
    _request_id.set(value)


def current_request_id() -> str:
    return _request_id.get()
