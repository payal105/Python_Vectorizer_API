"""Tracing engine.

Wraps VTracer (visioncortex): hierarchical colour clustering followed by
curve fitting, which is the same broad approach vectorizer.ai takes. The
engine is isolated behind :func:`trace` so a different backend can be
swapped in without touching the API layer.
"""

from __future__ import annotations

from dataclasses import dataclass

import vtracer

from app.core.errors import VectorizationFailed
from app.core.logging import get_logger
from app.schemas.params import VectorizeParams
from app.services.preprocess import PreparedImage, to_png_bytes

logger = get_logger("engine")

# Our curve-mode vocabulary -> VTracer's.
_CURVE_MODE = {"spline": "spline", "polygon": "polygon", "pixel": "none"}

# Smoothing presets, chosen by rendering test shapes at 4x and comparing.
#
# These move geometry only: corner_threshold is the angle below which a bend
# stays a hard corner rather than becoming a curve, so raising it rounds more
# of the outline, while splice_threshold and max_iterations give the curve
# fitter more freedom and more refinement passes.
#
# Deliberately NOT included, despite both reducing apparent jaggedness:
#   * filter_speckle -- it removes small shapes outright. At 16 it silently
#     erased 12px text from a test logo. Smoothing must not delete features.
#   * layer_difference -- merges near-identical colour bands, which helps the
#     stepping on anti-aliased edges but is a colour decision, not a
#     smoothing one, and it flattens genuine soft shading.
# Both remain available as explicit parameters.
#
# Pre-blurring or upscaling the bitmap was also tried and rejected: it makes
# edges visibly lumpy, because the softened ramp gives the curve fitter a
# wobbly boundary to follow.
SMOOTHING_PRESETS: dict[str, dict[str, float]] = {
    "none": {
        "corner_threshold": 60,
        "splice_threshold": 45,
        "max_iterations": 10,
        "length_threshold": 4.0,
    },
    "low": {
        "corner_threshold": 80,
        "splice_threshold": 50,
        "max_iterations": 16,
        "length_threshold": 4.0,
    },
    "medium": {
        "corner_threshold": 110,
        "splice_threshold": 60,
        "max_iterations": 24,
        "length_threshold": 4.5,
    },
    "high": {
        "corner_threshold": 160,
        "splice_threshold": 80,
        "max_iterations": 32,
        "length_threshold": 5.0,
    },
}

# Detail presets: how small a shape survives, and how precisely paths are
# written. Measured on a 200px test card carrying a 2px rule and 3-7px dots.
#
# The cliff is sharp and worth knowing about: filter_speckle=1 already loses
# a 2px rule, and only 0 keeps it. But 0 also keeps every single-pixel scrap
# of compression noise -- on a noisy JPEG the path count went 27 -> 2098 --
# and no amount of denoising brings that back down, because the survivors are
# real pixel differences rather than isolated outliers. So 'maximum' is a
# deliberate choice, not a better default.
DETAIL_PRESETS: dict[str, dict[str, int]] = {
    "low": {"filter_speckle": 8, "path_precision": 2},
    "standard": {"filter_speckle": 4, "path_precision": 3},
    "high": {"filter_speckle": 1, "path_precision": 4},
    "maximum": {"filter_speckle": 0, "path_precision": 5},
}

_DETAIL_OVERRIDES = {
    "filter_speckle": "processing_min_area_px",
    "path_precision": "processing_path_precision",
}

# VTracer argument name -> the model field that overrides the preset. These
# are field names, not dotted aliases. A field counts as supplied only when it
# holds something other than its default -- see VectorizeParams.asked_for --
# because clients routinely post every field they know about filled in with
# the defaults, and reading that as an override would replace a preset with a
# value the caller never chose.
_PRESET_OVERRIDES = {
    "corner_threshold": "processing_corner_threshold",
    "splice_threshold": "processing_splice_threshold",
    "max_iterations": "processing_max_iterations",
    "length_threshold": "processing_length_threshold",
}

# Extra latitude for the curve fitter when the bitmap was traced finer than
# the artwork. Measured on the reference file at 2x: 10,488 curve segments
# became 8,855 and the SVG 420K became 346K, with a hard-cornered test
# rectangle coming out pixel-identical to the conservative setting.
_FINER_LATITUDE = 1.5
_FINER_ITERATIONS = 32

# There used to be a _FINER_SPLICE = 80 here, raising splice_threshold on the
# finer trace on the theory that curves should splice rather than corner once
# the wobble is below one source pixel. Measured against cached Vectorizer.AI
# output it rounds off corners the artwork really has: on the reference
# lettering it turned the pointed tail of a B's counter into a plain blob, and
# dropping it removed both of that file's remaining 2-D shape errors, took the
# pixels we get wrong where the teacher is right from 1407 to 1209, and lifted
# agreement with the source from 98.55% to 98.69%. Across 25 cached samples it
# improved 5 and regressed none. The smoothing preset's own splice value stands.

ENGINE_NAME = "vtracer"


@dataclass(slots=True)
class TraceResult:
    svg: str
    width: int
    height: int
    engine: str = ENGINE_NAME
    prepared: "PreparedImage | None" = None

    @property
    def shape_count(self) -> int:
        return self.svg.count("<path")


def _tracer_kwargs(
    params: VectorizeParams, colours_are_pinned: bool, supersample: int = 1
) -> dict[str, object]:
    """Translate request parameters into VTracer's argument names."""
    # Start from the smoothing preset, then let any explicitly-supplied knob
    # take precedence over it.
    tuned = dict(SMOOTHING_PRESETS[params.processing_smoothing])
    supplied = {
        "corner_threshold": params.processing_corner_threshold,
        "splice_threshold": params.processing_splice_threshold,
        "max_iterations": params.processing_max_iterations,
        "length_threshold": params.processing_length_threshold,
    }
    for tracer_key, field_name in _PRESET_OVERRIDES.items():
        if params.asked_for(field_name):
            tuned[tracer_key] = supplied[tracer_key]

    # Same treatment for the detail preset.
    detail = dict(DETAIL_PRESETS[params.processing_detail])
    detail_supplied = {
        "filter_speckle": params.processing_min_area_px,
        "path_precision": params.processing_path_precision,
    }
    for tracer_key, field_name in _DETAIL_OVERRIDES.items():
        if params.asked_for(field_name):
            detail[tracer_key] = detail_supplied[tracer_key]

    color_precision = params.processing_color_precision
    layer_difference = params.processing_layer_difference

    # When preprocessing settled the colours -- a pinned palette, a colour
    # budget, or a palette detected from flat artwork -- the bitmap is already
    # exactly those colours. Letting the tracer cluster again would merge
    # neighbouring entries, so keep full precision unless the caller overrode
    # it themselves.
    if colours_are_pinned:
        if not params.asked_for("processing_color_precision"):
            color_precision = 8
        if not params.asked_for("processing_layer_difference"):
            layer_difference = 0

    # Coordinates come back in the supersampled space, so a decimal place of
    # precision buys nothing that the extra pixels have not already bought,
    # and the path data would be that much larger for it.
    path_precision = int(detail["path_precision"])
    if supersample > 1 and not params.asked_for("processing_path_precision"):
        path_precision = max(1, path_precision - 1)

    # Two of these knobs are measured in pixels, and the pixels change size
    # when the bitmap is traced at a multiple of its own resolution. Left
    # alone they quietly weaken: a 4-pixel shortest segment becomes 2 source
    # pixels, so the fitter starts following the staircase it was meant to cut
    # across, and the speckle filter loses three quarters of its reach. Both
    # are restored to what they mean at 1x -- a length scales with the factor,
    # an area with its square.
    length_threshold = float(tuned["length_threshold"]) * supersample
    filter_speckle = int(detail["filter_speckle"]) * supersample**2
    splice_threshold = int(tuned["splice_threshold"])
    max_iterations = int(tuned["max_iterations"])

    # Below one source pixel there is nothing real left to follow: whatever
    # wobble survives at that scale is where the quantizer happened to put the
    # boundary, not something the artwork contains. So the fitter is given
    # more latitude to cut across it -- longer segments, curves spliced rather
    # than cornered, and more passes to settle them.
    #
    # corner_threshold is deliberately left alone. It is the angle below which
    # a bend stays a hard corner, so anything above 90 rounds off a right
    # angle: raising it to the 110 of the 'medium' preset turned a test
    # rectangle's corner into a visible curve. Everything else here smooths
    # the long runs between corners and leaves the corners themselves intact.
    if supersample > 1:
        if not params.asked_for("processing_length_threshold"):
            length_threshold *= _FINER_LATITUDE
        if not params.asked_for("processing_max_iterations"):
            max_iterations = max(max_iterations, _FINER_ITERATIONS)

    return {
        "colormode": params.processing_color_mode,
        "hierarchical": params.processing_hierarchical,
        "mode": _CURVE_MODE[params.processing_curve_mode],
        "filter_speckle": filter_speckle,
        "color_precision": color_precision,
        "layer_difference": layer_difference,
        "corner_threshold": int(tuned["corner_threshold"]),
        "length_threshold": length_threshold,
        "max_iterations": max_iterations,
        "splice_threshold": splice_threshold,
        "path_precision": path_precision,
    }


def trace(prepared: PreparedImage, params: VectorizeParams) -> TraceResult:
    """Convert a prepared bitmap into SVG source, at the resolution that suits it.

    Preprocessing may offer a second copy of the bitmap at twice the
    resolution, and when it does, that copy is what gets traced. A one-pixel
    outline cannot be quantized evenly -- whether a pixel lands on the dark
    side depends on where the line falls inside it, so its width wanders and
    the curve fitter follows every wobble -- and the extra pixels halve that.

    This used to trace both copies and keep whichever came out with *fewer
    shapes*, on the theory that resampling sharpens noise as readily as
    geometry, so a jump in shape count meant the finer copy had multiplied a
    compressed image's mess. Shape count turns out to be the wrong question.
    It never asked whether the extra shapes were closer to the artwork, and
    measured against cached Vectorizer.AI corpus sources they are: on all 17
    samples where the two rules disagree the finer trace won on both colour
    distance and edge agreement, often heavily (one went 23.6% -> 42.2% edge
    agreement), and none regressed. The synthetic case the old rule was built
    for was checked against the *uncompressed* artwork it was drawn from
    rather than the compressed bitmap fed to the tracer, and the finer trace
    is closer there too, at JPEG quality 70 (detail 52.5% -> 67.4%) and at 50
    (47.2% -> 63.5%). It costs file size, which is what the shape count was
    really measuring.

    Simple flat artwork is unaffected either way: its finer trace already tied
    or won on shape count, so it was already being kept.

    This is CPU-bound and releases the GIL inside the Rust extension, so it is
    safe (and worthwhile) to call from a worker thread.
    """
    if prepared.finer is None:
        return _trace_one(prepared, params)

    try:
        return _trace_one(prepared.finer, params)
    except VectorizationFailed:
        # Never lose a conversion over the finer copy; the plain one still works.
        logger.warning("the 2x trace failed, falling back to the plain one")
        return _trace_one(prepared, params)


def _trace_one(prepared: PreparedImage, params: VectorizeParams) -> TraceResult:
    png = to_png_bytes(prepared.image)
    kwargs = _tracer_kwargs(
        params,
        colours_are_pinned=prepared.palette is not None,
        supersample=prepared.supersample,
    )
    logger.debug("tracing %sx%s with %s", prepared.traced_width, prepared.traced_height, kwargs)

    try:
        svg = vtracer.convert_raw_image_to_svg(png, img_format="png", **kwargs)
    except Exception as exc:  # the Rust binding raises bare exceptions
        logger.exception("tracer failed")
        raise VectorizationFailed(f"The tracer rejected this image: {exc}") from exc

    if not svg or "<svg" not in svg:
        raise VectorizationFailed("The tracer returned no geometry for this image.")

    return TraceResult(
        svg=svg,
        width=prepared.traced_width,
        height=prepared.traced_height,
        prepared=prepared,
    )
