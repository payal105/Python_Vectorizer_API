"""Print the ink-detection decision chain for an image.

Palette detection is where flat artwork loses detail, and the reasons a
colour is dropped are all internal to preprocess. This walks the same
candidates the real path walks and reports what happened to each, so a
"where did that detail go" question can be answered from measurements
rather than guesses.

    python tools/inkdebug.py path/to/image.png
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import preprocess as pp


def luma(c) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def main(path: str) -> None:
    rgb = Image.open(path).convert("RGB")
    print(f"{path}  {rgb.width}x{rgb.height}")

    colours, weights, total, ids = pp._ink_candidates(rgb)
    solid = pp._solid_shares(ids, len(colours))
    print(f"\n{len(colours)} candidates after merging at "
          f"_MIN_INK_SEPARATION={pp._MIN_INK_SEPARATION}\n")

    header = f"{'#':>3}  {'hex':<8} {'luma':>5} {'share%':>7} {'solid%':>7}  verdict"
    print(header)
    print("-" * len(header))

    inks: list[tuple[int, int, int]] = []
    for i, colour in enumerate(colours):
        share = weights[i] / total if total else 0.0
        hexed = "#%02x%02x%02x" % colour
        if share < pp._INK_MIN_SHARE:
            verdict = "DROPPED: below _INK_MIN_SHARE (debris)"
        elif solid[i] < pp._INK_SOLID_FLOOR and pp._explained_as_blend(colour, inks):
            verdict = "DROPPED: thin + explained as blend of accepted inks"
        elif len(inks) >= pp._FLAT_MAX_INKS + 1:
            verdict = "DROPPED: past the candidate limit"
        else:
            inks.append(colour)
            verdict = "kept"
        print(f"{i:>3}  {hexed:<8} {luma(colour):>5.0f} "
              f"{share * 100:>7.3f} {solid[i] * 100:>7.3f}  {verdict}")

    # The nearest neighbour of each kept ink, to show how much headroom the
    # merge threshold actually had on this image.
    print(f"\nclosest pair among kept inks:")
    pairs = [
        (sum((a - b) ** 2 for a, b in zip(x, y)) ** 0.5, x, y)
        for i, x in enumerate(inks) for y in inks[i + 1:]
    ]
    for dist, x, y in sorted(pairs)[:5]:
        print(f"  {dist:6.1f}  #%02x%02x%02x (luma %3.0f)  vs  #%02x%02x%02x (luma %3.0f)"
              % (*x, luma(x), *y, luma(y)))

    accepted = pp._accept_inks(rgb, pp._FLAT_MAX_INKS + 1)
    residual = pp._mean_residual(rgb, accepted) if accepted else 0.0
    ramp = pp._has_gradient_band(rgb, accepted) if accepted else False
    flat = pp._detect_flat_palette(rgb)

    print(f"\n_accept_inks      -> {len(accepted)} inks")
    print(f"_mean_residual    -> {residual:.2f}  (limit {pp._FLAT_MAX_RESIDUAL})")
    print(f"_has_gradient_band-> {ramp}")
    print(f"_detect_flat_palette -> "
          + (f"{len(flat)} inks, artwork flattened to them" if flat
             else "None, image left to the tracer"))
    if flat:
        print("  " + "  ".join("#%02x%02x%02x" % c for c in flat))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
