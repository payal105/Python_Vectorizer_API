"""Sweep vectorize settings over one image and compare the results.

    .venv\\Scripts\\python.exe tools/tune.py samples/my-logo.png

Writes a side-by-side PNG and one SVG per candidate into tools/out/, and
prints a table of path counts, colour counts and file sizes so you can see
what each setting costs before committing to it.

Runs the real pipeline in-process; no server needed.

    --zoom X,Y      centre the comparison crop on this point in the source
    --only a,b      run just these candidates by name
    --dpi N         rasterization density for the comparison (default 300)
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from app.config import Settings  # noqa: E402
from app.schemas.params import VectorizeParams  # noqa: E402
from app.services import engine, preprocess, render, svgdoc  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"

# Named starting points. "defaults" is whatever the API ships with today; the
# rest trade cleanliness against detail in both directions.
CANDIDATES: dict[str, dict[str, str]] = {
    "defaults": {},
    # Validated on a real 1600px JPEG of flat hand-lettering: took the
    # near-white ghost fringe clinging to every letter from 13 stray shapes
    # down to 3, and removed the floating specks. The cost is that the white
    # outline merges into the cream it borders.
    "clean": {
        "processing.denoise": "medium",
        "processing.detail": "low",
        "processing.color_merge": "40",
    },
    "balanced": {
        "processing.denoise": "low",
        "processing.detail": "high",
        "processing.color_merge": "24",
    },
    "detailed": {
        "processing.denoise": "none",
        "processing.detail": "high",
        "processing.color_merge": "12",
    },
    "max-detail": {
        "processing.denoise": "none",
        "processing.detail": "maximum",
        "processing.color_merge": "0",
        "processing.color_precision": "8",
    },
    "flat-logo": {
        "processing.denoise": "low",
        "processing.detail": "high",
        "processing.max_colors": "12",
        "processing.color_merge": "24",
    },
    # For shaded/painted artwork. Without an explicit colour budget the
    # tracer's own clustering collapses smooth shading into one flat blob --
    # a shaded sphere came back as a single colour. Pinning max_colors keeps
    # the shading as clean bands, and their boundaries trace as smooth curves
    # rather than the pixel staircase you get from ragged gradient edges.
    "shaded": {
        "processing.max_colors": "12",
        "processing.denoise": "medium",
        "processing.color_merge": "24",
        "processing.detail": "high",
    },
    "shaded-rich": {
        "processing.max_colors": "24",
        "processing.denoise": "medium",
        "processing.color_merge": "16",
        "processing.detail": "high",
    },
}


def suggest_palette(image: Image.Image, count: int) -> list[str]:
    """Propose a palette for artwork that is flat colour plus JPEG noise.

    Quantizing by pixel count alone is unreliable here: a dominant background
    swallows the budget and starves small but important colours. Taking a
    generous palette first and then keeping the most-used entries recovers
    the real ink colours.
    """
    # The first pass has to be very generous. Quantizing straight to N lets a
    # dominant background eat the budget: at 24 entries a pink and a white
    # collapsed into one muddy mauve, losing both.
    generous = image.convert("RGB").quantize(
        colors=256,
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.NONE,
    )
    table = generous.getpalette() or []
    counts = sorted(
        generous.getcolors(1 << 20) or [], key=lambda item: -item[0]
    )
    chosen: list[str] = []
    for _, index in counts:
        r, g, b = table[index * 3 : index * 3 + 3]
        hexed = f"#{r:02x}{g:02x}{b:02x}"
        # Skip anything nearly identical to a colour already chosen.
        if any(
            sum((a - c) ** 2 for a, c in zip((r, g, b), _hex_rgb(prev))) ** 0.5 < 40
            for prev in chosen  # distinct enough to be a real ink colour
        ):
            continue
        chosen.append(hexed)
        if len(chosen) >= count:
            break
    return chosen


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def busiest_point(image: Image.Image, grid: int = 24) -> tuple[int, int]:
    """Pick the most detailed spot, so the crop never lands on flat background.

    Scores each cell of a coarse grid by edge energy and returns the centre of
    the winner, in source coordinates.
    """
    from PIL import ImageFilter

    edges = image.convert("L").filter(ImageFilter.FIND_EDGES)
    cell_w = max(1, image.width // grid)
    cell_h = max(1, image.height // grid)
    best, best_score = (image.width // 2, image.height // 2), -1.0
    for gy in range(grid):
        for gx in range(grid):
            box = (gx * cell_w, gy * cell_h, (gx + 1) * cell_w, (gy + 1) * cell_h)
            if box[2] > image.width or box[3] > image.height:
                continue
            score = sum(i * n for i, n in enumerate(edges.crop(box).histogram()))
            if score > best_score:
                best_score = score
                best = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    return best


def run(data: bytes, overrides: dict[str, str], settings: Settings):
    params = VectorizeParams.model_validate(dict(overrides))
    prepared = preprocess.prepare(data, params, settings.max_input_pixels)
    traced = engine.trace(prepared, params)
    svg, meta = svgdoc.build(
        traced.svg,
        params,
        prepared.traced_width,
        prepared.traced_height,
        palette=prepared.palette,
    )
    return params, prepared, svg, meta


def rasterize(svg: bytes, params: VectorizeParams, settings: Settings, dpi: int):
    raster_params = params.model_copy(
        update={"output_file_format": "png", "output_bitmap_dpi": dpi}
    )
    png = render.to_png(svg, raster_params, settings)
    return Image.open(io.BytesIO(png)).convert("RGB")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--zoom", help="X,Y centre point in source pixels")
    parser.add_argument("--only", help="comma-separated candidate names")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--crop", type=int, default=420, help="crop size in output px")
    parser.add_argument(
        "--palette",
        type=int,
        metavar="N",
        help="suggest an N-colour palette for this image and exit",
    )
    args = parser.parse_args()

    if not args.image.exists():
        print(f"No such file: {args.image}")
        return 1

    data = args.image.read_bytes()
    settings = Settings(max_input_pixels=40_000_000)
    OUT.mkdir(parents=True, exist_ok=True)

    names = (
        [n.strip() for n in args.only.split(",")] if args.only else list(CANDIDATES)
    )
    source = Image.open(io.BytesIO(data)).convert("RGB")
    print(f"\n{args.image.name}  {source.width}x{source.height}\n")

    if args.palette:
        colours = suggest_palette(source, args.palette)
        print(f"  suggested {len(colours)}-colour palette:\n")
        print("    processing.palette=" + ",".join(colours) + "\n")
        print("  Pinning the palette is the strongest fix for ghost layers:")
        print("  every shape lands on one of these colours, so an editor sees")
        print("  exactly this many layers and no near-duplicate extras.\n")
        return 0

    print(f"  {'candidate':12s} {'paths':>7s} {'colours':>8s} {'svg':>10s}  settings")

    result_width = round(source.width * args.dpi / 96)
    tiles: list[tuple[str, Image.Image]] = [
        (
            "source",
            source.resize(
                (result_width, round(result_width * source.height / source.width)),
                Image.Resampling.NEAREST,
            ),
        )
    ]
    for name in names:
        overrides = CANDIDATES.get(name)
        if overrides is None:
            print(f"  {name}: unknown candidate")
            continue
        try:
            params, prepared, svg, meta = run(data, overrides, settings)
        except Exception as exc:  # keep sweeping even if one setting fails
            print(f"  {name:12s} FAILED: {type(exc).__name__}: {exc}")
            continue

        colours = len(set(re.findall(rb'fill="(#[0-9a-fA-F]{6})"', svg)))
        (OUT / f"{args.image.stem}.{name}.svg").write_bytes(svg)
        summary = " ".join(f"{k.split('.')[-1]}={v}" for k, v in overrides.items())
        print(
            f"  {name:12s} {meta['paths']:>7} {colours:>8} "
            f"{len(svg) / 1024:>9.1f}K  {summary or '(as shipped)'}"
        )
        tiles.append((name, rasterize(svg, params, settings, args.dpi)))

    # Comparison strip, cropped to the same region of every result.
    scale = tiles[-1][1].width / source.width if len(tiles) > 1 else 1
    if args.zoom:
        cx, cy = (int(v) for v in args.zoom.split(","))
    else:
        cx, cy = busiest_point(source)
    half = args.crop // 2

    strip: list[tuple[str, Image.Image]] = []
    for label, image in tiles:
        factor = image.width / source.width
        x, y = int(cx * factor), int(cy * factor)
        box = (
            max(0, x - half),
            max(0, y - half),
            min(image.width, x + half),
            min(image.height, y + half),
        )
        strip.append((label, image.crop(box).resize((args.crop, args.crop))))

    canvas = Image.new("RGB", (len(strip) * (args.crop + 8), args.crop + 22), "white")
    draw = ImageDraw.Draw(canvas)
    for i, (label, image) in enumerate(strip):
        canvas.paste(image, (i * (args.crop + 8), 20))
        draw.text((i * (args.crop + 8) + 4, 5), label, fill=(0, 0, 0))
    comparison = OUT / f"{args.image.stem}.compare.png"
    canvas.save(comparison)

    print(f"\n  wrote {comparison}")
    print(f"  wrote {len(tiles) - 1} SVGs to {OUT}")
    print(f"  crop centred on ({cx},{cy}) - re-run with --zoom X,Y to inspect elsewhere\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
