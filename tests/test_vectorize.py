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
    response = post(client, sample_png, **{"output.size.scale": "3"})
    assert b'viewBox="0 0 240 180"' in response.content
    assert b'width="720"' in response.content


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
            VectorizeParams.model_validate({"processing.smoothing": level})
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
            VectorizeParams.model_validate({"processing.smoothing": level})
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
        )
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
            VectorizeParams.model_validate({"processing.detail": level})
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
        )
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


def test_color_merge_collapses_near_duplicate_fills(client):
    """Anti-aliasing between two flat regions leaves transition shapes, often
    split into fills a single RGB unit apart. No tracer setting merges those."""
    art = _banded_artwork()
    off = post(client, art, name="a.jpg", **{"processing.color_merge": "0"})
    on = post(client, art, name="a.jpg", **{"processing.color_merge": "40"})
    before, after = _fills(off.content), _fills(on.content)
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
    softens a boundary into intermediate tones, and nearest-colour mapping
    hands those to whatever palette entry sits nearest in RGB. On real
    artwork the charcoal/cream midpoint landed on a sage green, fringing
    every letter. 7.8k stray sage pixels without denoising, 31k with it."""
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
