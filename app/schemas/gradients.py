"""Request parameters for the gradient refinement stage.

Kept apart from :mod:`app.schemas.params` because the stage is its own API:
it takes a raster and a finished SVG and hands back a better SVG, and knows
nothing about tracing, billing or output geometry. ``/api/v1/vectorize``
passes these through under ``processing.gradients.*``; the standalone endpoint
takes them under ``gradients.*``.

Every default is chosen so that leaving all of them alone is the right answer
for ordinary artwork. Nothing here has to be switched on to get gradients.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GradientParams(BaseModel):
    """How hard to look for gradients, and how faithfully to reproduce them."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    enabled: bool = Field(
        default=True,
        alias="gradients.enabled",
        description=(
            "Fit gradients to shapes whose source pixels shade. Turn off to "
            "keep the tracer's flat fills exactly as they came."
        ),
    )
    radial: bool = Field(
        default=True,
        alias="gradients.radial",
        description=(
            "Also consider radial gradients. Shading that spreads from a "
            "point -- a sphere, a glow, a spotlit background -- cannot be "
            "described by a straight ramp, and comes back flat without this."
        ),
    )
    merge_patches: bool = Field(
        default=True,
        alias="gradients.merge_patches",
        description=(
            "Fit neighbouring shapes that are pieces of one ramp together and "
            "give them a single shared gradient, so the colour runs "
            "continuously across the seam instead of stepping at it. This is "
            "what removes the hard-edged patches a traced gradient arrives "
            "as. No geometry is changed; only the paint."
        ),
    )
    max_stops: int = Field(
        default=12,
        alias="gradients.max_stops",
        ge=2,
        le=64,
        description=(
            "Most stops one gradient may use. A straight ramp needs two; "
            "shading that curves needs more, and gets only as many as it "
            "takes to stay inside gradients.tolerance."
        ),
    )
    tolerance: float = Field(
        default=1.5,
        alias="gradients.tolerance",
        gt=0,
        le=20,
        description=(
            "How far the fitted ramp may sit from the artwork's own colours, "
            "as a CIELAB distance, before another stop is added. Around 1 is "
            "the threshold of visibility for a large flat area; lower values "
            "track the shading more closely at the cost of a few more stops."
        ),
    )
    min_travel: float = Field(
        default=2.5,
        alias="gradients.min_travel",
        ge=0,
        le=100,
        description=(
            "How far the colour has to move from one end of a shape to the "
            "other, as a CIELAB distance, to be worth a gradient at all. "
            "Below this it is a flat fill with a slight cast, and fitting a "
            "ramp to it would only be fitting the noise."
        ),
    )
    max_residual: float = Field(
        default=6.0,
        alias="gradients.max_residual",
        gt=0,
        le=100,
        description=(
            "How far the shape's pixels may sit from the fitted ramp, as a "
            "mean CIELAB distance, before the shape is called textured rather "
            "than shaded and keeps its flat fill."
        ),
    )
    patch_distance: float = Field(
        default=12.0,
        alias="gradients.patch_distance",
        ge=0,
        le=100,
        description=(
            "How close in CIELAB two neighbouring flat shapes have to be "
            "before gradients.merge_patches will consider them slices of one "
            "ramp rather than two different colours."
        ),
    )
    min_area_px: int = Field(
        default=64,
        alias="gradients.min_area_px",
        ge=16,
        le=1_000_000,
        description=(
            "Smallest shape examined, in source pixels. A handful of pixels "
            "carries no reliable evidence of a ramp."
        ),
    )
