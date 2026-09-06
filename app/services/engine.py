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

# VTracer argument name -> the model field that overrides the preset.
# These are field names, not dotted aliases: pydantic records what was
# supplied in `model_fields_set` under the field name.
_PRESET_OVERRIDES = {
    "corner_threshold": "processing_corner_threshold",
    "splice_threshold": "processing_splice_threshold",
    "max_iterations": "processing_max_iterations",
    "length_threshold": "processing_length_threshold",
}

ENGINE_NAME = "vtracer"


@dataclass(slots=True)
class TraceResult:
    svg: str
    width: int
    height: int
    engine: str = ENGINE_NAME


def _tracer_kwargs(params: VectorizeParams, colours_are_pinned: bool) -> dict[str, object]:
    """Translate request parameters into VTracer's argument names."""
    explicit = params.model_fields_set

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
        if field_name in explicit:
            tuned[tracer_key] = supplied[tracer_key]

    # Same treatment for the detail preset.
    detail = dict(DETAIL_PRESETS[params.processing_detail])
    detail_supplied = {
        "filter_speckle": params.processing_min_area_px,
        "path_precision": params.processing_path_precision,
    }
    for tracer_key, field_name in _DETAIL_OVERRIDES.items():
        if field_name in explicit:
            detail[tracer_key] = detail_supplied[tracer_key]

    color_precision = params.processing_color_precision
    layer_difference = params.processing_layer_difference

    # When preprocessing settled the colours -- a pinned palette, a colour
    # budget, or a palette detected from flat artwork -- the bitmap is already
    # exactly those colours. Letting the tracer cluster again would merge
    # neighbouring entries, so keep full precision unless the caller overrode
    # it themselves.
    if colours_are_pinned:
        if "processing_color_precision" not in explicit:
            color_precision = 8
        if "processing_layer_difference" not in explicit:
            layer_difference = 0

    return {
        "colormode": params.processing_color_mode,
        "hierarchical": params.processing_hierarchical,
        "mode": _CURVE_MODE[params.processing_curve_mode],
        "filter_speckle": int(detail["filter_speckle"]),
        "color_precision": color_precision,
        "layer_difference": layer_difference,
        "corner_threshold": int(tuned["corner_threshold"]),
        "length_threshold": float(tuned["length_threshold"]),
        "max_iterations": int(tuned["max_iterations"]),
        "splice_threshold": int(tuned["splice_threshold"]),
        "path_precision": int(detail["path_precision"]),
    }


def trace(prepared: PreparedImage, params: VectorizeParams) -> TraceResult:
    """Convert a prepared bitmap into SVG source.

    This is CPU-bound and releases the GIL inside the Rust extension, so it
    is safe (and worthwhile) to call from a worker thread.
    """
    png = to_png_bytes(prepared.image)
    kwargs = _tracer_kwargs(params, colours_are_pinned=prepared.palette is not None)
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
    )
