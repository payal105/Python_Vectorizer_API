"""Score the engine as it currently stands against every cached reference.

The one number that decides whether a change ships, plus the per-image
detail so a change that lifts the average by wrecking a few images is
visible rather than hidden inside a mean.

    python tools/validate.py                 measure and print
    python tools/validate.py --save was.json  keep it to compare against
    python tools/validate.py --against was.json   show what moved
"""
from __future__ import annotations

import argparse
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


def _init() -> None:
    global SETTINGS
    SETTINGS = Settings()


def _row(name: str) -> dict:
    path = Path(name)
    try:
        params = VectorizeParams()
        data = path.read_bytes()
        prepared = preprocess.prepare(data, params, SETTINGS.max_input_pixels)
        traced = engine.trace(prepared, params)
        used = traced.prepared or prepared
        svg, _ = svgdoc.build(
            traced.svg, params, used.traced_width, used.traced_height,
            for_print=False, palette=used.palette, supersample=used.supersample,
            shading=used.image, source_has_alpha=used.has_transparency)
        reference = Image.open(CACHE / f"{path.stem}.png").convert("RGB")
        shot = bench._rasterize(svg, reference.width, reference.height)
        if shot is None:
            return {"file": path.stem, "error": "did not render"}
        return {"file": path.stem,
                "match": round(bench._detail_agreement(shot, reference), 2),
                "colour": round(bench._colour_error(shot, reference), 3),
                "inks": len(used.palette or []),
                "kb": round(len(svg) / 1024, 1)}
    except Exception as error:
        return {"file": path.stem, "error": repr(error)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", type=Path)
    parser.add_argument("--against", type=Path)
    parser.add_argument("--workers", type=int, default=max(1, mp.cpu_count() - 1))
    args = parser.parse_args()

    images = [p for p in bench._images([Path("corpus")])
              if (CACHE / f"{p.stem}.png").exists()]
    with mp.Pool(args.workers, initializer=_init) as pool:
        rows = pool.map(_row, [str(p) for p in images], chunksize=1)

    good = [r for r in rows if "match" in r]
    match = sum(r["match"] for r in good) / len(good)
    colour = sum(r["colour"] for r in good) / len(good)
    print(f"\n  {len(good)} images   mean match {match:.2f}%   "
          f"mean colour {colour:.3f}")

    if args.against and args.against.exists():
        old = {r["file"]: r for r in json.loads(args.against.read_text())
               if "match" in r}
        moved = [(r["file"], r["match"] - old[r["file"]]["match"])
                 for r in good if r["file"] in old]
        was = sum(old[f]["match"] for f, _ in moved) / len(moved)
        print(f"  was {was:.2f}%   now {match:.2f}%   "
              f"{match - was:+.2f} points")
        better = [m for m in moved if m[1] > 0.5]
        worse = sorted([m for m in moved if m[1] < -0.5], key=lambda m: m[1])
        print(f"  improved {len(better)} · worse {len(worse)} · "
              f"unchanged {len(moved) - len(better) - len(worse)}")
        if worse:
            print("  biggest losses: " +
                  ", ".join(f"{f} {d:+.1f}" for f, d in worse[:6]))
        if better:
            top = sorted(better, key=lambda m: -m[1])[:6]
            print("  biggest gains:  " +
                  ", ".join(f"{f} {d:+.1f}" for f, d in top))

    if args.save:
        args.save.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"  saved {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
