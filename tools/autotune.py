"""Search the engine's settings for the ones that best match the reference.

    .venv\\Scripts\\python.exe tools/autotune.py corpus/

The target is the Vectorizer.AI output cached beside each corpus image, not
the photograph it came from. That is a deliberate choice and it matters.

Scoring against the source rewards reproducing whatever is in the source,
including its JPEG noise -- app/services/adaptive.py says so in as many words.
It also has nothing to say about the judgement calls a good tracer makes:
how much shading to keep, where to round a corner, when two near-identical
fills are really one. The reference made those calls, they were reviewed, and
they are what we are being asked to match. So the reference is the target.

Three things are measured, all against the reference render:

    match    do our edges land where the reference's edges land (higher
             better). This is "did we keep the same detail".
    colour   how far our colours sit from the reference's (lower better).
    bumps    how much geometry we spend for every unit the reference spends.
             1.0 is parity. Well above it is the jagged edges, speckle and
             stray bumps -- anti-aliasing being traced instead of removed.

A setting is only kept if it improves *match* without making colour worse and
without inflating bumps, so the search cannot win by tracing more rubbish.

The winner is re-measured on the whole corpus, not the sample it was searched
on, before it is reported.

    --sample N    search on N images (stratified by how far off they are now),
                  then validate the winner on all of them. Default 24.
    --rounds N    how many times to walk the whole setting list. Default 3.
    --workers N   parallel processes. Default: one fewer than the CPU count.
"""

from __future__ import annotations

import argparse
import io
import json
import multiprocessing as mp
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402

import bench  # noqa: E402
from app.config import Settings  # noqa: E402
from app.schemas.params import VectorizeParams  # noqa: E402
from app.services import engine, preprocess, svgdoc  # noqa: E402

# Rendering the reference is pure overhead repeated on every trial, and it
# never changes, so it is done once and kept on disk.
CACHE = Path(__file__).resolve().parent / ".reference-cache"

_NODE_RE = re.compile(rb"[MmLlHhVvCcSsQqTtAa]")
_D_RE = re.compile(rb'\sd="([^"]*)"')

# The settings to search, and the values to try for each. Names beginning with
# an underscore are module constants in app/services/preprocess.py; the rest
# are ordinary request parameters. The first value in each list is what ships
# today, so a round that finds nothing changes nothing.
KNOBS: list[tuple[str, list]] = [
    ("_FLAT_MAX_INKS", [16, 20, 24, 32, 48]),
    ("_FLAT_MAX_RESIDUAL", [5.0, 6.5, 8.0, 10.0, 14.0]),
    ("_MIN_INK_SEPARATION", [30.0, 20.0, 25.0, 36.0]),
    ("_INK_SOLID_FLOOR", [0.001, 0.0002, 0.0005, 0.002]),
    ("_BLEND_TOLERANCE", [12.0, 8.0, 16.0, 20.0]),
    ("_INK_MIN_SHARE", [0.0005, 0.0002, 0.001]),
    ("processing.denoise", ["low", "none", "medium"]),
    ("processing.detail", ["standard", "low", "maximum"]),
    ("processing.color_merge", [16.0, 0.0, 8.0, 24.0, 40.0]),
    ("processing.shapes.min_area_px", [4, 1, 2, 8]),
    ("processing.smoothing", ["low", "none", "medium"]),
    ("processing.corner_threshold", [60, 30, 45, 80]),
    ("processing.length_threshold", [4.0, 3.5, 5.0, 7.0]),
    ("processing.color_precision", [6, 5, 7, 8]),
    ("processing.layer_difference", [16, 8, 12, 24]),
]

SETTINGS: Settings | None = None
_BASE_CONSTANTS: dict[str, object] = {}


def _nodes(svg: bytes) -> int:
    """How many drawing commands a document spends in total."""
    return sum(len(_NODE_RE.findall(d)) for d in _D_RE.findall(svg))


def _reference_for(path: Path) -> Path | None:
    candidate = path.parent / (path.stem + ".teacher.svg")
    return candidate if candidate.exists() else None


def build_cache(images: list[Path]) -> dict[str, dict]:
    """Render every reference once and remember what it costs to draw.

    Returns the facts each trial is scored against, keyed by image stem.
    """
    CACHE.mkdir(exist_ok=True)
    facts: dict[str, dict] = {}
    for path in images:
        reference = _reference_for(path)
        if reference is None:
            continue
        shot = CACHE / f"{path.stem}.png"
        if not shot.exists():
            source = Image.open(path)
            rendered = bench._rasterize(
                reference.read_bytes(), source.width, source.height)
            if rendered is None:
                continue
            rendered.save(shot)
        facts[path.stem] = {"nodes": _nodes(reference.read_bytes())}
    return facts


def _worker_init(facts: dict) -> None:
    global SETTINGS, REFERENCE
    SETTINGS = Settings()
    REFERENCE = facts
    for name, _ in KNOBS:
        if name.startswith("_"):
            _BASE_CONSTANTS[name] = getattr(preprocess, name)


def _apply(config: dict) -> VectorizeParams:
    """Put *config* into effect and return the request parameters from it."""
    for name, base in _BASE_CONSTANTS.items():
        setattr(preprocess, name, config.get(name, base))
    overrides = {k: v for k, v in config.items() if not k.startswith("_")}
    return VectorizeParams(**overrides)


def _trace(path: Path, params: VectorizeParams) -> bytes:
    data = path.read_bytes()
    prepared = preprocess.prepare(data, params, SETTINGS.max_input_pixels)
    traced = engine.trace(prepared, params)
    used = traced.prepared or prepared
    svg, _ = svgdoc.build(
        traced.svg, params, used.traced_width, used.traced_height,
        for_print=False, palette=used.palette, supersample=used.supersample,
        shading=used.image, source_has_alpha=used.has_transparency,
    )
    return svg


# What a failed trace scores. Deliberately terrible in every direction so the
# search moves away from a configuration that cannot finish.
FAILED = (0.0, 999.0, 9.0)


def _measure(task: tuple[str, dict]) -> tuple[str, float, float, float]:
    """Trace one image and score it against the reference. Runs in a pool."""
    name, config = task
    path = Path(name)
    try:
        reference = Image.open(CACHE / f"{path.stem}.png").convert("RGB")
        svg = _trace(path, _apply(config))
        shot = bench._rasterize(svg, reference.width, reference.height)
        if shot is None:
            return (name, *FAILED)
        reference_nodes = REFERENCE.get(path.stem, {}).get("nodes") or 1
        return (name,
                bench._detail_agreement(shot, reference),
                bench._colour_error(shot, reference),
                _nodes(svg) / reference_nodes)
    except Exception:
        return (name, *FAILED)


class Score:
    """What one configuration achieved, and whether it beats another."""

    __slots__ = ("match", "colour", "bumps")

    def __init__(self, match: float, colour: float, bumps: float):
        self.match, self.colour, self.bumps = match, colour, bumps

    def __str__(self) -> str:
        return (f"match {self.match:6.2f}%  colour {self.colour:6.3f}  "
                f"bumps {self.bumps:5.2f}x")


# How much worse colour may get while chasing a closer match. Small, because
# colour is the guard against "more edges" meaning "more traced noise".
COLOUR_SLACK = 1.02
# And how much more geometry than the reference we will tolerate before a
# result counts as over-traced no matter how well it scores.
BUMPS_CEILING = 1.60
# How much a value has to add before it displaces the shipped one. Below this
# the two are the same result with different numbers on it.
MATCH_MARGIN = 0.15


def _better(trial: Score, best: Score, shipped: Score) -> bool:
    """Whether *trial* is a real improvement rather than a trade."""
    return (trial.match > best.match + MATCH_MARGIN
            and trial.colour <= shipped.colour * COLOUR_SLACK
            and trial.bumps <= max(BUMPS_CEILING, shipped.bumps))


class Search:
    """One coordinate-descent search over an image set."""

    def __init__(self, images: list[Path]):
        self.images = [str(p) for p in images]
        self.evaluations = 0

    def evaluate(self, pool, config: dict) -> Score:
        rows = pool.map(_measure, [(n, config) for n in self.images], chunksize=1)
        self.evaluations += 1
        count = len(rows)
        return Score(sum(r[1] for r in rows) / count,
                     sum(r[2] for r in rows) / count,
                     sum(r[3] for r in rows) / count)


def _stratified(scores: dict, images: list[Path], count: int) -> list[Path]:
    """A sample spanning the whole range of how bad things currently are.

    Searching only on the worst images tunes for them and quietly wrecks the
    ones that already work, which is what this harness exists to prevent.
    """
    def gap(path: Path) -> float:
        row = scores.get(path.stem, {})
        if "teacher_detail" in row and "detail" in row:
            return row["teacher_detail"] - row["detail"]
        return 0.0

    ordered = sorted(images, key=gap, reverse=True)
    if count >= len(ordered):
        return ordered
    step = len(ordered) / count
    return [ordered[int(i * step)] for i in range(count)]


def _walk(search: Search, pool, config: dict, shipped: Score, log: list,
          skip_until: str = "") -> dict:
    """One pass over every setting, keeping any value that measurably helps."""
    best = search.evaluate(pool, config)
    skipping = bool(skip_until)
    for name, values in KNOBS:
        if skipping:
            if name == skip_until:
                skipping = False
            print(f"    (already searched: {name})", flush=True)
            continue
        current = config.get(name, values[0])
        winner, winning = current, best
        for value in values:
            if value == current:
                continue
            trial = search.evaluate(pool, dict(config, **{name: value}))
            gain = _better(trial, winning, shipped)
            print(f"    {name} = {value:<14} {trial}"
                  f"{'   <-- better' if gain else ''}", flush=True)
            if gain:
                winner, winning = value, trial
        if winner != current:
            config[name] = winner
            best = winning
            log.append({"setting": name, "from": current, "to": winner,
                        "match": round(best.match, 2)})
            print(f"  KEEP {name} = {winner}   match now {best.match:.2f}%",
                  flush=True)
    return config


def _report(shipped: Score, tuned: Score, config: dict, out: Path,
            result: dict) -> None:
    print(f"\n  shipped   {shipped}")
    print(f"  tuned     {tuned}")
    print(f"  change    match {tuned.match - shipped.match:+.2f} points   "
          f"colour {tuned.colour - shipped.colour:+.3f}   "
          f"bumps {tuned.bumps - shipped.bumps:+.2f}x")
    if not config:
        print("\n  the shipped settings won; nothing to change")
    else:
        print("\n  settings that won:")
        for name, value in config.items():
            print(f"    {name} = {value}")
    print(f"\n  wrote {out}  ({result['minutes']} min)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="+")
    parser.add_argument("--sample", type=int, default=24)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--workers", type=int, default=max(1, mp.cpu_count() - 1))
    parser.add_argument("--baseline", type=Path,
                        help="a bench --save JSON, used to stratify the sample")
    parser.add_argument("--out", type=Path, default=Path("autotune.json"))
    parser.add_argument("--start", type=str, default="",
                        help="JSON of settings already adopted, to resume from")
    parser.add_argument("--from-setting", type=str, default="",
                        help="skip every setting up to and including this one "
                             "on the first round")
    args = parser.parse_args()

    every = [p for p in bench._images(args.paths) if _reference_for(p)]
    if not every:
        print("no images with a .teacher.svg beside them")
        return 1

    print(f"rendering {len(every)} references (once)...", flush=True)
    facts = build_cache(every)
    scores = json.loads(args.baseline.read_text()) if args.baseline else {}
    sample = _stratified(scores, every, args.sample)
    print(f"corpus {len(every)} · searching on {len(sample)} · "
          f"{args.workers} workers\n", flush=True)

    started = time.time()
    config: dict = json.loads(args.start) if args.start else {}
    log: list = []
    if config:
        print(f"resuming from {config}", flush=True)
    with mp.Pool(args.workers, initializer=_worker_init, initargs=(facts,)) as pool:
        full = Search(every)
        shipped = full.evaluate(pool, {})
        print(f"shipped settings, all {len(every)}: {shipped}\n", flush=True)

        search = Search(sample)
        sample_shipped = search.evaluate(pool, {})
        for number in range(1, args.rounds + 1):
            print(f"round {number}", flush=True)
            before = dict(config)
            config = _walk(search, pool, config, sample_shipped, log,
                           args.from_setting if number == 1 else "")
            if config == before:
                print("  nothing moved; stopping early", flush=True)
                break

        print("\nvalidating the winner on the whole corpus...", flush=True)
        tuned = full.evaluate(pool, config)

    result = {
        "config": config,
        "changes": log,
        "shipped": {"match": round(shipped.match, 2),
                    "colour": round(shipped.colour, 3),
                    "bumps": round(shipped.bumps, 2)},
        "tuned": {"match": round(tuned.match, 2),
                  "colour": round(tuned.colour, 3),
                  "bumps": round(tuned.bumps, 2)},
        "images": len(every),
        "sample": len(sample),
        "minutes": round((time.time() - started) / 60, 1),
    }
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    _report(shipped, tuned, config, args.out, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
