"""Job orchestration.

Tracing is CPU-bound and can take seconds on a large image, so the whole
synchronous chain runs in a worker thread behind a capacity limiter. That
keeps the event loop responsive and stops a burst of large uploads from
saturating every core at once.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import anyio
import anyio.to_thread

from app.config import Settings
from app.core.errors import APIError, ImageTooLarge, JobTimeout, ServerBusy
from app.core.logging import get_logger
from app.schemas.gradients import GradientParams
from app.schemas.params import VectorizeParams
from app.services import engine, preprocess, render, svgdoc

logger = get_logger("pipeline")

_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(slots=True)
class VectorizeOutcome:
    data: bytes
    media_type: str
    filename: str
    meta: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class GradientOutcome:
    data: bytes
    report: dict[str, object] = field(default_factory=dict)


def safe_filename(original: str | None, extension: str) -> str:
    stem = PurePosixPath(original or "image").stem or "image"
    stem = _SAFE_STEM.sub("-", stem).strip("-.") or "image"
    return f"{stem[:80]}.{extension}"


def _run_sync(
    data: bytes,
    params: VectorizeParams,
    settings: Settings,
    filename: str,
) -> VectorizeOutcome:
    """The full blocking chain: decode -> trace -> assemble -> render."""
    started = time.perf_counter()

    prepared = preprocess.prepare(data, params, settings.max_input_pixels)
    t_prepared = time.perf_counter()

    traced = engine.trace(prepared, params)
    # trace() may have preferred the finer copy of the bitmap.
    prepared = traced.prepared or prepared
    t_traced = time.perf_counter()

    svg_bytes, geometry = svgdoc.build(
        traced.svg,
        params,
        prepared.traced_width,
        prepared.traced_height,
        for_print=params.output_file_format in ("pdf", "eps"),
        palette=prepared.palette,
        supersample=prepared.supersample,
        # Whether a shape was shaded is a question about the artwork, not
        # about what the quantizer made of it, so hand over the copy from
        # before the colours were mapped when there is one.
        shading=prepared.shading or prepared.image,
        source_has_alpha=prepared.has_transparency,
    )
    t_built = time.perf_counter()

    payload = render.render(
        svg_bytes,
        params,
        settings,
        title=filename,
        source_has_alpha=prepared.has_transparency,
    )
    finished = time.perf_counter()

    meta: dict[str, object] = {
        "engine": traced.engine,
        "source_format": prepared.source_format,
        "source_width": prepared.source_width,
        "source_height": prepared.source_height,
        "traced_width": prepared.traced_width,
        "traced_height": prepared.traced_height,
        "downscaled": prepared.downscaled,
        "colors": prepared.color_count,
        "shading": geometry.get("shading") or {},
        "paths": geometry["paths"],
        "shapes": geometry["shapes"],
        "combined": geometry["combined"],
        "output_width": geometry["output_width"],
        "output_height": geometry["output_height"],
        "bytes": len(payload),
        "timings_ms": {
            "preprocess": round((t_prepared - started) * 1000, 1),
            "trace": round((t_traced - t_prepared) * 1000, 1),
            "assemble": round((t_built - t_traced) * 1000, 1),
            "render": round((finished - t_built) * 1000, 1),
            "total": round((finished - started) * 1000, 1),
        },
    }
    logger.info(
        "vectorized %sx%s %s -> %s (%s paths, %.0f ms)",
        prepared.source_width,
        prepared.source_height,
        prepared.source_format,
        params.output_file_format,
        geometry["paths"],
        meta["timings_ms"]["total"],  # type: ignore[index]
    )

    return VectorizeOutcome(
        data=payload,
        media_type=params.media_type,
        filename=safe_filename(filename, params.output_file_format),
        meta=meta,
    )


def _refine_sync(
    svg: bytes, data: bytes, params: GradientParams, max_pixels: int
) -> GradientOutcome:
    """The gradient stage on its own: decode the bitmap, then re-fit."""
    from app.services import gradients

    image, _ = preprocess.decode(data)
    width, height = image.size
    if width * height > max_pixels:
        raise ImageTooLarge(
            f"Input is {width}x{height} ({width * height:,} pixels); the "
            f"limit is {max_pixels:,}."
        )
    refined, report = gradients.refine(svg, image, params)
    return GradientOutcome(data=refined, report=report.as_dict())


class Vectorizer:
    """Runs vectorization jobs with bounded concurrency."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._limiter = anyio.CapacityLimiter(settings.worker_slots)

    @property
    def in_flight(self) -> int:
        return self._limiter.borrowed_tokens

    @property
    def capacity(self) -> int:
        return int(self._limiter.total_tokens)

    async def run(
        self, data: bytes, params: VectorizeParams, filename: str | None = None
    ) -> VectorizeOutcome:
        name = filename or "image"
        try:
            with anyio.fail_after(self.settings.job_timeout_seconds):
                return await anyio.to_thread.run_sync(
                    _run_sync,
                    data,
                    params,
                    self.settings,
                    name,
                    limiter=self._limiter,
                    abandon_on_cancel=True,
                )
        except TimeoutError:
            raise JobTimeout(
                "The image took longer than "
                f"{self.settings.job_timeout_seconds:.0f}s to vectorize. Try a "
                "smaller input.max_pixels or a higher "
                "processing.shapes.min_area_px."
            ) from None
        except APIError:
            raise
        except MemoryError as exc:
            raise ServerBusy("Ran out of memory processing this image.") from exc

    async def refine_gradients(
        self, svg: bytes, data: bytes, params: GradientParams
    ) -> GradientOutcome:
        """Run the gradient stage alone, under the same limits as a job.

        It is the same work by the same code that a conversion does, so it
        borrows the same worker slot and the same deadline rather than
        competing with tracing for the machine.
        """
        try:
            with anyio.fail_after(self.settings.job_timeout_seconds):
                return await anyio.to_thread.run_sync(
                    _refine_sync,
                    svg,
                    data,
                    params,
                    self.settings.max_input_pixels,
                    limiter=self._limiter,
                    abandon_on_cancel=True,
                )
        except TimeoutError:
            raise JobTimeout(
                "Refitting the gradients took longer than "
                f"{self.settings.job_timeout_seconds:.0f}s."
            ) from None
        except APIError:
            raise
        except MemoryError as exc:
            raise ServerBusy("Ran out of memory processing this image.") from exc
