"""Raster preparation.

Everything that happens to the bitmap *before* the tracer sees it. Getting
this stage right matters more for output quality than any tracer knob:
clean, quantized, correctly-oriented input produces far fewer stray paths.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import NamedTuple

from PIL import Image, ImageChops, ImageFilter, ImageOps, UnidentifiedImageError

from app.core.errors import BadImageData, ImageTooLarge
from app.core.logging import get_logger
from app.schemas.params import VectorizeParams, normalise_hex

logger = get_logger("preprocess")

# Pillow refuses suspiciously large images by default; we enforce our own
# explicit limit instead and want a clear error rather than Pillow's warning.
Image.MAX_IMAGE_PIXELS = None

SUPPORTED_INPUT_FORMATS = frozenset(
    {"PNG", "JPEG", "WEBP", "BMP", "GIF", "TIFF", "PPM", "TGA", "ICO"}
)


@dataclass(slots=True)
class PreparedImage:
    """A bitmap ready for the tracer, plus provenance for the response."""

    image: Image.Image
    source_format: str
    source_width: int
    source_height: int
    traced_width: int
    traced_height: int
    downscaled: bool
    color_count: int | None
    palette: list[str] | None
    has_transparency: bool
    supersample: int = 1
    finer: "PreparedImage | None" = None


def decode(data: bytes) -> tuple[Image.Image, str]:
    """Decode raw bytes into a Pillow image, honouring EXIF orientation."""
    if not data:
        raise BadImageData("The supplied image is empty.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            source_format = (probe.format or "UNKNOWN").upper()
            probe.load()
            image = probe.copy()
    except UnidentifiedImageError:
        raise BadImageData(
            "Unrecognised image format. Supported inputs: "
            + ", ".join(sorted(SUPPORTED_INPUT_FORMATS))
        ) from None
    except (OSError, ValueError) as exc:
        raise BadImageData(f"The image could not be decoded: {exc}") from exc

    if source_format not in SUPPORTED_INPUT_FORMATS:
        raise BadImageData(f"Unsupported input format: {source_format}.")

    # Rotate to the orientation the photographer actually saw.
    image = ImageOps.exif_transpose(image) or image
    return image, source_format


def _to_rgba(image: Image.Image) -> Image.Image:
    if image.mode == "RGBA":
        return image
    if image.mode in ("LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        return image.convert("RGBA")
    if image.mode == "RGB":
        return image
    return image.convert("RGBA" if "A" in image.getbands() else "RGB")


def _flatten(image: Image.Image, background: str) -> Image.Image:
    """Composite an alpha image onto a solid colour."""
    if image.mode != "RGBA":
        return image
    hexed = normalise_hex(background)
    rgb = tuple(int(hexed[i : i + 2], 16) for i in (1, 3, 5))
    canvas = Image.new("RGB", image.size, rgb)
    canvas.paste(image, mask=image.getchannel("A"))
    return canvas


def _fit_within(image: Image.Image, max_pixels: int) -> tuple[Image.Image, bool]:
    """Downscale proportionally so width*height <= *max_pixels*."""
    width, height = image.size
    pixels = width * height
    if pixels <= max_pixels:
        return image, False
    scale = (max_pixels / pixels) ** 0.5
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    logger.info("downscaling %sx%s -> %sx%s", width, height, *new_size)
    return image.resize(new_size, Image.Resampling.LANCZOS), True


# Median filter window per denoise level. A median is edge-preserving: it
# replaces each pixel with the median of its neighbourhood, so it erases
# isolated noise pixels without softening a real boundary the way a blur does.
_DENOISE_WINDOW = {"none": 0, "low": 3, "medium": 5, "high": 7}


def _denoise(image: Image.Image, level: str) -> Image.Image:
    """Strip compression noise that would otherwise trace as ragged slivers."""
    window = _DENOISE_WINDOW[level]
    if window <= 0:
        return image
    # Too small an image and the window eats real detail rather than noise.
    if min(image.size) < window * 8:
        return image

    if image.mode == "RGBA":
        # Filter the colour channels only; running a median over alpha nibbles
        # at the edges of cut-outs.
        alpha = image.getchannel("A")
        rgb = image.convert("RGB").filter(ImageFilter.MedianFilter(window))
        result = rgb.convert("RGBA")
        result.putalpha(alpha)
        return result
    return image.filter(ImageFilter.MedianFilter(window))


def _palette_rgbs(colors: list[str]) -> list[tuple[int, int, int]]:
    return [
        tuple(int(normalise_hex(c)[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[misc]
        for c in colors
    ]


def _flat_palette(entries: list[tuple[int, int, int]]) -> list[int]:
    """Pillow wants a full 768-byte palette; pad by repeating the last entry."""
    flat = [channel for entry in entries for channel in entry]
    last = flat[-3:] if flat else [0, 0, 0]
    while len(flat) < 768:
        flat.extend(last)
    return flat[:768]


# How finely each pair-segment is sampled, when there is room for it.
_RAMP_SAMPLES = 9


# Two candidate colours closer than this are treated as the same ink. It has
# to clear the anti-aliasing shades sitting beside each ink without merging
# inks that are genuinely close: the test artwork pairs a #ffffff outline with
# a #fbf7da fill only 38 apart, and at 40 the outline was swallowed and came
# back cream. 30 keeps them separate.
_MIN_INK_SEPARATION = 30.0

# How close to the line between two accepted inks a colour has to sit before
# it is read as a blend of them rather than an ink of its own.
_BLEND_TOLERANCE = 12.0

# Candidates holding less of the image than this are compression debris.
_INK_MIN_SHARE = 0.0005

# ...and holding less than this *after a mode filter* they are a thread rather
# than a region. Measured on the test artwork: the six real inks came in
# between 80.7% and 0.27% solid, every transition tone below 0.03%.
_INK_SOLID_FLOOR = 0.001

# Candidates are gathered from a reduced copy: this stage identifies colours,
# it does not resolve detail. Nearest-neighbour, because any smooth resampling
# would invent blends -- the exact thing being measured.
_DETECT_MAX_EDGE = 768

# Above this many inks the artwork is not flat, and is left alone.
_FLAT_MAX_INKS = 16

# How far the average pixel may sit from the nearest ink. This is what tells a
# handful of inks apart from a handful of *bands cut through a gradient*,
# which counting cannot: both come back as a short list. On flat artwork
# almost every pixel lands on an ink -- measured at 0.00 for a PNG logo, 1.59
# for a lettering JPEG, 2.93 for hard-edged shapes with heavy JPEG ringing.
# Shading spreads pixels between the bands instead: 9.58 for a shaded sticker,
# 26.50 for a shaded sphere.
_FLAT_MAX_RESIDUAL = 5.0


def _reduced(rgb: Image.Image) -> Image.Image:
    if max(rgb.size) <= _DETECT_MAX_EDGE:
        return rgb
    scale = _DETECT_MAX_EDGE / max(rgb.size)
    return rgb.resize(
        (max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))),
        Image.Resampling.NEAREST,
    )


def _generous(rgb: Image.Image) -> tuple[Image.Image, list[int]]:
    quantized = _reduced(rgb).quantize(
        colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
    )
    return quantized, list(quantized.getpalette() or [])


def _ink_candidates(
    rgb: Image.Image,
) -> tuple[list[tuple[int, int, int]], list[int], int, Image.Image]:
    """Distinct colours in the artwork, most-used first, with their weights.

    Quantizing generously and then folding near-duplicates together is what
    turns thousands of compression shades back into a short list. Each
    candidate absorbs the shades that belong to it, so its weight is the share
    of the image it really accounts for.
    """
    generous, table = _generous(rgb)

    colours: list[tuple[int, int, int]] = []
    weights: list[int] = []
    assign: dict[int, int] = {}
    total = 0
    for count, index in sorted(
        generous.getcolors(1 << 20) or [], key=lambda item: -item[0]
    ):
        entry = tuple(table[index * 3 : index * 3 + 3])
        if len(entry) < 3:
            continue
        total += count
        merged = False
        for position, colour in enumerate(colours):
            distance = sum((a - b) ** 2 for a, b in zip(entry, colour)) ** 0.5
            if distance < _MIN_INK_SEPARATION:
                weights[position] += count
                assign[index] = position
                merged = True
                break
        if not merged:
            assign[index] = len(colours)
            colours.append(entry)  # type: ignore[arg-type]
            weights.append(count)

    order = sorted(range(len(colours)), key=lambda k: -weights[k])
    rank = {old: new for new, old in enumerate(order)}
    ids = Image.frombytes(
        "P",
        generous.size,
        generous.tobytes().translate(bytes(rank.get(assign.get(i, 0), 0) for i in range(256))),
    )
    return [colours[k] for k in order], [weights[k] for k in order], total, ids


def _explained_as_blend(
    colour: tuple[int, int, int], inks: list[tuple[int, int, int]]
) -> bool:
    """True if *colour* is just a mixture of two inks already accepted.

    This is what separates a real ink from the transition tone beside it, and
    it is the only test that gets both halves right. Weighing candidates by
    area does not: on the test artwork the band along the letter edges covered
    more of the image (0.50%) than the sage green of the flower leaves
    (0.27%). Weighing them by how solid a region they form does not either --
    that reads a hairline as debris, and a synthetic outline one pixel wide
    was dropped outright, taking the colour out of the palette altogether.

    A transition tone, though, is by definition a mixture: the pixels between
    a charcoal stroke and a cream fill lie on the line between the two. A
    hairline drawn in charcoal lies between nothing, however thin it is.
    """
    for i in range(len(inks)):
        for j in range(i + 1, len(inks)):
            a, b = inks[i], inks[j]
            delta = tuple(bc - ac for ac, bc in zip(a, b))
            length = sum(d * d for d in delta)
            if length == 0:
                continue
            weight = sum((c - ac) * d for c, ac, d in zip(colour, a, delta)) / length
            weight = max(0.0, min(1.0, weight))
            mixed = tuple(ac + weight * d for ac, d in zip(a, delta))
            error = sum((c - m) ** 2 for c, m in zip(colour, mixed)) ** 0.5
            if error <= _BLEND_TOLERANCE:
                return True
    return False


def _solid_shares(ids: Image.Image, count: int) -> list[float]:
    """Share of the image each candidate holds once thin runs are removed.

    A mode filter keeps whatever fills a region and discards whatever is only
    a thread, so what survives measures shape rather than area.
    """
    filtered = ids.filter(ImageFilter.ModeFilter(5))
    shares = [0.0] * count
    total = ids.size[0] * ids.size[1]
    for pixels, index in filtered.getcolors(1 << 20) or []:
        if index < count and total:
            shares[index] = pixels / total
    return shares


def _accept_inks(rgb: Image.Image, limit: int) -> list[tuple[int, int, int]]:
    """Walk the candidates strongest-first, keeping the ones that are inks.

    Two conditions have to hold together before a colour is dismissed as a
    transition tone, and either on its own gets a case badly wrong.

    It must be *explained as a blend* of two inks already accepted. Area alone
    cannot decide this: on the test artwork the band along the letter edges
    covered more of the image (0.50%) than the sage green of the flower leaves
    (0.27%), so any threshold on size keeps the wrong one and drops the right
    one.

    And it must be *thin*, holding almost nothing once a mode filter has
    removed everything that is merely a thread. Blend alone cannot decide it
    either, because a neutral grey sits exactly on the line between black and
    white: on that same artwork it dismissed the grey background -- eighty
    percent of the image -- and took the whole palette down with it.

    Together they are right in all four directions. A grey background is a
    blend but not thin. A charcoal hairline is thin but not a blend. An edge
    band is both. A flower leaf is neither.
    """
    colours, weights, total, ids = _ink_candidates(rgb)
    solid = _solid_shares(ids, len(colours))

    inks: list[tuple[int, int, int]] = []
    for position, colour in enumerate(colours):
        if total and weights[position] / total < _INK_MIN_SHARE:
            continue
        if solid[position] < _INK_SOLID_FLOOR and _explained_as_blend(colour, inks):
            continue
        inks.append(colour)
        if len(inks) >= limit:
            break

    # The walk can only test a candidate against the inks accepted before it,
    # so a transition tone that outweighs one of the two colours it sits
    # between slips through -- on a lettering test image that left two
    # near-identical charcoals, which is a ghost layer by another name. One
    # more pass over the settled set catches those.
    keep = [
        colour
        for index, colour in enumerate(inks)
        if not (
            solid[colours.index(colour)] < _INK_SOLID_FLOOR
            and _explained_as_blend(colour, inks[:index] + inks[index + 1 :])
        )
    ]
    return keep or inks


def _derive_palette(rgb: Image.Image, count: int) -> list[tuple[int, int, int]]:
    """Work out the *count* real ink colours of the artwork.

    Quantizing straight to N is unreliable, because the buckets are chosen by
    pixel count and a dominant background swallows the budget. On the test
    artwork ``max_colors=6`` returned five muddy greys and lost the white
    outline and the pink flower entirely; at 8 it spent half the budget on
    transition tones like ``#d4cbc0`` that exist only along an edge.
    """
    return _accept_inks(rgb, count)


def _mean_residual(rgb: Image.Image, inks: list[tuple[int, int, int]]) -> float:
    """How far the average pixel sits from the nearest ink.

    Cheap, because the candidate buckets already stand in for the pixels.
    """
    generous, table = _generous(rgb)
    error = weight = 0.0
    for count, index in generous.getcolors(1 << 20) or []:
        entry = tuple(table[index * 3 : index * 3 + 3])
        if len(entry) < 3:
            continue
        error += count * min(
            sum((a - b) ** 2 for a, b in zip(entry, ink)) ** 0.5 for ink in inks
        )
        weight += count
    return error / weight if weight else 0.0


# Deciding whether a colour is a band cut through a gradient.
#
# A gradient sliced into flat colours passes every test that asks "are the
# pixels close to some ink" -- add enough bands and they always are. What gives
# it away is where the bands sit in the picture: a band is the *only* thing
# separating the two colours either side of it, because that is what a ramp is.
# A real ink that happens to lie between two others in RGB -- a grey background
# between black lettering and a white halo, say -- is not, because those two
# also meet each other directly all over the artwork.
#
# Measured on a lettering JPEG and on strokes filled with a smooth gradient:
# the grey background's neighbours touch each other 7,216 times directly
# against 9,199 through it, a ratio of 0.78, while the gradient band's
# neighbours touch **zero** times except through it.

# How much of a candidate's border each of the two colours has to account for
# before it is even considered to lie between them.
_RAMP_MIN_SIDE = 0.25

# ...and how rarely those two may meet elsewhere for it to be the ramp's doing
# rather than a colour that merely sits between them.
_RAMP_MAX_DIRECT = 0.15

# The adjacency count runs a pass over the pixels in Python, so it gets a
# smaller copy than the rest of detection. Boundaries survive the reduction --
# it is which colours meet that matters, not exactly where.
_RAMP_MAX_EDGE = 384


def _adjacency(rgb: Image.Image, inks: list[tuple[int, int, int]]) -> list[list[int]]:
    """How often each pair of inks meets along a boundary."""
    work = rgb
    if max(work.size) > _RAMP_MAX_EDGE:
        scale = _RAMP_MAX_EDGE / max(work.size)
        work = work.resize(
            (max(1, int(work.width * scale)), max(1, int(work.height * scale))),
            Image.Resampling.NEAREST,
        )
    probe, resolves = _blend_ramp(inks)
    quantized = work.quantize(palette=probe, dither=Image.Dither.NONE)
    ids = list(
        Image.frombytes("P", quantized.size, quantized.tobytes().translate(resolves))
        .get_flattened_data()
    )
    width, height = work.size
    counts = [[0] * len(inks) for _ in inks]
    for y in range(height):
        row = y * width
        for x in range(width - 1):
            here, there = ids[row + x], ids[row + x + 1]
            if here != there:
                counts[here][there] += 1
                counts[there][here] += 1
    for y in range(height - 1):
        row = y * width
        for x in range(width):
            here, there = ids[row + x], ids[row + width + x]
            if here != there:
                counts[here][there] += 1
                counts[there][here] += 1
    return counts


def _has_gradient_band(rgb: Image.Image, inks: list[tuple[int, int, int]]) -> bool:
    """True if one of *inks* is a slice through a ramp rather than an ink."""
    if len(inks) < 3:
        return False
    counts = _adjacency(rgb, inks)
    for index, colour in enumerate(inks):
        border = sum(counts[index])
        if not border:
            continue
        for left in range(len(inks)):
            for right in range(left + 1, len(inks)):
                if index in (left, right):
                    continue
                if not _explained_as_blend(colour, [inks[left], inks[right]]):
                    continue
                through = min(counts[index][left], counts[index][right])
                if (
                    counts[index][left] / border < _RAMP_MIN_SIDE
                    or counts[index][right] / border < _RAMP_MIN_SIDE
                ):
                    continue
                if not through or counts[left][right] / through <= _RAMP_MAX_DIRECT:
                    return True
    return False


def _carries_a_ramp(rgb: Image.Image) -> bool:
    """True if the artwork contains a genuine gradient, rather than flat inks.

    The same question :func:`_detect_flat_palette` asks before declining to
    flatten the colours, asked on its own so the enlarged trace can decline
    for the same reason. Both stand down for shaded artwork, and they do it
    because of the same mechanism: the tracer cuts a ramp into as many flat
    layers as its colour distance allows, and giving it four times the pixels
    lets it find more of them. Measured on artwork whose background is one
    broad sweep, the ramp came back as one fill at 1x and five at 2x, and
    since a gradient is then fitted per fill each got a fifth of the sweep --
    which pads flat past its ends, so the corners of the picture lost their
    colour. Mean error against the source went from 5.1 to 14.2.

    A radial glow inside a lens is not this: it is a blob, not a band lying
    between two colours, so illustration with a little soft shading in it
    still gets the enlarged trace it needs.
    """
    inks = _accept_inks(rgb, _FLAT_MAX_INKS + 1)
    return bool(inks) and _has_gradient_band(rgb, inks)


def _detect_flat_palette(rgb: Image.Image) -> list[tuple[int, int, int]] | None:
    """Return the artwork's inks, or None if it is not flat artwork.

    Flat artwork traced as-is picks up a shape for every anti-aliasing band,
    which is what makes outlines read grey and edges look faceted. Finding its
    inks removes those at the source.

    Anything continuous-tone -- a photograph, a gradient, a shaded
    illustration -- gets no palette at all, because flattening it to a dozen
    colours would be vandalism rather than cleanup.
    """
    inks = _accept_inks(rgb, _FLAT_MAX_INKS + 1)
    if not inks or len(inks) > _FLAT_MAX_INKS:
        return None
    if _mean_residual(rgb, inks) > _FLAT_MAX_RESIDUAL:
        return None
    # A gradient sliced into flat bands passes everything above -- add enough
    # bands and the pixels are always close to one. Shaded artwork has to be
    # left to the tracer, so one band anywhere declines the whole palette.
    if _has_gradient_band(rgb, inks):
        return None
    return inks


def _blend_ramp(rgbs: list[tuple[int, int, int]]) -> tuple[Image.Image, bytes]:
    """Build the palette to quantize *against*, and what each entry resolves to.

    Mapping every pixel to its nearest palette colour is wrong along an
    anti-aliased edge, and wrong in a way that is very visible. On the test
    artwork a pixel halfway between the charcoal stroke and the cream fill is
    ``(148, 146, 130)``, whose nearest palette entry is the *grey background*
    -- a colour that does not touch that edge anywhere in the image. Every
    letter therefore picked up a one-to-two pixel grey ribbon along its
    outline, and because the ribbon is a solid run rather than scattered
    pixels the tracer duly turned it into grey shapes lying against the white
    outline, breaking it up.

    So quantize against a richer set instead: the palette colours plus samples
    taken along the segment between every pair of them. Each sample is
    labelled with the endpoint it sits nearer to, and re-labelling the
    quantized result collapses the ramps back onto the palette. A blend of two
    colours then resolves to one of those two, never to a third that merely
    happens to sit nearby in RGB space.

    This is the same reasoning :func:`app.services.svgdoc._resolve_blend`
    applies to the tracer's output fills, moved upstream to the pixels, where
    it prevents the wrong shapes from being traced in the first place.

    Returns the probe palette image, and a 256-byte table translating a probe
    index back to the index of the palette colour it resolves to.
    """
    pairs = [(i, j) for i in range(len(rgbs)) for j in range(i + 1, len(rgbs))]

    # The probe palette has the same 256-entry ceiling as any other, and the
    # palette colours themselves have to fit first. A palette large enough to
    # leave no room simply degrades to plain nearest-colour mapping.
    budget = 256 - len(rgbs)
    per_pair = min(_RAMP_SAMPLES, budget // len(pairs)) if pairs else 0

    probe = list(rgbs)
    resolves_to = list(range(len(rgbs)))
    for i, j in pairs:
        a, b = rgbs[i], rgbs[j]
        for step in range(1, per_pair + 1):
            weight = step / (per_pair + 1)
            probe.append(
                tuple(round(ac + weight * (bc - ac)) for ac, bc in zip(a, b))  # type: ignore[arg-type]
            )
            resolves_to.append(j if weight > 0.5 else i)

    probe_img = Image.new("P", (1, 1))
    probe_img.putpalette(_flat_palette(probe))

    # Unused probe slots repeat the last real entry, so point them at its
    # resolution rather than leaving them mapped to themselves.
    table = resolves_to + [resolves_to[-1]] * (256 - len(resolves_to))
    return probe_img, bytes(table[:256])


# Ranks that decide what counts as a speckle. Comparing the Nth smallest and
# Nth largest of a 3x3 window asks "do at least 9-2N of these nine agree?",
# so (2, 6) means seven of nine. See _drop_speckles for why seven.
_SPECKLE_RANKS = (2, 6)


def _drop_speckles(ids: Image.Image) -> Image.Image:
    """Clear specks of a pixel or two without eroding anything a pixel wide.

    The blend ramps settle which two colours an edge pixel lies between; what
    is left is stray compression speckles. A mode filter is the obvious tool
    and the wrong one: a hairline is a minority in its own 3x3 window, so the
    filter eats it. On the test artwork it removed 6,154 pixels of a charcoal
    outline drawn one to two pixels wide, and that is what left the outline
    thick in places, thin in others and broken into dashes.

    The distinction that matters is not how many pixels disagree but whether
    they form a line. A pixel on a one-pixel line has two more of its own kind
    in its window, so six of the nine agree on the other colour; a speck of
    one or two pixels leaves seven or more agreeing. Requiring seven keeps
    every line and still clears both sizes of speck.

    RankFilter does the counting without a pass over the pixels in Python:
    when seven of the nine share a value, the third-smallest and third-largest
    are both that value whatever the other two are, and any more variety makes
    them differ.
    """
    flat = Image.frombytes("L", ids.size, ids.tobytes())
    low = flat.filter(ImageFilter.RankFilter(3, _SPECKLE_RANKS[0]))
    high = flat.filter(ImageFilter.RankFilter(3, _SPECKLE_RANKS[1]))
    uniform = ImageChops.difference(low, high).point(lambda v: 255 if v == 0 else 0)
    differs = ImageChops.difference(flat, low).point(lambda v: 255 if v else 0)
    cleaned = Image.composite(low, flat, ImageChops.multiply(uniform, differs))
    return Image.frombytes("P", ids.size, cleaned.tobytes())


# Counting how many of the nine pixels in a window carry one ink, as the mean
# of a 0/255 mask: a single occurrence comes back as 28, two as 57, three as
# 85. Ranking and tallying filters cannot answer this -- they compare *values*,
# and what matters is how many of the neighbours match the one in the middle --
# but a convolution can, and counts in C.
_COMPANY = ImageFilter.Kernel((3, 3), [1] * 9, scale=9)

# How many pixels of its own ink a pixel needs around it before it counts as
# part of the artwork rather than as a stray thrown there by compression. Two
# means "not entirely alone", which is as much as may be asked of a bitmap
# traced at its own resolution: a one-pixel line's last pixel has a single
# neighbour of its own kind, and detail='maximum' promises to keep it.
_COMPANY_ALONE = 2

# At twice the resolution the same question can be put more strictly, because
# a pixel is then half a source pixel across: three of a kind is still less
# than one source pixel's worth of ink, so nothing that was drawn can fail the
# test, while a stray and a stray's pair both do.
_COMPANY_SUBPIXEL = 3


def _luminance(rgbs: list[tuple[int, int, int]]) -> list[int]:
    """Each ink's brightness, as the label-to-grey table point() takes."""
    table = [0] * 256
    for ink, (red, green, blue) in enumerate(rgbs):
        table[ink] = round(0.299 * red + 0.587 * green + 0.114 * blue)
    return table


class _Neighbourhood(NamedTuple):
    """What the nine pixels of each 3x3 window say about the one in the middle."""

    commonest: Image.Image  # the ink most of the nine carry
    company: Image.Image  # how many of the nine carry the middle pixel's ink
    darkest: Image.Image  # brightness of the darkest ink among the nine
    lightest: Image.Image  # ...and of the lightest


def _neighbourhoods(
    flat: Image.Image, rgbs: list[tuple[int, int, int]]
) -> _Neighbourhood:
    """Answer all four questions in one pass per ink.

    One convolution per ink gives that ink's count everywhere, and the rest
    falls out of it: a running maximum over the counts leaves the commonest
    ink behind, the count read where the ink's own mask is set is that pixel's
    company, and the inks whose count is not zero are the ones present, which
    is what bounds how dark and how light the window gets.

    Pillow's ModeFilter would give the first of the four on its own, and on a
    nine-megapixel label image of six inks it takes 1.3s where all four of
    these together take 0.75s -- it sorts a window per pixel, and a handful of
    convolutions does not.

    Pillow leaves the border of a convolution unfiltered, so the outermost
    pixels come back holding mask values rather than counts. That reads as
    ample company and as a window of one ink, which leaves the border alone --
    the right answer, since those pixels have no full window to be judged by.
    """
    brightness = _luminance(rgbs)
    blank = Image.new("L", flat.size, 0)
    solid = Image.new("L", flat.size, 255)
    commonest = Image.new("L", flat.size, 0)
    best = Image.new("L", flat.size, 0)
    company = Image.new("L", flat.size, 0)
    darkest = Image.new("L", flat.size, 255)
    lightest = Image.new("L", flat.size, 0)
    for ink, present in enumerate(flat.histogram()):
        if not present:
            continue
        mask = flat.point([255 if value == ink else 0 for value in range(256)])
        count = mask.filter(_COMPANY)
        anywhere = count.point([255 if value else 0 for value in range(256)])
        wins = ImageChops.subtract(count, best).point(
            [255 if value else 0 for value in range(256)]
        )
        best = ImageChops.lighter(best, count)
        commonest = Image.composite(Image.new("L", flat.size, ink), commonest, wins)
        company = ImageChops.lighter(company, ImageChops.multiply(mask, count))
        shade = Image.new("L", flat.size, brightness[ink])
        darkest = ImageChops.darker(darkest, Image.composite(shade, solid, anywhere))
        lightest = ImageChops.lighter(lightest, Image.composite(shade, blank, anywhere))
    return _Neighbourhood(commonest, company, darkest, lightest)


def _drop_strays(
    ids: Image.Image, rgbs: list[tuple[int, int, int]], keep_at_least: int
) -> Image.Image:
    """Clear the overshoots compression leaves along a boundary.

    :func:`_drop_speckles` only reaches a speck in a plain field, because it
    asks whether seven of the nine agree and where two inks meet they never
    do. So the strays that survive it are exactly the ones sitting on a
    boundary -- a charcoal pixel thrown out into the grey by JPEG ringing,
    beside the white outline it rang off -- and those are the ones that cost
    the most. The tracer cannot pass one by: the boundary running alongside it
    has to detour around it and back, and that detour is a notch in a curve
    that was otherwise smooth. On the reference artwork there were 9,969 of
    them in nine megapixels -- a tenth of one per cent of the bitmap, and
    enough to leave a notch every 250 pixels of finished outline, which is
    what made the lettering look faceted at any real zoom.

    Clearing them is also the one kind of smoothing that cannot open a seam.
    Every shape is traced separately, so nudging one shape's outline moves it
    away from the shape that abutted it and lets the background show through
    the crack; but a stray taken out of the bitmap is taken out for both shapes
    at once, and both stop detouring in the same place. Measured on the
    reference artwork it took the notches down by a fifth and closed an eighth
    of the hairline seams, rather than opening any.

    Having no company cannot be the whole test, because a thin feature is made
    of lonely pixels too. A one-pixel white line on a dark ground quantizes
    into white and mid-grey pixels alternating along it, and every one of them
    is alone among its immediate neighbours; judged on company alone the whole
    line reads as strays, and outline-only lettering disappeared when this was
    first tried that way.

    What tells the two apart is where the colour sits. Mixing two inks can only
    ever land between them in brightness, so the mid-grey beside that white
    line -- a blend of the line and the ground -- is never the darkest or the
    lightest thing in its window. Ringing is the opposite: it overshoots past
    everything around it, which is why the charcoal speck is darker than both
    the grey it sits in and the white it borders. So a pixel goes only when it
    is alone *and* an extreme, and anything that could be a blend of what
    surrounds it stays.
    """
    flat = Image.frombytes("L", ids.size, ids.tobytes())
    near = _neighbourhoods(flat, rgbs)
    brightness = flat.point(_luminance(rgbs))
    cutoff = int(255 * (keep_at_least - 0.5) / 9)
    alone = near.company.point([255 if value < cutoff else 0 for value in range(256)])
    # An extreme is a pixel that matches one end of its window's brightness
    # range exactly, so take the smaller of the two distances rather than
    # multiplying them: a product of two small distances rounds to zero, and
    # would read a window of near-identical inks as an overshoot.
    extreme = ImageChops.darker(
        ImageChops.difference(brightness, near.darkest),
        ImageChops.difference(brightness, near.lightest),
    ).point([255 if value == 0 else 0 for value in range(256)])
    cleaned = Image.composite(near.commonest, flat, ImageChops.multiply(alone, extreme))
    return Image.frombytes("P", ids.size, cleaned.tobytes())


def _smooth_broad_boundaries(ids: Image.Image) -> Image.Image:
    """Even out the edges of broad regions, leaving thin ones exactly as they are.

    Hard-quantizing a soft edge leaves it ragged: the ramp crosses the
    boundary, compression noise makes it cross back, and the result is a
    fringe of single pixels that the curve fitter then traces. A mode filter
    settles that fringe -- but it also eats anything narrower than its window,
    which is how a hairline outline gets chewed into dashes.

    Both are wanted, so the filter is applied only where it cannot do harm. A
    pixel sits in a structure at least three pixels wide exactly when some 3x3
    window containing it holds a single ink, and that window's ink must be its
    own. So mark the windows that are uniform, spread that mark by one pixel,
    and smooth only what it covers; everything left over is a thread and keeps
    the value it had.

    Three filter passes settle it whatever the ink count -- the obvious form,
    an opening per ink, gives pixel-for-pixel the same answer for twice the
    work.

    This is only worth doing on a bitmap traced at a multiple of its own
    resolution, where a 3x3 window spans a pixel and a half of the original
    and the fringe it removes is genuinely sub-pixel.
    """
    flat = Image.frombytes("L", ids.size, ids.tobytes())
    uniform = ImageChops.difference(
        flat.filter(ImageFilter.MinFilter(3)), flat.filter(ImageFilter.MaxFilter(3))
    ).point(lambda v: 255 if v == 0 else 0)
    broad = uniform.filter(ImageFilter.MaxFilter(3))
    smoothed = Image.frombytes(
        "L", ids.size, ids.filter(ImageFilter.ModeFilter(3)).tobytes()
    )
    return Image.frombytes(
        "P", ids.size, Image.composite(smoothed, flat, broad).tobytes()
    )


# Collapsing an anti-aliasing ramp into a hard edge whose *position* is
# sub-pixel accurate. This is the smoothing that continuous-tone artwork gets:
# it cannot be given a palette -- flattening one would band its shading, which
# is the whole reason palette detection stands down for it -- but the edges it
# does have deserve to be placed as precisely as a flat drawing's.
#
# The defect being fixed: a soft edge in the source carries the boundary's
# true position in its ramp, and the tracer throws that away. It clusters the
# ramp's own tones into a layer of their own, so every outline comes back with
# a thin sliver of blend running along it, and the two hard boundaries either
# side of that sliver each follow the pixel grid. Zoomed in, the line is not
# smooth: it steps, and it carries a band.
#
# So the ramp is read rather than traced. On a copy enlarged 2x, each pixel
# inside a ramp is pushed to whichever end of its own 3x3 neighbourhood it
# sits nearer -- which puts the boundary within half an enlarged pixel, a
# quarter of a source pixel, of where the ramp says it is -- and the ramp's
# own tones stop existing, so there is no sliver left to trace.
#
# Two properties make this safe to run on any artwork, and both are worth
# stating because the obvious alternatives have neither. It can only ever
# write a value that already occurs in the pixel's own neighbourhood, so it
# cannot invent a colour or bulge a curve between its nodes the way moving
# control points does. And it is monotone -- a pixel moves to an end, never
# past it -- so it cannot erase a feature: a median or mode filter eats
# anything narrower than its window, which is how a hairline becomes dashes,
# where this leaves a one-pixel line exactly where it was.

# How big the jump across the transition has to be before it is read as an
# edge rather than as shading. Measured over the corpus, as the bitmap reaches
# this stage: smooth shading barely moves from pixel to pixel (a shaded
# sphere's 95th percentile is 4, a gradient illustration's 8), sensor and JPEG
# noise are held under the denoise filter that ran before this (a greyscale
# photograph peaks at 40), and real edges land between 126 and 238. At 96 the
# separation is wide in both directions.
_EDGE_CONTRAST = 96

# How wide a transition may be and still count as an edge. A soft edge is not
# one pixel wide: artwork that has been rendered and resized carries a ramp
# two or three pixels across, and the first version of this looked for the
# whole jump inside a 3x3 window of the *enlarged* copy -- a pixel and a half
# of the original, which such a ramp cannot supply -- so it snapped the outer
# pixels of every outline and left the middle of the ramp behind as a thinner
# sliver of the same defect.
#
# The gate is measured on the source rather than on the enlargement, for the
# same reason softness is: it asks what the artwork contains, and every
# enlargement puts a ramp along every edge. Being a quarter of the pixels with
# windows half as wide, it is also many times cheaper -- Pillow's rank filter
# sorts each window, so a 15x15 one over an enlarged bitmap cost five seconds
# on a one-megapixel illustration.
_EDGE_WINDOW = 3

# ...and how much of a *doubly* wide neighbourhood's jump has to already be
# present in that window. This is what separates a step from a ramp, and it is
# the only test that can: both come back as a large range. A step's range stops
# growing once the window spans it, so the two match and the ratio is near 1,
# while shading keeps spreading and reads about a half. Measured against a
# gradient steep enough to clear the contrast floor -- a full sweep over eight
# source pixels does -- which reads 0.47 here and is left alone. Without this
# test such a gradient would be snapped into bands, which is the one way this
# stage could damage artwork it was not aimed at.
_EDGE_COMPLETE_SHARE = 0.6
_EDGE_WIDE_WINDOW = 7

# The snap itself reaches one enlarged pixel, however wide the gate was, and
# is repeated instead. Reaching as far as the gate would let a region three
# source pixels away supply the value, which is enough to recolour a hairline
# that happens to run near something darker; reaching one pixel at a time
# cannot, because the only values in range are the pixel's own neighbours.
# Repetition still crosses a wide ramp, since each pass turns the outermost
# ramp pixels into plateau and the plateau advances a pixel from each side --
# three passes close the six enlarged pixels that _EDGE_WINDOW admits.
_EDGE_PASSES = 3

# How much of an edge has to be made of intermediate tones before the bitmap
# is believed to have soft edges at all.
#
# This is the entry condition for the whole stage, and it exists because an
# edge that is already hard carries no sub-pixel position to recover -- a
# circle plotted pixel by pixel, artwork resized nearest-neighbour, pixel art.
# Enlarging one of those invents a ramp either side of every step of its
# staircase, and snapping that invented ramp squares the staircase off instead
# of cutting across it; on a shaded sphere whose rim was plotted that way it
# came back visibly scalloped, where tracing the original at 1x had given a
# clean circle.
#
# Measured over the corpus as the share of high-contrast edge pixels whose
# brightness is strictly between their neighbours': artwork that was rendered
# with anti-aliasing reads between 56% and 90%, and everything drawn or
# resampled without it reads under 10% -- a hand-plotted sphere rim 9.7%, a
# nearest-neighbour logo 0.2%. At 40% neither kind is near the line.
_EDGE_MIN_SOFTNESS = 0.40

# Below this share of the bitmap there is no edge worth the enlarged trace.
# A photograph marks nothing at all -- measured at 0.000%, because noise never
# clears the contrast floor -- while every drawing in the corpus marks between
# 0.70% and 4.54%, so this only ever excludes images that would have paid four
# times the tracing cost for no change to their geometry.
_EDGE_MIN_SHARE = 0.0005

# How small the source may be before the enlarged trace stops being worth
# taking on this path.
#
# Two of the tracer's knobs are measured in source pixels and are restored to
# that meaning when the bitmap is enlarged, and the shortest segment the curve
# fitter will use is additionally given half again as much latitude at 2x --
# six source pixels, all told. That is a reasonable distance to cut a curve
# across on a large bitmap and a sixteenth of the width of a small one, where
# it stops following features and starts cutting through them.
#
# Rendered at a range of sizes, the same illustration crosses over between 96
# and 128 pixels: at 64 the enlarged trace loses the camera body and breaks
# the arcs into blobs, at 96 it keeps the body but fragments an arc, and from
# 128 up it is the better of the two everywhere -- smoother arcs, cleaner lens
# rings, less lumpy facets.
#
# The measurement is of the smaller side, since it is the smaller side that
# runs out first, and it applies only to this path: artwork whose colours were
# settled has each pixel pinned to an ink, which is what carries it through
# the same enlargement at small sizes.
_SUPERSAMPLE_MIN_EDGE = 128


def _grown(band: Image.Image, window: int, rank: type) -> Image.Image:
    """A wide min or max, reached by repeating the 3x3 one.

    Pillow's rank filter sorts every window, so its cost climbs with the
    square of the width; repeating the smallest one gives pixel-identical
    output -- a square structuring element decomposes -- for a fraction of the
    work, measured at 311ms against 726ms for a 15-wide max over a
    two-megapixel band.
    """
    for _ in range(window // 2):
        band = band.filter(rank(3))
    return band


def _widest_range(bands: list[Image.Image], window: int) -> Image.Image:
    """How far apart the extremes of each *window* sit, over the widest channel.

    An edge is a property of the colour, not of one channel at a time, so the
    channel that moves furthest decides and all three are then snapped
    together. Judging each channel on its own lets two of them disagree about
    which side of a ramp a pixel belongs to, which invents a colour that is in
    the artwork nowhere -- magenta speckles along a charcoal curve, when this
    was first tried that way.
    """
    widest = None
    for band in bands:
        spread = ImageChops.difference(
            _grown(band, window, ImageFilter.MaxFilter),
            _grown(band, window, ImageFilter.MinFilter),
        )
        widest = spread if widest is None else ImageChops.lighter(widest, spread)
    assert widest is not None
    return widest


def _soft_edges(bands: list[Image.Image]) -> Image.Image:
    """Mark the pixels sitting in a step between two regions."""
    near = _widest_range(bands, _EDGE_WINDOW)
    wide = _widest_range(bands, _EDGE_WIDE_WINDOW)
    big = near.point([255 if v >= _EDGE_CONTRAST else 0 for v in range(256)])
    # near >= share * wide, rearranged so the division lands on the small
    # number and the comparison stays a subtract-and-threshold in C.
    stretched = near.point(
        [min(255, int(v / _EDGE_COMPLETE_SHARE)) for v in range(256)]
    )
    complete = ImageChops.subtract(wide, stretched).point(
        [255 if v == 0 else 0 for v in range(256)]
    )
    return ImageChops.multiply(big, complete)


def _interior(
    brightness: Image.Image,
    darkest: Image.Image | None = None,
    lightest: Image.Image | None = None,
) -> Image.Image:
    """Mark pixels whose brightness is strictly between their neighbours'.

    These are the only pixels this stage may move, and saying so is what makes
    it idempotent: a pixel already sitting at an end of its own neighbourhood
    is plateau, not ramp, so once a ramp has been consumed nothing moves again.
    Without that, repeated passes stop sharpening and start eroding -- convex
    corners lose a pixel per pass and concave ones gain one, which is
    morphological rounding, and on a staircase it shows up as scalloping.

    The neighbourhood extremes are passed in where the caller has already paid
    for them.
    """
    if darkest is None:
        darkest = brightness.filter(ImageFilter.MinFilter(3))
    if lightest is None:
        lightest = brightness.filter(ImageFilter.MaxFilter(3))
    above = ImageChops.subtract(brightness, darkest).point(
        [255 if v else 0 for v in range(256)]
    )
    below = ImageChops.subtract(lightest, brightness).point(
        [255 if v else 0 for v in range(256)]
    )
    return ImageChops.multiply(above, below)


def _edge_softness(bands: list[Image.Image], brightness: Image.Image) -> float:
    """What share of this bitmap's edges are made of intermediate tones."""
    edge = _widest_range(bands, _EDGE_WINDOW).point(
        [255 if v >= _EDGE_CONTRAST else 0 for v in range(256)]
    )
    total = edge.histogram()[255]
    if not total:
        return 0.0
    return ImageChops.multiply(edge, _interior(brightness)).histogram()[255] / total


def _mask_share(mask: Image.Image) -> float:
    counts = mask.histogram()
    return counts[255] / (mask.width * mask.height) if mask.width else 0.0


def _steepen(bands: list[Image.Image], edge: Image.Image) -> list[Image.Image]:
    """One pass: move each marked pixel onto the nearer of its neighbours' extremes."""
    brightness = Image.merge("RGB", bands).convert("L")
    darkest = brightness.filter(ImageFilter.MinFilter(3))
    lightest = brightness.filter(ImageFilter.MaxFilter(3))
    # The side is chosen once, from brightness, and applied to all three
    # channels -- see _widest_range on why they must not choose separately.
    # Ties stay put: a pixel exactly between its two extremes is as likely to
    # be the crest of a thin feature as a point on a ramp.
    upper = ImageChops.subtract(
        ImageChops.difference(brightness, darkest),
        ImageChops.difference(lightest, brightness),
    ).point([255 if v else 0 for v in range(256)])
    moving = ImageChops.multiply(edge, _interior(brightness, darkest, lightest))
    return [
        Image.composite(
            Image.composite(
                band.filter(ImageFilter.MaxFilter(3)),
                band.filter(ImageFilter.MinFilter(3)),
                upper,
            ),
            band,
            moving,
        )
        for band in bands
    ]


def _even_broad_edges(rgb: Image.Image) -> Image.Image:
    """Even out the staircase collapsing the ramps leaves behind.

    Snapping puts the boundary within half an enlarged pixel of where the ramp
    said it was, which is a quarter of a source pixel -- but it puts it there
    *on the enlarged grid*, so a long shallow edge comes out as a run of steps
    rather than a line. The curve fitter cuts across most of them and turns
    the rest into corners, because a step of half a source pixel on an
    otherwise straight run bends further than the angle it keeps sharp; at
    high zoom those read as notches along an edge that should be smooth.

    This is :func:`_smooth_broad_boundaries` for pixels rather than for ink
    labels, and it is guarded the same way: a median is applied only where
    some 3x3 window is a single colour, spread by one pixel, so it reaches the
    edges of broad regions and never a thread -- a median eats anything
    narrower than its window, which is how a hairline becomes dashes.

    A median is safe on colour here for the same reason the snap is: after
    snapping, the window either side of an edge holds two colours and no
    blend, so every channel's median comes from the same majority pixels and
    the result is one of the two colours actually there.
    """
    bands = list(rgb.split())
    uniform = _widest_range(bands, 3).point([255 if v == 0 else 0 for v in range(256)])
    broad = uniform.filter(ImageFilter.MaxFilter(3))
    return Image.merge(
        "RGB",
        [
            Image.composite(band.filter(ImageFilter.MedianFilter(3)), band, broad)
            for band in bands
        ],
    )


def _snap_soft_edges(rgb: Image.Image, edge: Image.Image) -> Image.Image:
    """Collapse every soft edge into a hard one, sub-pixel accurately placed.

    *rgb* is the enlarged copy and *edge* the mark taken from the source it
    was enlarged from, scaled up to match. The enlargement has to be by a
    filter that does not overshoot. Lanczos was the obvious choice and is
    wrong here: its ringing puts values outside the range the two regions
    actually span, and a channel that has rung past its neighbour picks the
    opposite end from the other two, so the boundary comes back stippled with
    colours the artwork never had. Bilinear has no overshoot, and the only
    thing this stage needs from the enlargement is where the ramp crosses its
    own halfway point, which bilinear places exactly.

    The mark is taken once and every pass is confined to it. Recomputing it as
    the edges steepen would let it creep outwards into whatever the sharpened
    boundary now contrasts with.
    """
    bands = list(rgb.split())
    for _ in range(_EDGE_PASSES):
        bands = _steepen(bands, edge)
    return _even_broad_edges(Image.merge("RGB", bands))


def _resolve_palette(
    image: Image.Image,
    max_colors: int,
    palette: list[str] | None,
    auto: bool,
) -> list[tuple[int, int, int]] | None:
    """Settle which colours the pixels will be mapped onto, or None for neither.

    A pinned palette is the caller's. A colour budget means derive that many
    inks. With neither, look at the artwork: flat work gets its own inks found
    for it, and anything continuous-tone is left exactly as it is. The pixels
    are then assigned the same way in every case, so the automatic result
    matches what naming the colours would have given.
    """
    if palette:
        return _palette_rgbs(palette) or None
    rgb = image.convert("RGB")
    if max_colors > 0:
        return _derive_palette(rgb, max(2, min(256, max_colors))) or None
    if auto:
        return _detect_flat_palette(rgb)
    return None


def _quantize(
    image: Image.Image,
    rgbs: list[tuple[int, int, int]],
    subpixel: bool = False,
) -> tuple[Image.Image, list[str]]:
    """Map every pixel onto *rgbs*, preserving any alpha channel.

    Returns the conditioned image and the palette it was mapped onto, so the
    caller can pass that on and have the traced fills snapped to the same
    list. Pinning the pixels is only half the job: the tracer averages within
    each cluster, so boundary clusters still come back as blends.

    *subpixel* says the bitmap is being traced finer than the artwork it came
    from, which is what licenses the stricter conditioning: at twice the
    resolution a single pixel is half a source pixel across, so a feature that
    narrow is where the quantizer landed rather than anything drawn.

    Dithering is deliberately disabled: dither noise becomes thousands of
    tiny paths once traced.
    """
    alpha = image.getchannel("A") if image.mode == "RGBA" else None
    rgb = image.convert("RGB")

    probe, resolves_to = _blend_ramp(rgbs)
    quantized = rgb.quantize(palette=probe, dither=Image.Dither.NONE)
    # Collapse the ramp samples onto the palette entry each resolves to, so
    # what follows sees one index per palette colour and nothing else.
    # Translating the raw index bytes keeps this in C.
    quantized = Image.frombytes(
        "P", quantized.size, quantized.tobytes().translate(resolves_to)
    )
    quantized = _drop_speckles(quantized)
    quantized = _drop_strays(
        quantized, rgbs, _COMPANY_SUBPIXEL if subpixel else _COMPANY_ALONE
    )
    if subpixel:
        quantized = _smooth_broad_boundaries(quantized)
    quantized.putpalette(_flat_palette(rgbs))

    result = quantized.convert("RGB")
    if alpha is not None:
        result = result.convert("RGBA")
        result.putalpha(alpha)
    return result, ["#%02x%02x%02x" % entry for entry in rgbs]


# Tracing at twice the resolution, then presenting the result at the original
# size. A one-pixel line cannot be quantized evenly: whether a given pixel
# lands on the dark side of the boundary depends on where the line falls
# within that pixel, so its width wanders between one and four pixels and the
# curve fitter follows every wobble. On the reference artwork that is exactly
# what left the charcoal outline uneven -- measured 1-4 pixels where the
# source varies 1-2. At twice the resolution the wander is half as large
# relative to the line and the outline comes out even: 194 traced shapes
# became 89.
#
# The same reasoning applies to artwork whose colours are left alone, for
# which there is no quantizer but the same lost half-pixel: see
# :func:`_snap_soft_edges`. Which conditioning the enlarged copy gets, and
# whether it is offered at all, is :func:`_finer_copy`'s decision; tracing it
# in preference to the plain one is engine.trace's.
_SUPERSAMPLE = 2
_SUPERSAMPLE_MAX_PIXELS = 12_000_000


def _plan_soft_edges(
    image: Image.Image, params: VectorizeParams, budget: int
) -> Image.Image | None:
    """The mark to collapse the ramps by, or None if this bitmap should not.

    Taken before denoising, because the answer decides whether to denoise at
    all -- see :func:`prepare` -- and because every gate here asks about the
    artwork rather than about the noise in it.

    It declines for five reasons: the caller is driving the colours
    themselves, the bitmap is too small for the curve fitter's own shortest
    segment to be a small part of it, its edges are already hard and so carry
    no sub-pixel position to recover, it is a gradient rather than artwork
    with edges, or it has no edges at all.
    """
    if not params.auto_palette:
        logger.debug("caller is driving the colours, tracing at 1x")
        return None
    if image.width * image.height * _SUPERSAMPLE**2 > budget:
        logger.debug("too large to trace at %sx", _SUPERSAMPLE)
        return None
    if min(image.size) < _SUPERSAMPLE_MIN_EDGE:
        logger.debug("source is %sx%s, tracing at 1x", *image.size)
        return None
    flat = image.convert("RGB")
    bands = list(flat.split())
    softness = _edge_softness(bands, flat.convert("L"))
    if softness < _EDGE_MIN_SOFTNESS:
        logger.debug("edges are already hard (%.0f%% soft), tracing at 1x", softness * 100)
        return None
    if _carries_a_ramp(flat):
        logger.debug("artwork carries a gradient, tracing at 1x")
        return None
    edge = _soft_edges(bands)
    share = _mask_share(edge)
    if share < _EDGE_MIN_SHARE:
        logger.debug("no edges to place (%.4f%%), tracing at 1x", share * 100)
        return None
    return edge


def _finer_copy(
    original: Image.Image,
    palette_rgbs: list[tuple[int, int, int]] | None,
    palette: list[str] | None,
    *,
    plan: Image.Image | None,
    source_format: str,
    source_width: int,
    source_height: int,
    downscaled: bool,
) -> PreparedImage | None:
    """The enlarged copy of the bitmap, or None if there is nothing to gain.

    Both kinds of artwork want the same thing from the enlargement -- the
    boundary placed where the source's soft edge says it is, rather than on
    the nearest pixel corner -- but they cannot be conditioned the same way.

    Artwork whose colours were settled gets quantized again at the finer
    scale, which snaps the ramp onto real inks and licenses the stricter
    stray and boundary treatment; the enlargement may as well be lanczos
    there, since every value lands on an ink afterwards and its ringing is
    clamped away.

    Continuous-tone artwork has no inks to snap to, so its ramps are collapsed
    in place instead, and the enlargement has to be one that does not
    overshoot -- see :func:`_snap_soft_edges`. It is also the path that can
    decline, and it declines for four reasons: a bitmap too small for the
    curve fitter's own minimum segment to be a small part of it, one whose
    edges are already hard and so carry no sub-pixel position to recover, one
    that is a gradient rather than artwork with edges, and one with no edges
    at all. Each keeps the plain 1x trace it had before.
    """
    size = (original.width * _SUPERSAMPLE, original.height * _SUPERSAMPLE)

    if palette_rgbs is not None:
        enlarged, _ = _quantize(
            original.resize(size, Image.Resampling.LANCZOS),
            palette_rgbs,
            subpixel=True,
        )
    elif plan is None:
        return None
    else:
        enlarged = original.resize(size, Image.Resampling.BILINEAR)
        alpha = enlarged.getchannel("A") if enlarged.mode == "RGBA" else None
        # The mark is a region, not a boundary, so nearest-neighbour is the
        # right way to carry it up; it is then grown by one enlarged pixel so
        # it still covers the ramp at the edges of what it marked.
        grown = plan.resize(size, Image.Resampling.NEAREST).filter(
            ImageFilter.MaxFilter(3)
        )
        snapped = _snap_soft_edges(enlarged.convert("RGB"), grown)
        if alpha is not None:
            snapped = snapped.convert("RGBA")
            snapped.putalpha(alpha)
        enlarged = snapped

    return PreparedImage(
        image=enlarged,
        source_format=source_format,
        source_width=source_width,
        source_height=source_height,
        traced_width=enlarged.size[0],
        traced_height=enlarged.size[1],
        downscaled=downscaled,
        color_count=len(palette) if palette else None,
        palette=palette,
        has_transparency=_has_transparency(enlarged),
        supersample=_SUPERSAMPLE,
    )


def prepare(
    data: bytes, params: VectorizeParams, hard_max_pixels: int
) -> PreparedImage:
    """Decode and condition *data* according to *params*."""
    image, source_format = decode(data)
    source_width, source_height = image.size

    if source_width * source_height > hard_max_pixels:
        raise ImageTooLarge(
            f"Input is {source_width}x{source_height} "
            f"({source_width * source_height:,} pixels); the limit is "
            f"{hard_max_pixels:,}. Downscale it or raise "
            "VECTOR_MAX_INPUT_PIXELS."
        )

    image = _to_rgba(image)

    if params.output_background and params.output_background != "transparent":
        image = _flatten(image, params.output_background)

    budget = hard_max_pixels
    if params.input_max_pixels is not None:
        budget = min(budget, params.input_max_pixels)
    image, downscaled = _fit_within(image, budget)

    # Settle the colours before denoising, because that decides whether
    # denoising should happen at all. A median filter is the right tool when
    # the tracer will see raw pixels, and the wrong one when every pixel is
    # about to be mapped onto a known ink: it is redundant there, and it eats
    # anything a pixel or two wide, because a hairline is a minority in its
    # own window. On the test artwork the default median left the charcoal
    # outline around the lettering thick in places, thin in others and broken
    # into dashes -- while the same file with no median traced it as one even
    # line. So denoise only when the colours are staying as they are, or when
    # the caller asked for a level themselves.
    #
    # It stays on the ramp-collapsing path even though that path has its own,
    # gentler cleanup, because the two are not interchangeable: the collapse
    # is monotone and so cannot erase a thin feature, but it also cannot undo
    # the overshoot compression leaves along an edge, and snapping a bitmap
    # with that overshoot still in it doubled the shapes on a JPEG. The cost
    # is that a rule one pixel wide still loses most of its colour to the
    # median -- measured from (65, 72, 86) to (177, 179, 179) -- so hairlines
    # come out paler here than they do once a palette has pinned them.
    palette_rgbs = _resolve_palette(
        image,
        params.processing_max_colors,
        params.processing_palette,
        params.auto_palette,
    )
    plan = None
    if palette_rgbs is None or params.asked_for("processing_denoise"):
        image = _denoise(image, params.processing_denoise)
    if palette_rgbs is None:
        plan = _plan_soft_edges(image, params, _SUPERSAMPLE_MAX_PIXELS)

    original = image
    if palette_rgbs is None:
        palette = None
    else:
        image, palette = _quantize(image, palette_rgbs)

    finer = None
    if original.width * original.height * _SUPERSAMPLE**2 <= _SUPERSAMPLE_MAX_PIXELS:
        finer = _finer_copy(
            original,
            palette_rgbs,
            palette,
            plan=plan,
            source_format=source_format,
            source_width=source_width,
            source_height=source_height,
            downscaled=downscaled,
        )

    return PreparedImage(
        image=image,
        source_format=source_format,
        source_width=source_width,
        source_height=source_height,
        traced_width=image.size[0],
        traced_height=image.size[1],
        downscaled=downscaled,
        color_count=len(palette) if palette else None,
        palette=palette,
        has_transparency=_has_transparency(image),
        finer=finer,
    )


def _has_transparency(image: Image.Image) -> bool:
    """True only if the alpha channel actually carries non-opaque pixels."""
    if image.mode != "RGBA":
        return False
    minimum, _ = image.getchannel("A").getextrema()
    return minimum < 255


def to_png_bytes(image: Image.Image) -> bytes:
    """Re-encode for the tracer, which takes encoded bytes rather than pixels."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=1)
    return buffer.getvalue()
