**SUPERSEDED — see `PLAN.md` at the repo root for the current state, what's
shipped, what was tried and rejected since 14:40, and the active plan.** This
file is kept as a historical snapshot of the first ~4 hours only.

# Smart Vector quality work — state as of 10 Sep 2026, 14:40

Written so this survives a session or account change. Everything below is
measured, not estimated; the commands to reproduce each number are included.

## The problem

Our tracer output is well short of the Vectorizer.AI output the client
approved. Measured across all 63 corpus images, against the cached reference
SVGs:

    ours       mean detail 41.71%      mean colour 8.158
    reference  mean detail 61.86%      mean colour 3.608

Symptoms reported: missing detail, jagged edges, distortion, stray bumps.
Triage separates those into three distinct causes (below).

## What was built today

    tools/triage.py    Ranks every corpus image by how far it is from the
                       reference and writes one HTML page: source / ours /
                       reference / edge-disagreement, plus an auto-cropped
                       zoom of the single worst region per image. Replaces
                       converting and screenshotting images by hand.
                           python tools/triage.py corpus/ --out triage

    tools/autotune.py  Searches the engine's 15 settings for the combination
                       that best matches the reference, scored over the
                       corpus, one setting at a time. Guards against winning
                       by tracing noise (colour must not worsen, geometry must
                       not inflate). Validates the winner on all 63.
                           python tools/autotune.py corpus/ --baseline before.json

    tools/inkdebug.py  Prints the palette decision chain for one image: every
                       candidate colour, its share, and why it was kept or
                       dropped.
                           python tools/inkdebug.py corpus/000.png

Note: bench.py and autotune.py must be run with `--mode artwork` semantics.
The web app sends `processing.max_colors = 0` (see
src/app/api/vectorize/route.js), so bench's default `--mode auto` measures a
path production never takes.

## The three failure families

From triage over the whole corpus. "nodes" is drawing commands ours/theirs;
1.0 is parity.

    family              images                  evidence
    flattened           010 047 032 029 021     nodes 0.17-0.65x, palette
                                                refused, raw trace
    over-traced         014                     nodes 6.06x  (51899 vs 8570)
                                                -- the jagged edges / speckle
    misplaced geometry  055                     nodes 0.99x but still 29.6
                                                points short -- right amount
                                                of curve, wrong places

## Things tried and rejected

Both died on the 63-image gate, which is the point of the gate.

1. `_INK_SURVIVAL_FLOOR` in preprocess.py -- rescue a thin colour that keeps
   enough of itself through the mode filter, so shading mid-tones are not
   dismissed as anti-aliasing. Helped the lens logo (+0.8) and 034 (+5.1),
   destroyed 018 (-24.1). Net mean detail -0.39. Reverted.

2. Reusing the `_has_gradient_band` adjacency evidence for the same rescue.
   Let genuine anti-aliasing through as well; detail 55.93 -> 51.60 on the
   lens logo. Discarded before it reached the corpus.

## Tuning run in progress

`tools/autotune.py corpus/ --sample 24 --rounds 3 --workers 3`
Started 13:32. Round 1 of 3 expected ~15:35; all three ~19:30.

Baseline on all 63:  match 43.61%   colour 8.199   bumps 0.67x

Adopted so far (search sample of 24):

    _FLAT_MAX_INKS      16 -> 20          match 40.77%
    processing.denoise  low -> none       match 43.09%   colour 8.00
                                          bumps 0.51 -> 0.74

`denoise = none` is the significant one: +2.3 points, colour improved, and
geometry moved toward parity with the reference. The median filter cannot
tell JPEG grain from a thin line and removes both -- and it only runs when
the palette gate has already rejected the image, which is exactly the
flattened family above.

Correction worth recording: the palette thresholds were predicted to be the
main lever and were not. All six were searched; one moved anything, by under
a point.

Live log: notes/autotune-run.log (snapshot) and the harness task output.
If the run is killed, nothing important is lost -- the adopted settings are
in the `KEEP` lines of the log, and revalidating them is one bench pass.

## Next steps, in order

1. Take the round-1 number, validate on all 63, check that images which
   already looked acceptable did not regress.
2. If it holds, apply the settings and re-run triage.
3. Then the three families above, which are code changes rather than
   settings. The over-traced and misplaced-geometry cases are not diagnosed
   yet.

## Unrelated things noticed, not acted on

* src/app/api/vectorize/route.js still describes an in-browser fallback that
  was removed in 562f015.
* bench.py's docstring points at scripts/export-corpus.mjs in the app repo;
  that file does not exist, so there is currently no way to rebuild corpus/
  from MongoDB. corpus/ is gitignored and exists only on this machine --
  the paid reference SVGs are not backed up.
* corpus images are capped at 700px by ingest-corpus.mjs; production handles
  1600px+. All numbers here are honest for the 700px regime only.
