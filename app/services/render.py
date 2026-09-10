"""Output rendering: SVG -> PDF / EPS / PNG.

PDF and EPS stay fully vector — the traced paths become native path
operators, not an embedded bitmap — so the result scales losslessly and can
be opened and edited in Illustrator, Inkscape or Affinity.
"""

from __future__ import annotations

import io
import re

from app.config import Settings
from app.core.errors import OutputTooLarge, RenderFailed, UnsupportedOutputFormat
from app.core.logging import get_logger
from app.schemas.params import VectorizeParams
from app.utils import units

logger = get_logger("render")

PRODUCER = "Python Vector API"


_GRADIENT_RE = re.compile(
    rb'<(linear|radial)Gradient[^>]*id="([^"]+)"[^>]*>(.*?)</(?:linear|radial)Gradient>',
    re.S,
)
_STOP_RE = re.compile(
    rb'offset="([0-9.]+)"\s+stop-color="#([0-9a-fA-F]{6})"'
)


def _mean_along(stops: list[tuple[bytes, bytes]]) -> bytes:
    """The average colour a viewer would see across the whole ramp.

    Averaging the stop colours alone is only right when the stops are evenly
    spaced, and they deliberately are not: a stop is placed where the shading
    turns, so a ramp that spends most of its length in one colour carries most
    of its stops somewhere else. Weighting each stop by how much of the ramp
    it governs is what makes the stand-in the colour the gradient actually
    reads as.
    """
    offsets = [float(offset) for offset, _ in stops]
    colours = [[int(colour[i : i + 2], 16) for i in (0, 2, 4)] for _, colour in stops]
    if len(stops) == 1:
        return b"#%02x%02x%02x" % tuple(colours[0])

    total = 0.0
    accumulated = [0.0, 0.0, 0.0]
    for (start, first), (end, second) in zip(
        zip(offsets, colours), zip(offsets[1:], colours[1:])
    ):
        span = max(0.0, end - start)
        total += span
        for channel in range(3):
            accumulated[channel] += span * (first[channel] + second[channel]) / 2.0
    if total <= 0:
        average = [sum(c[i] for c in colours) / len(colours) for i in range(3)]
    else:
        average = [value / total for value in accumulated]
    return b"#%02x%02x%02x" % tuple(int(round(v)) for v in average)


def flatten_gradients(svg_bytes: bytes) -> bytes:
    """Replace every gradient fill with the average colour along it.

    Neither of reportlab's PNG and PostScript backends supports a gradient --
    they do not ignore one, they raise partway through drawing. Its PDF
    backend does, and writes a real shading, so PDF and SVG keep the gradient
    and the other two get the flat stand-in.
    """
    if b"Gradient" not in svg_bytes:
        return svg_bytes
    middles: dict[bytes, bytes] = {}
    for match in _GRADIENT_RE.finditer(svg_bytes):
        stops = _STOP_RE.findall(match.group(3))
        if not stops:
            continue
        middles[match.group(2)] = _mean_along(stops)
    if not middles:
        return svg_bytes
    for name, colour in middles.items():
        svg_bytes = svg_bytes.replace(b'"url(#' + name + b')"', b'"' + colour + b'"')
    return re.sub(rb"<defs>.*?</defs>", b"", svg_bytes, flags=re.S)


def _load_drawing(svg_bytes: bytes):
    """Parse SVG into a ReportLab drawing."""
    from svglib.svglib import svg2rlg

    try:
        drawing = svg2rlg(io.BytesIO(svg_bytes))
    except Exception as exc:
        logger.exception("svg parse for rendering failed")
        raise RenderFailed(f"The SVG could not be prepared for output: {exc}") from exc

    if drawing is None:
        raise RenderFailed("The SVG produced an empty drawing.")
    return drawing


def to_pdf(svg_bytes: bytes, title: str = "Vectorized image") -> bytes:
    """Render to a single-page PDF whose page box hugs the artwork."""
    from reportlab.graphics import renderPDF
    from reportlab.pdfgen import canvas as pdfcanvas

    drawing = _load_drawing(svg_bytes)
    buffer = io.BytesIO()
    try:
        pdf = pdfcanvas.Canvas(
            buffer,
            pagesize=(drawing.width, drawing.height),
            invariant=True,
        )
        pdf.setTitle(title)
        pdf.setCreator(PRODUCER)
        pdf.setProducer(PRODUCER)
        pdf.setSubject("Vector tracing of a raster image")
        renderPDF.draw(drawing, pdf, 0, 0)
        pdf.showPage()
        pdf.save()
    except Exception as exc:
        logger.exception("pdf render failed")
        raise RenderFailed(f"PDF generation failed: {exc}") from exc
    return buffer.getvalue()


def to_eps(svg_bytes: bytes) -> bytes:
    from reportlab.graphics import renderPS

    # renderPS has no gradient support either, and fails the same way renderPM
    # does. Only the PDF backend writes a real shading.
    drawing = _load_drawing(flatten_gradients(svg_bytes))
    try:
        data = renderPS.drawToString(drawing)
    except Exception as exc:
        logger.exception("eps render failed")
        raise RenderFailed(f"EPS generation failed: {exc}") from exc
    return data if isinstance(data, bytes) else data.encode("latin-1", "replace")


def to_png(
    svg_bytes: bytes,
    params: VectorizeParams,
    settings: Settings,
    transparent: bool = False,
) -> bytes:
    """Rasterize the *vector* result, so the PNG matches the vector exactly."""
    from reportlab.graphics import renderPM

    drawing = _load_drawing(flatten_gradients(svg_bytes))

    # drawing dimensions are points; renderPM scales by dpi/72, which lands on
    # exactly (css pixels x dpi/96) as intended.
    dpi = params.output_bitmap_dpi
    out_w = drawing.width * dpi / units.POINTS_PER_INCH
    out_h = drawing.height * dpi / units.POINTS_PER_INCH
    if out_w * out_h > settings.max_output_pixels:
        raise OutputTooLarge(
            f"The requested output would be {int(out_w)}x{int(out_h)} "
            f"({int(out_w * out_h):,} pixels); the limit is "
            f"{settings.max_output_pixels:,}. Lower output.bitmap.dpi or "
            "output.size."
        )

    try:
        if transparent:
            # backendFmt="RGBA" with no background gives a genuine alpha
            # channel; bg=None alone silently renders onto black.
            return renderPM.drawToString(
                drawing, fmt="PNG", dpi=dpi, bg=None, backendFmt="RGBA"
            )
        return renderPM.drawToString(drawing, fmt="PNG", dpi=dpi, bg=0xFFFFFF)
    except Exception as exc:
        if transparent:
            # Not every renderPM backend can do alpha; a white page beats a 500.
            logger.warning("transparent PNG unsupported, falling back to white: %s", exc)
            try:
                return renderPM.drawToString(drawing, fmt="PNG", dpi=dpi, bg=0xFFFFFF)
            except Exception as inner:  # pragma: no cover
                exc = inner
        logger.exception("png render failed")
        raise RenderFailed(f"PNG generation failed: {exc}") from exc


def render(
    svg_bytes: bytes,
    params: VectorizeParams,
    settings: Settings,
    title: str,
    source_has_alpha: bool = False,
) -> bytes:
    """Dispatch to the renderer for the requested output format."""
    fmt = params.output_file_format
    if fmt == "svg":
        return svg_bytes
    if fmt == "pdf":
        return to_pdf(svg_bytes, title=title)
    if fmt == "eps":
        return to_eps(svg_bytes)
    if fmt == "png":
        # A transparent source stays transparent unless the caller asked for a
        # background; SVG/PDF get this for free, PNG has to be told.
        transparent = params.output_background == "transparent" or (
            params.output_background is None and source_has_alpha
        )
        return to_png(svg_bytes, params, settings, transparent=transparent)
    raise UnsupportedOutputFormat(f"Unsupported output format: {fmt}.")
