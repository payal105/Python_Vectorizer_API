"""The gradient stage: end to end through /api/v1/gradients, and up close.

/vectorize runs this stage for every conversion, so most of what it does is
covered where the conversion is. What is tested here is the stage on its own:
that it can be handed a document and a bitmap and improve the one against the
other, and the pieces of it that are easiest to get quietly wrong.
"""

from __future__ import annotations

import math
import re

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.schemas.gradients import GradientParams
from app.services import gradients
from tests.conftest import AUTH, png_bytes

ENDPOINT = "/api/v1/gradients"


def ramp(width: int = 400, height: int = 200, start=(240, 60, 40), end=(40, 70, 230)):
    """A left-to-right linear ramp, the shape the tracer cannot express."""
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for x in range(width):
        t = x / (width - 1)
        draw.line(
            [(x, 0), (x, height)],
            fill=tuple(int(a + (b - a) * t) for a, b in zip(start, end)),
        )
    return image


def flat_svg(width: int, height: int, fill: str = "#808080", transform: str = "") -> bytes:
    attribute = f' transform="{transform}"' if transform else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">'
        f'<path d="M0 0L{width} 0L{width} {height}L0 {height}Z" '
        f'fill="{fill}"{attribute}/></svg>'
    ).encode()


def post(client, image: bytes, svg: bytes, *, auth=AUTH, **fields):
    return client.post(
        ENDPOINT,
        files={
            "image": ("art.png", image, "image/png"),
            "vector": ("art.svg", svg, "image/svg+xml"),
        },
        data=fields,
        auth=auth,
    )


# --- the endpoint -------------------------------------------------------------


def test_a_flat_fill_over_a_ramp_becomes_a_gradient(client):
    art = ramp()
    response = post(client, png_bytes(art), flat_svg(*art.size))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert b"<linearGradient" in response.content
    assert int(response.headers["X-Gradient-Count"]) == 1
    assert float(response.headers["X-Gradient-Residual-After"]) < float(
        response.headers["X-Gradient-Residual-Before"]
    )


def test_the_stage_is_idempotent(client):
    """Running it on its own output re-fits from the source rather than
    compounding, so a document does not grow a layer of definitions per pass."""
    art = png_bytes(ramp())
    once = post(client, art, flat_svg(400, 200)).content
    twice = post(client, art, once).content
    assert once.count(b"<linearGradient") == twice.count(b"<linearGradient") == 1
    assert twice.count(b"<stop") == once.count(b"<stop")


def test_flat_artwork_is_left_alone(client):
    art = Image.new("RGB", (300, 200), (70, 130, 180))
    response = post(client, png_bytes(art), flat_svg(300, 200, "#4682b4"))
    assert response.status_code == 200
    assert b"Gradient" not in response.content
    assert response.headers["X-Gradient-Count"] == "0"


def test_json_envelope_carries_the_report(client):
    art = ramp()
    response = client.post(
        ENDPOINT,
        files={
            "image": ("art.png", png_bytes(art), "image/png"),
            "vector": ("art.svg", flat_svg(*art.size), "image/svg+xml"),
        },
        headers={"Accept": "application/json"},
        auth=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["image"]["format"] == "svg"
    report = body["meta"]["shading"]
    assert report["gradients"] == 1 and report["linear"] == 1
    assert report["residual_after"] < report["residual_before"]


def test_the_svg_may_be_sent_as_text(client):
    art = ramp()
    response = client.post(
        ENDPOINT,
        files={"image": ("art.png", png_bytes(art), "image/png")},
        data={"vector.svg": flat_svg(*art.size).decode()},
        auth=AUTH,
    )
    assert response.status_code == 200
    assert b"<linearGradient" in response.content


def test_a_missing_vector_is_rejected(client):
    response = client.post(
        ENDPOINT,
        files={"image": ("art.png", png_bytes(ramp()), "image/png")},
        auth=AUTH,
    )
    assert response.status_code == 400
    assert "vector" in response.json()["error"]["message"]


def test_a_missing_image_is_rejected(client):
    response = client.post(
        ENDPOINT,
        files={"vector": ("art.svg", flat_svg(400, 200), "image/svg+xml")},
        auth=AUTH,
    )
    assert response.status_code == 400


def test_credentials_are_required(client):
    response = post(client, png_bytes(ramp()), flat_svg(400, 200), auth=("no", "one"))
    assert response.status_code == 401


def test_a_document_that_is_not_an_svg_is_rejected(client):
    response = post(client, png_bytes(ramp()), b"<html><body>no</body></html>")
    assert response.status_code == 400


def test_parameters_are_honoured(client):
    response = post(
        client, png_bytes(ramp()), flat_svg(400, 200), **{"gradients.enabled": "false"}
    )
    assert response.status_code == 200
    assert b"Gradient" not in response.content


def test_an_unknown_parameter_is_loud(client):
    response = post(
        client, png_bytes(ramp()), flat_svg(400, 200), **{"gradients.enabld": "false"}
    )
    assert response.status_code == 400
    assert "gradients.enabld" in response.json()["error"]["message"]


# --- placement ----------------------------------------------------------------


def test_a_shape_is_read_where_its_transform_puts_it():
    """The tracer offsets shapes with translate() rather than writing the
    offset into the coordinates. Ignoring that fits every shape to whatever
    the artwork holds at the top left corner instead -- which came back as a
    landscape whose ground was painted with the sky's gradient."""
    art = Image.new("RGB", (200, 200), (255, 255, 255))
    art.paste(ramp(200, 100), (0, 100))

    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
        '<path d="M0 0L200 0L200 100L0 100Z" fill="#ffffff"/>'
        '<path d="M0 0L200 0L200 100L0 100Z" fill="#808080" '
        'transform="translate(0,100)"/></svg>'
    ).encode()

    refined, report = gradients.refine(svg, art)
    assert report.gradients == 1, report
    # The ramp runs left to right across the lower half, so the fitted axis
    # has to be horizontal and sit inside that half.
    match = re.search(rb'y1="([\d.]+)"', refined)
    assert match and 90 <= float(match.group(1)) <= 210, refined


def test_a_transform_that_cannot_be_read_leaves_the_shape_alone():
    art = ramp(200, 200)
    svg = flat_svg(200, 200, transform="ref(svg)")
    refined, report = gradients.refine(svg, art)
    assert report.gradients == 0 and report.regions == 0
    assert b"Gradient" not in refined


@pytest.mark.parametrize(
    ("transform", "expected"),
    [
        ("", gradients.IDENTITY),
        ("translate(5,7)", (1.0, 0.0, 0.0, 1.0, 5.0, 7.0)),
        ("translate(5)", (1.0, 0.0, 0.0, 1.0, 5.0, 0.0)),
        ("scale(2)", (2.0, 0.0, 0.0, 2.0, 0.0, 0.0)),
        ("matrix(1,2,3,4,5,6)", (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)),
        ("translate(10,0) scale(2)", (2.0, 0.0, 0.0, 2.0, 10.0, 0.0)),
        ("nonsense(1)", None),
    ],
)
def test_transform_parsing(transform, expected):
    got = gradients.affine(transform)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_rotation_about_a_point_matches_the_svg_definition():
    matrix = gradients.affine("rotate(90 10 10)")
    point = gradients._apply(matrix, np.array([[10.0, 0.0]]))
    assert point[0] == pytest.approx([20.0, 10.0], abs=1e-6)


# --- masks --------------------------------------------------------------------


def test_a_hole_is_not_part_of_the_shape():
    """Subpaths combine with exclusive-or, so a counter belongs to whatever
    is behind it. Filling it solid would attribute the pixels inside a letter
    to the letter, and fit its ramp to the page showing through."""
    outer = "M10 10L90 10L90 90L10 90Z"
    inner = "M30 30L70 30L70 70L30 70Z"
    subpaths = gradients.flatten(outer + inner)
    assert subpaths is not None and len(subpaths) == 2
    index_map = gradients._index_map([(0, subpaths)], 100, 100)
    assert index_map[50, 50] == -1      # inside the hole
    assert index_map[20, 50] == 0       # inside the ring
    assert index_map[5, 5] == -1        # outside altogether


def test_curves_are_flattened_close_to_the_arc():
    """A circle drawn as four cubics has to rasterize as a circle, or the
    pixels sampled along its edge come from outside it."""
    k = 0.5522847498
    d = (
        f"M0 -1C{k} -1 1 -{k} 1 0C1 {k} {k} 1 0 1"
        f"C-{k} 1 -1 {k} -1 0C-1 -{k} -{k} -1 0 -1Z"
    )
    points = np.concatenate(gradients.flatten(d))
    radii = np.hypot(points[:, 0], points[:, 1])
    assert abs(radii.max() - 1.0) < 0.01 and abs(radii.min() - 1.0) < 0.01


def test_relative_and_shorthand_commands_are_understood():
    absolute = gradients.flatten("M0 0L10 0L10 10L0 10Z")
    relative = gradients.flatten("m0 0l10 0l0 10l-10 0z")
    assert absolute is not None and relative is not None
    assert np.allclose(np.concatenate(absolute), np.concatenate(relative))


def test_an_arc_is_declined_rather_than_guessed_at():
    assert gradients.flatten("M0 0A5 5 0 0 1 10 0Z") is None


def test_baking_a_translate_keeps_the_shape_where_it_was():
    baked = gradients.bake_translation("M0 0L10 0L10 10Z", 5, 7)
    assert gradients.flatten(baked) is not None
    assert np.allclose(
        np.concatenate(gradients.flatten(baked)),
        np.concatenate(gradients.flatten("M0 0L10 0L10 10Z")) + [5, 7],
    )


def test_relative_data_is_not_baked():
    """Relative coordinates are deltas, and a translate does not apply to
    them; shifting the numbers would move every segment instead of the shape."""
    assert gradients.bake_translation("m0 0l10 0l0 10z", 5, 7) is None


# --- fitting ------------------------------------------------------------------


def test_a_ramp_whose_channels_move_apart_is_still_found():
    """Red climbing exactly as blue falls is the case that defeats summing
    the channel gradients: the sum points nowhere, and the ramp reads as
    flat. The direction has to come from the strongest singular vector."""
    art = ramp(300, 120, start=(255, 128, 0), end=(0, 128, 255))
    _, report = gradients.refine(flat_svg(300, 120), art)
    assert report.gradients == 1 and report.linear == 1, report
    assert report.residual_after < 2.0, report


def test_a_curved_ramp_gets_the_stops_it_needs():
    """Two stops describe a straight ramp and nothing else. Shading that
    curves has to pick up stops until it is inside tolerance, or the gradient
    is quietly replaced by the chord across it."""
    art = Image.new("RGB", (400, 100))
    draw = ImageDraw.Draw(art)
    for x in range(400):
        t = x / 399
        value = int(255 * math.sin(t * math.pi) ** 2)
        draw.line([(x, 0), (x, 100)], fill=(value, value, value))
    _, report = gradients.refine(flat_svg(400, 100), art)
    assert report.gradients == 1, report
    assert report.stops > 4, report
    assert report.residual_after < 3.0, report


def test_neighbouring_bands_of_one_ramp_share_a_gradient():
    """The patch case: a ramp the tracer had to slice into flat bands. Each
    band really is nearly flat on its own, so each is honestly fitted flat
    and the steps between them stay visible. One ramp across all of them has
    no step anywhere."""
    art = ramp(300, 120, start=(250, 220, 40), end=(200, 40, 60))
    step = 20
    bands = [
        f'<path d="M{x} 0L{x + step} 0L{x + step} 120L{x} 120Z" fill="#c88c32"/>'
        for x in range(0, 300, step)
    ]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 120">'
        + "".join(bands)
        + "</svg>"
    ).encode()

    refined, report = gradients.refine(svg, art)
    assert report.merged == len(bands), report
    # One definition, and all three bands pointing at it: that is what makes
    # the colour continuous across the seams rather than merely close.
    assert refined.count(b"<linearGradient") == 1
    assert refined.count(b"url(#shade0)") >= len(bands)
    assert report.residual_after < report.residual_before / 2


def test_two_different_colours_are_not_averaged_together():
    """Merging is only ever an improvement when the neighbours really are one
    ramp. Two flat fills that simply touch have to stay two flat fills."""
    art = Image.new("RGB", (200, 100), (40, 160, 90))
    ImageDraw.Draw(art).rectangle([100, 0, 200, 100], fill=(220, 60, 40))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">'
        '<path d="M0 0L100 0L100 100L0 100Z" fill="#28a05a"/>'
        '<path d="M100 0L200 0L200 100L100 100Z" fill="#dc3c28"/></svg>'
    ).encode()
    refined, report = gradients.refine(svg, art)
    assert report.gradients == 0, report
    assert b"Gradient" not in refined


def test_a_shape_smaller_than_the_floor_is_left_alone():
    """A handful of pixels carries no reliable evidence of a ramp."""
    art = ramp(9, 6)
    _, report = gradients.refine(flat_svg(9, 6), art)
    assert report.regions == 0 and report.gradients == 0


def test_the_source_is_resampled_into_the_documents_own_space():
    """The endpoint is handed the original bitmap, which need not be the size
    the document was traced at."""
    art = ramp(800, 400)
    refined, report = gradients.refine(flat_svg(200, 100), art)
    assert report.gradients == 1, report
    match = re.search(rb'x2="([\d.]+)"', refined)
    assert match and float(match.group(1)) <= 200.0


def test_the_seam_stroke_follows_the_fill_onto_the_gradient():
    """The gap filler seals each edge with a hairline in the fill's colour.
    Left flat while the fill becomes a ramp, it draws the old averaged colour
    right around the shape -- the seam made visible rather than hidden."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 120">'
        '<path d="M0 0L300 0L300 120L0 120Z" fill="#808080" stroke="#808080" '
        'stroke-width="1"/></svg>'
    ).encode()
    refined, report = gradients.refine(svg, ramp(300, 120))
    assert report.gradients == 1
    assert b'stroke="url(#shade0)"' in refined


def test_disabling_the_stage_returns_the_document_untouched():
    svg = flat_svg(300, 120)
    refined, report = gradients.refine(
        svg, ramp(300, 120), GradientParams(**{"gradients.enabled": False})
    )
    assert report.gradients == 0
    assert b"Gradient" not in refined


def test_stops_are_written_in_offset_order():
    """A renderer reads stops in document order; one out of sequence draws a
    band where the ramp should have been smooth."""
    refined, _ = gradients.refine(flat_svg(400, 120), ramp(400, 120))
    offsets = [float(o) for o in re.findall(rb'offset="([\d.]+)"', refined)]
    assert offsets == sorted(offsets) and offsets[0] >= 0 and offsets[-1] <= 1


def test_the_same_input_produces_the_same_file():
    """The fit subsamples large regions, so it has to do it deterministically
    or the same request comes back different every time."""
    art = ramp(500, 300)
    first, _ = gradients.refine(flat_svg(500, 300), art)
    second, _ = gradients.refine(flat_svg(500, 300), art)
    assert first == second


def test_transparent_pixels_are_not_fitted():
    """An alpha hole is not part of the shape; sampling it would fit the ramp
    to whatever the transparent pixels happen to carry underneath."""
    art = ramp(300, 120).convert("RGBA")
    hole = Image.new("RGBA", (300, 120), (0, 0, 0, 0))
    hole.paste(art.crop((0, 0, 150, 120)), (0, 0))
    refined, report = gradients.refine(flat_svg(300, 120), hole)
    # Only the opaque half carries evidence, so the fitted axis stops there.
    if report.gradients:
        match = re.search(rb'x2="([\d.]+)"', refined)
        assert match and float(match.group(1)) <= 160.0


# --- artwork the palette detector calls flat -----------------------------------


def sticker(width: int = 560, height: int = 280) -> Image.Image:
    """Flat sticker lettering whose one coloured ink shades top to bottom.

    The shape of the reported defect: everything about this reads as flat
    artwork -- a grey ground, a white halo, a near-black outline, one colour
    on the letters -- so the palette detector finds inks for it and the
    bitmap is mapped onto them. What that mapping does to the one part that
    is *not* flat is cut it into two or three pinks, which is how a letter
    comes back as hard-edged patches of two shades with a ragged step
    between them.
    """
    scale = 3
    size = (width * scale, height * scale)
    core = Image.new("L", size, 0)
    draw = ImageDraw.Draw(core)
    for x in range(60, width - 120, 140):
        draw.rounded_rectangle(
            [x * scale, int(0.18 * height) * scale,
             (x + 110) * scale, int(0.82 * height) * scale],
            radius=40 * scale, fill=255,
        )
    halo = core.filter(ImageFilter.MaxFilter(15))
    outline = core.filter(ImageFilter.MaxFilter(7))

    rows = np.arange(size[1], dtype=np.float64)[:, None]
    t = ((rows - 0.18 * height * scale) / (0.64 * height * scale)).clip(0, 1)
    ramp = np.zeros((size[1], size[0], 3))
    for channel, (a, b) in enumerate(((214, 243), (150, 218), (163, 224))):
        ramp[:, :, channel] = a + (b - a) * t

    art = Image.new("RGB", size, (128, 128, 128))
    art.paste(Image.new("RGB", size, (255, 255, 255)), (0, 0), halo)
    art.paste(Image.new("RGB", size, (26, 26, 26)), (0, 0), outline)
    art.paste(Image.fromarray(ramp.astype(np.uint8)), (0, 0), core)
    return art.resize((width, height), Image.LANCZOS)


def _build(art: Image.Image, **overrides):
    from app.schemas.params import VectorizeParams
    from app.services import engine, preprocess, svgdoc

    params = VectorizeParams.model_validate(dict(overrides))
    prepared = preprocess.prepare(png_bytes(art), params, 40_000_000)
    traced = engine.trace(prepared, params)
    used = traced.prepared or prepared
    return svgdoc.build(
        traced.svg,
        params,
        used.traced_width,
        used.traced_height,
        palette=used.palette,
        supersample=used.supersample,
        shading=used.shading or used.image,
    )


def test_the_fixture_really_is_read_as_flat_artwork():
    """The whole point of the case is that the detector calls it flat, so the
    test is worthless if it ever stops doing so."""
    from app.services.preprocess import _detect_flat_palette

    inks = _detect_flat_palette(sticker())
    assert inks is not None, "fixture no longer exercises the palette path"
    # ...and the shading is what it spends its extra inks on.
    pinks = [ink for ink in inks if ink[0] > 180 and ink[2] > 140 and ink[1] < ink[0]]
    assert len(pinks) >= 2, inks


def test_a_ramp_the_palette_cut_into_bands_is_made_even():
    """The reported defect. A derived palette is a guess that the artwork is
    flat, not an instruction, so the guess is checked against the artwork's
    own pixels -- the copy from before they were mapped onto the inks, which
    is the only one that still has the ramp in it."""
    svg, meta = _build(sticker())
    shading = meta["shading"]
    assert meta["gradients"] >= 1, meta
    assert b"Gradient" in svg
    assert shading["residual_after"] < shading["residual_before"] / 2, shading


def test_a_palette_the_caller_pinned_still_gets_no_gradients():
    """A palette the caller chose is a promise. Asking for three inks and
    getting a ramp is not what was ordered."""
    svg, meta = _build(
        sticker(), **{"processing.palette": "#808080,#ffffff,#1a1a1a,#e0b4bd"}
    )
    assert meta["gradients"] == 0, meta
    assert b"Gradient" not in svg


def test_a_colour_budget_the_caller_set_still_gets_no_gradients():
    _, meta = _build(sticker(), **{"processing.max_colors": "5"})
    assert meta["gradients"] == 0, meta


def test_a_thin_outline_is_not_ramped_to_the_ink_beside_it():
    """A traced outline is a few pixels wide with a different ink on each
    side, so much of what it covers is the blend between them. Fitted as it
    stands it comes back as a ramp from its own colour to its neighbour's --
    which put a grey wedge across the top of every black outline."""
    art = Image.new("RGB", (300, 200), (255, 255, 255))
    ImageDraw.Draw(art).rectangle([0, 90, 300, 110], fill=(20, 20, 20))
    art = art.filter(ImageFilter.GaussianBlur(2))

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 200">'
        '<path d="M0 0L300 0L300 200L0 200Z" fill="#ffffff"/>'
        '<path d="M0 90L300 90L300 110L0 110Z" fill="#141414"/></svg>'
    ).encode()
    refined, report = gradients.refine(svg, art)
    assert report.gradients == 0, report
    assert b"Gradient" not in refined


def test_a_fill_that_already_matches_the_artwork_is_left_alone():
    """A fill inside the threshold of visibility has nothing wrong with it
    that a gradient could put right, and one would cost an object."""
    art = ramp(300, 200, start=(120, 140, 160), end=(122, 142, 162))
    _, report = gradients.refine(flat_svg(300, 200), art)
    assert report.gradients == 0, report
