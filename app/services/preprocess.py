"""Raster preparation.

Everything that happens to the bitmap *before* the tracer sees it. Getting
this stage right matters more for output quality than any tracer knob:
clean, quantized, correctly-oriented input produces far fewer stray paths.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

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
    smooth_boundaries: bool = False,
) -> tuple[Image.Image, list[str]]:
    """Map every pixel onto *rgbs*, preserving any alpha channel.

    Returns the conditioned image and the palette it was mapped onto, so the
    caller can pass that on and have the traced fills snapped to the same
    list. Pinning the pixels is only half the job: the tracer averages within
    each cluster, so boundary clusters still come back as blends.

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
    if smooth_boundaries:
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
# But resampling sharpens noise as readily as geometry. On a heavily
# compressed test image, where the tracer was already splitting one dark ring
# into two shades, the same treatment took 55 traced shapes to 154. Which way
# it goes cannot be predicted from the image, so it is measured instead: both
# are traced and the one that came out simpler is kept. See engine.trace.
_SUPERSAMPLE = 2
_SUPERSAMPLE_MAX_PIXELS = 12_000_000


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
    palette_rgbs = _resolve_palette(
        image,
        params.processing_max_colors,
        params.processing_palette,
        params.auto_palette,
    )
    if palette_rgbs is None or params.asked_for("processing_denoise"):
        image = _denoise(image, params.processing_denoise)

    finer = None
    if palette_rgbs is None:
        palette = None
    else:
        original = image
        image, palette = _quantize(image, palette_rgbs)
        pixels = original.width * original.height
        if pixels * _SUPERSAMPLE**2 <= _SUPERSAMPLE_MAX_PIXELS:
            enlarged, _ = _quantize(
                original.resize(
                    (original.width * _SUPERSAMPLE, original.height * _SUPERSAMPLE),
                    Image.Resampling.LANCZOS,
                ),
                palette_rgbs,
                smooth_boundaries=True,
            )
            finer = PreparedImage(
                image=enlarged,
                source_format=source_format,
                source_width=source_width,
                source_height=source_height,
                traced_width=enlarged.size[0],
                traced_height=enlarged.size[1],
                downscaled=downscaled,
                color_count=len(palette),
                palette=palette,
                has_transparency=_has_transparency(enlarged),
                supersample=_SUPERSAMPLE,
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
