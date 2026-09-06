"""Length-unit helpers.

Internally the pipeline works in CSS pixels (the tracer's coordinate space).
Physical units are resolved against a 96 dpi reference, matching the SVG spec
and what Illustrator/Inkscape assume for unitless SVG user units.
"""

from __future__ import annotations

CSS_DPI = 96.0
POINTS_PER_INCH = 72.0

# How many CSS pixels one unit represents.
_PX_PER_UNIT: dict[str, float] = {
    "px": 1.0,
    "pt": CSS_DPI / POINTS_PER_INCH,   # 1pt = 1/72in = 1.3333px
    "pc": CSS_DPI / 6.0,               # 1pc = 12pt
    "in": CSS_DPI,
    "mm": CSS_DPI / 25.4,
    "cm": CSS_DPI / 2.54,
}

SUPPORTED_UNITS = tuple(_PX_PER_UNIT)


def to_pixels(value: float, unit: str) -> float:
    """Convert *value* expressed in *unit* into CSS pixels."""
    try:
        return value * _PX_PER_UNIT[unit]
    except KeyError:  # pragma: no cover - guarded by schema validation
        raise ValueError(f"Unsupported unit: {unit!r}") from None


def from_pixels(value_px: float, unit: str) -> float:
    """Convert *value_px* CSS pixels into *unit*."""
    try:
        return value_px / _PX_PER_UNIT[unit]
    except KeyError:  # pragma: no cover
        raise ValueError(f"Unsupported unit: {unit!r}") from None


def px_to_points(value_px: float) -> float:
    return from_pixels(value_px, "pt")
