"""Trace an image several ways and keep whichever came closest to it.

No single set of settings suits every image. Measured against the cached
Vectorizer.AI corpus, the best-performing settings differ per image: of 63
samples, the shipped defaults won 23, turning denoising off won 26, and asking
for maximum detail won 14. Any one of them fixed for everything leaves most of
the corpus worse than it needs to be.

Which one wins cannot be read off the image — the classifier signals we have
(ink count, residual) do not predict it. But it *can* be measured after the
fact, because the source bitmap is right there to compare against. So each
candidate is traced, rendered small, and scored against the source, and the
closest one is what the caller gets.

That this works is not obvious, and it is the part that was verified rather
than assumed: scoring against the source rewards reproducing compression
noise, which is exactly why the same trick could not be used to choose between
the plain and the finer trace (see engine.trace). For choosing between these
settings it turns out to be reliable — it picked the same candidate a perfect
oracle would have on 60 of 63 samples, capturing 14.0% of a 14.0% ceiling.

The cost is real: three traces instead of one, about 3.4x the work. Callers
who have asked for the settings this varies get one trace, because overriding
an explicit choice to chase a metric would be wrong.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageStat

from app.config import Settings
from app.core.logging import get_logger
from app.schemas.params import VectorizeParams
from app.services import engine, preprocess

logger = get_logger("adaptive")

# The candidates, as field overrides. `default` has to be first: it is the
# fallback if scoring cannot run, and the tie-break winner.
CANDIDATES: tuple[tuple[str, dict[str, object]], ...] = (
    ("default", {}),
    ("denoise=none", {"processing_denoise": "none"}),
    ("detail=maximum", {"processing_detail": "maximum"}),
)

# Fields the candidates vary. If the caller set any of them, their choice
# stands and no search happens.
VARIED_FIELDS = ("processing_denoise", "processing_detail")

# Candidates are ranked on a small render: this only has to order three
# results, and a 400px view does that for a fraction of a full rasterize.
PICK_EDGE = 400

# How much better than the defaults a candidate has to be before it displaces
# them. Without this, ties are settled by floating-point noise: on flat
# lettering all three candidates traced to the same 12 shapes and scored
# 0.2279 apiece, and the search still swapped in maximum detail, which writes
# the identical geometry with two more decimal places and a 6% bigger file.
# The gains worth having are nothing like that small — the images this helps
# move 30-60% — so the bar costs nothing real and keeps output stable for the
# images where the settings genuinely do not matter.
MARGIN = 0.98


@dataclass(slots=True)
class Choice:
    label: str
    prepared: "preprocess.PreparedImage"
    traced: "engine.TraceResult"
    params: VectorizeParams
    considered: int


def _reference(data: bytes) -> Image.Image | None:
    """The source, small, as the thing every candidate is measured against."""
    try:
        source = Image.open(io.BytesIO(data))
        source.load()
        source = source.convert("RGB")
    except Exception:
        return None
    scale = PICK_EDGE / max(source.width, source.height)
    if scale >= 1:
        return source
    return source.resize(
        (max(1, round(source.width * scale)), max(1, round(source.height * scale))),
        Image.LANCZOS,
    )


def _distance(svg: bytes, reference: Image.Image) -> float | None:
    """Mean channel difference between a rendered candidate and the source."""
    from reportlab.graphics import renderPM
    from svglib.svglib import svg2rlg

    from app.services import render

    try:
        drawing = svg2rlg(io.BytesIO(render.flatten_gradients(svg)))
        if drawing is None or not drawing.width or not drawing.height:
            return None
        drawing.scale(reference.width / drawing.width,
                      reference.height / drawing.height)
        drawing.width, drawing.height = reference.width, reference.height
        shot = renderPM.drawToPIL(drawing, dpi=72, bg=0xFFFFFF).convert("RGB")
        stat = ImageStat.Stat(ImageChops.difference(shot, reference))
        return sum(stat.mean) / len(stat.mean)
    except Exception:
        logger.debug("candidate could not be scored", exc_info=True)
        return None


def _trace_candidate(data: bytes, params: VectorizeParams, settings: Settings):
    prepared = preprocess.prepare(data, params, settings.max_input_pixels)
    traced = engine.trace(prepared, params)
    return traced.prepared or prepared, traced


def choose(data: bytes, params: VectorizeParams, settings: Settings) -> Choice:
    """Trace the candidates and return the one closest to the source."""
    plain_prepared, plain_traced = _trace_candidate(data, params, settings)
    plain = Choice("default", plain_prepared, plain_traced, params, 1)

    if not settings.adaptive_enabled:
        return plain
    if any(params.asked_for(field) for field in VARIED_FIELDS):
        logger.debug("caller set the settings the search varies; not searching")
        return plain
    pixels = plain_prepared.source_width * plain_prepared.source_height
    if pixels > settings.adaptive_max_pixels:
        logger.debug("image is %d pixels, above the %d searching limit",
                     pixels, settings.adaptive_max_pixels)
        return plain

    reference = _reference(data)
    if reference is None:
        return plain

    # Scoring needs a finished document, so the assembly step runs per
    # candidate. It is cheap next to tracing.
    from app.services import svgdoc

    def score_of(prepared, traced, candidate_params) -> float | None:
        try:
            svg, _ = svgdoc.build(
                traced.svg, candidate_params,
                prepared.traced_width, prepared.traced_height,
                for_print=False, palette=prepared.palette,
                supersample=prepared.supersample, shading=prepared.image,
                source_has_alpha=prepared.has_transparency,
            )
        except Exception:
            return None
        return _distance(svg, reference)

    baseline_score = score_of(plain_prepared, plain_traced, params)
    if baseline_score is None:
        return plain

    # The defaults are the incumbent, and a candidate has to clear MARGIN
    # against *them* rather than against whatever is currently winning — so
    # that a chain of tiny improvements cannot walk the result away from the
    # defaults one hair at a time.
    threshold = baseline_score * MARGIN
    best, best_score = plain, baseline_score
    considered = 1

    for label, overrides in CANDIDATES[1:]:
        candidate_params = params.model_copy(update=dict(overrides))
        try:
            prepared, traced = _trace_candidate(data, candidate_params, settings)
        except Exception:
            logger.debug("candidate %s failed to trace", label, exc_info=True)
            continue
        considered += 1
        score = score_of(prepared, traced, candidate_params)
        if score is None or score >= threshold or score >= best_score:
            continue
        best_score, best = score, Choice(
            label, prepared, traced, candidate_params, considered)

    best.considered = considered
    logger.info(
        "adaptive kept %s (distance %.4f vs %.4f for the defaults, %d candidates)",
        best.label, best_score, baseline_score, considered,
    )
    return best
