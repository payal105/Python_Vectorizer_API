# Smart Vector engine quality — continuation brief

Paste everything below this line to a fresh Claude Code session as your first
message. It is written to be self-contained — full context, exact current
state, what's been tried, and the exact plan to run. You should not need to
re-explain anything.

---

## Who you are picking up from

A previous Claude session spent today (10 Sep 2026) closing the quality gap
between our self-hosted vectorization engine and the Vectorizer.AI output the
client has already approved. That session ran out of time; you are continuing
it under the same client deadline (~8 hours from when this brief is handed
over). Everything measured below is real, reproduced, and file-verified as of
handoff — not estimated.

## The two repositories

```
C:\Users\rishikalpa\Projects\mdb\
├── Python_Vectorizer_API\     the engine. FastAPI + VTracer. All quality
│                              work happens here. git repo, branch dev/rishi,
│                              pushed to github.com/payal105/Python_Vectorizer_API
└── mydesignbazaar\            the Next.js app. /smart-vector page calls this
                               engine via a server-side proxy. git repo,
                               branch hotfixes, pushed to
                               github.com/rishikalpadas/mydesignbazaar
```

`mydesignbazaar/.env` → `SELF_HOSTED_VECTOR_API_URL=http://127.0.0.1:8000`,
so the app talks to a locally-running copy of the engine. Both repos are
clean and fully pushed as of handoff — cloning them fresh gets you everything
described here, including the 63-image corpus (see below), with no extra
transfer needed.

## The goal, in the client's and my own words

> convert all 63 test images to vector SVG using our engine, then compare
> them with the reference SVGs bought from Vectorizer.AI in every aspect —
> rough/jagged edges, weird patterns, distortion, colour changes, missing
> detail — and tune the engine based on the differences found, to get output
> as close to the reference as possible.

Not "as close to the original photo as possible" — **as close to the
Vectorizer.AI SVG as possible**. That distinction changed the whole
methodology partway through today; see below.

## Two scoring methodologies exist in this codebase — use the right one

This matters more than anything else in this brief. Getting it backwards
wastes hours on the wrong signal.

- **Source-based** (`tools/bench.py`, `tools/triage.py` as they exist today):
  scores our SVG, rendered to pixels, against the **original source photo**.
  Also reports how the *reference* SVG scores against that same photo
  (`teacher_detail`/`teacher_colour`), so a gap can be inferred, but the
  comparison is indirect. This was the **original**, now-superseded approach.

- **Reference-based** (`tools/autotune.py`, `tools/validate.py`,
  `tools/palette_ab.py`): renders the **reference SVG itself** to pixels
  once (cached in `tools/.reference-cache/`, already built and committed —
  63 PNGs, no need to regenerate), then scores our SVG **directly against
  that render**. This is the methodology the client actually asked for and
  the one all of today's real findings came from.

**`tools/triage.py` was never updated to the reference-based method.** Its
existing output (`triage/index.html`, `triage/triage.json` — both present,
generated at 13:29 today, now stale) still uses source-based scoring. If you
regenerate it, either accept that inconsistency and note it, or port its
scoring calls (`bench._detail_agreement(ours, source)` →
`bench._detail_agreement(ours, reference)`, same pattern as
`tools/palette_ab.py`) — that's a real, un-flagged gap in the harness, not a
deliberate choice.

Whenever you invoke `tools/bench.py`, use `--mode artwork`, never the default
`--mode auto`. The web app always sends `processing.max_colors=0` (see
`mydesignbazaar/src/app/api/vectorize/route.js`, `MAX_COLORS_BY_MODE = {
logo: 8, artwork: 0 }`, and `vectorMode` is normalised to `'logo'` or
`'artwork'` before that lookup) — `--mode artwork` mirrors production;
`--mode auto` pins 8 colours and measures a path production never takes.

## The corpus

`corpus/` — 63 real client images (WhatsApp uploads, 22 Aug–3 Sep) each
paired with a `.teacher.svg` bought once from Vectorizer.AI (1 credit each,
cached forever — never re-buy an image already present). `corpus/index.json`
is the manifest. **Capped at 700px** by the ingestion script
(`mydesignbazaar/scripts/ingest-corpus.mjs`, `TRAINING_MAX_DIM = 700`) —
production sees real uploads at 1600px+ (confirmed from server logs). Every
number in this brief was measured at 700px. Whether the gains transfer to
production resolution is **untested** — see Phase 0.

`corpus/` is committed to git (was meant to be gitignored; the ignore rules
in `.gitignore` got commented out earlier today and it, plus a 25MB
`triage/index.html`, ended up pushed to GitHub). This is a known, unresolved
issue — leave it alone unless the user explicitly asks you to fix it; undoing
it means rewriting git history and force-pushing, which is not your call to
make unilaterally.

## Current shipped state — this is the floor, do not regress below it

Two settings changed from their original values and are live in the source
right now:

```
app/services/preprocess.py:180
    _FLAT_MAX_INKS = 20          (was 16)

app/schemas/params.py:180-182
    processing_denoise: Denoise = Field(
        default="none",          (was "low")
```

`processing_color_merge` (`app/schemas/params.py:153`) was tried at `0.0`,
measured, and **reverted back to its original `16.0`** — see rejected
experiments below. Confirm both settings are still exactly as stated above
before trusting anything downstream; if they're not, something changed after
this brief was written.

Measured with `tools/validate.py` against all 63 references (not a sample):

```
  original (both settings at old defaults)   match 43.61%   colour 8.199
  current shipped (as above)                 match 45.14%   colour 7.717
                                              +1.53 points, confirmed 4x reproducible

  improved 36 · worse 13 · unchanged 14
  biggest losses:  024 -16.8   058 -16.2   033 -16.1   061 -7.1   036 -6.1   007 -2.7
  biggest gains:   047 +17.9   010 +13.8   021 +10.7   009 +9.8   004 +8.6   013 +7.2

  mean file size   252 KB -> 315 KB   (1.25x)
  worst-case size  1468 KB -> 1468 KB (unchanged)
  images that get a palette   13 -> 18 of 63
```

Timing cost of the shipped pair (denoise=none + inks=20, NOT color_merge):
roughly **1.3-1.5x slower** per conversion on the heaviest images (image 057:
36.6s → 43.4s; image 037: 18.9s → 26.3s). Confirmed safely under both
timeouts (below).

**This 45.14%/7.717 pair is the number every future change must beat or at
least not regress, on the full 63, not a sample.** `tools/validate.py` with
no arguments reproduces it; save it once with `--save baseline-shipped.json`
and diff every future change against that file with `--against`.

## Known hard constraints

- `mydesignbazaar/src/app/api/vectorize/route.js`: proxy gives up after
  `UPSTREAM_TIMEOUT_MS = 100_000` (100s).
- The engine's own job timeout: `VECTOR_JOB_TIMEOUT_SECONDS`, default 120s
  (`.env` / `app/config.py`).
- Any setting change must be timed on the heaviest corpus images before
  shipping, not just scored. A quality win that risks a timeout is not a win
  — it turns "slightly worse" into "conversion failed" for the client's real
  users.
- Restart the engine with `./restart.ps1` (PowerShell, from the
  `Python_Vectorizer_API` directory), **never** Ctrl+C-and-rerun. Uvicorn's
  reloader orphans a child process that keeps holding port 8000 and keeps
  serving stale code — indistinguishable from "my change did nothing."

## Tools already built (all in `tools/`, all committed)

| tool | what it does | invocation |
|---|---|---|
| `validate.py` | **the ship/no-ship gate.** Scores current source against all 63 references, optionally diffs against a saved run. Nothing ships without this confirming no regression. | `python tools/validate.py --save X.json` / `--against X.json --save Y.json` |
| `autotune.py` | Coordinate-descent search over 15 engine settings, reference-scored, guarded against winning by tracing noise (colour can't worsen >2%, geometry can't exceed 1.6x the reference or the shipped ratio). Validates winner on all 63 before reporting. Resumable — see Phase 1. | `python tools/autotune.py corpus/ --sample 24 --rounds N --workers 3 --out X.json` |
| `palette_ab.py` | Traces all 63 both with and without the flat-palette detection step, per-image delta. Already run; result committed as `palette_ab.json` — no need to rerun unless settings changed meaningfully since. | `python tools/palette_ab.py` |
| `inkdebug.py` | Per-image palette decision trace — every candidate colour, its share of the image, why kept or dropped. | `python tools/inkdebug.py corpus/047.png` |
| `bench.py` | Legacy source-based scorer (see methodology note above). Still useful for a quick single-image or single-folder check when a reference isn't the point. | `python tools/bench.py --mode artwork corpus/` |
| `triage.py` | Visual HTML report, worst-image-first, with auto-cropped zoom of the worst region per image. **Uses source-based scoring, stale (generated 13:29 today, before any of today's tuning).** Regenerate after your changes land; consider porting it to reference-based scoring while you're in there. | `python tools/triage.py corpus/ --out triage` |

`tools/autotune.py`'s KNOBS list documents its own candidate values; **9 of
15 have been searched, 6 have not** (see Phase 1 — this is the most concrete
unfinished item from today).

## What was tried and rejected today — do not repeat these

1. **`_INK_SURVIVAL_FLOOR`** (new constant in `preprocess.py`, rescued a thin
   colour from being dismissed as anti-aliasing if it kept ≥10% of itself
   through a mode filter). Helped image 000 (+0.8) and 034 (+5.1), destroyed
   018 (-24.1). Net corpus-wide **-0.39**. Reverted; code removed entirely.
2. **Adjacency-based variant of the same rescue**, reusing the
   `_has_gradient_band` evidence. Discarded before a corpus run — let genuine
   anti-aliasing through too, hurt image 000 alone (55.93% → 51.60%).
3. **`processing.color_merge`: 16.0 → 0.0.** Real average gain measured
   (+1.79 vs the shipped pair's +1.53), but combined with `denoise=none` it
   made image 009 take **148 seconds** to convert (vs 14.5s originally) —
   over both timeouts above. Reverted to `16.0`. Do not re-apply without
   separately solving the performance blowup (isolated cause:
   `color_merge=0` alone costs +37s on image 009; the combination compounds
   past linear).
4. **Forcing a palette onto all 63 images unconditionally** (bypassing the
   flat-artwork gate entirely, via `_accept_inks(rgb, 24)`). Net **-0.30**
   corpus-wide — the gate is wrong in both directions (20 of the 50
   currently-rejected images would improve, 27 would get worse), so a global
   flip is not the fix. See "the palette lever" below.

## The central finding — read this before doing any more dial-turning

Tested what happens when the engine is forced to draw **exactly as much
geometry as the reference** (`processing.detail = "maximum"`): geometry hit
1.04x parity with the reference — and match **collapsed to 28.96%** (down
from 43.37% at standard settings, on the same search sample). We do not draw
too little. **We draw the right amount in the wrong places.** This is a
curve-fitting/boundary-placement problem inside VTracer's handling of our
preprocessed bitmap, not a settings problem, and it was diagnosed but never
investigated today.

Ruled out already: a uniform pixel misalignment. Tested shifting our
rendered output by every offset from -2 to +2 px in x and y on 3 images
(000, 055, 047) — the unshifted (0,0) position scored best on all three every
time. The shapes are genuinely wrong, not offset.

## The palette lever — diagnosed, not fixed

Ablation (removing each pre-tracing stage on 5 images: 000, 055, 047, 014,
010) found the 2x supersample enlargement is worth keeping (removing it costs
5-15 points on 4 of 5), edge-snapping barely matters, and **whether an image
gets a flat-colour palette at all** is the single biggest per-image swing —
in both directions:

```
                    palette ON    palette OFF   delta
  014 (over-traced)   17.7%         50.3%       +32.6   (wants OFF)
  055 (misplaced)     56.4%         69.2%       +12.8   (wants OFF)
  047 (flattened)     33.7%         51.5%       +17.9   (wants ON, doesn't get one)
```

All three of today's named "failure families" (flattened / over-traced /
misplaced-geometry) turn out to be the **same lever pointing the wrong way**,
not three separate bugs. The corpus-wide ceiling from perfect per-image
routing is small — **+0.79 points** (`palette_ab.json`, already computed,
all 63) — because the current binary gate is already correct on most images;
it's wrong on a specific unlucky handful in both directions.

**No fix has been built.** The concrete next step: `app/services/adaptive.py`
already solves this exact shape of problem for other settings — trace an
image two ways, render small, score each against the **source** (not the
reference, which won't exist at inference time), keep whichever is closer.
Extending that same mechanism to the palette on/off decision is the bounded,
known-payoff piece of work described as Phase 2 below.

## Two things that exist but are outside this plan

- **`/smart-vector-js`** (`mydesignbazaar/src/app/smart-vector-js/page.js`):
  a clone of the main `/smart-vector` page wired to the **in-browser JS
  pipeline**
  (`src/lib/smartVectorPipeline.js`) instead of this Python engine. Built
  today, committed, **never benchmarked** against the corpus. Not part of
  this plan; mention only if the client conversation touches on alternative
  engines.
- The git/GitHub exposure of `corpus/` and `triage/index.html` — flagged
  above, not yours to fix without being asked.

---

## The plan — work in this order, gate every phase

Budget assumes ~8 hours total including buffer. **Every time estimate in
today's session ran 2-4x over** (a single tuner trial once hung for 21
minutes; a "1.5-2 hour" tuning run was still not done after 2 hours). Treat
these as targets, not promises, and say so if you're running long rather than
silently eating the whole budget on one phase.

### Phase 0 — resolution sanity check (~15 min)

Everything above was measured at 700px. Get 2-3 real images at production
size (1600px+) — check if the original, pre-downscale WhatsApp images still
exist anywhere locally; if not, ask the user for a couple of real uploads.
Convert each through the shipped engine, eyeball the result. If the
improvement direction doesn't hold at real size, say so immediately and
reprioritize — don't spend the rest of the budget on gains that don't
transfer. **If confirming this properly means regenerating the corpus at a
higher resolution cap, that costs real money (~1 Vectorizer.AI credit per
image, ~63 credits) — do not spend it without the user's explicit go-ahead.**

### Phase 1 — finish the interrupted settings search (~45 min)

6 of 15 settings were never tested: `processing.shapes.min_area_px`,
`processing.smoothing`, `processing.corner_threshold`,
`processing.length_threshold`, `processing.color_precision`,
`processing.layer_difference`. These are exactly the corner/curve controls
most likely to matter for jagged edges and stray bumps. Resume straight into
them rather than re-searching the 9 already-adopted settings:

```bash
python tools/autotune.py corpus/ --sample 24 --rounds 1 --workers 3 \
  --start '{"_FLAT_MAX_INKS": 20, "processing.denoise": "none"}' \
  --from-setting "processing.color_merge" \
  --out autotune-phase1.json
```

`--from-setting` skips every setting up to and including the named one on
round 1 only. Watch for any single trial running past ~3-4 minutes — that's
the same pathological-combination failure mode from earlier today (a
setting combo with no denoising and no fill-merging traces every speck of
grain); kill it, skip that value, move on, don't let it eat the clock.

Apply whatever wins, then **`python tools/validate.py --against
baseline-shipped.json`** on the full 63. Anything that doesn't clear the
current 45.14%/7.717 floor does not ship.

### Phase 2 — per-image palette routing (~1-1.5 hrs)

Build the self-referential on/off decision described above, modeled on
`app/services/adaptive.py`'s existing pattern. Validate the result against
all 63 **references** (fine to use the reference for validation even though
the mechanism itself can only use the source at inference time). Known
ceiling from the diagnosis: **+0.79 to +2.25 points** depending on exact
implementation — small, bounded, worth doing, not a game-changer on its own.

### Phase 3 — curve-placement investigation, hard time-box (~1.5-2 hrs, HARD STOP)

The real ceiling-breaker, per the central finding above, and the least
understood. Suggested starting points: check what curve-fitting tolerance
VTracer itself exposes and whether it's being passed through; inspect
`_snap_soft_edges` and the "grow by one enlarged pixel" step in
`_finer_copy` (`app/services/preprocess.py`) for where a boundary could be
shifting during the 2x-enlargement conditioning; compare our traced path
data directly against the reference's path data (not just rendered pixels)
on one clean example like image 000, node by node, to see where they
diverge.

**Set a hard 2-hour limit. If there's no concrete, testable lead by then,
stop and ship without it.** This is exactly the kind of open-ended thread
that ran unchecked earlier today; don't repeat that mistake twice in one
project.

### Phase 4 — final gate and ship (~45 min)

One more full `tools/validate.py --against baseline-shipped.json` run with
whatever survived phases 1-3. Drop anything that doesn't clear the gate, no
exceptions. Restart via `./restart.ps1`. Spot-check in the actual app at
`/smart-vector` — but only after the restart, and only after validation has
already passed; testing against a stale-code server or an unvalidated change
wastes time and produces false signal.

### Phase 5 — the actual client deliverable (~45 min)

Not `triage/index.html` — that's a debugging tool, not a client-facing
artifact, and it's also 25MB and stale. Build a clean before/after
comparison: pick 4-6 representative images (include at least one from the
"biggest gains" list and be honest about one from "biggest losses" if it's
still short), show original / this-morning's-output / current-output side by
side, with one honest paragraph — measured improvement, what got better,
what's still short and why, framed as this iteration's progress rather than
a finished state.

---

## Rules that apply throughout

- **Never ship a change without `tools/validate.py` confirming it on the
  full 63** — not the search sample, not one image, not "it looked better."
  This exact discipline caught two bad ideas today before they shipped.
- **Time and file-size, not just quality score, before shipping anything.**
  A quality win that risks the 100s/120s timeout is not a win.
- If you find yourself explaining away a bad number instead of trusting it,
  stop and trust the number — that happened at least twice today and both
  times the number was right.
- Ask the user before: spending Vectorizer.AI credits, rewriting git
  history, or abandoning the 8-hour budget for an open-ended investigation.
  Everything else — running the tools above, applying and validating
  settings changes, building Phase 2's routing mechanism — is normal
  execution of an already-agreed plan; don't stop to ask permission for
  those.
