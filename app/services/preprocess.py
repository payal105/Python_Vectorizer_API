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


def _palette_image(colors: list[str]) -> Image.Image:
    """Build the 256-entry palette image Pillow wants for fixed-palette quantize."""
    flat: list[int] = []
    for hex_color in colors:
        hexed = normalise_hex(hex_color)
        flat.extend(int(hexed[i : i + 2], 16) for i in (1, 3, 5))
    # Pillow requires a full 768-byte palette; pad by repeating the last entry.
    last = flat[-3:] if flat else [0, 0, 0]
    while len(flat) < 768:
        flat.extend(last)
    palette_img = Image.new("P", (1, 1))
    palette_img.putpalette(flat[:768])
    return palette_img


def _quantize(
    image: Image.Image, max_colors: int, palette: list[str] | None
) -> tuple[Image.Image, int | None]:
    """Reduce the colour count, preserving any alpha channel.

    Dithering is deliberately disabled: dither noise becomes thousands of
    tiny paths once traced.
    """
    if not palette and max_colors <= 0:
        return image, None

    alpha = image.getchannel("A") if image.mode == "RGBA" else None
    rgb = image.convert("RGB")

    if palette:
        quantized = rgb.quantize(
            palette=_palette_image(palette),
            dither=Image.Dither.NONE,
        )
        # Nearest-colour mapping goes wrong along edges. The pixels blending
        # a dark charcoal into a cream are, in RGB terms, closest to a sage
        # green -- so every letter picked up a scattered green fringe that
        # exists nowhere in the artwork, and traced as hundreds of slivers.
        # A mode filter over the palette indices replaces each pixel with the
        # commonest colour around it, which dissolves those thin wrong bands
        # into whichever real colour surrounds them while leaving solid
        # regions untouched.
        quantized = quantized.filter(ImageFilter.ModeFilter(3))
        used = len(palette)
    else:
        colors = max(2, min(256, max_colors))
        quantized = rgb.quantize(
            colors=colors,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.NONE,
        )
        used = colors

    result = quantized.convert("RGB")
    if alpha is not None:
        result = result.convert("RGBA")
        result.putalpha(alpha)
    return result, used


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

    image, color_count = _quantize(
        image, params.processing_max_colors, params.processing_palette
    )

    return PreparedImage(
        image=image,
        source_format=source_format,
        source_width=source_width,
        source_height=source_height,
        traced_width=image.size[0],
        traced_height=image.size[1],
        downscaled=downscaled,
        color_count=color_count,
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
