"""Application settings, loaded from environment / .env with the VECTOR_ prefix."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VECTOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Identity ------------------------------------------------------------
    app_name: str = "Python Vector API"
    version: str = "1.0.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False

    # --- Server --------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    # --- Authentication ------------------------------------------------------
    require_auth: bool = True
    api_keys: str = "demo_id:demo_secret"
    """Comma-separated ``id:secret`` pairs."""

    # --- Input limits --------------------------------------------------------
    max_upload_bytes: int = Field(default=32 * 1024 * 1024, ge=1024)
    max_input_pixels: int = Field(default=30_000_000, ge=1_000)
    max_output_pixels: int = Field(default=33_000_000, ge=1_000)
    job_timeout_seconds: float = Field(default=120.0, gt=0)

    # --- Remote fetching -----------------------------------------------------
    allow_url_fetch: bool = True
    allow_private_network_fetch: bool = False
    fetch_timeout_seconds: float = Field(default=20.0, gt=0)
    fetch_max_redirects: int = Field(default=3, ge=0, le=10)

    # --- Throughput ----------------------------------------------------------
    max_concurrent_jobs: int = Field(default=0, ge=0)
    """Simultaneous tracing jobs; 0 means 'use the CPU count'."""

    rate_limit_per_minute: int = Field(default=60, ge=0)
    """Requests per minute per API key; 0 disables rate limiting."""

    # --- Credits -------------------------------------------------------------
    credits_enabled: bool = True
    default_credit_balance: float = Field(default=1000.0, ge=0)

    # --- Docs ----------------------------------------------------------------
    docs_enabled: bool = True
    cors_origins: str = "*"

    @field_validator("environment", mode="before")
    @classmethod
    def _normalise_environment(cls, value: object) -> object:
        return value.lower().strip() if isinstance(value, str) else value

    # --- Derived helpers -----------------------------------------------------
    @property
    def credentials(self) -> dict[str, str]:
        """Parsed ``{id: secret}`` mapping from :attr:`api_keys`."""
        pairs: dict[str, str] = {}
        for chunk in self.api_keys.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            key_id, _, secret = chunk.partition(":")
            key_id, secret = key_id.strip(), secret.strip()
            if key_id and secret:
                pairs[key_id] = secret
        return pairs

    @property
    def worker_slots(self) -> int:
        if self.max_concurrent_jobs > 0:
            return self.max_concurrent_jobs
        return max(1, os.cpu_count() or 1)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
