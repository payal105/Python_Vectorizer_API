"""Development entrypoint: python run.py

For production use a process manager and multiple workers, e.g.
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
Note that credits and rate limits are per-process in the default in-memory
stores; back them with Redis or a database before scaling out.
"""

from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.environment == "development",
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    main()
