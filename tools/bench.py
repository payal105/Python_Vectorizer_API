"""Score the tracer against cached Vectorizer.AI output, and catch regressions.

    .venv\\Scripts\\python.exe tools/bench.py corpus/

Every win in this engine so far came from measuring against real reference
output rather than eyeballing one image, and every dead end came from
reasoning about it instead. This is that measurement, kept in the repo.

The corpus is a folder of pairs — an image and the Vectorizer.AI SVG bought
for it, sharing a stem:

    corpus/00.png   corpus/00.teacher.svg
    corpus/01.jpeg  corpus/01.teacher.svg

Use scripts/export-corpus.mjs in the mydesignbazaar repo to write one out of
the training collection. Images with no teacher beside them are still scored
against themselves, just without the bar.

    colour  mean per-pixel distance from the source; lower is better
    detail  edge agreement with the source; higher is better, and it is what
            "lost the fine detail" actually means
    leak    pixels where the backdrop shows through the artwork, which should
            be 0 — see _apply_gap_filler in app/services/svgdoc.py

    --mode        logo | artwork | auto (default auto: logo when a palette is
                  detected, matching what the web app sends)
    --save FILE   write the results as JSON, to compare against later
    --check FILE  compare against a saved run and fail on any regression

`--check` is the guard rail. Flat artwork must not move when detailed artwork
is being worked on, and this is what proves it: it reports an exact-match hash
per sample, so an unintended change anywhere shows up as CHANGED.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageChops, ImageFilter, ImageStat  # noqa: E402

from app.config import Settings  # noqa: E402
from app.schemas.params import VectorizeParams  # noqa: E402
from app.services import engine, preprocess, render, svgdoc  # noqa: E402

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
# The colour budget the web app sends per mode; see src/app/api/vectorize/route.js.
MAX_COLORS = {"logo": 8, "artwork": 0}
# Edge strength that counts as structure. Chosen so a clean flat-art boundary
# registers while JPEG grain mostly does not.
EDGE_FLOOR = 28


def _rasterize(svg: bytes, width: int, height: int) -> Image.Image | None:
    from reportlab.graphics import renderPM
    from svglib.svglib import svg2rlg

    # renderPM has no gradient support and raises partway through drawing
    # rather than ignoring one, which is why render.to_png flattens first.
    svg = render.flatten_gradients(svg)
    try:
        drawing = svg2rlg(io.BytesIO(svg))
        if drawing is None or not drawing.width or not drawing.height:
            return None
        drawing.scale(width / drawing.width, height / drawing.height)
        drawing.width, drawing.height = width, height
        return renderPM.drawToPIL(drawing, dpi=72, bg=0xFFFFFF).convert("RGB")
    except Exception:
        return None


def _colour_error(render: Image.Image, source: Image.Image) -> float:
    stat = ImageStat.Stat(ImageChops.difference(render, source))
    return sum(stat.mean) / len(stat.mean)


def _edges(image: Image.Image) -> Image.Image:
    return (
        image.convert("L")
        .filter(ImageFilter.FIND_EDGES)
        .point(lambda v: 255 if v > EDGE_FLOOR else 0)
    )


def _detail_agreement(render: Image.Image, source: Image.Image) -> float:
    a, b = _edges(render), _edges(source)
    intersection = ImageStat.Stat(ImageChops.multiply(a, b)).sum[0] / 255
    union = ImageStat.Stat(ImageChops.lighter(a, b)).sum[0] / 255
    return intersection / union * 100 if union else 0.0


def _leak(svg: bytes, width: int, height: int) -> int:
    """Pixels that differ between a black and a white backdrop.

    Anywhere the two renders disagree, the artwork did not cover the pixel and
    the page is showing through — the black hairlines that used to appear along
    every seam in an editor.
    """
    from reportlab.graphics import renderPM
    from svglib.svglib import svg2rlg

    svg = render.flatten_gradients(svg)
    shots = []
    for background in (0x000000, 0xFFFFFF):
        try:
            drawing = svg2rlg(io.BytesIO(svg))
            if drawing is None:
                return -1
            drawing.scale(width / drawing.width, height / drawing.height)
            drawing.width, drawing.height = width, height
            shots.append(
                renderPM.drawToPIL(drawing, dpi=72, bg=background).convert("RGB")
            )
        except Exception:
            return -1
    difference = ImageChops.difference(*shots).convert("L")
    return sum(
        count for value, count in enumerate(difference.histogram()) if value > 8
    )


def _trace(data: bytes, mode: str, settings: Settings) -> tuple[bytes, dict]:
    params = VectorizeParams(**{"processing.max_colors": MAX_COLORS[mode]})
    prepared = preprocess.prepare(data, params, settings.max_input_pixels)
    traced = engine.trace(prepared, params)
    used = traced.prepared or prepared
    svg, geometry = svgdoc.build(
        traced.svg,
        params,
        used.traced_width,
        used.traced_height,
        for_print=False,
        palette=used.palette,
        supersample=used.supersample,
        shading=used.image,
        source_has_alpha=used.has_transparency,
    )
    return svg, {
        "shapes": geometry["shapes"],
        "paths": geometry["paths"],
        "inks": len(used.palette or []),
        "supersample": used.supersample,
    }


def _pick_mode(requested: str, data: bytes, settings: Settings) -> str:
    if requested != "auto":
        return requested
    probe = preprocess.prepare(data, VectorizeParams(), settings.max_input_pixels)
    return "logo" if probe.palette else "artwork"


def _score(path: Path, requested_mode: str, settings: Settings) -> dict:
    data = path.read_bytes()
    source = Image.open(io.BytesIO(data)).convert("RGB")
    width, height = source.size
    mode = _pick_mode(requested_mode, data, settings)

    svg, facts = _trace(data, mode, settings)
    render = _rasterize(svg, width, height)
    row = {
        "mode": mode,
        "bytes": len(svg),
        "sha": hashlib.sha256(svg).hexdigest()[:16],
        "leak": _leak(svg, width, height),
        **facts,
    }
    if render is not None:
        row["colour"] = round(_colour_error(render, source), 3)
        row["detail"] = round(_detail_agreement(render, source), 2)

    teacher = path.with_suffix("").with_suffix(".teacher.svg")
    if not teacher.exists():
        teacher = path.parent / (path.stem + ".teacher.svg")
    if teacher.exists():
        shot = _rasterize(teacher.read_bytes(), width, height)
        if shot is not None:
            row["teacher_colour"] = round(_colour_error(shot, source), 3)
            row["teacher_detail"] = round(_detail_agreement(shot, source), 2)
    return row


def _images(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(
                p for p in sorted(path.iterdir())
                if p.suffix.lower() in IMAGE_SUFFIXES
            )
        elif path.suffix.lower() in IMAGE_SUFFIXES:
            found.append(path)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", type=Path, nargs="+", help="images or folders")
    parser.add_argument("--mode", choices=("auto", "logo", "artwork"), default="auto")
    parser.add_argument("--save", type=Path, help="write results as JSON")
    parser.add_argument("--check", type=Path, help="compare against a saved run")
    args = parser.parse_args()

    images = _images(args.paths)
    if not images:
        print("no images found", file=sys.stderr)
        return 2

    settings = Settings()
    results: dict[str, dict] = {}
    print(f"  {'file':22s} {'mode':8s} {'inks':>4s} {'shapes':>6s} {'paths':>5s} "
          f"{'colour':>7s} {'detail':>7s} {'leak':>6s} {'KB':>6s}   vs teacher")
    for path in images:
        try:
            row = _score(path, args.mode, settings)
        except Exception as exc:  # a bad file must not abandon the run
            print(f"  {path.name:22s} FAILED {type(exc).__name__}: {exc}")
            continue
        results[path.stem] = row
        against = ""
        if "teacher_colour" in row and "colour" in row:
            against = (f"colour {row['colour'] - row['teacher_colour']:+.3f}  "
                       f"detail {row['detail'] - row['teacher_detail']:+.2f}")
        print(f"  {path.name:22s} {row['mode']:8s} {row['inks']:4d} "
              f"{row['shapes']:6d} {row['paths']:5d} "
              f"{row.get('colour', float('nan')):7.3f} "
              f"{row.get('detail', float('nan')):6.2f}% {row['leak']:6d} "
              f"{row['bytes']/1024:6.1f}   {against}")

    scored = [r for r in results.values() if "colour" in r]
    if scored:
        print(f"\n  {len(scored)} scored   "
              f"mean colour {sum(r['colour'] for r in scored)/len(scored):.3f}   "
              f"mean detail {sum(r['detail'] for r in scored)/len(scored):.2f}%   "
              f"total leak {sum(max(0, r['leak']) for r in scored)}")
        withbar = [r for r in scored if "teacher_colour" in r]
        if withbar:
            print(f"  {len(withbar)} with a teacher   "
                  f"teacher mean colour "
                  f"{sum(r['teacher_colour'] for r in withbar)/len(withbar):.3f}   "
                  f"mean detail "
                  f"{sum(r['teacher_detail'] for r in withbar)/len(withbar):.2f}%")

    if args.save:
        args.save.write_text(json.dumps(results, indent=1))
        print(f"\n  saved {len(results)} results to {args.save}")

    if args.check:
        was = json.loads(args.check.read_text())
        changed = [k for k, v in results.items()
                   if k in was and v["sha"] != was[k]["sha"]]
        missing = [k for k in was if k not in results]
        print(f"\n  identical {len(results) - len(changed)}   changed {len(changed)}")
        for key in changed:
            old, new = was[key], results[key]
            print(f"    {key:20s} shapes {old['shapes']:5d} -> {new['shapes']:5d}   "
                  f"colour {old.get('colour', 0):.3f} -> {new.get('colour', 0):.3f}   "
                  f"detail {old.get('detail', 0):.2f} -> {new.get('detail', 0):.2f}")
        if missing:
            print(f"    not re-run: {', '.join(missing)}")
        return 1 if changed else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
