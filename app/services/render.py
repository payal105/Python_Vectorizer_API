"""Output rendering: SVG -> PDF / EPS / PNG.

PDF and EPS stay fully vector — the traced paths become native path
operators, not an embedded bitmap — so the result scales losslessly and can
be opened and edited in Illustrator, Inkscape or Affinity.
"""

from __future__ import annotations

import io

from app.config import Settings
from app.core.errors import OutputTooLarge, RenderFailed, UnsupportedOutputFormat
from app.core.logging import get_logger
from app.schemas.params import VectorizeParams
from app.utils import units

logger = get_logger("render")

PRODUCER = "Python Vector API"


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

    drawing = _load_drawing(svg_bytes)
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

    drawing = _load_drawing(svg_bytes)

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
