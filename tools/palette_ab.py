"""Measure every corpus image traced with the detected palette and without it.

Mapping the pixels onto a palette before tracing is the largest single
decision this pipeline makes, and it is currently made for every image the
detector accepts. Ablation says that is wrong: on some artwork the palette is
worth a point or two, and on others it costs thirty, because banding the
colours gives the tracer an edge to outline everywhere two bands meet.

This prints both numbers per image so the decision can be made on evidence
rather than on one gate.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402

import bench  # noqa: E402
from app.config import Settings  # noqa: E402
from app.schemas.params import VectorizeParams  # noqa: E402
from app.services import engine, preprocess, svgdoc  # noqa: E402

CACHE = Path(__file__).resolve().parent / ".reference-cache"
SETTINGS: Settings | None = None
_REAL_DETECT = None


def _init() -> None:
    global SETTINGS, _REAL_DETECT
    SETTINGS = Settings()
    _REAL_DETECT = preprocess._detect_flat_palette


def _trace(path: Path, with_palette: bool):
    preprocess._detect_flat_palette = (
        _REAL_DETECT if with_palette else (lambda rgb: None))
    params = VectorizeParams()
    data = path.read_bytes()
    prepared = preprocess.prepare(data, params, SETTINGS.max_input_pixels)
    traced = engine.trace(prepared, params)
    used = traced.prepared or prepared
    svg, _ = svgdoc.build(
        traced.svg, params, used.traced_width, used.traced_height,
        for_print=False, palette=used.palette, supersample=used.supersample,
        shading=used.image, source_has_alpha=used.has_transparency)
    return svg, used


def _score(path: Path, with_palette: bool) -> tuple[float, float, int]:
    reference = Image.open(CACHE / f"{path.stem}.png").convert("RGB")
    svg, used = _trace(path, with_palette)
    shot = bench._rasterize(svg, reference.width, reference.height)
    if shot is None:
        return 0.0, 999.0, 0
    return (bench._detail_agreement(shot, reference),
            bench._colour_error(shot, reference),
            len(used.palette or []))


def _row(name: str) -> dict:
    path = Path(name)
    try:
        on = _score(path, True)
        off = _score(path, False)
    except Exception as error:
        return {"file": path.stem, "error": repr(error)}
    finally:
        preprocess._detect_flat_palette = _REAL_DETECT
    return {"file": path.stem,
            "on_match": round(on[0], 2), "on_colour": round(on[1], 3),
            "inks": on[2],
            "off_match": round(off[0], 2), "off_colour": round(off[1], 3),
            "delta": round(off[0] - on[0], 2)}


def main() -> int:
    images = [p for p in bench._images([Path("corpus")])
              if (CACHE / f"{p.stem}.png").exists()]
    workers = max(1, mp.cpu_count() - 1)
    print(f"{len(images)} images, {workers} workers\n", flush=True)
    with mp.Pool(workers, initializer=_init) as pool:
        rows = pool.map(_row, [str(p) for p in images], chunksize=1)

    good = [r for r in rows if "delta" in r]
    good.sort(key=lambda r: -r["delta"])
    print(f"  {'file':>6} {'inks':>5} {'palette on':>11} {'palette off':>12} "
          f"{'change':>8}")
    for r in good:
        print(f"  {r['file']:>6} {r['inks']:>5} {r['on_match']:10.2f}% "
              f"{r['off_match']:11.2f}% {r['delta']:+8.2f}")

    helped = [r for r in good if r["delta"] > 1]
    hurt = [r for r in good if r["delta"] < -1]
    print(f"\n  palette OFF is better on {len(helped)} images "
          f"(best {good[0]['delta']:+.1f})")
    print(f"  palette ON  is better on {len(hurt)} images "
          f"(best {good[-1]['delta']:+.1f})")
    now = sum(r["on_match"] for r in good) / len(good)
    best = sum(max(r["on_match"], r["off_match"]) for r in good) / len(good)
    print(f"\n  mean match now              {now:.2f}%")
    print(f"  mean if each image chose right {best:.2f}%   "
          f"({best - now:+.2f} points available)")
    Path("palette_ab.json").write_text(json.dumps(rows, indent=2),
                                       encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
