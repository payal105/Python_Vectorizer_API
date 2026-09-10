"""Report what the pipeline decides about each image in a folder.

    .venv\\Scripts\\python.exe tools/audit.py samples/

Vectorizing is one long chain of automatic decisions, and most of them are
invisible in the finished file. This prints them, so a new image can be
checked in one line rather than opened in an editor and squinted at:

    file                 size       inks  resid  objects  fills  grads  shade  ms
    sample_bump.jpeg     1600x1459     6   1.59       13      6  588
      #666666 #fdf9dc #2b2a28 #ffffff #f19fc3 #c9cfa1

*inks* is the palette detected from the artwork, *resid* how far the average
pixel sits from the nearest one. A dash means the image was read as
continuous-tone and left alone, which is what should happen to photographs.

*grads* is how many shapes came back as gradients rather than flat fills, and
*shade* how far the fitted paint sits from the artwork's own pixels in CIELAB
once they are in -- under about 1 is invisible at any zoom. Both are dashes on
flat artwork, where gradients are never looked for.

*2x* is how the bitmap was conditioned for the enlarged trace: 'ink' if a
palette was found and the pixels mapped onto it, 'edge' if the colours were
left alone and the soft edges collapsed instead, and a dash if neither applied
and it was traced at its own resolution.

Warnings are printed for the failure modes worth knowing about: a palette that
lost a colour the artwork clearly uses, an object count high enough to mean
anti-aliasing is still being traced, and a residual close to the cutoff, where
a small change in the image would flip the decision.

    --write DIR   also save each result as an SVG, for opening in an editor
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from app.config import Settings  # noqa: E402
from app.schemas.params import VectorizeParams  # noqa: E402
from app.services import engine, preprocess, svgdoc  # noqa: E402

SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

# An object count this far above the ink count means shapes are being spent on
# something other than the artwork -- usually anti-aliasing traced as its own
# slivers, which is the defect this pipeline exists to avoid.
OBJECTS_PER_INK_WARNING = 12


def audit(path: Path, settings: Settings, write: Path | None) -> list[str]:
    data = path.read_bytes()
    params = VectorizeParams()
    started = time.perf_counter()
    prepared = preprocess.prepare(data, params, settings.max_input_pixels)
    traced = engine.trace(prepared, params)
    prepared = traced.prepared or prepared
    svg, meta = svgdoc.build(
        traced.svg,
        params,
        prepared.traced_width,
        prepared.traced_height,
        palette=prepared.palette,
        supersample=prepared.supersample,
        # The gradient stage runs on every real conversion, so leaving it out
        # here would report a file nobody is ever served -- and it has to be
        # handed the same bitmap the pipeline hands it, from before the
        # colours were mapped onto the palette, or the ramps it exists to find
        # have already been flattened into bands by the time it looks.
        shading=prepared.shading or prepared.image,
    )
    elapsed = (time.perf_counter() - started) * 1000

    fills = sorted({f.decode() for f in re.findall(rb'fill="(#[0-9a-fA-F]{6})"', svg)})
    inks = prepared.palette
    source = Image.open(path).convert("RGB")
    residual = (
        preprocess._mean_residual(source, preprocess._palette_rgbs(inks))
        if inks
        else None
    )

    # How the bitmap was conditioned for the enlarged trace. This is a
    # decision the finished file does not show: 'ink' means a palette was
    # found and the pixels mapped onto it, 'edge' that the colours were left
    # alone and the soft edges collapsed instead, '-' that neither happened
    # and it was traced at its own resolution.
    if prepared.supersample == 1:
        conditioning = "-"
    else:
        conditioning = "ink" if inks else "edge"

    shading = meta["shading"] or {}
    print(
        f"  {path.name:<28} {prepared.source_width}x{prepared.source_height:<7} "
        f"{len(inks) if inks else '-':>4} "
        f"{residual if residual is not None else float('nan'):>6.2f} "
        f"{conditioning:>5} "
        f"{meta['paths']:>8} {len(fills):>6} "
        f"{shading.get('gradients') or '-':>6} "
        f"{format(shading['residual_after'], '.2f') if shading else '-':>6} "
        f"{elapsed:>6.0f}"
    )
    if inks:
        print("      " + " ".join(inks))

    warnings = []
    if inks:
        if residual is not None and residual > preprocess._FLAT_MAX_RESIDUAL * 0.8:
            warnings.append(
                f"{path.name}: residual {residual:.2f} is close to the "
                f"{preprocess._FLAT_MAX_RESIDUAL} cutoff — a slightly noisier "
                "version of this image would be left alone instead"
            )
        if meta["paths"] > len(inks) * OBJECTS_PER_INK_WARNING:
            warnings.append(
                f"{path.name}: {meta['paths']} objects for {len(inks)} inks — "
                "check the edges for slivers"
            )
        missing = set(inks) - set(fills)
        if missing:
            warnings.append(
                f"{path.name}: {', '.join(sorted(missing))} was detected in the "
                "artwork but no shape came out in it"
            )
    if write:
        write.mkdir(parents=True, exist_ok=True)
        (write / f"{path.stem}.svg").write_bytes(svg)
    return warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="+", help="images or folders")
    parser.add_argument("--write", type=Path, help="also save each result as SVG")
    args = parser.parse_args()

    files: list[Path] = []
    for path in args.paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in SUFFIXES))
        elif path.suffix.lower() in SUFFIXES:
            files.append(path)
    if not files:
        print("No images found.")
        return 1

    settings = Settings(max_input_pixels=40_000_000)
    print(
        f"\n  {'file':<28} {'size':<11} {'inks':>4} {'resid':>6} {'2x':>5} "
        f"{'objects':>8} {'fills':>6} {'grads':>6} {'shade':>6} {'ms':>6}"
    )
    warnings: list[str] = []
    for path in files:
        try:
            warnings.extend(audit(path, settings, args.write))
        except Exception as exc:  # keep going; one bad file is not the run
            print(f"  {path.name:<28} FAILED: {type(exc).__name__}: {exc}")

    if warnings:
        print("\n  warnings:")
        for warning in warnings:
            print(f"    ! {warning}")
    else:
        print("\n  no warnings")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
