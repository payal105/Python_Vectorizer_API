"""Raster preparation.

Everything that happens to the bitmap *before* the tracer sees it. Getting
this stage right matters more for output quality than any tracer knob:
clean, quantized, correctly-oriented input produces far fewer stray paths.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

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


# Two candidate colours closer than this are treated as the same ink when a
# palette is being derived. It has to clear the anti-aliasing shades sitting
# beside each ink without merging inks that are genuinely close: the test
# artwork pairs a #ffffff outline with a #fbf7da fill only 38 apart, and at 40
# the outline was swallowed and came back cream. 30 keeps them separate.
_MIN_INK_SEPARATION = 30.0


def _derive_palette(rgb: Image.Image, count: int) -> list[tuple[int, int, int]]:
    """Work out the real ink colours of flat artwork, to quantize onto.

    Quantizing straight to N is unreliable here, because the buckets are
    chosen by pixel count and a dominant background swallows the budget. On
    the test artwork ``max_colors=6`` returned five muddy greys and lost the
    white outline and the pink flower entirely; at 8 it spent half the budget
    on transition tones like ``#d4cbc0`` that exist only along an edge.

    So take a generous palette first and then keep the most-used entries that
    are far enough apart to be separate inks. What comes back is the artwork's
    own colours, which the blend ramps can then map pixels onto cleanly.
    """
    generous = rgb.quantize(
        colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
    )
    table = generous.getpalette() or []
    ranked = sorted(generous.getcolors(1 << 20) or [], key=lambda item: -item[0])

    chosen: list[tuple[int, int, int]] = []
    for _, index in ranked:
        entry = tuple(table[index * 3 : index * 3 + 3])
        if len(entry) < 3:
            continue
        if any(
            sum((a - b) ** 2 for a, b in zip(entry, prev)) ** 0.5
            < _MIN_INK_SEPARATION
            for prev in chosen
        ):
            continue
        chosen.append(entry)  # type: ignore[arg-type]
        if len(chosen) >= count:
            break
    return chosen


# Detection only has to identify colours, not resolve detail, so it runs on a
# reduced copy. Nearest-neighbour, because any smooth resampling would invent
# blends -- the exact thing being measured. 768 keeps a 1600px source's 5px
# outline about 2px wide, well clear of the floor below, for a quarter of the
# cost of working full size.
_DETECT_MAX_EDGE = 768

# An ink has to hold this share of the image *after* the mode filter below.
# Measured on the test artwork: the six real inks came in between 80.7% and
# 0.27%, while every transition tone fell to 0.03% or less.
_INK_SOLID_FLOOR = 0.002

# Above this many inks the artwork is continuous-tone, not flat, and is left
# alone. A gradient or a photograph runs to 90 or more.
_FLAT_MAX_INKS = 16

# ...and the inks kept have to account for essentially the whole image, or
# what was thrown away was real content rather than edge tones.
_FLAT_MIN_COVERAGE = 0.98

# How far the average pixel may sit from the ink it would be assigned. This is
# what tells a handful of inks apart from a handful of *bands cut through a
# gradient*, which no amount of counting can: both come back as a short list.
# On flat artwork almost every pixel lands on its ink and only the edge tones
# are far away, so the mean stays near zero -- measured at 0.00 for a PNG logo
# and 0.81 for a lettering JPEG. Shading spreads pixels evenly between the
# bands instead: 8.54 for a shaded sticker, 10.41 for a shaded sphere.
_FLAT_MAX_RESIDUAL = 3.0


def _detect_flat_palette(rgb: Image.Image) -> list[tuple[int, int, int]] | None:
    """Return the artwork's inks, or None if it is not flat artwork.

    Ranking candidate colours by pixel count cannot tell a real ink from the
    transition tone beside it: on the test artwork the band along the letter
    edges covered more of the image (0.50%) than the sage green of the flower
    leaves (0.27%), so any population threshold keeps the wrong one.

    What separates them is shape, not size. A transition tone is a thread one
    or two pixels wide and is always a minority in its own neighbourhood; an
    ink fills regions. So fold the near-duplicate shades together, run a mode
    filter over the result, and weigh each ink by what survives. The threads
    collapse to almost nothing, the inks keep their area, and the gap between
    them is an order of magnitude rather than a judgement call.

    Artwork that does not resolve to a handful of such inks is continuous-tone
    -- a photograph, a gradient -- and gets no palette at all, because
    flattening it to a dozen colours would be vandalism rather than cleanup.
    """
    work = rgb
    if max(work.size) > _DETECT_MAX_EDGE:
        scale = _DETECT_MAX_EDGE / max(work.size)
        work = work.resize(
            (max(1, int(work.width * scale)), max(1, int(work.height * scale))),
            Image.Resampling.NEAREST,
        )

    generous = work.quantize(
        colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
    )
    table = generous.getpalette() or []

    # Fold every bucket onto the nearest ink already seen, most-used first, so
    # an ink absorbs the anti-aliasing shades that belong to it.
    inks: list[tuple[int, int, int]] = []
    assign: dict[int, int] = {}
    for _, index in sorted(generous.getcolors(1 << 20) or [], key=lambda i: -i[0]):
        entry = tuple(table[index * 3 : index * 3 + 3])
        if len(entry) < 3:
            continue
        nearest, best = None, _MIN_INK_SEPARATION
        for position, ink in enumerate(inks):
            distance = sum((a - b) ** 2 for a, b in zip(entry, ink)) ** 0.5
            if distance < best:
                nearest, best = position, distance
        if nearest is None:
            assign[index] = len(inks)
            inks.append(entry)  # type: ignore[arg-type]
        else:
            assign[index] = nearest
    if not inks:
        return None

    ids = Image.frombytes(
        "P",
        generous.size,
        generous.tobytes().translate(bytes(assign.get(i, 0) for i in range(256))),
    )
    solid = [0] * len(inks)
    for count, index in ids.filter(ImageFilter.ModeFilter(5)).getcolors(1 << 20) or []:
        if index < len(inks):
            solid[index] = count

    total = ids.size[0] * ids.size[1]
    ranked = sorted(range(len(inks)), key=lambda k: -solid[k])
    kept = [k for k in ranked if solid[k] / total >= _INK_SOLID_FLOOR]
    coverage = sum(solid[k] for k in kept) / total

    if not kept or len(kept) > _FLAT_MAX_INKS or coverage < _FLAT_MIN_COVERAGE:
        return None

    # Weigh how far the artwork actually sits from the inks it would be
    # mapped onto. Cheap, because the buckets already stand in for the pixels.
    error = weight = 0.0
    for count, index in generous.getcolors(1 << 20) or []:
        entry = tuple(table[index * 3 : index * 3 + 3])
        if len(entry) < 3:
            continue
        ink = inks[assign.get(index, 0)]
        error += count * sum((a - b) ** 2 for a, b in zip(entry, ink)) ** 0.5
        weight += count
    if weight and error / weight > _FLAT_MAX_RESIDUAL:
        return None

    return [inks[k] for k in kept]


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


def _quantize(
    image: Image.Image,
    max_colors: int,
    palette: list[str] | None,
    auto: bool = False,
) -> tuple[Image.Image, list[str] | None]:
    """Reduce the colour count, preserving any alpha channel.

    Returns the conditioned image and the palette it was mapped onto, so the
    caller can pass that on and have the traced fills snapped to the same
    list. Pinning the pixels is only half the job: the tracer averages within
    each cluster, so boundary clusters still come back as blends.

    Dithering is deliberately disabled: dither noise becomes thousands of
    tiny paths once traced.
    """
    alpha = image.getchannel("A") if image.mode == "RGBA" else None
    rgb = image.convert("RGB")

    # A pinned palette is the caller's. A colour budget means derive that many
    # inks. With neither, look at the artwork: flat work gets its own inks
    # found for it, and anything continuous-tone is left exactly as it is.
    # Either way the pixels are then assigned the same way, so the automatic
    # result matches what naming the colours would have given.
    if palette:
        rgbs = _palette_rgbs(palette)
    elif max_colors > 0:
        rgbs = _derive_palette(rgb, max(2, min(256, max_colors)))
    elif auto:
        detected = _detect_flat_palette(rgb)
        if detected is None:
            return image, None
        rgbs = detected
    else:
        return image, None

    if not rgbs:
        return image, None

    probe, resolves_to = _blend_ramp(rgbs)
    quantized = rgb.quantize(palette=probe, dither=Image.Dither.NONE)
    # Collapse the ramp samples onto the palette entry each resolves to, so
    # what follows sees one index per palette colour and nothing else.
    # Translating the raw index bytes keeps this in C.
    quantized = Image.frombytes(
        "P", quantized.size, quantized.tobytes().translate(resolves_to)
    )
    quantized.putpalette(_flat_palette(rgbs))
    # The ramp settles which two colours an edge pixel lies between; what is
    # left is single-pixel disagreement about exactly where the boundary
    # falls, plus stray compression speckles. A mode filter over the palette
    # indices replaces each pixel with the commonest colour around it, which
    # tidies both while leaving solid regions untouched.
    quantized = quantized.filter(ImageFilter.ModeFilter(3))

    result = quantized.convert("RGB")
    if alpha is not None:
        result = result.convert("RGBA")
        result.putalpha(alpha)
    return result, ["#%02x%02x%02x" % entry for entry in rgbs]


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

    # Denoise before quantizing: quantization would otherwise lock the noise
    # into the palette it picks.
    image = _denoise(image, params.processing_denoise)

    image, palette = _quantize(
        image,
        params.processing_max_colors,
        params.processing_palette,
        auto=params.auto_palette,
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
