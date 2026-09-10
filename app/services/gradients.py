"""Gradient refinement: the second stage, judged against the source bitmap.

The tracer can only emit flat fills. Everything it is given that shades -- a
sky, a sphere, a badge, a stroke that fades -- has to leave it as either a
stack of flat bands or one averaged colour, and both are wrong in a way that
is obvious on screen: bands read as hard-edged patches inside something that
should be smooth, and the average throws the shading away entirely.

So this stage goes back to the pixels. It rasterizes the finished vector so it
knows exactly which source pixels each shape covers, fits a colour model to
those pixels, and where they turn out to lie on a ramp replaces the flat fill
with a real SVG gradient along it. Three things it does that a fill-colour
histogram cannot:

* **It knows where each shape is.** Attributing pixels by "nearest fill
  colour" mixes every shape of one colour together, wherever it sits, so a
  ramp fitted to them describes nothing in particular. A geometric mask is
  what makes a per-shape fit mean anything.
* **It fits curves, not only lines.** Shading is rarely linear in sRGB --
  falloff across a sphere certainly is not -- so the ramp is profiled along
  its own axis and reduced to as many stops as it takes to stay inside
  tolerance. Two stops where two will do, more where the shading needs them.
* **It repairs patches.** Neighbouring shapes that are pieces of one ramp are
  fitted together and share a single gradient in user space, so the colour
  runs continuously across the seam between them. No geometry is touched;
  only the paint changes.

The analysis runs on numpy, scipy and scikit-image: least squares for the
axial fit, a quadratic-form solve refined by Nelder-Mead for the radial
centre, and CIELAB for every judgement about colour, because a threshold in
raw RGB means one thing among dark colours and something else among light
ones.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

import numpy as np
from lxml import etree
from PIL import Image
from scipy import ndimage, optimize
from skimage import color as skcolor

from app.core.logging import get_logger
from app.schemas.gradients import GradientParams

logger = get_logger("gradients")

SVG_NS = "http://www.w3.org/2000/svg"

# Above this the analysis raster is reduced. Colour varies slowly across a
# region by definition -- that is what makes it a gradient -- so the fit does
# not need every pixel, and the masks come out very much cheaper.
_ANALYSIS_MAX_PIXELS = 4_000_000

# Pixels fitted per region. Past this the fit stops improving and only the
# clock moves.
_MAX_SAMPLES = 40_000

# Bins used to profile the colour along a gradient's own axis.
_PROFILE_BINS = 64

# Deterministic subsampling: the same request has to produce the same file.
_SEED = 0x5EED

# How much of a shape has to resolve to the shape's own ink before its pixels
# are worth fitting. Below this it is more boundary than shape.
_OWN_INK_QUORUM = 0.35

# Border pixels dropped from every mask before fitting. A traced boundary sits
# in the middle of the source's anti-aliasing ramp, so the pixels either side
# of it are blends of two regions and belong to neither.
_ERODE_PX = 2


def _q(tag: str) -> str:
    return f"{{{SVG_NS}}}{tag}"


def _fmt(value: float) -> str:
    """Trim trailing zeros; SVG numbers are text and the file gets shipped."""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


_HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")


def _rgb(value):
    if not value:
        return None
    match = _HEX_RE.match(str(value).strip())
    if not match:
        return None
    body = match.group(1)
    return tuple(int(body[i : i + 2], 16) for i in (0, 2, 4))


def _hex(rgb) -> str:
    r, g, b = (int(max(0, min(255, round(float(c))))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _lab(rgb) -> np.ndarray:
    """sRGB 0-255 -> CIELAB. Shape is preserved bar the trailing axis."""
    array = np.asarray(rgb, dtype=np.float64) / 255.0
    return skcolor.rgb2lab(array.reshape(-1, 1, 3)).reshape(array.shape)


def _delta_e(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """CIE76 between two Lab arrays: a Euclidean distance, and fast.

    CIEDE2000 is the better metric and is what the report quotes, but it costs
    an order of magnitude more and every use here is a threshold comparison
    where the two agree on the answer.
    """
    return np.sqrt(((a - b) ** 2).sum(axis=-1))


@dataclass(slots=True)
class GradientReport:
    """What the stage did, for the response headers and the logs."""

    regions: int = 0            # shapes whose pixels were examined
    gradients: int = 0          # gradients written into the document
    linear: int = 0
    radial: int = 0
    merged: int = 0             # flat patches fused onto one shared ramp
    stops: int = 0              # stops emitted in total
    residual_before: float = 0.0    # mean dE of the flat fills against source
    residual_after: float = 0.0     # mean dE once the gradients are in
    ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "regions": self.regions,
            "gradients": self.gradients,
            "linear": self.linear,
            "radial": self.radial,
            "merged": self.merged,
            "stops": self.stops,
            "residual_before": round(self.residual_before, 3),
            "residual_after": round(self.residual_after, 3),
            "ms": round(self.ms, 1),
        }


# --- path flattening ---------------------------------------------------------

_COMMAND_RE = re.compile(r"([MmLlHhVvCcSsQqTtZz])([^MmLlHhVvCcSsQqTtZzAa]*)")
_NUMBER_RE = re.compile(r"-?\d*\.?\d+(?:[eE][-+]?\d+)?")
_ARC_RE = re.compile(r"[Aa]")
_ARITY = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2}

# --- transforms ---------------------------------------------------------------
#
# The tracer hands every shape a translate() rather than writing the offset
# into the coordinates, so a mask built from raw path data lands somewhere the
# shape is not. Getting this wrong is not a small error: a shape rasterized at
# the wrong place is fitted to whatever the artwork happens to hold there, and
# the ground of a landscape comes back painted with the sky's gradient.

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

_TRANSFORM_RE = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^()]*)\)")


def _multiply(one, two):
    """Compose two affines, in SVG's order: `transform="A B"` applies A to B."""
    a1, b1, c1, d1, e1, f1 = one
    a2, b2, c2, d2, e2, f2 = two
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def affine(transform):
    """An element's transform attribute as (a, b, c, d, e, f), or None.

    None means "this one cannot be read", and a shape that cannot be placed is
    left out of the analysis rather than guessed at.
    """
    if not transform or not transform.strip():
        return IDENTITY

    matrix = IDENTITY
    position = 0
    for match in _TRANSFORM_RE.finditer(transform):
        if transform[position : match.start()].strip(" ,\t\r\n"):
            return None
        position = match.end()
        name = match.group(1)
        values = [float(n) for n in _NUMBER_RE.findall(match.group(2))]
        if name == "matrix" and len(values) == 6:
            step = tuple(values)
        elif name == "translate" and len(values) in (1, 2):
            step = (1.0, 0.0, 0.0, 1.0, values[0], values[1] if len(values) > 1 else 0.0)
        elif name == "scale" and len(values) in (1, 2):
            sx = values[0]
            sy = values[1] if len(values) > 1 else sx
            step = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif name == "rotate" and len(values) in (1, 3):
            angle = math.radians(values[0])
            cos, sin = math.cos(angle), math.sin(angle)
            step = (cos, sin, -sin, cos, 0.0, 0.0)
            if len(values) == 3:
                cx, cy = values[1], values[2]
                step = _multiply(
                    _multiply((1.0, 0.0, 0.0, 1.0, cx, cy), step),
                    (1.0, 0.0, 0.0, 1.0, -cx, -cy),
                )
        elif name == "skewX" and len(values) == 1:
            step = (1.0, 0.0, math.tan(math.radians(values[0])), 1.0, 0.0, 0.0)
        elif name == "skewY" and len(values) == 1:
            step = (1.0, math.tan(math.radians(values[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            return None
        matrix = _multiply(matrix, step)

    if transform[position:].strip(" ,\t\r\n"):
        return None
    return matrix


def _is_translation(matrix) -> bool:
    a, b, c, d, _, _ = matrix
    return abs(a - 1) < 1e-9 and abs(d - 1) < 1e-9 and abs(b) < 1e-9 and abs(c) < 1e-9


def _invert(matrix):
    a, b, c, d, e, f = matrix
    determinant = a * d - b * c
    if abs(determinant) < 1e-12:
        return None
    return (
        d / determinant,
        -b / determinant,
        -c / determinant,
        a / determinant,
        (c * f - d * e) / determinant,
        (b * e - a * f) / determinant,
    )


def _apply(matrix, points: np.ndarray) -> np.ndarray:
    a, b, c, d, e, f = matrix
    x, y = points[:, 0], points[:, 1]
    return np.column_stack([a * x + c * y + e, b * x + d * y + f])


def bake_translation(d: str, dx: float, dy: float):
    """Fold a translate into absolute path data. None if that is not safe.

    Worth the trouble because it puts shapes into a common coordinate system.
    Shapes that share a ramp can then share one definition of it rather than
    needing a copy each in their own space -- and, because they end up sharing
    a fill as well, the stage that fuses touching shapes of one colour can
    still fuse them afterwards.
    """
    if not d:
        return None
    out: list[str] = []
    for match in _COMMAND_RE.finditer(d):
        letter, body = match.group(1), match.group(2)
        kind = letter.upper()
        if letter.islower() and kind != "Z":
            return None  # relative coordinates: the shift does not apply
        if kind == "Z":
            out.append("Z")
            continue
        numbers = [float(n) for n in _NUMBER_RE.findall(body)]
        if not numbers:
            return None
        if kind == "H":
            shifted = [n + dx for n in numbers]
        elif kind == "V":
            shifted = [n + dy for n in numbers]
        else:
            if len(numbers) % 2:
                return None
            shifted = [n + (dx if i % 2 == 0 else dy) for i, n in enumerate(numbers)]
        out.append(kind + " ".join(_fmt(n) for n in shifted))
    return "".join(out) or None


def _bezier(p0, p1, p2, p3, steps: int) -> np.ndarray:
    t = np.linspace(0.0, 1.0, steps + 1)[1:, None]
    u = 1.0 - t
    return u**3 * p0 + 3 * u**2 * t * p1 + 3 * u * t**2 * p2 + t**3 * p3


def _steps_for(points) -> int:
    """Enough segments that the flattened curve stays inside a pixel of it."""
    span = 0.0
    for a, b in zip(points, points[1:]):
        span += math.hypot(float(b[0] - a[0]), float(b[1] - a[1]))
    return int(max(2, min(96, span / 1.5)))


def flatten(d: str):
    """Path data -> one polygon per subpath, or None if it cannot be read.

    Arcs are the only SVG command left out. Nothing in this pipeline emits
    one, and a path carrying one keeps the fill it already has rather than
    being judged from a mask that would be wrong.
    """
    if not d or _ARC_RE.search(d):
        return None

    subpaths: list[np.ndarray] = []
    current: list[tuple[float, float]] = []
    x = y = start_x = start_y = 0.0
    control = None
    previous = ""

    def close_subpath() -> None:
        if len(current) > 2:
            subpaths.append(np.asarray(current, dtype=np.float64))

    for match in _COMMAND_RE.finditer(d):
        letter = match.group(1)
        numbers = [float(n) for n in _NUMBER_RE.findall(match.group(2))]
        relative = letter.islower()
        kind = letter.upper()

        if kind == "Z":
            close_subpath()
            current = []
            x, y = start_x, start_y
            control, previous = None, kind
            continue

        need = _ARITY[kind]
        if not numbers or len(numbers) % need:
            return None

        for offset in range(0, len(numbers), need):
            chunk = numbers[offset : offset + need]
            step = kind
            if kind == "M":
                if offset == 0:
                    close_subpath()
                    current = []
                    x, y = (x + chunk[0], y + chunk[1]) if relative else tuple(chunk)
                    start_x, start_y = x, y
                    current.append((x, y))
                    previous = "M"
                    continue
                step = "L"  # pairs after a moveto are implicit linetos

            if step == "L":
                x, y = (x + chunk[0], y + chunk[1]) if relative else tuple(chunk)
                current.append((x, y))
            elif step == "H":
                x = x + chunk[0] if relative else chunk[0]
                current.append((x, y))
            elif step == "V":
                y = y + chunk[0] if relative else chunk[0]
                current.append((x, y))
            else:
                p0 = np.array([x, y], dtype=np.float64)
                if step == "C":
                    points = np.asarray(chunk, dtype=np.float64).reshape(3, 2)
                    if relative:
                        points = points + p0
                    p1, p2, p3 = points
                    control = p2
                elif step == "S":
                    points = np.asarray(chunk, dtype=np.float64).reshape(2, 2)
                    if relative:
                        points = points + p0
                    p1 = 2 * p0 - control if previous in ("C", "S") else p0
                    p2, p3 = points
                    control = p2
                elif step == "Q":
                    points = np.asarray(chunk, dtype=np.float64).reshape(2, 2)
                    if relative:
                        points = points + p0
                    knot, p3 = points
                    p1 = p0 + 2.0 / 3.0 * (knot - p0)
                    p2 = p3 + 2.0 / 3.0 * (knot - p3)
                    control = knot
                else:  # T
                    p3 = np.asarray(chunk, dtype=np.float64)
                    if relative:
                        p3 = p3 + p0
                    knot = 2 * p0 - control if previous in ("Q", "T") else p0
                    p1 = p0 + 2.0 / 3.0 * (knot - p0)
                    p2 = p3 + 2.0 / 3.0 * (knot - p3)
                    control = knot
                curve = _bezier(p0, p1, p2, p3, _steps_for((p0, p1, p2, p3)))
                current.extend((float(cx), float(cy)) for cx, cy in curve)
                x, y = float(p3[0]), float(p3[1])
            previous = step

    close_subpath()
    return subpaths or None


# --- rasterizing the vector back to pixels ------------------------------------


def _fill_polygon(shape: tuple[int, int], polygon: np.ndarray) -> np.ndarray:
    """Scanline coverage for one polygon, sampled at pixel centres.

    Written out rather than handed to a drawing library because every subpath
    has to be combined with exclusive-or to get the even-odd rule the traced
    paths are written for -- holes and counters are subpaths of the shape they
    sit in, and filling them solid would put a letter's counter in the wrong
    colour.
    """
    height, width = shape
    mask = np.zeros(shape, dtype=bool)
    x0, y0 = polygon[:, 0], polygon[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)

    top = int(max(0, math.floor(y0.min())))
    bottom = int(min(height - 1, math.ceil(y0.max())))
    if bottom < top:
        return mask

    for row in range(top, bottom + 1):
        centre = row + 0.5
        # Half-open in y so a vertex shared by two edges is counted once.
        crossing = ((y0 <= centre) & (y1 > centre)) | ((y1 <= centre) & (y0 > centre))
        if not crossing.any():
            continue
        ay, by = y0[crossing], y1[crossing]
        ax, bx = x0[crossing], x1[crossing]
        xs = ax + (centre - ay) * (bx - ax) / (by - ay)
        xs.sort()
        # Even-odd: the span between each pair of crossings is inside.
        starts = np.ceil(xs[0::2] - 0.5).astype(np.int64)
        ends = np.ceil(xs[1::2] - 0.5).astype(np.int64)
        for begin, finish in zip(starts, ends):
            begin = max(0, begin)
            finish = min(width, finish)
            if finish > begin:
                mask[row, begin:finish] ^= True
    return mask


def _index_map(shapes, height: int, width: int) -> np.ndarray:
    """Paint the shapes in document order, recording which one owns each pixel.

    -1 means no shape covers it. Later shapes overwrite earlier ones, which is
    what a renderer does, so the map matches what a viewer would show.

    Each shape is rasterized inside its own bounding box rather than across the
    whole canvas. Traced artwork is mostly small shapes on a large page -- a
    lettering file runs to a few hundred of them -- and a full-canvas buffer
    per shape spends nearly all of its time allocating and clearing pixels no
    shape was ever going to cover.
    """
    index_map = np.full((height, width), -1, dtype=np.int32)
    for index, subpaths in shapes:
        if subpaths is None:
            continue
        low = np.floor(np.min([polygon.min(axis=0) for polygon in subpaths], axis=0))
        high = np.ceil(np.max([polygon.max(axis=0) for polygon in subpaths], axis=0))
        x0, y0 = int(max(0, low[0])), int(max(0, low[1]))
        x1, y1 = int(min(width, high[0] + 1)), int(min(height, high[1] + 1))
        if x1 <= x0 or y1 <= y0:
            continue

        window = (y1 - y0, x1 - x0)
        covered = np.zeros(window, dtype=bool)
        for polygon in subpaths:
            covered ^= _fill_polygon(window, polygon - (x0, y0))
        index_map[y0:y1, x0:x1][covered] = index
    return index_map


# --- profiling a ramp ---------------------------------------------------------


def _profile(t: np.ndarray, rgb: np.ndarray, bins: int = _PROFILE_BINS):
    """Median colour in each slice along the gradient axis.

    A median rather than a mean because a region's pixels include whatever the
    mask could not erode away -- a sliver of the neighbouring colour, a speck
    of noise -- and one bright outlier drags a mean somewhere the artwork
    never goes. The result is smoothed by a hair afterwards, which is what
    keeps a fitted ramp from inheriting the bin-to-bin jitter of its samples.
    """
    labels = np.clip((t * bins).astype(np.int64), 0, bins - 1)
    counts = np.bincount(labels, minlength=bins)
    present = np.nonzero(counts > 0)[0]
    if present.size < 3:
        return None, None

    colours = np.stack(
        [
            np.asarray(
                ndimage.median(rgb[:, channel], labels=labels, index=present),
                dtype=np.float64,
            )
            for channel in range(3)
        ],
        axis=1,
    )
    if present.size >= 5:
        colours = ndimage.uniform_filter1d(colours, size=3, axis=0, mode="nearest")
    centres = (present + 0.5) / bins
    return centres, colours


def _stops(centres: np.ndarray, colours: np.ndarray, tolerance: float, limit: int):
    """Reduce a sampled ramp to the fewest stops that still describe it.

    Straight ramps come out as two stops. Anything that curves -- and shading
    usually does, because sRGB is not linear in light and falloff is not
    linear in distance -- gets a stop wherever the straight answer would be
    visibly wrong, and nowhere else. That is what stops a gradient from being
    quietly replaced by the chord across it.
    """
    lab = _lab(colours)
    chosen = [0, len(centres) - 1]
    while len(chosen) < limit:
        approximated = np.stack(
            [
                np.interp(centres, centres[chosen], colours[chosen, channel])
                for channel in range(3)
            ],
            axis=1,
        )
        error = _delta_e(_lab(approximated), lab)
        worst = int(error.argmax())
        if error[worst] <= tolerance or worst in chosen:
            break
        chosen = sorted(chosen + [worst])
    return centres[chosen], colours[chosen]


def _ramp_at(t: np.ndarray, offsets: np.ndarray, colours: np.ndarray) -> np.ndarray:
    """Evaluate a stop list the way a renderer would: clamped, and linear."""
    return np.stack(
        [np.interp(t, offsets, colours[:, channel]) for channel in range(3)], axis=1
    )


# --- the three colour models --------------------------------------------------


@dataclass(slots=True)
class _Fit:
    """A candidate answer for one region."""

    kind: str                   # "flat", "linear" or "radial"
    residual: float             # mean dE against the source pixels
    travel: float               # dE from one end of the ramp to the other
    offsets: np.ndarray | None = None
    colours: np.ndarray | None = None
    geometry: tuple | None = None   # (x1, y1, x2, y2) or (cx, cy, r)


def _flat_fit(rgb: np.ndarray, lab: np.ndarray) -> _Fit:
    middle = np.median(rgb, axis=0)
    residual = float(_delta_e(lab, _lab(middle)).mean())
    return _Fit(kind="flat", residual=residual, travel=0.0)


def _axis_from_jacobian(jacobian: np.ndarray) -> np.ndarray | None:
    """The compass direction along which the colour moves fastest.

    Adding the three channel gradients together, which is the obvious thing to
    do, cancels itself out on any ramp whose channels move in opposite
    directions -- a blue-to-orange fade has red climbing exactly as blue
    falls, and the sum points nowhere. The leading left singular vector of the
    2x3 Jacobian is the direction that actually maximizes colour change, and
    it does not care which way the individual channels went.
    """
    left, singular, _ = np.linalg.svd(jacobian, full_matrices=False)
    if not np.isfinite(singular).all() or singular[0] <= 1e-9:
        return None
    return left[:, 0]


def _profiled_fit(
    kind: str,
    t: np.ndarray,
    rgb: np.ndarray,
    lab: np.ndarray,
    params: GradientParams,
    geometry: tuple,
) -> _Fit | None:
    """Turn a parameterization of the region into stops, and score it."""
    centres, colours = _profile(t, rgb)
    if centres is None:
        return None
    # Stops are only worth what the pixels can pay for. A sliver of a hundred
    # pixels has no evidence for a twelve-stop curve, and fitting one to it
    # just writes down the noise; a large field has evidence for every stop
    # the tolerance asks for.
    affordable = min(params.max_stops, max(2, int(math.sqrt(len(t)) / 4)))
    offsets, stops = _stops(centres, colours, params.tolerance, affordable)
    residual = float(_delta_e(_lab(_ramp_at(t, offsets, stops)), lab).mean())
    travel = float(_delta_e(_lab(stops[0]), _lab(stops[-1])))
    return _Fit(
        kind=kind,
        residual=residual,
        travel=travel,
        offsets=offsets,
        colours=stops,
        geometry=geometry,
    )


def _linear_fit(
    xy: np.ndarray, rgb: np.ndarray, lab: np.ndarray, params: GradientParams
) -> _Fit | None:
    centre = xy.mean(axis=0)
    centred = xy - centre
    design = np.column_stack([np.ones(len(xy)), centred])
    coefficients, *_ = np.linalg.lstsq(design, rgb, rcond=None)
    axis = _axis_from_jacobian(coefficients[1:])
    if axis is None:
        return None

    reach = centred @ axis
    low, high = float(reach.min()), float(reach.max())
    if high - low < 1e-6:
        return None
    t = (reach - low) / (high - low)
    start = centre + axis * low
    end = centre + axis * high
    return _profiled_fit(
        "linear", t, rgb, lab, params, (start[0], start[1], end[0], end[1])
    )


def _radial_centre(xy: np.ndarray, rgb: np.ndarray) -> np.ndarray | None:
    """Where a circular ramp is centred, from the quadratic term of its fit.

    A colour that depends only on distance from a point is quadratic in
    position: c = k*(x-cx)^2 + k*(y-cy)^2 + ... Fitting that form gives the
    centre in closed form as -b / 2k per axis, which is a starting guess good
    enough that the refinement below only ever has to nudge it.
    """
    design = np.column_stack(
        [np.ones(len(xy)), xy, (xy**2).sum(axis=1)]
    )
    coefficients, *_ = np.linalg.lstsq(design, rgb, rcond=None)
    quadratic = coefficients[3]
    weights = np.abs(quadratic)
    if weights.sum() <= 1e-12:
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        centres = -coefficients[1:3] / (2.0 * quadratic)
    usable = np.isfinite(centres).all(axis=0)
    if not usable.any():
        return None
    weights = np.where(usable, weights, 0.0)
    if weights.sum() <= 1e-12:
        return None
    return (centres[:, usable] * weights[usable]).sum(axis=1) / weights[usable].sum()


def _radial_fit(
    xy: np.ndarray, rgb: np.ndarray, lab: np.ndarray, params: GradientParams
) -> _Fit | None:
    guess = _radial_centre(xy, rgb)
    if guess is None:
        return None

    span = float(np.hypot(*(xy.max(axis=0) - xy.min(axis=0))))
    if span <= 1e-6:
        return None
    # Keep the search near the artwork. A centre flung far outside the region
    # describes a ramp that is straight over it, which is the linear model's
    # job and it does it better.
    limit = 2.0 * span
    middle = xy.mean(axis=0)

    def score(candidate: np.ndarray) -> float:
        if np.hypot(*(candidate - middle)) > limit:
            return 1e6
        distance = np.hypot(xy[:, 0] - candidate[0], xy[:, 1] - candidate[1])
        reach = float(distance.max())
        if reach <= 1e-6:
            return 1e6
        centres, colours = _profile(distance / reach, rgb)
        if centres is None:
            return 1e6
        return float(
            _delta_e(_lab(_ramp_at(distance / reach, centres, colours)), lab).mean()
        )

    result = optimize.minimize(
        score,
        np.clip(guess, middle - limit, middle + limit),
        method="Nelder-Mead",
        options={"maxiter": 60, "xatol": 0.75, "fatol": 0.02, "disp": False},
    )
    centre = result.x if result.success or np.isfinite(result.fun) else guess

    distance = np.hypot(xy[:, 0] - centre[0], xy[:, 1] - centre[1])
    radius = float(distance.max())
    if radius <= 1e-6:
        return None
    return _profiled_fit(
        "radial", distance / radius, rgb, lab, params, (centre[0], centre[1], radius)
    )


def _best_fit(xy: np.ndarray, rgb: np.ndarray, params: GradientParams):
    """Flat, linear or radial -- whichever the pixels actually support."""
    lab = _lab(rgb)
    flat = _flat_fit(rgb, lab)
    # A flat fill that already sits inside the threshold of visibility is not
    # a problem to be solved. Fitting a ramp to it can only be fitting the
    # noise, and it costs a definition, an object an editor has to carry, and
    # the time to find it -- which on flat artwork is nearly every shape.
    if flat.residual <= _FLAT_ENOUGH:
        return flat, flat
    candidates = [flat]

    linear = _linear_fit(xy, rgb, lab, params)
    if linear is not None:
        candidates.append(linear)
    # Searching for a radial centre is the expensive part of the whole stage,
    # and there is nothing for it to find when a straight ramp already tracks
    # the pixels to inside the tolerance -- which is the ordinary case, and on
    # lettering it is nearly every shape.
    if params.radial and (linear is None or linear.residual > params.tolerance):
        radial = _radial_fit(xy, rgb, lab, params)
        # Occam: a radial gradient has three degrees of freedom the axial one
        # does not, so it has to earn them rather than merely tie.
        if radial is not None and (
            linear is None or radial.residual < linear.residual * _RADIAL_MARGIN
        ):
            candidates.append(radial)

    ramps = [c for c in candidates if c.kind != "flat"]
    if not ramps:
        return flat, flat
    best = min(ramps, key=lambda c: c.residual)

    # A gradient has to be both visible and an improvement. Without the first
    # test every flat fill picks up a ramp across the noise in it; without the
    # second, a region that simply is not a ramp gets one anyway.
    if best.travel < params.min_travel:
        return flat, flat
    if best.residual > flat.residual * _MIN_IMPROVEMENT:
        return flat, flat
    # ...and the improvement has to be one somebody could see. A fill a whisker
    # outside the visible threshold, made a whisker better, is not worth a
    # definition and an extra object in the editor.
    if flat.residual - best.residual < _FLAT_ENOUGH:
        return flat, flat
    # Past the tolerance the shape is normally called textured rather than
    # shaded and keeps its flat fill. The exception is a ramp that still
    # explains the shape far better than any single colour can: an artwork
    # whose shading no one gradient can follow exactly is still shaded, and
    # answering it with a flat slab is the larger error of the two.
    if best.residual > params.max_residual and best.residual > flat.residual * _RESCUE:
        return flat, flat
    return best, flat


# A radial fit has to beat the axial one by this much to be preferred.
_RADIAL_MARGIN = 0.85

# How closely a flat fill has to match the artwork before it is left alone.
# Around 1 is the threshold of visibility for a large area, so a fill inside
# it has nothing wrong with it that a gradient could put right.
_FLAT_ENOUGH = 1.0

# A ramp has to explain the pixels this much better than a flat fill does.
_MIN_IMPROVEMENT = 0.8

# ...and this much better to be kept even when it misses the tolerance.
_RESCUE = 0.7


# --- regions ------------------------------------------------------------------


@dataclass(slots=True)
class _Region:
    """One traced shape, and the source pixels it turned out to cover."""

    index: int
    element: etree._Element
    fill: str | None
    area: int
    xy: np.ndarray
    rgb: np.ndarray
    matrix: tuple = IDENTITY


def _analysis_scale(width: int, height: int) -> float:
    pixels = width * height
    if pixels <= _ANALYSIS_MAX_PIXELS:
        return 1.0
    return math.sqrt(_ANALYSIS_MAX_PIXELS / pixels)


def _source_array(source: Image.Image, width: int, height: int):
    """The bitmap in the vector's own coordinate space, plus its opacity."""
    image = source
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    alpha = None
    if image.mode in ("RGBA", "LA", "PA"):
        alpha = np.asarray(image.convert("RGBA").getchannel("A"))
    if image.mode != "RGB":
        image = image.convert("RGB")
    return np.asarray(image, dtype=np.uint8), alpha


def _subsample(count: int, limit: int = _MAX_SAMPLES):
    if count <= limit:
        return None
    return np.random.default_rng(_SEED).choice(count, size=limit, replace=False)


def _own_family(rgb: np.ndarray, allowed: np.ndarray, inks: np.ndarray) -> np.ndarray:
    """Which of a shape's pixels are its own colour rather than a blend.

    Only answerable when the artwork was mapped onto a palette, and only
    worth asking then -- but there it is worth a great deal. A traced outline
    is a few pixels wide with a different ink on each side of it, so a large
    share of what it covers is the ramp between them, and eroding the mask
    cannot remove all of it. Fitted as it stands, a black outline beside a
    white halo comes back as a ramp running from black to nearly white: a
    perfectly good description of that boundary, and nothing whatever to do
    with the shape, which is one flat black.

    A palette says what every pixel was meant to be, so the pixels that
    resolve to something else can be set aside. What counts as "something
    else" has to be a *family* of inks rather than one ink, though, because
    the case this exists to serve is a ramp the quantizer had to cut into two
    inks of the same colour: insisting on the shape's exact ink would throw
    away the half of its own ramp that fell on the other side of the cut, and
    with it the evidence that there was a ramp at all.
    """
    distances = ((rgb[:, None, :] - inks[None, :, :]) ** 2).sum(axis=2)
    return allowed[distances.argmin(axis=1)]


def _collect(
    paths: list,
    source: Image.Image,
    width: int,
    height: int,
    params: GradientParams,
    inks: np.ndarray | None = None,
):
    """Rasterize the vector and hand back the source pixels under each shape."""
    scale = _analysis_scale(width, height)
    raster_w = max(1, int(round(width * scale)))
    raster_h = max(1, int(round(height * scale)))

    shapes = []
    matrices: dict = {}
    for index, path in enumerate(paths):
        fill = path.get("fill")
        matrix = affine(path.get("transform"))
        if not fill or fill == "none" or matrix is None:
            shapes.append((index, None))
            continue
        matrices[index] = matrix
        subpaths = flatten(path.get("d") or "")
        if subpaths is not None:
            # Into the coordinates of the analysis raster: the element's own
            # transform first, then the reduction the raster was built at.
            placed = _multiply((scale, 0.0, 0.0, scale, 0.0, 0.0), matrix)
            subpaths = [_apply(placed, polygon) for polygon in subpaths]
        shapes.append((index, subpaths))

    index_map = _index_map(shapes, raster_h, raster_w)
    pixels, alpha = _source_array(source, raster_w, raster_h)

    # find_objects wants labels from 1 up, and returns every bounding box in
    # one pass -- far cheaper than scanning the whole canvas once per shape.
    boxes = ndimage.find_objects(index_map + 1)
    floor = max(params.min_area_px * scale * scale, 64.0)

    regions: list[_Region] = []
    for index, path in enumerate(paths):
        box = boxes[index] if index < len(boxes) else None
        if box is None:
            continue
        window = index_map[box] == index
        if window.sum() < floor:
            continue
        inner = ndimage.binary_erosion(window, iterations=_ERODE_PX)
        if not inner.any():
            # Nothing survives the erosion, so the shape is thinner than the
            # boundary blend on either side of it and every pixel it covers is
            # a mixture of its neighbours rather than its own colour. Fitting
            # a ramp to that describes the blend, not the artwork: it is what
            # put a grey wedge across the top of a black outline, ramping from
            # the ink to the halo beside it. There is no evidence here either
            # way, so the shape keeps the flat fill it came with.
            continue
        rows, cols = np.nonzero(inner)
        rows = rows + box[0].start
        cols = cols + box[1].start
        if alpha is not None:
            opaque = alpha[rows, cols] >= 250
            rows, cols = rows[opaque], cols[opaque]
        if rows.size < floor:
            continue

        keep = _subsample(rows.size)
        if keep is not None:
            rows, cols = rows[keep], cols[keep]

        colours = pixels[rows, cols].astype(np.float64)
        if inks is not None:
            own = _rgb(path.get("fill"))
            if own is not None:
                nearest = int(
                    ((np.asarray(own, dtype=np.float64) - inks) ** 2).sum(axis=1).argmin()
                )
                allowed = (
                    _delta_e(_lab(inks), _lab(inks[nearest])) <= params.patch_distance
                )
                mine = _own_family(colours, allowed, inks)
                # Below a quorum the shape is mostly boundary and what is left
                # is too little to fit anything to; it keeps its flat fill.
                if mine.sum() < max(floor, colours.shape[0] * _OWN_INK_QUORUM):
                    continue
                colours = colours[mine]
                rows, cols = rows[mine], cols[mine]

        regions.append(
            _Region(
                index=index,
                element=path,
                fill=path.get("fill"),
                area=int(window.sum()),
                # Pixel centres, in the coordinates of the analysis raster.
                xy=np.column_stack([cols + 0.5, rows + 0.5]).astype(np.float64),
                rgb=colours,
                matrix=matrices.get(index, IDENTITY),
            )
        )
    return regions, index_map, scale


def _adjacency(index_map: np.ndarray) -> dict:
    """Which shapes touch which: how long the shared edge is, and where.

    The midpoint matters as much as the length. Whether two neighbours are
    slices of one ramp is decided by whether their colours agree *at the edge
    between them*, and that question needs a point to ask it at.
    """
    height, width = index_map.shape
    rows, cols = np.mgrid[0:height, 0:width]
    borders: dict = {}
    for a, b, x, y in (
        (index_map[:, :-1], index_map[:, 1:], cols[:, :-1] + 1.0, rows[:, :-1] + 0.5),
        (index_map[:-1, :], index_map[1:, :], cols[:-1, :] + 0.5, rows[:-1, :] + 1.0),
    ):
        differing = (a != b) & (a >= 0) & (b >= 0)
        if not differing.any():
            continue
        low = np.minimum(a[differing], b[differing])
        high = np.maximum(a[differing], b[differing])
        px, py = x[differing], y[differing]
        pairs, inverse, counts = np.unique(
            np.stack([low, high], axis=1), axis=0, return_inverse=True, return_counts=True
        )
        inverse = inverse.ravel()
        sum_x = np.bincount(inverse, weights=px, minlength=len(pairs))
        sum_y = np.bincount(inverse, weights=py, minlength=len(pairs))
        for position, pair in enumerate(pairs):
            key = (int(pair[0]), int(pair[1]))
            count, cx, cy = borders.get(key, (0, 0.0, 0.0))
            borders[key] = (
                count + int(counts[position]),
                cx + float(sum_x[position]),
                cy + float(sum_y[position]),
            )
    return {
        key: (count, np.array([cx / count, cy / count]))
        for key, (count, cx, cy) in borders.items()
    }


# A shared edge shorter than this is a corner touch, not a seam worth healing.
_MIN_SHARED_BORDER = 8

# No more shapes than this are fused onto one ramp. A run of bands across one
# gradient is a handful of shapes; a chain longer than this has wandered.
_MAX_PATCH_GROUP = 24

# How much worse one shared ramp may fit than the shapes fitted separately.
# Some slack is right -- the shared ramp buys a seam that cannot show, and the
# separate fits are each free to drift toward their own edge -- but only some,
# or two colours that merely neighbour each other end up averaged together.
_MERGE_SLACK = 1.3
_MERGE_FLOOR = 0.4


def _pooled(members: list, by_index: dict):
    """The members' pixels, drawn in proportion to the area each covers.

    Every region is capped at the same number of samples, so concatenating
    them lets a sliver weigh as much as the field it sits in and pulls the
    shared fit toward the sliver. Sampling by area is what makes the merged
    ramp describe what the group actually looks like.
    """
    areas = np.array([max(1, by_index[i].area) for i in members], dtype=np.float64)
    quota = np.maximum((areas / areas.sum() * _MAX_SAMPLES).astype(np.int64), 64)
    rng = np.random.default_rng(_SEED)
    xy, rgb = [], []
    for index, want in zip(members, quota):
        region = by_index[index]
        count = len(region.xy)
        if count > want:
            keep = rng.choice(count, size=int(want), replace=False)
            xy.append(region.xy[keep])
            rgb.append(region.rgb[keep])
        else:
            xy.append(region.xy)
            rgb.append(region.rgb)
    return np.concatenate(xy), np.concatenate(rgb)


def _colour_at(fit: _Fit, region: _Region, point: np.ndarray) -> np.ndarray:
    """What a shape's fitted answer paints at one point inside the document.

    Used to ask the only question that separates two slices of one ramp from
    two colours that merely touch: do they agree along the edge they share?
    Comparing whole-shape averages instead gets both answers wrong. Two wide
    bands of one gradient average to colours far apart even though they meet
    seamlessly, and two narrow flat regions of genuinely different inks can
    average close together.
    """
    if fit.kind == "flat" or fit.offsets is None:
        return np.median(region.rgb, axis=0)
    if fit.kind == "radial":
        cx, cy, radius = fit.geometry
        t = np.hypot(point[0] - cx, point[1] - cy) / max(radius, 1e-9)
    else:
        x1, y1, x2, y2 = fit.geometry
        axis = np.array([x2 - x1, y2 - y1])
        length = float(axis @ axis)
        t = 0.0 if length <= 1e-12 else float((point - [x1, y1]) @ axis) / length
    return _ramp_at(np.array([min(1.0, max(0.0, t))]), fit.offsets, fit.colours)[0]


def _patch_groups(
    regions: list, fits: dict, borders: dict, params: GradientParams
) -> list:
    """Runs of neighbouring shapes that may be slices of one ramp.

    This is the patch case exactly. A ramp the tracer had to cut into bands
    arrives as several fills, each of which really is almost flat on its own,
    so each is honestly fitted as flat and the steps between them stay
    visible. Taken together the same pixels are plainly one ramp, and one
    gradient across all of them has no steps in it anywhere.

    Shapes that did each get a gradient of their own are candidates too, and
    for the same reason. Two fragments of one shaded area fitted separately
    agree closely in the middle and drift at the edges, which is a seam; one
    ramp across both cannot drift. It also leaves them sharing a fill, so the
    stage that fuses touching shapes of one colour can still fuse them, and
    the artwork does not arrive in an editor as more objects than it needs.

    Only proposals are made here. Whether a group really is one ramp is
    settled by fitting it, back in refine_tree.
    """
    if len(regions) < 2:
        return []
    flat = {region.index: region for region in regions}
    parent = {index: index for index in flat}

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    inks = {
        index: _lab(_rgb(region.fill) or np.median(region.rgb, axis=0))
        for index, region in flat.items()
    }

    candidates = []
    for pair, (length, midpoint) in borders.items():
        one, two = pair
        if one not in flat or two not in flat or length < _MIN_SHARED_BORDER:
            continue
        # Two tests, and both have to pass. Continuity at the shared edge says
        # the pair could be one ramp; agreeing on which ink they are says they
        # are the same thing being shaded.
        #
        # Continuity alone is not enough, because groups grow by chaining and
        # a chain is continuous all the way across a boundary it should never
        # cross: a pink fill meets the sliver of pink-white blend beside it,
        # which meets a paler sliver, which meets the white halo, and every
        # link in that chain is continuous. On the reported lettering it grew
        # groups spanning pink, white and black, and painted the halo with the
        # letter's ramp. Neighbours have to be the same ink as well.
        if float(_delta_e(inks[one], inks[two])) > params.patch_distance:
            continue
        distance = float(
            _delta_e(
                _lab(_colour_at(fits[one], flat[one], midpoint)),
                _lab(_colour_at(fits[two], flat[two], midpoint)),
            )
        )
        if distance <= params.patch_distance:
            candidates.append((distance, one, two))

    sizes = {index: 1 for index in flat}
    for _, one, two in sorted(candidates):
        root_a, root_b = find(one), find(two)
        if root_a == root_b:
            continue
        if sizes[root_a] + sizes[root_b] > _MAX_PATCH_GROUP:
            continue
        parent[root_b] = root_a
        sizes[root_a] += sizes[root_b]

    grouped: dict = {}
    for index in flat:
        grouped.setdefault(find(index), []).append(index)
    return [sorted(members) for members in grouped.values() if len(members) > 1]


# --- writing the document -----------------------------------------------------


def _defs(root: etree._Element) -> etree._Element:
    for child in root:
        if child.tag == _q("defs"):
            return child
    defs = etree.Element(_q("defs"))
    root.insert(0, defs)
    return defs


def _retire_gradients(root: etree._Element, paths: list) -> None:
    """Rename the gradients already in a document, and repoint what uses them.

    The stage is idempotent by design: run it on its own output and it re-fits
    from the source rather than compounding. That only works if the names it
    is about to write are free, because a fresh `shade0` alongside the old one
    is two definitions with the same id, and a renderer picks whichever it
    saw first. Retiring the old names keeps the new ones predictable, and
    whatever nothing points at afterwards is dropped at the end.
    """
    renamed: dict = {}
    for defs in root.findall(_q("defs")):
        for node in defs:
            if node.tag not in (_q("linearGradient"), _q("radialGradient")):
                continue
            previous = node.get("id")
            if previous:
                renamed[previous] = f"prior{len(renamed)}"
                node.set("id", renamed[previous])
    if not renamed:
        return
    for path in paths:
        for attribute in ("fill", "stroke"):
            value = path.get(attribute) or ""
            if value.startswith("url(#"):
                name = renamed.get(value[5:].rstrip(")"))
                if name:
                    path.set(attribute, f"url(#{name})")


def _drop_stale_gradients(root: etree._Element, paths: list) -> None:
    """Remove gradient definitions nothing points at any more.

    The stage is idempotent by design -- run it on its own output and it
    re-fits from the source rather than compounding -- so a document that
    arrives with gradients in it has them replaced, and what they leave behind
    has to go with them or the file grows on every pass.
    """
    referenced = set()
    for path in paths:
        for attribute in ("fill", "stroke"):
            value = path.get(attribute) or ""
            if value.startswith("url(#"):
                referenced.add(value[5:].rstrip(")"))
    for defs in root.findall(_q("defs")):
        for node in list(defs):
            if node.tag in (_q("linearGradient"), _q("radialGradient")):
                if node.get("id") not in referenced:
                    defs.remove(node)
        if len(defs) == 0:
            root.remove(defs)


def _write_gradient(
    defs: etree._Element, name: str, fit: _Fit, scale: float, matrix
) -> None:
    """Emit one gradient in the user space of the shapes that will use it.

    ``gradientUnits="userSpaceOnUse"`` means the coordinates are read in the
    element's own space, so an element carrying a transform needs the geometry
    expressed inside that transform. A translation -- which is all the tracer
    ever emits -- is just a shift of the numbers, and is done that way in
    preference to a gradientTransform because a plain gradient is what every
    downstream renderer handles best. Anything more general is handed to
    gradientTransform, where an inverse of the element's own matrix leaves the
    coordinates meaning what they say.
    """
    reduction = 1.0 / scale if scale else 1.0
    inverse = _invert(matrix) or IDENTITY
    shift_x, shift_y = (
        (-matrix[4], -matrix[5]) if _is_translation(matrix) else (0.0, 0.0)
    )

    if fit.kind == "radial":
        cx, cy, radius = (value * reduction for value in fit.geometry)
        node = etree.SubElement(defs, _q("radialGradient"))
        node.set("id", name)
        node.set("gradientUnits", "userSpaceOnUse")
        node.set("cx", _fmt(cx + shift_x))
        node.set("cy", _fmt(cy + shift_y))
        node.set("r", _fmt(radius))
    else:
        x1, y1, x2, y2 = (value * reduction for value in fit.geometry)
        node = etree.SubElement(defs, _q("linearGradient"))
        node.set("id", name)
        node.set("gradientUnits", "userSpaceOnUse")
        node.set("x1", _fmt(x1 + shift_x))
        node.set("y1", _fmt(y1 + shift_y))
        node.set("x2", _fmt(x2 + shift_x))
        node.set("y2", _fmt(y2 + shift_y))

    if not _is_translation(matrix):
        node.set("gradientTransform", "matrix(%s)" % ",".join(_fmt(v) for v in inverse))

    for offset, colour in zip(fit.offsets, fit.colours):
        stop = etree.SubElement(node, _q("stop"))
        stop.set("offset", _fmt(float(offset)))
        stop.set("stop-color", _hex(colour))


def _flatten_transforms(members: list, by_index: dict):
    """Rewrite a group's shapes into the document's own coordinates.

    Returns the new path data per member, or None if any of them cannot be
    moved -- in which case nothing is written and the caller falls back to one
    copy of the ramp per coordinate system. Nothing is applied until every
    member has come through, so a group is never left half-moved.
    """
    baked: dict = {}
    for index in members:
        region = by_index[index]
        if region.matrix == IDENTITY:
            baked[index] = None
            continue
        if not _is_translation(region.matrix):
            return None
        data = bake_translation(
            region.element.get("d") or "", region.matrix[4], region.matrix[5]
        )
        if data is None:
            return None
        baked[index] = data
    return baked


def _paint(element: etree._Element, reference: str) -> None:
    """Point a shape at a gradient, taking its seam stroke along with it.

    The gap filler seals every shared edge with a hairline in the colour of
    the fill. Leaving that stroke flat while the fill becomes a ramp would
    draw a line of the old averaged colour right around the shape, which is
    the seam made visible rather than hidden. Painting the stroke with the
    same user-space gradient keeps it agreeing with the fill at every point.
    """
    previous = element.get("fill")
    element.set("fill", reference)
    stroke = element.get("stroke")
    if stroke and stroke != "none" and stroke == previous:
        element.set("stroke", reference)


# --- the stage ----------------------------------------------------------------


def shapes_of(root: etree._Element) -> list:
    """Every filled path in the document, in the order a renderer paints them."""
    return [
        node
        for node in root.iter(_q("path"))
        if (node.get("fill") or "none") != "none"
    ]


def viewbox_size(root: etree._Element):
    """The coordinate space the path data is written in."""
    box = (root.get("viewBox") or "").replace(",", " ").split()
    if len(box) == 4:
        try:
            width, height = float(box[2]), float(box[3])
            if width > 0 and height > 0:
                return int(round(width)), int(round(height))
        except ValueError:
            pass
    return None


def refine_tree(
    root: etree._Element,
    paths: list,
    source: Image.Image,
    width: int,
    height: int,
    params: GradientParams | None = None,
    palette: list | None = None,
) -> GradientReport:
    """Fit gradients to *paths* from the pixels of *source*, in place.

    *width* and *height* are the document's own coordinate space, which the
    source is resampled into so that a mask taken from the vector indexes the
    bitmap directly. *palette* is the list of inks the artwork was mapped
    onto, when it was mapped onto any: it says what each pixel was meant to
    be, which is how a shape's own colour is told from the blend along its
    edge. See :func:`_own_ink`.
    """
    params = params or GradientParams()
    report = GradientReport()
    if not params.enabled or not paths or width <= 0 or height <= 0:
        return report

    started = time.perf_counter()
    inks = None
    if palette:
        entries = [_rgb(colour) for colour in palette]
        entries = [entry for entry in entries if entry is not None]
        if entries:
            inks = np.asarray(entries, dtype=np.float64)
    regions, index_map, scale = _collect(paths, source, width, height, params, inks)
    if not regions:
        report.ms = (time.perf_counter() - started) * 1000
        return report
    _retire_gradients(root, paths)

    fits: dict = {}
    flats: dict = {}
    for region in regions:
        fit, flat = _best_fit(region.xy, region.rgb, params)
        fits[region.index] = fit
        flats[region.index] = flat
    report.regions = len(regions)

    # Every shape gets a second hearing alongside its neighbours, which is
    # where a ramp the tracer had to slice into bands shows itself.
    groups = [[region.index] for region in regions]
    by_index = {region.index: region for region in regions}
    if params.merge_patches:
        for members in _patch_groups(regions, fits, _adjacency(index_map), params):
            xy, rgb = _pooled(members, by_index)
            fit, _ = _best_fit(xy, rgb, params)
            if fit.kind == "flat":
                continue
            # One ramp for the group has to describe it about as well as the
            # separate answers did. Where it does, it is the better answer:
            # the same colours, and no step where two shapes meet.
            weights = np.array([by_index[i].area for i in members], dtype=np.float64)
            apart = float(
                (weights * [fits[i].residual for i in members]).sum() / weights.sum()
            )
            if fit.residual > max(apart * _MERGE_SLACK, apart + _MERGE_FLOOR):
                continue
            for index in members:
                fits[index] = fit
            groups = [g for g in groups if g[0] not in members]
            groups.append(members)
            report.merged += len(members)

    weight = 0.0
    for region in regions:
        weight += region.area
        report.residual_before += flats[region.index].residual * region.area
        report.residual_after += fits[region.index].residual * region.area
    if weight:
        report.residual_before /= weight
        report.residual_after /= weight

    # A shape that arrived carrying a gradient and comes out flat has to be
    # given a colour, or it would be left pointing at a definition that is
    # about to be dropped. Fixing a gradient sometimes means removing one.
    for region in regions:
        if fits[region.index].kind == "flat" and (region.fill or "").startswith("url(#"):
            _paint(region.element, _hex(np.median(region.rgb, axis=0)))

    defs = None
    for members in sorted(groups, key=lambda g: g[0]):
        fit = fits[members[0]]
        if fit.kind == "flat":
            continue
        if defs is None:
            defs = _defs(root)

        # Put the whole group into one coordinate system if it can be done, so
        # that one definition serves all of it. Where it cannot -- a transform
        # this cannot bake, or path data written in relative coordinates --
        # each transform gets the same ramp written in its own space, which
        # costs a few hundred bytes of repeated stops and is cheaper than the
        # seam that leaving them separately fitted would show.
        baked = _flatten_transforms(members, by_index)
        if baked is not None:
            for index, data in baked.items():
                element = by_index[index].element
                if data is not None:
                    element.set("d", data)
                    element.attrib.pop("transform", None)
            spaces = [IDENTITY]
        else:
            spaces = list(dict.fromkeys(by_index[index].matrix for index in members))

        for matrix in spaces:
            name = f"shade{report.gradients}"  # ids retired above, so this is free
            _write_gradient(defs, name, fit, scale, matrix)
            for index in members:
                if baked is not None or by_index[index].matrix == matrix:
                    _paint(by_index[index].element, f"url(#{name})")
            report.gradients += 1
            report.stops += len(fit.offsets)
            if fit.kind == "radial":
                report.radial += 1
            else:
                report.linear += 1

    _drop_stale_gradients(root, paths)
    report.ms = (time.perf_counter() - started) * 1000
    logger.info(
        "gradients: %s of %s regions shaded (%s linear, %s radial, %s merged), "
        "dE %.2f -> %.2f in %.0f ms",
        report.gradients,
        report.regions,
        report.linear,
        report.radial,
        report.merged,
        report.residual_before,
        report.residual_after,
        report.ms,
    )
    return report


def refine(
    svg: bytes, source: Image.Image, params: GradientParams | None = None
):
    """Refine a finished SVG against the bitmap it was traced from.

    This is the whole of the stage as an API: bytes and a raster in, better
    bytes and a report out. ``/api/v1/vectorize`` runs it over the document it
    has just built; the standalone endpoint runs it over one it is handed.
    """
    from app.core.errors import BadParameter

    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False, huge_tree=False)
    try:
        root = etree.fromstring(svg, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise BadParameter(f"The SVG could not be parsed: {exc}") from None
    if root.tag != _q("svg"):
        raise BadParameter("The supplied document is not an SVG.")

    size = viewbox_size(root)
    if size is None:
        size = source.size
    report = refine_tree(root, shapes_of(root), source, size[0], size[1], params)

    body = etree.tostring(root, xml_declaration=False, encoding="UTF-8", pretty_print=True)
    prologue = b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
    return prologue + body, report
