"""Request parameters for POST /api/v1/vectorize.

Parameters use vectorizer.ai's flat dotted-name convention
(``output.file_format``, ``processing.max_colors``, ...) so they can be sent
as multipart form fields, urlencoded fields, or a flat JSON object without
any nesting gymnastics on the client side.

Unknown parameters are rejected rather than silently ignored: a typo in
``output.file_format`` should be loud, not produce a surprise SVG.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.gradients import GradientParams
from app.utils import units


def _gradient_default(name: str) -> Any:
    """Read a default from GradientParams rather than restating it.

    The gradient stage owns these values; the vectorize surface only forwards
    them. Copying the numbers across would let the two drift apart, and a
    default that means one thing on /vectorize and another on the stage it
    calls is the kind of difference nobody finds until it matters.
    """
    return GradientParams.model_fields[name].default

Mode = Literal["production", "preview", "test"]
FileFormat = Literal["svg", "pdf", "eps", "png"]
ColorMode = Literal["color", "binary"]
Hierarchy = Literal["stacked", "cutout"]
CurveMode = Literal["spline", "polygon", "pixel"]
Smoothing = Literal["none", "low", "medium", "high"]
Denoise = Literal["none", "low", "medium", "high"]
Detail = Literal["low", "standard", "high", "maximum"]
DrawStyle = Literal["fill_shapes", "stroke_shapes", "stroke_edges"]
GroupBy = Literal["none", "color"]
CombinePaths = Literal["none", "shapes", "colors"]
SizeUnit = Literal["px", "pt", "pc", "in", "mm", "cm"]
AspectRatio = Literal["preserve_inset", "preserve_overflow", "stretch"]

VECTOR_FORMATS: frozenset[str] = frozenset({"svg", "pdf", "eps"})
RASTER_FORMATS: frozenset[str] = frozenset({"png"})

_HEX_RE = re.compile(r"^#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

MEDIA_TYPES: dict[str, str] = {
    "svg": "image/svg+xml",
    "pdf": "application/pdf",
    "eps": "application/postscript",
    "png": "image/png",
}


def normalise_hex(value: str) -> str:
    """``fff`` / ``#FFF`` / ``#ffffff`` -> ``#ffffff``."""
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return "#" + value.lower()


def _split_list(value: Any) -> Any:
    """Accept ``"#fff,#000"``, ``["#fff", "#000"]`` or ``None``."""
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, str):
        return [part for part in (p.strip() for p in value.split(",")) if part]
    return value


def _coerce_combine(value: Any) -> Any:
    """Accept booleans for output.combine_paths.

    The parameter started life as a bool, and clients cache schemas: a stale
    Swagger page happily submits `true` long after the field became an enum.
    Rejecting that is pure friction, so map it onto the equivalent mode --
    `true` means the recommended per-shape merge, not the blunt per-colour one.
    """
    if isinstance(value, bool):
        return "shapes" if value else "none"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return "shapes"
        if lowered in ("false", "no", "off", "0"):
            return "none"
    return value


class VectorizeParams(BaseModel):
    """Validated, engine-agnostic description of one vectorization job."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    # --- Billing / quality tier ---------------------------------------------
    mode: Mode = Field(
        default="production",
        description=(
            "production = full quality, billed. preview = full quality but "
            "watermarked and cheaper. test = free and watermarked, for wiring "
            "up an integration."
        ),
    )

    # --- Input ---------------------------------------------------------------
    input_max_pixels: int | None = Field(
        default=None,
        alias="input.max_pixels",
        ge=100,
        description=(
            "Downscale the input to at most this many pixels before tracing. "
            "Lower values trace faster and produce simpler paths."
        ),
    )

    # --- Processing (how the raster becomes shapes) --------------------------
    processing_color_mode: ColorMode = Field(
        default="color",
        alias="processing.color_mode",
        description="binary traces a single-colour silhouette; color keeps colours.",
    )
    processing_max_colors: int = Field(
        default=0,
        alias="processing.max_colors",
        ge=0,
        le=256,
        description=(
            "Reduce the artwork to at most N colours. The palette is derived "
            "from the image's own inks, and both pixels and traced fills are "
            "mapped onto it. Leave it at 0 and flat artwork gets this "
            "automatically, with the number of inks worked out for it; "
            "continuous-tone images are left alone."
        ),
    )
    processing_palette: list[str] | None = Field(
        default=None,
        alias="processing.palette",
        max_length=256,
        description="Force output colours to this palette. Comma-separated hex values.",
    )
    processing_hierarchical: Hierarchy = Field(
        default="cutout",
        alias="processing.hierarchical",
        description=(
            "cutout emits non-overlapping shapes; stacked layers them "
            "back-to-front for a smaller file. output.combine_paths needs "
            "cutout, so choosing stacked turns combining off."
        ),
    )
    processing_curve_mode: CurveMode = Field(
        default="spline",
        alias="processing.curve_mode",
        description="spline fits Bezier curves, polygon straight edges, pixel none.",
    )
    processing_color_merge: float = Field(
        default=16.0,
        alias="processing.color_merge",
        ge=0,
        le=160,
        description=(
            "After tracing, snap each minor fill colour onto the nearest more "
            "prominent one within this RGB distance. Cleans up the pale lumps "
            "left where anti-aliasing blends two flat regions, and the "
            "near-identical fills the tracer sometimes splits a band into. "
            "0 disables it; raise it if transition shapes are still visible, "
            "lower it if genuinely distinct colours are being merged."
        ),
    )
    processing_detail: Detail = Field(
        default="standard",
        alias="processing.detail",
        description=(
            "How small a feature has to be before it is discarded, plus the "
            "coordinate precision kept in the path data. Hairline rules and "
            "specks of a few pixels only survive at 'maximum', which also "
            "keeps every scrap of compression noise, so expect a far larger "
            "file. Raising processing.denoise does not rescue that -- and it "
            "washes out the very thin features 'maximum' exists to keep, so "
            "lower the denoise level when you turn detail up."
        ),
    )
    processing_denoise: Denoise = Field(
        default="low",
        alias="processing.denoise",
        description=(
            "Median despeckle applied before tracing. JPEG artefacts and scan "
            "noise create thousands of near-duplicate colours, and the tracer "
            "emits each as its own ragged sliver -- the usual cause of chunky, "
            "notched edges. A median filter removes that noise while keeping "
            "hard edges sharp, unlike a blur. Raise it for noisier sources; "
            "set 'none' for pristine synthetic artwork or very small images."
        ),
    )
    processing_smoothing: Smoothing = Field(
        default="low",
        alias="processing.smoothing",
        description=(
            "How hard to round off corners. Raises corner_threshold, "
            "splice_threshold, max_iterations and length_threshold together. "
            "'high' rounds aggressively and can bow thin straight elements, "
            "so step up only as far as your artwork tolerates. Individual "
            "processing.* values you set explicitly override the preset."
        ),
    )
    processing_min_area_px: int = Field(
        default=4,
        alias="processing.shapes.min_area_px",
        ge=0,
        le=4096,
        description="Discard specks smaller than this many pixels.",
    )
    processing_color_precision: int = Field(
        default=6,
        alias="processing.color_precision",
        ge=1,
        le=8,
        description="Bits of colour kept per channel when clustering.",
    )
    processing_layer_difference: int = Field(
        default=16,
        alias="processing.layer_difference",
        ge=0,
        le=128,
        description="Minimum colour distance between stacked layers.",
    )
    processing_corner_threshold: int = Field(
        default=60,
        alias="processing.corner_threshold",
        ge=0,
        le=180,
        description="Angle below which a corner is kept sharp instead of smoothed.",
    )
    processing_length_threshold: float = Field(
        default=4.0,
        alias="processing.length_threshold",
        ge=3.5,
        le=10.0,
        description="Shortest path segment the curve fitter will emit.",
    )
    processing_splice_threshold: int = Field(
        default=45,
        alias="processing.splice_threshold",
        ge=0,
        le=180,
        description="Angle above which adjacent curves are spliced rather than joined.",
    )
    processing_max_iterations: int = Field(
        default=10,
        alias="processing.max_iterations",
        ge=1,
        le=64,
        description="Curve-fitting refinement passes.",
    )
    processing_path_precision: int = Field(
        default=3,
        alias="processing.path_precision",
        ge=0,
        le=8,
        description="Decimal places kept in path coordinates. Lower = smaller files.",
    )

    # --- Gradients (forwarded to the refinement stage) ------------------------
    processing_gradients: bool = Field(
        default=_gradient_default("enabled"),
        alias="processing.gradients",
        description=(
            "After tracing, compare each shape against the pixels it came "
            "from and replace flat fills that cover shading with a real "
            "gradient. On by default: the tracer can only emit flat colours, "
            "so without this a sky or a sphere comes back either banded or "
            "averaged into one tone. Turn it off to keep the flat fills."
        ),
    )
    processing_gradients_radial: bool = Field(
        default=_gradient_default("radial"),
        alias="processing.gradients.radial",
        description=(
            "Let a shape be fitted with a radial gradient as well as a linear "
            "one. Shading that spreads from a point cannot be described by a "
            "straight ramp and comes back flat without this."
        ),
    )
    processing_gradients_merge_patches: bool = Field(
        default=_gradient_default("merge_patches"),
        alias="processing.gradients.merge_patches",
        description=(
            "Fit neighbouring shapes that are slices of one ramp together, so "
            "they share a single gradient and the colour runs continuously "
            "across the seam. This is what removes the hard-edged patches a "
            "traced gradient otherwise arrives as."
        ),
    )
    processing_gradients_max_stops: int = Field(
        default=_gradient_default("max_stops"),
        alias="processing.gradients.max_stops",
        ge=2,
        le=64,
        description=(
            "Most stops one fitted gradient may use. A straight ramp needs "
            "two; shading that curves gets as many as tolerance demands."
        ),
    )
    processing_gradients_tolerance: float = Field(
        default=_gradient_default("tolerance"),
        alias="processing.gradients.tolerance",
        gt=0,
        le=20,
        description=(
            "How far a fitted gradient may sit from the artwork's own "
            "colours, as a CIELAB distance, before another stop is added."
        ),
    )

    # --- Output --------------------------------------------------------------
    output_file_format: FileFormat = Field(
        default="svg",
        alias="output.file_format",
        description="svg, pdf, eps (vector) or png (rasterized preview of the vector).",
    )
    output_draw_style: DrawStyle = Field(
        default="fill_shapes",
        alias="output.draw_style",
        description="Fill shapes, outline them, or draw only the shape edges.",
    )
    output_stroke_width: float = Field(
        default=1.0,
        alias="output.shapes.stroke_width",
        gt=0,
        le=100,
        description="Stroke width used by the stroke_* draw styles, in pixels.",
    )
    output_combine_paths: CombinePaths = Field(
        default="shapes",
        alias="output.combine_paths",
        description=(
            "How much to fuse traced shapes into single path objects, which "
            "is what editors count and select. 'none' keeps every fragment "
            "separate, so one letter can arrive as dozens of objects. "
            "'shapes' merges only fragments that touch, giving one object per "
            "letter or motif -- the useful middle. 'colors' merges everything "
            "of one fill into a single object, which makes a whole word "
            "un-selectable letter by letter. Rendering is identical either "
            "way. Accepts true/false as aliases for shapes/none."
        ),
    )
    output_group_by: GroupBy = Field(
        default="none",
        alias="output.group_by",
        description="Group emitted paths, e.g. one <g> per colour for easier editing.",
    )
    output_gap_filler_enabled: bool = Field(
        default=True,
        alias="output.gap_filler.enabled",
        description=(
            "Hide the seams renderers show between abutting shapes: seal each "
            "boundary with a non-scaling stroke in the blend of the two "
            "colours that meet, and lay the most-covering ink under the whole "
            "canvas so a split seam can never reveal the page. Turn off to "
            "emit bare fills."
        ),
    )
    output_gap_filler_stroke_width: float = Field(
        default=1.0,
        alias="output.gap_filler.stroke_width",
        gt=0,
        le=10,
        description=(
            "Width of the seam seal, in device pixels — the stroke is "
            "non-scaling, so this is a screen/print width and not a width in "
            "the artwork's own coordinates. One pixel is the width of the "
            "anti-aliasing seam it exists to cover. It used to have to be "
            "wider to hide real gaps between independently fitted curves too, "
            "but the backdrop handles those now, and wider only softens every "
            "edge: measured on the reference lettering, 1.0 costs 0.055 of "
            "mean colour error against 0.35 and 1.5 costs 0.094, with no "
            "difference in sealing."
        ),
    )
    output_background: str | None = Field(
        default=None,
        alias="output.background",
        description="Hex colour painted behind the artwork, or transparent.",
    )

    # --- Output geometry -----------------------------------------------------
    output_size_scale: float | None = Field(
        default=None,
        alias="output.size.scale",
        gt=0,
        le=100,
        description="Uniform scale factor applied to the traced artwork.",
    )
    output_size_width: float | None = Field(
        default=None, alias="output.size.width", gt=0
    )
    output_size_height: float | None = Field(
        default=None, alias="output.size.height", gt=0
    )
    output_size_unit: SizeUnit = Field(
        default="px",
        alias="output.size.unit",
        description="Unit for output.size.width / output.size.height.",
    )
    output_size_aspect_ratio: AspectRatio = Field(
        default="preserve_inset",
        alias="output.size.aspect_ratio",
        description="How to reconcile a width+height that differs from the source ratio.",
    )

    # --- Raster output tuning ------------------------------------------------
    output_bitmap_dpi: int = Field(
        default=96,
        alias="output.bitmap.dpi",
        ge=24,
        le=1200,
        description="Rasterization density for output.file_format=png.",
    )
    output_bitmap_anti_aliasing: bool = Field(
        default=True, alias="output.bitmap.anti_aliasing"
    )

    # --- Policy --------------------------------------------------------------
    policy_retention_days: int = Field(
        default=0,
        alias="policy.retention_days",
        ge=0,
        le=30,
        description=(
            "Accepted for API compatibility. This server is stateless and "
            "never persists images, so only 0 has any effect."
        ),
    )

    # --- Validators ----------------------------------------------------------
    @model_validator(mode="before")
    @classmethod
    def _coerce_inputs(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        cleaned = {}
        for key, value in data.items():
            if key in ("processing.palette", "processing_palette"):
                value = _split_list(value)
            elif key in ("output.combine_paths", "output_combine_paths"):
                value = _coerce_combine(value)
            # An empty form field means "not supplied", so clients can post a
            # complete field set with blanks and still get the defaults.
            if value == "" or value is None:
                continue
            cleaned[key] = value
        return cleaned

    # Asking any of these for something other than its default means the
    # caller is driving the colour pipeline themselves, and the automatic
    # palette stands down. detail and denoise are included because both make
    # promises about what survives tracing -- detail=maximum exists to keep
    # hairline features and every scrap of compression noise -- and flattening
    # the colours first would quietly break them.
    _COLOUR_PIPELINE_FIELDS = frozenset(
        {
            "processing_max_colors",
            "processing_detail",
            "processing_color_precision",
            "processing_layer_difference",
        }
    )

    def asked_for(self, field: str) -> bool:
        """True when *field* holds something other than its default.

        What counts as a choice is the value, not the mention. Plenty of
        clients post every field they know about, filled in with the defaults
        -- Swagger's "Try it out" form does exactly that -- so reading the
        presence of a field as an instruction quietly changes the output for
        those callers while looking like it has done nothing at all.
        """
        return getattr(self, field) != type(self).model_fields[field].default

    @property
    def auto_palette(self) -> bool:
        """Whether preprocessing may derive a palette from the artwork itself.

        Flat artwork traced as-is picks up a shape for every anti-aliasing
        band, which is what makes outlines look grey and edges look faceted.
        Deriving the artwork's own inks fixes that without the caller naming
        anything -- but only as a default, never over an explicit choice.

        What counts as a choice is the value, not the mention -- see
        :meth:`asked_for`.
        """
        if self.processing_palette:
            return False
        return not any(self.asked_for(name) for name in self._COLOUR_PIPELINE_FIELDS)

    @model_validator(mode="after")
    def _check_combinations(self) -> VectorizeParams:
        if self.processing_palette is not None:
            bad = [c for c in self.processing_palette if not _HEX_RE.match(c)]
            if bad:
                joined = ", ".join(bad[:5])
                raise ValueError(
                    "processing.palette contains invalid hex colours: " + joined
                )
            self.processing_palette = [
                normalise_hex(c) for c in self.processing_palette
            ]

        if self.output_background is not None:
            bg = self.output_background.strip().lower()
            if bg in ("transparent", "none"):
                self.output_background = "transparent"
            elif _HEX_RE.match(bg):
                self.output_background = normalise_hex(bg)
            else:
                raise ValueError(
                    "output.background must be a hex colour or transparent."
                )

        if self.output_size_scale is not None and (
            self.output_size_width is not None or self.output_size_height is not None
        ):
            raise ValueError(
                "output.size.scale cannot be combined with "
                "output.size.width / output.size.height."
            )

        if self.processing_palette and not self.asked_for("processing_denoise"):
            # Median denoising and a pinned palette fight each other. The
            # filter widens the boundary between two colours into a ramp of
            # intermediate tones, and every tone in that ramp is a pixel the
            # quantizer has to guess at. Preprocessing resolves those blends
            # onto the two colours they lie between, so the guess is a good
            # one -- but a wider ramp still eats thin features from both
            # sides. On the test artwork denoising cost 3.7k pixels of the
            # white outline around the lettering. Quantizing to a fixed
            # palette already removes noise, so the filter has nothing to add
            # here.
            self.processing_denoise = "none"

        if self.output_combine_paths != "none":
            # Combining reorders shapes into one element per colour, which
            # destroys the back-to-front paint order that 'stacked' relies on:
            # counters and holes get painted over solid. Non-overlapping
            # 'cutout' shapes carry no order dependency, so combining is safe.
            if self.processing_hierarchical == "stacked":
                # Rendering correctness wins over the object-count nicety, and
                # this must not be an error: clients that echo every default
                # back (Swagger's "Try it out" does) would all trip it.
                self.output_combine_paths = "none"

        if self.processing_color_mode == "binary" and self.processing_palette:
            raise ValueError(
                "processing.palette is not meaningful with "
                "processing.color_mode=binary."
            )

        return self

    # --- Derived -------------------------------------------------------------
    @property
    def gradient_params(self) -> GradientParams:
        """The subset of this request the gradient stage needs to see."""
        return GradientParams.model_validate(
            {
                "gradients.enabled": self.processing_gradients,
                "gradients.radial": self.processing_gradients_radial,
                "gradients.merge_patches": self.processing_gradients_merge_patches,
                "gradients.max_stops": self.processing_gradients_max_stops,
                "gradients.tolerance": self.processing_gradients_tolerance,
            }
        )

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES[self.output_file_format]

    @property
    def is_vector_output(self) -> bool:
        return self.output_file_format in VECTOR_FORMATS

    @property
    def watermark(self) -> bool:
        """test and preview results are marked so they cannot ship unnoticed."""
        return self.mode in ("test", "preview")

    def target_size_px(self, source_w: int, source_h: int) -> tuple[float, float]:
        """Resolve the requested output geometry to CSS pixels."""
        if self.output_size_scale is not None:
            return source_w * self.output_size_scale, source_h * self.output_size_scale

        unit = self.output_size_unit
        want_w = (
            units.to_pixels(self.output_size_width, unit)
            if self.output_size_width is not None
            else None
        )
        want_h = (
            units.to_pixels(self.output_size_height, unit)
            if self.output_size_height is not None
            else None
        )

        if want_w is None and want_h is None:
            return float(source_w), float(source_h)

        ratio = source_h / source_w if source_w else 1.0
        if want_w is not None and want_h is None:
            return want_w, want_w * ratio
        if want_h is not None and want_w is None:
            return (want_h / ratio if ratio else want_h), want_h

        if self.output_size_aspect_ratio == "stretch":
            return want_w, want_h

        # Fit the source box inside (inset) or around (overflow) the request.
        scale_w, scale_h = want_w / source_w, want_h / source_h
        scale = (
            min(scale_w, scale_h)
            if self.output_size_aspect_ratio == "preserve_inset"
            else max(scale_w, scale_h)
        )
        return source_w * scale, source_h * scale
