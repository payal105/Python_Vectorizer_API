"""End-to-end tests for the vectorize endpoint."""

from __future__ import annotations

import base64
import io
import re

import pytest
from PIL import Image

from tests.conftest import AUTH, jpeg_bytes, make_image, png_bytes

ENDPOINT = "/api/v1/vectorize"


def post(client, data: bytes | None = None, *, auth=AUTH, name="sample.png", **fields):
    return client.post(
        ENDPOINT,
        files={"image": (name, data or png_bytes(), "image/png")},
        data=fields,
        auth=auth,
    )


# --- Output formats ----------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "media_type", "magic"),
    [
        ("svg", "image/svg+xml", b"<?xml"),
        ("pdf", "application/pdf", b"%PDF"),
        ("eps", "application/postscript", b"%!PS"),
        ("png", "image/png", b"\x89PNG"),
    ],
)
def test_each_output_format(client, sample_png, fmt, media_type, magic):
    response = post(client, sample_png, **{"output.file_format": fmt})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(media_type)
    assert response.content.startswith(magic)
    assert int(response.headers["X-Shape-Count"]) > 0


def test_svg_is_the_default_format(client, sample_png):
    response = post(client, sample_png)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


def test_pdf_is_vector_not_an_embedded_bitmap(client, sample_png):
    response = post(client, sample_png, **{"output.file_format": "pdf"})
    body = response.content
    assert b"/Subtype /Image" not in body and b"/Subtype/Image" not in body


def test_jpeg_input_is_accepted(client):
    response = post(client, jpeg_bytes(), name="photo.jpg")
    assert response.status_code == 200
    assert response.headers["X-Source-Width"] == "240"


def test_content_disposition_keeps_the_original_stem(client, sample_png):
    response = post(
        client, sample_png, name="My Logo v2.png", **{"output.file_format": "pdf"}
    )
    assert 'filename="My-Logo-v2.pdf"' in response.headers["content-disposition"]


def test_file_is_served_as_an_attachment(client, sample_png):
    """Swagger UI only shows its "Download file" link for attachments, and
    browsers try to render "inline" responses instead of saving them."""
    response = post(client, sample_png, **{"output.file_format": "pdf"})
    assert response.headers["content-disposition"].startswith("attachment;")


def test_json_envelope_is_not_labelled_as_a_file(client, sample_png):
    response = client.post(
        ENDPOINT,
        files={"image": ("a.png", sample_png, "image/png")},
        data={"output.file_format": "pdf"},
        headers={"Accept": "application/json"},
        auth=AUTH,
    )
    assert response.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in response.headers


# --- Image input variants ----------------------------------------------------


def test_base64_input(client, sample_png_b64):
    response = client.post(ENDPOINT, data={"image.base64": sample_png_b64}, auth=AUTH)
    assert response.status_code == 200, response.text


def test_data_url_input(client, sample_png_b64):
    response = client.post(
        ENDPOINT,
        data={"image.base64": f"data:image/png;base64,{sample_png_b64}"},
        auth=AUTH,
    )
    assert response.status_code == 200, response.text


def test_json_body_input(client, sample_png_b64):
    response = client.post(
        ENDPOINT,
        json={"image.base64": sample_png_b64, "output.file_format": "pdf"},
        auth=AUTH,
    )
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_missing_image_is_rejected(client):
    response = client.post(ENDPOINT, data={"mode": "test"}, auth=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == 1001


def test_two_images_are_rejected(client, sample_png, sample_png_b64):
    response = client.post(
        ENDPOINT,
        files={"image": ("a.png", sample_png, "image/png")},
        data={"image.base64": sample_png_b64},
        auth=AUTH,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == 1002


def test_undecodable_image_is_rejected(client):
    response = post(client, b"this is definitely not a png")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == 1003


# --- Parameters --------------------------------------------------------------


def test_unknown_parameter_is_rejected_with_a_helpful_message(client, sample_png):
    response = post(client, sample_png, **{"output.file_formatt": "pdf"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == 1006
    assert "output.file_formatt" in error["message"]


def test_out_of_range_parameter_is_rejected(client, sample_png):
    response = post(client, sample_png, **{"processing.max_colors": "9999"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == 1006


def test_scale_and_explicit_width_conflict(client, sample_png):
    response = post(
        client, sample_png, **{"output.size.scale": "2", "output.size.width": "100"}
    )
    assert response.status_code == 400
    assert "scale" in response.json()["error"]["message"]


def test_query_string_parameters_are_honoured(client, sample_png):
    response = client.post(
        ENDPOINT + "?output.file_format=pdf",
        files={"image": ("a.png", sample_png, "image/png")},
        auth=AUTH,
    )
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


# --- Geometry ----------------------------------------------------------------


def test_scale_multiplies_the_output_size(client, sample_png):
    response = post(client, sample_png, **{"output.size.scale": "2"})
    assert response.headers["X-Image-Width"] == "480.0"
    assert response.headers["X-Image-Height"] == "360.0"


def test_width_in_inches_sets_the_pdf_page_size(client, sample_png):
    # 5in = 480 CSS px = 360 pt, and the 240x180 source keeps its 4:3 ratio.
    response = post(
        client,
        sample_png,
        **{
            "output.file_format": "pdf",
            "output.size.width": "5",
            "output.size.unit": "in",
        },
    )
    assert response.status_code == 200
    media_box = re.search(rb"/MediaBox\s*\[([^\]]*)\]", response.content)
    assert media_box is not None
    width, height = (float(v) for v in media_box.group(1).split()[2:4])
    assert width == pytest.approx(360.0, abs=0.5)
    assert height == pytest.approx(270.0, abs=0.5)


def test_svg_carries_a_viewbox_so_it_scales_cleanly(client, sample_png):
    """The viewBox stays in tracer pixel space and the width carries the
    requested size, so scaling never touches the path data. Flat artwork may
    be traced at a multiple of its own resolution, which shows up in the
    viewBox and must not change the size the document reports."""
    response = post(client, sample_png, **{"output.size.scale": "3"})
    box = re.search(rb'viewBox="0 0 (\d+) (\d+)"', response.content)
    assert box is not None
    width, height = int(box.group(1)), int(box.group(2))
    assert width % 240 == 0 and height % 180 == 0, (width, height)
    assert width // 240 == height // 180, (width, height)
    assert b'width="720"' in response.content
    assert b'height="540"' in response.content


def test_png_dpi_controls_the_raster_size(client, sample_png):
    response = post(
        client, sample_png, **{"output.file_format": "png", "output.bitmap.dpi": "192"}
    )
    image = Image.open(io.BytesIO(response.content))
    assert image.size == (480, 360)  # 2x the 96dpi baseline


# --- Colour and style --------------------------------------------------------


def test_max_colors_limits_the_palette(client, sample_png):
    response = post(client, sample_png, **{"processing.max_colors": "3"})
    fills = set(re.findall(rb'fill="(#[0-9a-fA-F]{6})"', response.content))
    assert 0 < len(fills) <= 3


def test_explicit_palette_is_respected(client, sample_png):
    response = post(
        client, sample_png, **{"processing.palette": "#ff0000,#00ff00,#0000ff"}
    )
    fills = {f.lower() for f in re.findall(rb'fill="(#[0-9a-fA-F]{6})"', response.content)}
    assert fills <= {b"#ff0000", b"#00ff00", b"#0000ff"}


def test_group_by_color_emits_groups(client, sample_png):
    response = post(client, sample_png, **{"output.group_by": "color"})
    assert b"<g " in response.content
    assert b"data-color=" in response.content


def test_stroke_draw_style_unfills_shapes(client, sample_png):
    response = post(
        client,
        sample_png,
        **{"output.draw_style": "stroke_shapes", "output.shapes.stroke_width": "2"},
    )
    assert b'fill="none"' in response.content
    assert b'stroke-width="2"' in response.content


def test_gap_filler_can_be_disabled(client, sample_png):
    on = post(client, sample_png, **{"output.gap_filler.enabled": "true"})
    off = post(client, sample_png, **{"output.gap_filler.enabled": "false"})
    assert b"stroke-width" in on.content
    assert b"stroke-width" not in off.content


def test_background_is_painted_when_requested(client, sample_png):
    response = post(client, sample_png, **{"output.background": "#ff00ff"})
    assert b'<rect' in response.content
    assert b'fill="#ff00ff"' in response.content


def _transparent_png() -> bytes:
    from PIL import ImageDraw

    image = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse([20, 20, 140, 140], fill=(200, 30, 90, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_transparent_input_stays_transparent_in_png(client):
    response = post(
        client, _transparent_png(), **{"output.file_format": "png"}
    )
    image = Image.open(io.BytesIO(response.content))
    assert image.mode == "RGBA"
    assert image.getpixel((2, 2))[3] == 0


def test_explicit_background_flattens_a_transparent_input(client):
    response = post(
        client,
        _transparent_png(),
        **{"output.file_format": "png", "output.background": "#ffffff"},
    )
    image = Image.open(io.BytesIO(response.content))
    assert image.mode == "RGB"
    assert image.getpixel((2, 2)) == (255, 255, 255)


def test_opaque_input_gets_a_white_png_background(client, sample_png):
    response = post(client, sample_png, **{"output.file_format": "png"})
    image = Image.open(io.BytesIO(response.content))
    assert image.mode == "RGB"


def test_binary_mode_traces_a_silhouette(client, sample_png):
    response = post(client, sample_png, **{"processing.color_mode": "binary"})
    assert response.status_code == 200
    assert int(response.headers["X-Shape-Count"]) > 0


# --- Modes, credits and metadata --------------------------------------------


def test_test_mode_is_free_and_watermarked(client, sample_png):
    response = post(client, sample_png, mode="test")
    assert response.status_code == 200
    assert response.headers["X-Credits-Charged"] == "0.00"
    assert b'id="watermark"' in response.content
    assert b">TEST<" in response.content


def test_preview_mode_costs_a_fraction(client, sample_png):
    response = post(client, sample_png, mode="preview")
    assert response.headers["X-Credits-Charged"] == "0.20"
    assert b'id="watermark"' in response.content


def test_production_mode_is_clean_and_billed(client, sample_png):
    response = post(client, sample_png, mode="production")
    assert response.headers["X-Credits-Charged"] == "1.00"
    assert b'id="watermark"' not in response.content


def test_credits_decrease_across_requests(client, sample_png):
    first = post(client, sample_png)
    second = post(client, sample_png)
    assert float(first.headers["X-Credits-Balance"]) - float(
        second.headers["X-Credits-Balance"]
    ) == pytest.approx(1.0)


def test_response_headers_describe_the_result(client, sample_png):
    response = post(client, sample_png)
    assert response.headers["X-Source-Width"] == "240"
    assert response.headers["X-Source-Height"] == "180"
    assert response.headers["X-Engine"] == "vtracer"
    assert float(response.headers["X-Processing-Ms"]) >= 0
    assert response.headers["X-Request-Id"]


def test_json_envelope_when_requested(client, sample_png):
    response = client.post(
        ENDPOINT,
        files={"image": ("a.png", sample_png, "image/png")},
        data={"output.file_format": "pdf"},
        headers={"Accept": "application/json"},
        auth=AUTH,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["image"]["format"] == "pdf"
    assert base64.b64decode(payload["image"]["base64"]).startswith(b"%PDF")
    assert payload["meta"]["engine"] == "vtracer"
    assert payload["meta"]["timings_ms"]["total"] >= 0
    assert payload["credits"]["charged"] == 1.0


# --- Limits ------------------------------------------------------------------


def test_input_pixel_ceiling_is_enforced(client):
    huge = png_bytes(Image.new("RGB", (2100, 2100), "white"))
    response = post(client, huge)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == 1004


def test_input_max_pixels_downscales_instead_of_failing(client):
    source = png_bytes(make_image(800, 600))
    response = post(client, source, **{"input.max_pixels": "20000"})
    assert response.status_code == 200
    # The traced geometry shrinks, but the reported source size does not.
    assert response.headers["X-Source-Width"] == "800"
    assert float(response.headers["X-Image-Width"]) < 800


# --- Smoothing ---------------------------------------------------------------


def test_smoothing_presets_all_produce_output(client, sample_png):
    for level in ("none", "low", "medium", "high"):
        response = post(client, sample_png, **{"processing.smoothing": level})
        assert response.status_code == 200, f"{level}: {response.text[:200]}"
        assert int(response.headers["X-Shape-Count"]) > 0


def test_smoothing_rounds_more_corners_as_it_rises(client, sample_png):
    """Higher presets raise corner_threshold, so fewer bends stay sharp."""
    from app.schemas.params import VectorizeParams
    from app.services.engine import _tracer_kwargs

    thresholds = [
        _tracer_kwargs(
            VectorizeParams.model_validate({"processing.smoothing": level}),
            colours_are_pinned=False,
        )["corner_threshold"]
        for level in ("none", "low", "medium", "high")
    ]
    assert thresholds == sorted(thresholds)
    assert thresholds[0] < thresholds[-1]


def test_smoothing_never_touches_detail_removing_knobs(client):
    """filter_speckle deletes small shapes outright -- it erased 12px text in
    testing -- so no smoothing preset may move it or layer_difference."""
    from app.schemas.params import VectorizeParams
    from app.services.engine import _tracer_kwargs

    speckles, layer_diffs = set(), set()
    for level in ("none", "low", "medium", "high"):
        kwargs = _tracer_kwargs(
            VectorizeParams.model_validate({"processing.smoothing": level}),
            colours_are_pinned=False,
        )
        speckles.add(kwargs["filter_speckle"])
        layer_diffs.add(kwargs["layer_difference"])
    assert len(speckles) == 1
    assert len(layer_diffs) == 1


def test_explicit_parameter_overrides_the_smoothing_preset(client):
    from app.schemas.params import VectorizeParams
    from app.services.engine import _tracer_kwargs

    kwargs = _tracer_kwargs(
        VectorizeParams.model_validate(
            {"processing.smoothing": "high", "processing.corner_threshold": "20"}
        ),
        colours_are_pinned=False,
    )
    assert kwargs["corner_threshold"] == 20


# --- Denoise -----------------------------------------------------------------


def _noisy_artwork() -> bytes:
    """Sticker-style art with shading and JPEG artefacts.

    Compression noise around the outlines creates hundreds of near-duplicate
    colours, which the tracer would otherwise emit as ragged slivers.
    """
    from PIL import ImageDraw, ImageFilter

    scale, width, height = 3, 320, 260
    image = Image.new("RGB", (width * scale, height * scale), (150, 150, 150))
    draw = ImageDraw.Draw(image)
    for inset, colour in ((0, (255, 255, 255)), (16, (40, 32, 38)), (30, (224, 192, 202))):
        draw.rounded_rectangle(
            [(40 + inset) * scale, (30 + inset) * scale,
             (280 - inset) * scale, (230 - inset) * scale],
            radius=(60 - inset) * scale,
            fill=colour,
        )
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    shade = Image.new("L", (width, height))
    shade_draw = ImageDraw.Draw(shade)
    for y in range(height):
        shade_draw.line([(0, y), (width, y)], fill=int(255 * (0.35 + 0.5 * y / height)))
    image = Image.composite(
        image,
        Image.new("RGB", image.size, (110, 90, 100)),
        shade.filter(ImageFilter.GaussianBlur(12)),
    )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=72)
    return buffer.getvalue()


def test_denoise_defaults_to_low():
    from app.schemas.params import VectorizeParams

    assert VectorizeParams().processing_denoise == "low"


def test_denoise_collapses_compression_noise(client):
    """The headline fix for chunky, notched edges."""
    noisy = _noisy_artwork()
    stacked = {"processing.hierarchical": "stacked"}
    raw = post(client, noisy, name="art.jpg", **{"processing.denoise": "none"}, **stacked)
    cleaned = post(client, noisy, name="art.jpg", **{"processing.denoise": "low"}, **stacked)

    raw_paths = int(raw.headers["X-Shape-Count"])
    clean_paths = int(cleaned.headers["X-Shape-Count"])
    # Each sliver of noise is its own path, so the drop is dramatic.
    assert clean_paths < raw_paths / 2, (raw_paths, clean_paths)


def test_every_denoise_level_beats_none(client):
    """Path counts are not monotonic across levels -- a wider window can split
    a region as easily as merge one -- but every level collapses the noise."""
    noisy = _noisy_artwork()
    counts = {
        level: int(
            post(client, noisy, name="a.jpg", **{"processing.denoise": level},
                 **{"processing.hierarchical": "stacked"})
            .headers["X-Shape-Count"]
        )
        for level in ("none", "low", "medium", "high")
    }
    for level in ("low", "medium", "high"):
        assert counts[level] < counts["none"] / 2, counts


def test_denoise_skips_images_too_small_for_the_window(client):
    """A 7px window on a tiny image would erase content, not noise."""
    from app.services.preprocess import _denoise

    tiny = Image.new("RGB", (20, 20), "white")
    assert _denoise(tiny, "high") is tiny


def test_denoise_preserves_alpha(client):
    from app.services.preprocess import _denoise

    image = Image.new("RGBA", (120, 120), (255, 0, 0, 0))
    image.putalpha(Image.new("L", (120, 120), 128))
    result = _denoise(image, "low")
    assert result.mode == "RGBA"
    assert result.getchannel("A").getextrema() == (128, 128)


# --- Detail ------------------------------------------------------------------


def _detail_card(size: int = 200) -> bytes:
    """A 2px rule and 3/5/7px dots: features right at the discard threshold."""
    from PIL import ImageDraw

    big = Image.new("RGB", (size * 4, size * 4), "white")
    draw = ImageDraw.Draw(big)
    draw.ellipse([15 * 4, 15 * 4, 105 * 4, 105 * 4], fill=(200, 30, 90))
    draw.line([(15 * 4, 125 * 4), (185 * 4, 125 * 4)], fill=(20, 110, 200), width=2 * 4)
    for i, radius in enumerate((3, 5, 7)):
        left = (120 + i * 22) * 4
        draw.ellipse([left, 160 * 4, left + radius * 4, (160 + radius) * 4],
                     fill=(20, 160, 90))
    buffer = io.BytesIO()
    big.resize((size, size), Image.Resampling.LANCZOS).save(buffer, format="PNG")
    return buffer.getvalue()


def test_detail_defaults_to_standard():
    from app.schemas.params import VectorizeParams

    assert VectorizeParams().processing_detail == "standard"


def test_detail_presets_trade_speckle_for_precision():
    from app.schemas.params import VectorizeParams
    from app.services.engine import _tracer_kwargs

    speckles, precisions = [], []
    for level in ("low", "standard", "high", "maximum"):
        kwargs = _tracer_kwargs(
            VectorizeParams.model_validate({"processing.detail": level}),
            colours_are_pinned=False,
        )
        speckles.append(kwargs["filter_speckle"])
        precisions.append(kwargs["path_precision"])
    # Discard threshold falls as precision rises.
    assert speckles == sorted(speckles, reverse=True)
    assert precisions == sorted(precisions)
    assert speckles[-1] == 0


def test_maximum_detail_keeps_features_standard_discards(client):
    card = _detail_card()
    stacked = {"processing.hierarchical": "stacked"}
    standard = post(client, card, **{"processing.detail": "standard"}, **stacked)
    maximum = post(client, card, **{"processing.detail": "maximum"}, **stacked)
    # The 2px rule and the smallest dot only survive at maximum.
    assert int(maximum.headers["X-Shape-Count"]) > int(standard.headers["X-Shape-Count"]) * 10


def test_explicit_min_area_overrides_the_detail_preset():
    from app.schemas.params import VectorizeParams
    from app.services.engine import _tracer_kwargs

    kwargs = _tracer_kwargs(
        VectorizeParams.model_validate(
            {"processing.detail": "maximum", "processing.shapes.min_area_px": "6"}
        ),
        colours_are_pinned=False,
    )
    assert kwargs["filter_speckle"] == 6


# --- Colour merge ------------------------------------------------------------


def _fills(svg: bytes) -> set[bytes]:
    return set(re.findall(rb'fill="(#[0-9a-fA-F]{6})"', svg))


def _banded_artwork() -> bytes:
    """Outlined shapes over a strong gradient, saved at low JPEG quality.

    The gradient plus compression makes the tracer split each region into many
    near-identical fills -- the condition color_merge exists to clean up.
    """
    from PIL import ImageDraw, ImageFilter

    scale, width, height = 3, 340, 260
    image = Image.new("RGB", (width * scale, height * scale), (150, 150, 150))
    draw = ImageDraw.Draw(image)
    for cx in (110, 240):
        for inset, colour in (
            (0, (250, 250, 250)), (14, (45, 35, 42)), (26, (222, 180, 192))
        ):
            draw.rounded_rectangle(
                [(cx - 70 + inset) * scale, (30 + inset) * scale,
                 (cx + 70 - inset) * scale, (230 - inset) * scale],
                radius=(70 - inset) * scale,
                fill=colour,
            )
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    shade = Image.new("L", (width, height))
    shade_draw = ImageDraw.Draw(shade)
    for y in range(height):
        shade_draw.line([(0, y), (width, y)], fill=int(255 * (0.45 + 0.4 * y / height)))
    image = Image.composite(
        image,
        Image.new("RGB", image.size, (125, 105, 115)),
        shade.filter(ImageFilter.GaussianBlur(16)),
    )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=65)
    return buffer.getvalue()


def _built(art: bytes, **overrides) -> bytes:
    """Run the real chain but stop short of fitting gradients.

    The banded fixture is a gradient by construction, so end to end its fills
    are replaced by a linearGradient and there is nothing left to count. This
    keeps the measurement on the stage under test.
    """
    from app.schemas.params import VectorizeParams
    from app.services import engine, preprocess, svgdoc

    params = VectorizeParams.model_validate(dict(overrides))
    prepared = preprocess.prepare(art, params, 4_000_000)
    traced = engine.trace(prepared, params)
    used = traced.prepared or prepared
    svg, _ = svgdoc.build(
        traced.svg,
        params,
        used.traced_width,
        used.traced_height,
        palette=used.palette,
        supersample=used.supersample,
    )
    return svg


def test_color_merge_collapses_near_duplicate_fills():
    """Anti-aliasing between two flat regions leaves transition shapes, often
    split into fills a single RGB unit apart. No tracer setting merges those."""
    art = _banded_artwork()
    before = _fills(_built(art, **{"processing.color_merge": "0"}))
    after = _fills(_built(art, **{"processing.color_merge": "40"}))
    # The fixture reliably produces pairs like #4c3e46 / #4c3e47.
    assert len(before) > 5, f"fixture should exercise the merge, got {before}"
    assert len(after) < len(before), (before, after)


def test_color_merge_zero_disables_it(client):
    from app.services.svgdoc import _merge_similar_colors

    assert _merge_similar_colors([], 0) == 0


def test_color_merge_snaps_minor_colours_onto_prominent_ones():
    from lxml import etree

    from app.services.svgdoc import _merge_similar_colors

    ns = "http://www.w3.org/2000/svg"
    # A big square, plus a sliver one RGB unit away from its colour.
    big = etree.Element(f"{{{ns}}}path")
    big.set("d", "M0 0 L100 0 L100 100 L0 100 Z")
    big.set("fill", "#4d3e48")
    sliver = etree.Element(f"{{{ns}}}path")
    sliver.set("d", "M0 0 L100 0 L100 2 L0 2 Z")
    sliver.set("fill", "#4d3f48")

    assert _merge_similar_colors([big, sliver], 16) == 1
    assert sliver.get("fill") == "#4d3e48"
    assert big.get("fill") == "#4d3e48"


def test_color_merge_ranks_by_covered_area_not_bounding_box():
    """A thin ring's bounding box is as big as the shape it surrounds, so
    ranking by box would merge the fill into the ring rather than vice versa."""
    from lxml import etree

    from app.services.svgdoc import _filled_area

    ns = "http://www.w3.org/2000/svg"
    ring = etree.Element(f"{{{ns}}}path")
    ring.set("d", "M0 0 L100 0 L100 3 L0 3 Z")
    block = etree.Element(f"{{{ns}}}path")
    block.set("d", "M10 10 L90 10 L90 90 L10 90 Z")
    assert _filled_area(block) > _filled_area(ring)


def test_color_merge_keeps_the_gap_filler_stroke_in_sync(client):
    """The gap filler strokes each shape in its own fill colour; a remapped
    fill with a stale stroke would draw a halo in the old colour."""
    response = post(client, _noisy_artwork(), name="a.jpg",
                    **{"processing.color_merge": "40"})
    from lxml import etree

    root = etree.fromstring(response.content)
    for path in root.iter("{http://www.w3.org/2000/svg}path"):
        stroke = path.get("stroke")
        if stroke and stroke != "none":
            assert stroke == path.get("fill")


# --- Combining paths into editable objects -----------------------------------


def _objects(svg: bytes) -> int:
    return len(re.findall(rb"<path\b", svg))


def test_combining_collapses_fragments_into_objects(client):
    """A real file opened in CorelDRAW as "Group of 761 Objects", which is
    unusable for selecting or recolouring."""
    art = _banded_artwork()
    common = {"processing.max_colors": "6", "processing.hierarchical": "cutout"}
    off = post(client, art, name="a.jpg", **common,
               **{"output.combine_paths": "none"})
    on = post(client, art, name="a.jpg", **common)
    assert _objects(on.content) < _objects(off.content)
    # Shapes are preserved as subpaths, only the element count drops.
    assert int(on.headers["X-Shape-Count"]) > _objects(on.content)


def test_separate_glyphs_stay_separate_objects(client):
    """The whole point of 'shapes' over 'colors': merging a colour outright
    turns a word into one object you cannot pick letters out of."""
    from PIL import ImageDraw

    image = Image.new("RGB", (400, 140), "white")
    draw = ImageDraw.Draw(image)
    for left in (20, 160, 300):  # three well-separated same-colour blobs
        draw.ellipse([left, 30, left + 70, 100], fill=(20, 20, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    art = buffer.getvalue()

    shapes = post(client, art, **{"output.combine_paths": "shapes"})
    colors = post(client, art, **{"output.combine_paths": "colors"})
    assert _objects(shapes.content) > _objects(colors.content)
    assert shapes.headers["X-Combined-Paths"] == "shapes"


def test_touching_fragments_group_but_distant_ones_do_not():
    from lxml import etree

    from app.services.svgdoc import _touching_groups

    ns = "http://www.w3.org/2000/svg"

    def path(d: str):
        element = etree.Element(f"{{{ns}}}path")
        element.set("d", d)
        return element

    a = path("M0 0L10 0L10 10L0 10Z")
    b = path("M9 0L20 0L20 10L9 10Z")     # overlaps a
    far = path("M500 0L510 0L510 10L500 10Z")
    groups = _touching_groups([a, b, far])
    assert len(groups) == 2
    assert sorted(len(g) for g in groups) == [1, 2]


def test_default_is_shapes_on_cutout():
    from app.schemas.params import VectorizeParams

    params = VectorizeParams()
    assert params.output_combine_paths == "shapes"
    assert params.processing_hierarchical == "cutout"


def test_stacked_turns_combining_off_instead_of_failing(client, sample_png):
    """Must not be an error: Swagger's "Try it out" echoes every default back,
    so clients routinely send stacked and a combine mode together without
    meaning anything by it. Correct rendering wins, quietly."""
    response = post(
        client,
        sample_png,
        **{"processing.hierarchical": "stacked", "output.combine_paths": "colors"},
    )
    assert response.status_code == 200
    assert response.headers["X-Combined-Paths"] == "none"


def test_combining_reports_the_mode_in_a_header(client, sample_png):
    response = post(client, sample_png)
    assert response.headers["X-Combined-Paths"] == "shapes"


def test_combined_paths_carry_no_leftover_transform(client):
    """Each traced shape has its own translate(); combining has to bake those
    into the coordinates or the shapes land in the wrong place."""
    from lxml import etree

    # Needs artwork where a colour appears in more than one shape; the simple
    # fixture has exactly one shape per colour, so nothing would merge.
    response = post(client, _banded_artwork(), name="a.jpg",
                    **{"processing.max_colors": "6"})
    root = etree.fromstring(response.content)
    combined = [
        p
        for p in root.iter("{http://www.w3.org/2000/svg}path")
        if p.get("fill-rule") == "evenodd"  # the marker left on merged paths
    ]
    assert combined, "expected at least one merged path"
    for path in combined:
        assert path.get("transform") is None


def test_shift_path_data_bakes_the_offset():
    from app.services.svgdoc import _shift_path_data

    assert _shift_path_data("M0 0L10 5Z", 3, 4) == "M3 4L13 9Z"
    # Relative commands are left alone rather than silently mangled.
    assert _shift_path_data("m0 0l10 5z", 3, 4) is None


def test_combine_paths_accepts_boolean_spellings(client, sample_png):
    """The field was a bool before it was an enum, and clients cache schemas:
    a stale Swagger page keeps submitting `true` long after the change."""
    from app.schemas.params import VectorizeParams

    for truthy in ("true", "True", "1", "yes", "on"):
        params = VectorizeParams.model_validate({"output.combine_paths": truthy})
        assert params.output_combine_paths == "shapes", truthy
    for falsy in ("false", "False", "0", "no", "off"):
        params = VectorizeParams.model_validate({"output.combine_paths": falsy})
        assert params.output_combine_paths == "none", falsy

    # Over the wire, not just in the model.
    response = post(client, sample_png, **{"output.combine_paths": "true"})
    assert response.status_code == 200
    assert response.headers["X-Combined-Paths"] == "shapes"


def test_combine_paths_still_rejects_nonsense(client, sample_png):
    response = post(client, sample_png, **{"output.combine_paths": "banana"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == 1006


# --- Pinned palette ----------------------------------------------------------


def test_pinned_palette_is_honoured_exactly(client, sample_png):
    """The tracer averages the pixels in each cluster, so boundary clusters
    come back as blends: pinning six colours used to yield eighteen, and the
    extras showed up in an editor as ghost layers beside the real shapes."""
    palette = "#666666,#fbf7da,#2d2c2a,#f09ec2,#c7cda0,#ffffff"
    response = post(client, _banded_artwork(), name="a.jpg",
                    **{"processing.palette": palette})
    assert response.status_code == 200
    produced = {f.decode().lower() for f in _fills(response.content)}
    assert produced <= set(palette.split(",")), produced


def test_snap_to_palette_picks_the_nearest_colour():
    from lxml import etree

    from app.services.svgdoc import _snap_to_palette

    ns = "http://www.w3.org/2000/svg"
    path = etree.Element(f"{{{ns}}}path")
    path.set("d", "M0 0L10 0L10 10Z")
    path.set("fill", "#f2f0f1")          # a near-white blend
    path.set("stroke", "#f2f0f1")        # gap filler tracks the fill
    _snap_to_palette([path], ["#ffffff", "#666666"])
    assert path.get("fill") == "#ffffff"
    assert path.get("stroke") == "#ffffff"


def test_snap_to_palette_leaves_exact_matches_alone():
    from lxml import etree

    from app.services.svgdoc import _snap_to_palette

    ns = "http://www.w3.org/2000/svg"
    path = etree.Element(f"{{{ns}}}path")
    path.set("d", "M0 0L10 0L10 10Z")
    path.set("fill", "#666666")
    assert _snap_to_palette([path], ["#ffffff", "#666666"]) == 0
    assert path.get("fill") == "#666666"


def test_pinned_palette_disables_denoising_by_default():
    """Median denoising and a pinned palette fight each other: the filter
    widens a boundary into a ramp of intermediate tones, and a wider ramp
    eats thin features from both sides. On the test artwork denoising cost
    3.7k pixels of the white outline around the lettering."""
    from app.schemas.params import VectorizeParams

    pinned = VectorizeParams.model_validate({"processing.palette": "#ffffff,#000000"})
    assert pinned.processing_denoise == "none"
    # An explicit choice still wins.
    forced = VectorizeParams.model_validate(
        {"processing.palette": "#ffffff,#000000", "processing.denoise": "medium"}
    )
    assert forced.processing_denoise == "medium"
    # Unpinned work is unaffected.
    assert VectorizeParams().processing_denoise == "low"


def test_pinned_palette_does_not_bleed_a_colour_across_the_image(client):
    """The failure this guards: a palette colour that sits between two others
    in RGB gets painted along every boundary between them."""
    from PIL import ImageDraw

    # Charcoal text on cream, plus a small sage mark. Sage sits near the
    # charcoal/cream midpoint, exactly the trap.
    image = Image.new("RGB", (400, 300), "#fbf7da")
    draw = ImageDraw.Draw(image)
    draw.ellipse([40, 40, 260, 260], fill="#2d2c2a")
    draw.ellipse([300, 120, 360, 180], fill="#c7cda0")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)

    response = post(
        client,
        buffer.getvalue(),
        name="a.jpg",
        **{"processing.palette": "#fbf7da,#2d2c2a,#c7cda0"},
    )
    assert response.status_code == 200
    sage_paths = re.findall(rb'fill="#c7cda0"', response.content)
    # One sage mark in the artwork; a fringe would produce many more.
    assert len(sage_paths) <= 3, len(sage_paths)


def test_edge_blend_resolves_to_one_of_the_two_colours_it_lies_between():
    """Nearest-colour mapping is wrong along an anti-aliased edge.

    A pixel halfway between the charcoal stroke and the cream fill is
    (148, 146, 130), and its nearest palette entry is the *grey background* --
    a colour that touches that edge nowhere in the artwork. Quantizing
    against blend ramps instead asks which two palette colours explain the
    pixel, so it resolves to charcoal or cream and never to grey.
    """
    from app.services.preprocess import _quantize, _resolve_palette

    palette = ["#666666", "#fbf7da", "#2d2c2a"]
    grey, cream, charcoal = (102, 102, 102), (251, 247, 218), (45, 44, 42)
    midpoint = tuple(round((a + b) / 2) for a, b in zip(charcoal, cream))

    # The trap: plain nearest really does prefer grey here.
    def nearest(colour):
        return min(
            (grey, cream, charcoal),
            key=lambda t: sum((a - b) ** 2 for a, b in zip(t, colour)),
        )

    assert nearest(midpoint) == grey

    edge = Image.new("RGB", (32, 32), midpoint)
    rgbs = _resolve_palette(edge, 0, palette, auto=False)
    quantized, used = _quantize(edge, rgbs)
    assert used == palette
    assert quantized.convert("RGB").getpixel((16, 16)) in (charcoal, cream)


def test_thin_white_outline_is_not_overlaid_with_grey(client):
    """The reported defect: a grey ribbon laid over the white outline.

    Charcoal-stroked lettering on a grey background carries a thin white
    outline between the two. Under nearest-colour mapping the anti-aliased
    ramp either side of that outline lands on grey, so the outline was eaten
    from both edges and the grey traced as its own shapes lying against it --
    in an editor, grey jagged lines running through the white layer. No grey
    belongs anywhere inside the outline.
    """
    from PIL import ImageDraw

    from app.schemas.params import VectorizeParams
    from app.services.preprocess import prepare

    image = Image.new("RGB", (400, 300), "#666666")
    draw = ImageDraw.Draw(image)
    for cx in (130, 270):  # two overlapping "letters", all curved edges
        draw.ellipse([cx - 90, 40, cx + 90, 260], fill="#ffffff")
        draw.ellipse([cx - 86, 44, cx + 86, 256], fill="#2d2c2a")
        draw.ellipse([cx - 78, 52, cx + 78, 248], fill="#fbf7da")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=70)

    params = VectorizeParams.model_validate(
        {"processing.palette": "#666666,#fbf7da,#2d2c2a,#ffffff"}
    )
    prepared = prepare(buffer.getvalue(), params, 4_000_000)
    pixels = prepared.image.convert("RGB")
    # Flat artwork is traced at a multiple of its own resolution, so the
    # prepared bitmap is not in source coordinates.
    scale = prepared.supersample

    inside = Image.new("L", pixels.size, 0)
    stencil = ImageDraw.Draw(inside)
    for cx in (130, 270):
        stencil.ellipse(
            [(cx - 90) * scale, 40 * scale, (cx + 90) * scale, 260 * scale], fill=255
        )

    grey = sum(
        1
        for colour, covered in zip(
            pixels.get_flattened_data(), inside.get_flattened_data()
        )
        if covered and colour == (102, 102, 102)
    )
    # Nearest-colour mapping left 160 grey pixels here. What survives is a
    # handful of small clumps in the JPEG ringing: clearing those too would
    # mean a mode filter, and a mode filter eats hairlines -- see
    # test_a_hairline_outline_survives_at_full_width.
    assert grey <= 30 * scale * scale, (grey, scale)


def test_blend_ramp_stays_within_the_palette_ceiling():
    """The probe palette has the same 256-entry limit as any other, so a big
    palette gives up its ramps rather than overflowing."""
    from app.services.preprocess import _blend_ramp

    for size in (1, 2, 6, 20, 64, 256):
        colors = [(i, i, i) for i in range(size)]
        probe, resolves_to = _blend_ramp(colors)
        assert len(resolves_to) == 256
        assert max(resolves_to) < size
        assert len(probe.getpalette() or []) == 768
        # Every palette colour keeps its own index.
        assert list(resolves_to[:size]) == list(range(size))


def test_derived_palette_finds_the_real_inks_not_the_transition_tones():
    """Quantizing straight to N ranks buckets by pixel count, so a dominant
    background swallows the budget: on the test artwork max_colors=6 returned
    five muddy greys, losing the white outline and the pink accent. Deriving
    the palette generously first and keeping the most-used entries that are
    far enough apart recovers the artwork's own colours instead."""
    from PIL import ImageDraw

    from app.services.preprocess import _derive_palette

    # A grey field, a charcoal blob with a *thin* white outline, and a small
    # pink accent. The outline and the accent are both low-area, and both are
    # what a population-ranked quantizer drops first.
    image = Image.new("RGB", (400, 300), "#666666")
    draw = ImageDraw.Draw(image)
    draw.ellipse([60, 40, 340, 260], fill="#ffffff")
    draw.ellipse([66, 46, 334, 254], fill="#2d2c2a")
    draw.ellipse([150, 110, 250, 190], fill="#f09ec2")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    source = Image.open(io.BytesIO(buffer.getvalue())).convert("RGB")

    derived = _derive_palette(source, 4)

    def has(target, tolerance=25):
        return any(
            sum((a - b) ** 2 for a, b in zip(entry, target)) ** 0.5 <= tolerance
            for entry in derived
        )

    assert len(derived) == 4, derived
    assert has((102, 102, 102)), derived  # background
    assert has((45, 44, 42)), derived  # charcoal
    assert has((255, 255, 255)), derived  # the thin outline
    assert has((240, 158, 194)), derived  # the small accent


def test_colour_budget_gives_flat_artwork_the_palette_it_deserves(client):
    """End to end: asking for a colour budget should leave one fill per ink,
    not a fill per ink *plus* a fill for every band between them."""
    from PIL import ImageDraw

    image = Image.new("RGB", (400, 300), "#666666")
    draw = ImageDraw.Draw(image)
    draw.ellipse([60, 40, 340, 260], fill="#ffffff")
    draw.ellipse([66, 46, 334, 254], fill="#2d2c2a")
    draw.ellipse([150, 110, 250, 190], fill="#f09ec2")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)

    response = post(client, buffer.getvalue(), name="a.jpg",
                    **{"processing.max_colors": "4"})
    assert response.status_code == 200
    fills = {f.decode() for f in re.findall(rb'fill="(#[0-9a-fA-F]{6})"', response.content)}
    assert len(fills) <= 4, fills

    def rgb(value):
        return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))

    def has(target, tolerance=25):
        return any(
            sum((a - b) ** 2 for a, b in zip(rgb(f), target)) ** 0.5 <= tolerance
            for f in fills
        )

    # Both low-area inks have to survive as themselves. The old behaviour
    # spent the budget on greys and returned neither.
    assert has((255, 255, 255)), fills  # the thin white outline
    assert has((240, 158, 194)), fills  # the small pink accent


def _shaded_sphere(size: int = 300) -> Image.Image:
    """Continuous tone: every pixel a slightly different shade."""
    import math

    image = Image.new("RGB", (size, size), "#20304a")
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            dx, dy = (x - size / 2) / (size / 2), (y - size / 2) / (size / 2)
            radius = dx * dx + dy * dy
            if radius <= 1:
                z = math.sqrt(1 - radius)
                light = max(0.0, 0.5 * dx - 0.6 * dy + 0.7 * z)
                pixels[x, y] = (
                    int(30 + 200 * light),
                    int(60 + 170 * light),
                    int(90 + 140 * light),
                )
    return image


def test_flat_artwork_gets_its_own_inks_with_no_settings_at_all():
    """The default has to be good on flat artwork, because that is what most
    callers send. Traced as-is, every anti-aliasing band becomes its own
    shape: the outline reads grey instead of white and the curves come out
    faceted. Detecting the artwork's inks removes the bands at the source."""
    from PIL import ImageDraw

    from app.schemas.params import VectorizeParams
    from app.services.preprocess import prepare

    image = Image.new("RGB", (400, 300), "#666666")
    draw = ImageDraw.Draw(image)
    draw.ellipse([60, 40, 340, 260], fill="#ffffff")
    draw.ellipse([66, 46, 334, 254], fill="#2d2c2a")
    draw.ellipse([150, 110, 250, 190], fill="#f09ec2")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)

    params = VectorizeParams()
    assert params.auto_palette
    prepared = prepare(buffer.getvalue(), params, 4_000_000)

    assert prepared.palette is not None
    assert len(prepared.palette) <= 6, prepared.palette

    def has(target, tolerance=25):
        return any(
            sum((a - int(c[i : i + 2], 16)) ** 2 for a, i in zip(target, (1, 3, 5)))
            ** 0.5
            <= tolerance
            for c in prepared.palette
        )

    assert has((102, 102, 102)) and has((45, 44, 42))
    assert has((255, 255, 255)), prepared.palette  # the thin outline
    assert has((240, 158, 194)), prepared.palette  # the small accent


def test_continuous_tone_artwork_is_left_alone():
    """Flattening a photograph or a shaded illustration to a dozen colours
    would be vandalism, so detection has to decline it.

    Counting inks cannot tell a short palette from a few bands cut through a
    gradient — both come back as a short list. What separates them is how far
    the pixels sit from the inks they would be mapped onto: near zero for flat
    artwork, because only the edge tones are far away, and several units for
    shading, which spreads pixels evenly between the bands.
    """
    from app.schemas.params import VectorizeParams
    from app.services.preprocess import _detect_flat_palette

    assert _detect_flat_palette(_shaded_sphere()) is None

    gradient = Image.new("RGB", (300, 300))
    pixels = gradient.load()
    for y in range(300):
        for x in range(300):
            pixels[x, y] = (x * 255 // 300, y * 255 // 300, (x + y) * 255 // 600)
    assert _detect_flat_palette(gradient) is None

    assert VectorizeParams().auto_palette


@pytest.mark.parametrize(
    "override",
    [
        {"processing.detail": "maximum"},
        {"processing.max_colors": "8"},
        {"processing.color_precision": "8"},
        {"processing.layer_difference": "48"},
        {"processing.palette": "#ffffff,#000000"},
    ],
)
def test_driving_the_colour_pipeline_turns_the_automatic_palette_off(override):
    """An explicit choice outranks the default. detail=maximum in particular
    promises to keep hairline features and every scrap of compression noise —
    flattening the colours first would quietly break that promise.

    processing.denoise is deliberately *not* in this set: it runs before
    quantization and composes with it rather than contradicting it, and making
    a sweep across denoise levels silently toggle a second behaviour made that
    parameter's effect impossible to reason about."""
    from app.schemas.params import VectorizeParams

    assert VectorizeParams.model_validate(override).auto_palette is False


@pytest.mark.parametrize(
    "override",
    [
        {},
        {"processing.detail": "standard"},
        {"processing.denoise": "low"},
        {"processing.max_colors": "0"},
        {"processing.color_precision": "6"},
        {"processing.smoothing": "high"},
        {"output.file_format": "pdf"},
    ],
)
def test_posting_the_defaults_back_is_not_a_choice(override):
    """Clients post every field they know about, filled in with the values the
    schema showed them — Swagger's "Try it out" form does exactly that. Reading
    `processing.detail=standard` as an instruction switched the automatic
    palette off for those callers while looking like it had done nothing, so
    what counts is the value, not the mention."""
    from app.schemas.params import VectorizeParams

    assert VectorizeParams.model_validate(override).auto_palette is True


def test_a_hairline_outline_survives_at_full_width():
    """A median or mode filter destroys anything a pixel or two wide, because
    a hairline is a minority in its own window. On real lettering that left
    the charcoal outline thick in places, thin in others and broken into
    dashes. Nothing in preprocessing may thin or break it."""
    from PIL import ImageDraw

    from app.schemas.params import VectorizeParams
    from app.services.preprocess import prepare

    # Drawn large and reduced, the way the artwork got its hairline: what
    # lands in the file is a one-pixel line with anti-aliasing either side,
    # not a crisp two-pixel one that any filter would survive.
    scale = 3
    image = Image.new("RGB", (400 * scale, 300 * scale), "#666666")
    draw = ImageDraw.Draw(image)
    draw.ellipse([60 * scale, 40 * scale, 340 * scale, 260 * scale], fill="#fbf7da")
    draw.ellipse(
        [60 * scale, 40 * scale, 340 * scale, 260 * scale],
        outline="#2d2c2a",
        width=scale,
    )
    image = image.resize((400, 300), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)

    prepared = prepare(buffer.getvalue(), VectorizeParams(), 4_000_000)
    pixels = prepared.image.convert("RGB")
    scale = prepared.supersample
    charcoal = (45, 44, 42)

    def runs(y):
        """Lengths of the charcoal runs crossed by this scanline."""
        found, run = [], 0
        for x in range(400 * scale):
            near = (
                sum((a - b) ** 2 for a, b in zip(pixels.getpixel((x, y)), charcoal))
                ** 0.5
                <= 60
            )
            if near:
                run += 1
            elif run:
                found.append(run)
                run = 0
        return found

    # Every scanline through the body of the ellipse crosses the outline twice,
    # and neither crossing may be missing or worn down to nothing.
    for y in range(80 * scale, 221 * scale, 10 * scale):
        crossings = runs(y)
        assert len(crossings) == 2, (y, crossings)
        assert min(crossings) >= 1, (y, crossings)


def test_a_solid_grey_background_is_not_mistaken_for_a_blend():
    """A neutral grey sits exactly on the line between black and white, so the
    blend test alone dismisses it. On the reference artwork that threw away a
    background covering eighty percent of the image and took the rest of the
    palette with it. Only a colour that is *both* a blend and a thin thread is
    a transition tone."""
    from PIL import ImageDraw

    from app.services.preprocess import _detect_flat_palette

    image = Image.new("RGB", (400, 300), "#666666")
    draw = ImageDraw.Draw(image)
    draw.ellipse([80, 60, 320, 240], fill="#ffffff")
    draw.ellipse([110, 90, 290, 210], fill="#111111")
    inks = _detect_flat_palette(image)

    assert inks is not None
    assert any(
        sum((a - b) ** 2 for a, b in zip(ink, (102, 102, 102))) ** 0.5 <= 20
        for ink in inks
    ), inks


def _outlined_disc(quality: int, supersize: int = 1) -> bytes:
    """A disc with a fill, a dark ring and a light halo, saved as JPEG.

    Drawing large and reducing puts genuine sub-pixel detail in the file, the
    way real artwork carries it; drawing at final size and compressing hard
    instead gives ringing with nothing underneath it.
    """
    from PIL import ImageDraw

    s = supersize
    image = Image.new("RGB", (400 * s, 300 * s), "#666666")
    draw = ImageDraw.Draw(image)
    draw.ellipse([50 * s, 30 * s, 350 * s, 270 * s], fill="#ffffff")
    draw.ellipse([56 * s, 36 * s, 344 * s, 264 * s], fill="#2d2c2a")
    draw.ellipse([62 * s, 42 * s, 338 * s, 258 * s], fill="#fbf7da")
    if s > 1:
        image = image.resize((400, 300), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def test_the_finer_trace_is_kept_whenever_there_is_one():
    """A one-pixel outline cannot be quantized evenly — whether a pixel lands
    on the dark side depends on where the line falls inside it, so the width
    wanders and the curve fitter follows every wobble. Tracing at twice the
    resolution halves that.

    This used to be conditional: both copies were traced and the one with
    fewer shapes kept, because resampling sharpens noise as readily as
    geometry and a jump in shape count was read as a compressed image's mess
    being multiplied. But shape count never asked whether the extra shapes sat
    closer to the artwork. They do — on every corpus sample where the two
    rules disagree, and on this very disc when it is scored against the
    uncompressed original rather than the JPEG the tracer was handed. So a
    finer copy, once preprocessing has offered one, is simply used."""
    from app.schemas.params import VectorizeParams
    from app.services import engine, preprocess

    params = VectorizeParams()

    clean = preprocess.prepare(_outlined_disc(92, supersize=3), params, 4_000_000)
    assert clean.finer is not None
    assert engine.trace(clean, params).prepared.supersample == 2

    # Heavily compressed, and the finer copy genuinely does carry more shapes.
    # It is still the more faithful of the two, so it is still what we keep.
    noisy = preprocess.prepare(_outlined_disc(70), params, 4_000_000)
    assert noisy.finer is not None
    coarse = engine._trace_one(noisy, params)
    finer = engine._trace_one(noisy.finer, params)
    assert finer.shape_count > coarse.shape_count, (coarse.shape_count, finer.shape_count)
    assert engine.trace(noisy, params).prepared.supersample == 2


def test_tracing_finer_does_not_change_the_output_size():
    """The extra pixels buy accuracy, not size: they show up in the viewBox
    and divide back out of the width and height."""
    from app.schemas.params import VectorizeParams
    from app.services import engine, preprocess, svgdoc

    params = VectorizeParams()
    prepared = preprocess.prepare(_outlined_disc(92, supersize=3), params, 4_000_000)
    traced = engine.trace(prepared, params)
    used = traced.prepared
    assert used.supersample == 2

    svg, meta = svgdoc.build(
        traced.svg,
        params,
        used.traced_width,
        used.traced_height,
        palette=used.palette,
        supersample=used.supersample,
    )
    assert b'viewBox="0 0 800 600"' in svg
    assert b'width="400"' in svg and b'height="300"' in svg
    assert (meta["output_width"], meta["output_height"]) == (400.0, 300.0)


# Every parameter, spelled with the value the schema advertises as its default.
ALL_DEFAULTS = {
    "mode": "production",
    "processing.color_mode": "color",
    "processing.max_colors": "0",
    "processing.hierarchical": "cutout",
    "processing.curve_mode": "spline",
    "processing.color_merge": "16",
    "processing.detail": "standard",
    "processing.denoise": "low",
    "processing.smoothing": "low",
    "processing.shapes.min_area_px": "4",
    "processing.color_precision": "6",
    "processing.layer_difference": "16",
    "processing.corner_threshold": "60",
    "processing.length_threshold": "4.0",
    "processing.splice_threshold": "45",
    "processing.max_iterations": "10",
    "processing.path_precision": "3",
    "output.draw_style": "fill_shapes",
    "output.combine_paths": "shapes",
    "output.group_by": "none",
    "output.gap_filler.enabled": "true",
    "output.size.scale": "1",
}


def test_posting_every_parameter_at_its_default_changes_nothing(client):
    """Generated clients and Swagger's "Try it out" form post every field they
    know about, filled in with the values the schema showed them. Reading a
    field's presence as an instruction made those callers get a different file
    from callers who sent nothing — quietly, and for no reason they could see.

    Preprocessing, the tracer presets and the colour pipeline all decide on
    the value now, so the two requests have to come back identical."""
    plain = post(client, jpeg_bytes(), name="a.jpg")
    echoed = post(client, jpeg_bytes(), name="a.jpg", **ALL_DEFAULTS)

    assert plain.status_code == 200 and echoed.status_code == 200
    assert echoed.content == plain.content, (
        len(plain.content),
        len(echoed.content),
    )


def test_a_non_default_value_still_overrides_the_preset(client):
    """...while a value that differs from the default is still an instruction."""
    from app.schemas.params import VectorizeParams
    from app.services.engine import _tracer_kwargs

    preset = _tracer_kwargs(VectorizeParams(), colours_are_pinned=False)
    asked = _tracer_kwargs(
        VectorizeParams.model_validate({"processing.corner_threshold": "20"}),
        colours_are_pinned=False,
    )
    assert preset["corner_threshold"] != 20
    assert asked["corner_threshold"] == 20


def test_pixel_thresholds_keep_their_meaning_when_tracing_finer():
    """Two of the tracer's knobs are measured in pixels, and the pixels change
    size when the bitmap is traced at a multiple of its own resolution. Left
    alone they quietly weaken: a 4-pixel shortest segment becomes 2 source
    pixels, so the fitter starts following the staircase it was meant to cut
    across, and the speckle filter loses three quarters of its reach."""
    from app.schemas.params import VectorizeParams
    from app.services.engine import _tracer_kwargs

    from app.services.engine import _FINER_LATITUDE

    params = VectorizeParams()
    plain = _tracer_kwargs(params, colours_are_pinned=True, supersample=1)
    finer = _tracer_kwargs(params, colours_are_pinned=True, supersample=2)

    # A length scales with the factor, an area with its square. The length
    # gets a further nudge, because below one source pixel there is nothing
    # real left for the fitter to follow.
    assert finer["filter_speckle"] == plain["filter_speckle"] * 4
    assert finer["length_threshold"] == (
        plain["length_threshold"] * 2 * _FINER_LATITUDE
    )

    # corner_threshold is the angle below which a bend stays a hard corner,
    # so anything above 90 rounds off a right angle. It must not move.
    assert finer["corner_threshold"] == plain["corner_threshold"]
    assert finer["corner_threshold"] <= 90

    # Neither may splice_threshold, for the same reason. Raising it on the
    # finer trace reads as "the wobble left is below one source pixel, so let
    # the fitter splice through it", but measured against cached Vectorizer.AI
    # output it splices through corners the artwork really has — it turned the
    # pointed tail of a B's counter into a blob. Only the fitter's budget, not
    # its licence to ignore corners, opens up.
    assert finer["splice_threshold"] == plain["splice_threshold"]
    assert finer["max_iterations"] > plain["max_iterations"]


def test_boundary_smoothing_leaves_thin_features_untouched():
    """Hard-quantizing a soft edge leaves a ragged fringe of single pixels,
    and a mode filter settles it — but the same filter eats anything narrower
    than its window, which is how a hairline outline gets chewed into dashes.
    Smoothing therefore reaches only what is wide enough to survive it."""
    from PIL import ImageDraw

    from app.services.preprocess import _smooth_broad_boundaries

    # ink 0 is the ground, 1 a broad block, 2 a line one pixel wide
    ids = Image.new("P", (80, 60), 0)
    draw = ImageDraw.Draw(ids)
    draw.rectangle([10, 10, 50, 50], fill=1)
    draw.line([(65, 5), (65, 55)], fill=2)
    draw.point((66, 30), 2)  # a one-pixel bulge on the thread

    def pixels(image):
        return list(
            Image.frombytes("L", image.size, image.tobytes()).get_flattened_data()
        )

    before = pixels(ids)
    after = pixels(_smooth_broad_boundaries(ids))

    # The thread and its bulge are narrower than the window: untouched.
    assert [before[y * 80 + 65] for y in range(60)] == [
        after[y * 80 + 65] for y in range(60)
    ]
    assert after[30 * 80 + 66] == 2

    # The block is wide enough to smooth, so its sharp convex corners give up
    # the single pixel that juts furthest out.
    assert before[10 * 80 + 10] == 1 and after[10 * 80 + 10] == 0
    # ...and nothing else moves: this pass rounds a pixel, it does not erode.
    assert sum(1 for a, b in zip(before, after) if a != b) <= 4


def test_ringing_beside_a_boundary_is_cleared():
    """Compression throws dark pixels out past a light edge, and they land in
    the flat region beside it rather than on it. The speckle pass cannot reach
    those: it asks whether seven of a pixel's nine neighbours agree, and beside
    a boundary they never do. Every survivor costs a notch in each curve that
    runs past it, because the tracer has to detour around it and back — which
    is what made traced lettering look faceted at any real zoom."""
    from app.services.preprocess import (
        _COMPANY_ALONE,
        _COMPANY_SUBPIXEL,
        _drop_speckles,
        _drop_strays,
    )

    inks = [(102, 102, 102), (255, 255, 255), (43, 42, 40)]  # grey, white, charcoal
    ids = Image.new("P", (40, 40), 0)
    for x in range(40):  # a white band across the bottom half
        for y in range(20, 40):
            ids.putpixel((x, y), 1)
    ids.putpixel((10, 19), 2)  # ringing, hard against the band
    ids.putpixel((20, 19), 2)  # ...and a pair of it
    ids.putpixel((21, 19), 2)

    def ink_at(image, xy):
        return Image.frombytes("L", image.size, image.tobytes()).getpixel(xy)

    # Three inks in each of those windows, so the speckle pass leaves them all.
    speckled = _drop_speckles(ids)
    assert ink_at(speckled, (10, 19)) == 2
    assert ink_at(speckled, (20, 19)) == 2

    # Traced at its own resolution, a pixel with no company of its own goes and
    # takes the ink around it; a pair still counts as company, because a
    # one-pixel line's last pixel has exactly one neighbour of its own kind.
    cleaned = _drop_strays(ids, inks, _COMPANY_ALONE)
    assert ink_at(cleaned, (10, 19)) == 0
    assert ink_at(cleaned, (20, 19)) == 2

    # Traced at twice its resolution a pair is half a source pixel of ink, so
    # nothing that was drawn can fail the stricter test and the pair goes too.
    finer = _drop_strays(ids, inks, _COMPANY_SUBPIXEL)
    assert ink_at(finer, (10, 19)) == 0
    assert ink_at(finer, (20, 19)) == 0
    assert ink_at(finer, (21, 19)) == 0


def test_a_hairline_split_between_two_inks_is_not_read_as_strays():
    """A one-pixel white line on a dark ground does not quantize to white. The
    ramp puts some of its pixels on white and the rest on the mid-grey between
    the two, so pixel by pixel the line is alone among its own kind. Judged on
    company alone the whole line is a run of strays, and outline-only lettering
    disappeared when it was judged that way.

    What tells it from ringing is where the colour sits. Mixing two inks can
    only ever land between them in brightness, so the mid-grey beside a white
    line on a dark ground is never the darkest or the lightest thing in its
    window; ringing overshoots past everything around it. Only a pixel that is
    both alone and an extreme may go."""
    from app.services.preprocess import (
        _COMPANY_ALONE,
        _drop_speckles,
        _drop_strays,
        _neighbourhoods,
    )

    inks = [(51, 51, 51), (255, 255, 255), (102, 102, 102)]  # dark, white, mid
    ids = Image.new("P", (40, 40), 0)
    for x in range(2, 38):
        ids.putpixel((x, 20), 1 if x % 3 else 2)  # white, mid-grey every third

    flat = Image.frombytes("L", ids.size, ids.tobytes())
    company = _neighbourhoods(flat, inks).company
    # Every mid-grey pixel on the line really is alone: not one more of its own
    # ink anywhere in its window. Company cannot be what saves it.
    assert all(company.getpixel((x, 20)) < 57 for x in range(3, 37, 3))

    def line(image):
        pixels = Image.frombytes("L", image.size, image.tobytes())
        return [pixels.getpixel((x, 20)) for x in range(40)]

    # This pass erodes nothing: it hands on exactly what the speckle pass ahead
    # of it left. That pass clips the line's two end pixels, which have nothing
    # of their own kind beside them -- longstanding behaviour, see
    # _drop_speckles -- and everything between them stays.
    speckled = _drop_speckles(ids)
    assert line(_drop_strays(speckled, inks, _COMPANY_ALONE)) == line(speckled)
    assert line(speckled).count(0) == 40 - 34


def test_a_right_angle_survives_the_finer_trace(client):
    """The fitter is given more latitude when the bitmap is traced finer,
    because below one source pixel there is nothing real left to follow. That
    latitude must not extend to corner_threshold: it is the angle below which
    a bend stays a hard corner, so anything above 90 rounds off a right angle
    — raising it to the 'medium' preset's 110 turned a test rectangle's corner
    into a visible curve."""
    from PIL import ImageDraw

    image = Image.new("RGB", (300, 300), "#f4f1de")
    draw = ImageDraw.Draw(image)
    draw.rectangle([80, 80, 260, 260], fill="#3d405b")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    response = post(
        client,
        buffer.getvalue(),
        name="corner.png",
        **{"output.file_format": "png", "output.bitmap.dpi": "96"},
    )
    assert response.status_code == 200
    out = Image.open(io.BytesIO(response.content)).convert("RGB")

    def dark(x, y):
        return sum(out.getpixel((x, y))) < 380

    # The corner pixel itself, and both edges leading into it, stay filled.
    assert dark(82, 82), out.getpixel((82, 82))
    assert dark(84, 170) and dark(170, 84)
    # ...and the ground just outside the corner stays clear.
    assert not dark(76, 76), out.getpixel((76, 76))


def _ring(nodes: int, radius: float, wobble: float = 0.0) -> list[tuple[float, float]]:
    import math

    points = []
    for index in range(nodes):
        angle = 2 * math.pi * index / nodes
        reach = radius + (wobble if index % 2 else -wobble)
        points.append((reach * math.cos(angle), reach * math.sin(angle)))
    return points


def _as_path(points, skew_at: int | None = None, skew: float = 20.0) -> str:
    """Join points with cubics whose handles lie along the chords."""
    import math

    count = len(points)
    pieces = ["M%.4f %.4f" % points[0]]
    for index in range(count):
        here, there = points[index], points[(index + 1) % count]
        dx, dy = there[0] - here[0], there[1] - here[1]
        if index == skew_at:
            angle = math.radians(skew)
            dx, dy = (
                dx * math.cos(angle) - dy * math.sin(angle),
                dx * math.sin(angle) + dy * math.cos(angle),
            )
        pieces.append(
            "C%.4f %.4f %.4f %.4f %.4f %.4f"
            % (
                here[0] + dx / 3, here[1] + dy / 3,
                there[0] - (there[0] - here[0]) / 3, there[1] - (there[1] - here[1]) / 3,
                there[0], there[1],
            )
        )
    return "".join(pieces) + "Z"


def test_a_corner_split_across_two_nodes_is_still_a_corner():
    """The tracer often puts a corner between two nodes rather than on one: a
    right angle came back as a pair of 44-degree turns, neither of which looks
    like a corner on its own. Measuring the direction across a span of outline
    adds the halves back together."""
    from app.services.svgdoc import _corner_flags, _segments

    # A square with each corner cut into two 45-degree steps.
    points = []
    for x, y in ((-50, -50), (50, -50), (50, 50), (-50, 50)):
        points.append((x * 0.94, y))
        points.append((x, y * 0.94))
    d = _as_path(points)
    closed, segments, _ = _segments(d)[0]

    flags = _corner_flags(segments, len(segments), 3.0)
    assert sum(flags) >= 4, flags


def test_simplification_fuses_segments_without_moving_the_curve():
    """Every node the tracer emits marks somewhere the pixel boundary turned,
    so a run of them along one gentle curve is a run of chances to wobble.
    Fusing neighbours into a single segment is what makes a long edge read as
    one stroke — provided the replacement runs where the pair did."""
    import math

    from app.services.svgdoc import _cubic_at, _segments, _smooth_path_data

    d = _as_path(_ring(48, 120.0))
    before = _segments(d)[0][1]
    after = _segments(_smooth_path_data(d, 1.0, 1.2))[0][1]

    assert len(after) < len(before) * 0.8, (len(before), len(after))

    # Walk the simplified outline and check it never strays far from the ring
    # it came from, so the letterform is where it was, with fewer nodes.
    start = after[-1][4:6]
    worst = 0.0
    for segment in after:
        for step in range(9):
            point = _cubic_at(segment, start, step / 8)
            worst = max(worst, abs(math.hypot(*point) - 120.0))
        start = segment[4:6]
    assert worst < 1.5, worst


def _gradient_strokes() -> bytes:
    """Thick strokes filled with a smooth ramp, haloed and outlined.

    This is what a lot of sticker lettering looks like, and it is the case
    that counting colours cannot tell from flat artwork: slice a ramp into
    enough bands and every pixel is close to one of them.
    """
    from PIL import ImageDraw

    image = Image.new("RGB", (700, 500), "#9a9a9a")
    draw = ImageDraw.Draw(image)
    for x in (140, 260, 380, 500):
        draw.rounded_rectangle([x - 34, 112, x + 34, 408], radius=34, fill="#ffffff")
        draw.rounded_rectangle([x - 29, 117, x + 29, 403], radius=29, fill="#2b2b33")
        for y in range(120, 401):
            t = (y - 120) / 280
            draw.rectangle(
                [x - 26, y, x + 26, y + 1],
                fill=(
                    int(214 + (244 - 214) * t),
                    int(150 + (214 - 150) * t),
                    int(166 + (222 - 166) * t),
                ),
            )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_shaded_artwork_is_not_flattened_into_bands():
    """A gradient sliced into flat colours satisfies every test that asks
    whether the pixels are close to *some* ink — add enough bands and they
    always are. What gives it away is where the bands sit: a band is the only
    thing separating the two colours either side of it, because that is what a
    ramp is. A real ink that merely lies between two others in RGB — a grey
    background between black lettering and a white halo — is not, because
    those two meet each other directly all over the artwork."""
    from app.services.preprocess import _detect_flat_palette

    shaded = Image.open(io.BytesIO(_gradient_strokes())).convert("RGB")
    assert _detect_flat_palette(shaded) is None

    # ...while the flat case with a grey background between black and white
    # keeps its palette.
    from PIL import ImageDraw

    flat = Image.new("RGB", (400, 300), "#666666")
    draw = ImageDraw.Draw(flat)
    draw.ellipse([60, 40, 340, 260], fill="#ffffff")
    draw.ellipse([70, 50, 330, 250], fill="#111111")
    draw.ellipse([120, 90, 280, 210], fill="#666666")
    inks = _detect_flat_palette(flat)
    assert inks is not None and len(inks) == 3, inks


def test_a_gradient_keeps_its_shading_end_to_end(client):
    """The visible symptom: a stroke that shades from pink to pale came back
    as hard bands, the palest of which landed on white."""
    response = post(client, _gradient_strokes(), name="grad.png")
    assert response.status_code == 200
    assert "X-Color-Count" not in response.headers or not response.headers.get(
        "X-Color-Count"
    )
    fills = {f.decode().lower() for f in re.findall(rb'fill="(#[0-9a-fA-F]{6})"', response.content)}
    # No flat palette means no banding: the pale end of the ramp must not have
    # been snapped onto the white of the halo.
    pinks = [f for f in fills if f not in ("#ffffff", "#9a9a9a", "#2b2b33")]
    assert len(pinks) <= 3, sorted(fills)


def test_shaded_regions_come_out_as_gradients():
    """The tracer only emits flat fills, so shaded artwork had nowhere to go:
    banding it reads as stripes and collapsing it throws the shading away.
    The shading in this kind of artwork is a straight ramp, though, and SVG
    has a construct for exactly that."""
    import re as _re

    from app.schemas.params import VectorizeParams
    from app.services import engine, preprocess, svgdoc

    art = _gradient_strokes()
    params = VectorizeParams()
    prepared = preprocess.prepare(art, params, 40_000_000)
    traced = engine.trace(prepared, params)
    used = traced.prepared or prepared
    svg, meta = svgdoc.build(
        traced.svg,
        params,
        used.traced_width,
        used.traced_height,
        palette=used.palette,
        supersample=used.supersample,
        shading=used.image,
    )
    assert meta["gradients"] == 1, meta
    assert b"<linearGradient" in svg
    assert b'fill="url(#shade0)"' in svg

    # The stops have to be the ends of the artwork's own ramp, not guesses.
    stops = [
        tuple(int(s[i : i + 2], 16) for i in (0, 2, 4))
        for s in _re.findall(rb'stop-color="#([0-9a-fA-F]{6})"', svg)
    ]
    assert len(stops) == 2
    source = Image.open(io.BytesIO(art)).convert("RGB")
    top, bottom = source.getpixel((140, 130)), source.getpixel((140, 395))
    for fitted, actual in ((stops[0], top), (stops[1], bottom)):
        assert max(abs(a - b) for a, b in zip(fitted, actual)) <= 12, (fitted, actual)


def test_flat_artwork_gets_no_gradients(client):
    """A gradient is only ever fitted where the pixels are a ramp; flat work
    must come back with the flat fills it had."""
    response = post(client, jpeg_bytes(), name="a.jpg")
    assert response.status_code == 200
    assert b"linearGradient" not in response.content


@pytest.mark.parametrize("fmt", ["svg", "pdf", "eps", "png"])
def test_every_format_survives_a_gradient(client, fmt):
    """Only reportlab's PDF backend can draw a gradient. Its PNG and
    PostScript backends do not ignore one, they raise partway through, so
    those two get a flat stand-in rather than a 500."""
    response = post(
        client, _gradient_strokes(), name="g.png", **{"output.file_format": fmt}
    )
    assert response.status_code == 200, response.text
    if fmt == "svg":
        assert b"<linearGradient" in response.content
    if fmt == "pdf":
        assert b"/Shading" in response.content  # a real gradient, not flattened
    if fmt in ("png", "eps"):
        assert b"linearGradient" not in response.content
