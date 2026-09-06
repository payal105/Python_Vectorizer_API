"""Fetching input images from a remote URL (``image.url``).

Accepting a caller-supplied URL turns the API into an HTTP client acting on
their behalf, so every hop is validated against private address space before
we connect. Without this a caller could read cloud metadata endpoints or
probe hosts inside the deployment's network.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.core.errors import FileTooLarge, UrlFetchFailed, UrlNotAllowed
from app.core.logging import get_logger

logger = get_logger("fetch")

ALLOWED_SCHEMES = frozenset({"http", "https"})
_USER_AGENT = "PythonVectorAPI/1.0 (+image-fetch)"


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_and_check(host: str, port: int, allow_private: bool) -> None:
    """Refuse hosts that resolve into private or otherwise sensitive space."""
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UrlFetchFailed(f"Could not resolve host {host!r}.") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise UrlFetchFailed(f"Could not resolve host {host!r}.")

    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            raise UrlNotAllowed(f"Host {host!r} resolved to an unusable address.")
        if not _is_public(ip):
            raise UrlNotAllowed(
                f"Host {host!r} resolves to the non-public address {ip}. "
                "Set VECTOR_ALLOW_PRIVATE_NETWORK_FETCH=true to permit this."
            )


def _validate(url: str, allow_private: bool) -> httpx.URL:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlNotAllowed("Only http and https image URLs are supported.")
    if not parsed.hostname:
        raise UrlNotAllowed("The image URL has no host.")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    _resolve_and_check(parsed.hostname, port, allow_private)
    return httpx.URL(url)


async def fetch_image(url: str, settings: Settings) -> bytes:
    """Download *url*, enforcing scheme, address, redirect and size policy."""
    if not settings.allow_url_fetch:
        raise UrlNotAllowed("Fetching images by URL is disabled on this server.")

    allow_private = settings.allow_private_network_fetch
    target = _validate(url, allow_private)
    limit = settings.max_upload_bytes

    timeout = httpx.Timeout(settings.fetch_timeout_seconds)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT, "Accept": "image/*"},
    ) as client:
        for _ in range(settings.fetch_max_redirects + 1):
            try:
                async with client.stream("GET", target) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise UrlFetchFailed("Redirect response had no Location.")
                        # Re-validate every hop: the first URL being safe says
                        # nothing about where it points.
                        target = _validate(str(target.join(location)), allow_private)
                        continue

                    if response.status_code >= 400:
                        raise UrlFetchFailed(
                            f"The image URL returned HTTP {response.status_code}."
                        )

                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > limit:
                        raise FileTooLarge(
                            f"The remote image is {int(declared):,} bytes; "
                            f"the limit is {limit:,}."
                        )

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > limit:
                            raise FileTooLarge(
                                f"The remote image exceeds the {limit:,} byte limit."
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.HTTPError as exc:
                raise UrlFetchFailed(f"Could not fetch the image URL: {exc}") from exc

    raise UrlFetchFailed("Too many redirects while fetching the image URL.")
