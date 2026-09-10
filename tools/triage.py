"""Rank every corpus image by how far it is from the reference, and show why.

    .venv\\Scripts\\python.exe tools/triage.py corpus/ --out triage

tools/bench.py answers "did the corpus get better or worse". It cannot answer
"which images are broken, and broken *how*", and that question was being
answered by hand: convert ten images, open each PDF, hunt for the part that
went wrong, screenshot it. That does not scale past a handful and it is not
repeatable, so the same failure gets rediscovered instead of tracked.

This writes one HTML page instead. Every image gets a row -- source, our
trace, the Vectorizer.AI reference, and the edge disagreement between ours and
the source -- sorted worst-first by how far our detail falls short of the
reference. Each row also carries a crop of the single worst region, found by
scoring a grid over the disagreement, which is the part a person would have
gone looking for.

The failure labels come from measurements, not impressions:

    flattened     detail well short of the reference, and fewer shapes than it
                  used -- modelling collapsed into silhouette
    over-traced   detail short but *more* shapes than the reference -- the
                  ragged edges and speckle patterns, anti-aliasing being
                  traced instead of removed
    palette off   colour error well above the reference's
    gaps          the backdrop shows through the artwork (see bench --leak)
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageChops, ImageStat  # noqa: E402

import bench  # noqa: E402
from app.config import Settings  # noqa: E402

# Tiles per side when hunting for the worst region. Six keeps each crop big
# enough to read at a glance on a large logo without averaging the problem
# away, which is what a coarser grid does.
GRID = 6
THUMB = 300
CROP_MIN = 96

# How far behind the reference counts as a real shortfall rather than noise.
DETAIL_GAP = 6.0
COLOUR_GAP = 0.8
# Shape counts either side of the reference's, beyond which the trace is
# spending too few shapes (flattening) or too many (tracing the noise).
FEW_SHAPES = 0.8
MANY_SHAPES = 1.3

# Geometry is compared by drawing commands rather than by element count,
# because element count says more about how a writer groups its output
# than about how much detail it kept: output.combine_paths merges every
# region of one colour into a single <path>, so the lens logo emits 34
# elements against the reference's 625 while drawing a comparable amount.
# Command letters survive that grouping.
_NODE_RE = re.compile(rb"[MmLlHhVvCcSsQqTtAa]")
_D_RE = re.compile(rb'\sd="([^"]*)"')


def _nodes(svg: bytes) -> int:
    """How many drawing commands the document spends in total."""
    return sum(len(_NODE_RE.findall(d)) for d in _D_RE.findall(svg))


def _data_uri(image: Image.Image, width: int = THUMB) -> str:
    """A PNG small enough to embed, so the report is one portable file."""
    if image.width > width:
        height = max(1, round(image.height * width / image.width))
        image = image.resize((width, height), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _teacher_for(path: Path) -> Path | None:
    candidate = path.parent / (path.stem + ".teacher.svg")
    return candidate if candidate.exists() else None


def _disagreement(ours: Image.Image, source: Image.Image) -> Image.Image:
    """Where our edges and the source's edges fail to line up."""
    a, b = bench._edges(ours), bench._edges(source)
    return ImageChops.difference(a, b)


def _worst_tile(mark: Image.Image) -> tuple[int, int, int, int]:
    """The grid tile holding the most disagreement, as a crop box."""
    width, height = mark.size
    step_x, step_y = max(1, width // GRID), max(1, height // GRID)
    best, box = -1.0, (0, 0, min(width, CROP_MIN), min(height, CROP_MIN))
    for row in range(GRID):
        for column in range(GRID):
            here = (column * step_x, row * step_y,
                    min(width, (column + 1) * step_x),
                    min(height, (row + 1) * step_y))
            if here[2] <= here[0] or here[3] <= here[1]:
                continue
            score = ImageStat.Stat(mark.crop(here)).sum[0]
            if score > best:
                best, box = score, here
    return _padded(box, width, height)


def _padded(box: tuple[int, int, int, int], width: int, height: int):
    """Grow a tile to CROP_MIN so a small one still shows its surroundings."""
    left, top, right, bottom = box
    grow_x = max(0, CROP_MIN - (right - left)) // 2
    grow_y = max(0, CROP_MIN - (bottom - top)) // 2
    return (max(0, left - grow_x), max(0, top - grow_y),
            min(width, right + grow_x), min(height, bottom + grow_y))


def _labels(row: dict) -> list[str]:
    """What went wrong, from the numbers rather than from looking."""
    found = []
    detail_gap = row.get("detail_gap")
    ratio = row.get("shape_ratio")
    if detail_gap is not None and detail_gap >= DETAIL_GAP:
        if ratio is not None and ratio < FEW_SHAPES:
            found.append("flattened")
        elif ratio is not None and ratio > MANY_SHAPES:
            found.append("over-traced")
        else:
            found.append("detail short")
    if (row.get("colour_gap") or 0) >= COLOUR_GAP:
        found.append("palette off")
    if row.get("leak", 0) > 0:
        found.append("gaps")
    return found or ["close to reference"]


def _measure(path: Path, mode: str, settings: Settings) -> dict:
    """Trace one image and gather everything the report shows for it."""
    data = path.read_bytes()
    source = Image.open(io.BytesIO(data)).convert("RGB")
    width, height = source.size

    picked = bench._pick_mode(mode, data, settings)
    svg, facts = bench._trace(data, picked, settings)
    ours = bench._rasterize(svg, width, height)
    if ours is None:
        return {"file": path.name, "error": "our trace did not render"}

    row = {
        "file": path.name,
        "mode": picked,
        "inks": facts["inks"],
        "shapes": facts["shapes"],
        "kb": round(len(svg) / 1024, 1),
        "leak": bench._leak(svg, width, height),
        "colour": round(bench._colour_error(ours, source), 3),
        "detail": round(bench._detail_agreement(ours, source), 2),
    }

    teacher_path = _teacher_for(path)
    teacher = None
    if teacher_path is not None:
        teacher_svg = teacher_path.read_bytes()
        teacher = bench._rasterize(teacher_svg, width, height)
        row["teacher_nodes"] = _nodes(teacher_svg)
        if teacher is not None:
            row["teacher_colour"] = round(bench._colour_error(teacher, source), 3)
            row["teacher_detail"] = round(
                bench._detail_agreement(teacher, source), 2)
            row["detail_gap"] = round(row["teacher_detail"] - row["detail"], 2)
            row["colour_gap"] = round(row["colour"] - row["teacher_colour"], 3)
        row["nodes"] = _nodes(svg)
        if row["teacher_nodes"]:
            row["shape_ratio"] = round(row["nodes"] / row["teacher_nodes"], 2)

    row["labels"] = _labels(row)

    mark = _disagreement(ours, source)
    box = _worst_tile(mark)
    row["worst_box"] = list(box)
    row["_thumbs"] = {
        "source": _data_uri(source),
        "ours": _data_uri(ours),
        "teacher": _data_uri(teacher) if teacher is not None else None,
        "mark": _data_uri(mark),
        "crop_source": _data_uri(source.crop(box), CROP_MIN * 2),
        "crop_ours": _data_uri(ours.crop(box), CROP_MIN * 2),
        "crop_teacher": (_data_uri(teacher.crop(box), CROP_MIN * 2)
                         if teacher is not None else None),
    }
    return row


_CSS = """
body{font:13px/1.5 ui-sans-serif,system-ui,sans-serif;margin:0;padding:24px;
background:#f6f6f4;color:#1b1b1a}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#6b6b66;margin:0 0 24px}
.row{background:#fff;border:1px solid #e6e6e1;border-radius:14px;padding:16px;
margin-bottom:18px}
.head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.name{font-weight:700;font-size:15px}
.tag{border-radius:99px;padding:2px 10px;font-size:11px;font-weight:700;
text-transform:uppercase;letter-spacing:.04em}
.bad{background:#fdecec;color:#a01b1b}.ok{background:#eaf5ec;color:#1d6b32}
.nums{color:#6b6b66;font-variant-numeric:tabular-nums}
.nums b{color:#1b1b1a}
.strip{display:flex;gap:12px;flex-wrap:wrap}
figure{margin:0}
figcaption{font-size:11px;color:#6b6b66;margin-bottom:4px}
img{display:block;border:1px solid #e6e6e1;border-radius:8px;background:#fff;
max-width:100%;height:auto}
.crops{margin-top:14px;padding-top:14px;border-top:1px dashed #e0e0da}
"""


def _figure(caption: str, uri: str | None) -> str:
    if not uri:
        return ""
    return f'<figure><figcaption>{caption}</figcaption><img src="{uri}"></figure>'


def _row_html(row: dict) -> str:
    if "error" in row:
        return (f'<div class="row"><div class="name">{row["file"]}</div>'
                f'<p class="nums">{row["error"]}</p></div>')
    thumbs = row["_thumbs"]
    tags = "".join(
        f'<span class="tag {"ok" if l == "close to reference" else "bad"}">{l}</span>'
        for l in row["labels"]
    )
    gap = row.get("detail_gap")
    nums = (f'detail <b>{row["detail"]:.1f}%</b>'
            + (f' vs <b>{row["teacher_detail"]:.1f}%</b> (−{gap:.1f})'
               if gap is not None else "")
            + f' · colour <b>{row["colour"]:.2f}</b>'
            + (f' vs <b>{row["teacher_colour"]:.2f}</b>'
               if "teacher_colour" in row else "")
            + f' · nodes <b>{row.get("nodes", 0)}</b>'
            + (f' vs <b>{row["teacher_nodes"]}</b>'
               if "teacher_nodes" in row else "")
            + f' · shapes <b>{row["shapes"]}</b>' 
            + f' · inks <b>{row["inks"]}</b> · leak <b>{row["leak"]}</b>'
            + f' · <b>{row["kb"]}</b> KB')
    return f"""<div class="row">
  <div class="head"><span class="name">{row['file']}</span>{tags}
    <span class="nums">{nums}</span></div>
  <div class="strip">
    {_figure('source', thumbs['source'])}
    {_figure('ours', thumbs['ours'])}
    {_figure('reference', thumbs['teacher'])}
    {_figure('edge disagreement', thumbs['mark'])}
  </div>
  <div class="crops"><div class="strip">
    {_figure('worst region — source', thumbs['crop_source'])}
    {_figure('worst region — ours', thumbs['crop_ours'])}
    {_figure('worst region — reference', thumbs['crop_teacher'])}
  </div></div>
</div>"""


def _page(rows: list[dict]) -> str:
    scored = [r for r in rows if "detail_gap" in r]
    summary = ""
    if scored:
        mean = sum(r["detail_gap"] for r in scored) / len(scored)
        broken = sum(1 for r in scored if "close to reference" not in r["labels"])
        summary = (f"{len(scored)} scored against a reference · "
                   f"mean detail shortfall {mean:.1f} points · "
                   f"{broken} flagged")
    body = "".join(_row_html(r) for r in rows)
    return (f"<!doctype html><meta charset=utf-8><title>Vectorizer triage</title>"
            f"<style>{_CSS}</style><h1>Vectorizer triage</h1>"
            f"<p class=sub>{summary}</p>{body}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="+", help="images or folders")
    parser.add_argument("--mode", choices=("auto", "logo", "artwork"),
                        default="artwork",
                        help="artwork mirrors what the web app sends (max_colors=0)")
    parser.add_argument("--out", type=Path, default=Path("triage"))
    args = parser.parse_args()

    settings = Settings()
    rows = []
    for path in bench._images(args.paths):
        print(f"  {path.name}", flush=True)
        try:
            rows.append(_measure(path, args.mode, settings))
        except Exception as error:  # one bad image must not lose the report
            rows.append({"file": path.name, "error": repr(error)})

    rows.sort(key=lambda r: -(r.get("detail_gap") or -999))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(_page(rows), encoding="utf-8")
    (args.out / "triage.json").write_text(
        json.dumps([{k: v for k, v in r.items() if k != "_thumbs"} for r in rows],
                   indent=2),
        encoding="utf-8")
    print(f"\nwrote {args.out / 'index.html'}  ({len(rows)} images)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
