# Python Vector API

A raster-to-vector conversion API in the mould of [vectorizer.ai](https://vectorizer.ai):
upload a bitmap, get back clean, resolution-independent vector artwork as
**PDF**, SVG, EPS — or a rasterized PNG preview of the vector result.

Built on FastAPI with [VTracer](https://github.com/visioncortex/vtracer) as the
tracing engine (hierarchical colour clustering plus curve fitting, the same
broad approach commercial vectorizers use), and ReportLab for vector output.

```
raster bytes ──► decode / EXIF / quantize ──► trace ──► SVG document ──► PDF
                     preprocess.py           engine.py    svgdoc.py     render.py
```

---

## Quickstart

**Windows (PowerShell).** Use the `py` launcher to create the venv, then call
the venv's own `python.exe` directly — no activation needed, and it works even
when a bare `python` hits the Microsoft Store alias:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env

.venv\Scripts\python.exe run.py          # http://127.0.0.1:8000
```

If you prefer to activate the environment first, `.\.venv\Scripts\Activate.ps1`
makes a plain `python run.py` work for that session. Should PowerShell block
the script, allow it once with
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`.

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

python run.py                            # http://127.0.0.1:8000
```

> A bare `python` is not on `PATH` on stock Windows — it resolves to the
> Microsoft Store alias and prints *"Python was not found"*. Prefer `py` for
> creating the venv and `.venv\Scripts\python.exe` for everything after.

Interactive docs at **http://127.0.0.1:8000/docs**.

Convert something:

```bash
curl -u demo_id:demo_secret \
  -F "image=@logo.png" \
  -F "output.file_format=pdf" \
  -o logo.pdf \
  http://127.0.0.1:8000/api/v1/vectorize
```

The default credentials come from `VECTOR_API_KEYS` in `.env`. **Change them
before deploying**, or set `VECTOR_REQUIRE_AUTH=false` to run open.

---

## Authentication

Three interchangeable forms — HTTP Basic matches existing vectorizer.ai
integrations, so client code often ports over unchanged:

```http
Authorization: Basic base64(id:secret)
Authorization: Bearer <id>:<secret>
X-Api-Key: <id>:<secret>
```

---

## Endpoints

| Method | Path                    | Purpose                                          |
| ------ | ----------------------- | ------------------------------------------------ |
| `POST` | `/api/v1/vectorize`     | Convert a raster image to vector                 |
| `POST` | `/api/v1/gradients`     | Re-fit an SVG's gradients against its source     |
| `GET`  | `/api/v1/parameters`    | Machine-readable schema of every parameter       |
| `GET`  | `/api/v1/formats`       | Supported input/output formats and mode pricing  |
| `GET`  | `/api/v1/account`       | Credit balance, usage and limits for your key    |
| `GET`  | `/api/v1/health`        | Liveness probe                                   |
| `GET`  | `/api/v1/ready`         | Readiness probe with worker capacity             |

### Supplying the image

Exactly one of:

| Field          | Description                                        |
| -------------- | -------------------------------------------------- |
| `image`        | Multipart file part                                |
| `image.base64` | Base64 string, or a `data:image/png;base64,...` URL |
| `image.url`    | Public `http(s)` URL for the server to fetch        |

Accepted as `multipart/form-data`, `application/x-www-form-urlencoded`, or a
flat JSON object. Parameters may also be passed in the query string.

### Response

The raw file, plus metadata in headers:

```
Content-Type: application/pdf
Content-Disposition: attachment; filename="logo.pdf"
X-Image-Width: 240.0        X-Source-Width: 240
X-Image-Height: 180.0       X-Source-Height: 180
X-Path-Count: 4             X-Engine: vtracer
X-Gradient-Count: 2         X-Gradient-Merged: 5
X-Processing-Ms: 41.2       X-Receipt: 9f2c...
X-Credits-Charged: 1.00     X-Credits-Balance: 998.00
```

Send `Accept: application/json` to get a JSON envelope instead, with the file
base64-encoded alongside full timing and geometry metadata.

---

## Parameters

All parameters are optional. `GET /api/v1/parameters` returns this same list
with types, ranges and defaults, generated from the schema.

### Mode

| Parameter | Values | Default | Notes |
| --- | --- | --- | --- |
| `mode` | `production`, `preview`, `test` | `production` | `test` is free and watermarked; `preview` costs 0.2 credits and is watermarked; `production` costs 1.0 and is clean. |

### Input

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `input.max_pixels` | int ≥ 100 | server limit | Downscale before tracing. Lower = faster, simpler paths. |

### Processing

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `processing.color_mode` | `color`, `binary` | `color` | `binary` traces a single-colour silhouette. |
| `processing.max_colors` | 0–256 | `0` (auto) | Reduce to N colours, derived from the artwork's own inks. At `0`, flat artwork gets this automatically — see [Ghost layers](#ghost-layers-in-an-editor). |
| `processing.palette` | hex list | – | Pin output colours exactly, e.g. `#ff0000,#00ff00`. Output fills are snapped to this list, so you get exactly this many layers. The strongest fix for ghost layers — see [Ghost layers](#ghost-layers-in-an-editor). |
| `processing.color_merge` | 0–160 | `16` | Snap minor fills onto the nearest prominent one within this RGB distance. Removes the pale lumps left where anti-aliasing blends two flat regions. See [Transition lumps](#transition-lumps-between-two-colours). |
| `processing.detail` | `low`, `standard`, `high`, `maximum` | `standard` | Smallest feature kept, and path coordinate precision. See [Detail](#detail-thin-lines-and-small-features). |
| `processing.denoise` | `none`, `low`, `medium`, `high` | `low` | Median despeckle before tracing, applied to **lossy sources only** unless you set it yourself. The main defence against chunky, notched edges on a JPEG; on a lossless file there is no compression noise to remove and the filter only costs detail. See [Edge smoothness](#edge-smoothness). |
| `processing.smoothing` | `none`, `low`, `medium`, `high` | `low` | How hard to round off corners. See [Edge smoothness](#edge-smoothness). |
| `processing.hierarchical` | `stacked`, `cutout` | `cutout` | `cutout` emits non-overlapping shapes; `stacked` layers them back-to-front for a smaller file. Defaults to `cutout` because `output.combine_paths` requires it — asking for `stacked` turns combining off. |
| `processing.curve_mode` | `spline`, `polygon`, `pixel` | `spline` | Bézier curves, straight edges, or no fitting. |
| `processing.shapes.min_area_px` | 0–4096 | `4` | Discard specks smaller than this. Raise it to de-noise photos. |
| `processing.color_precision` | 1–8 | `6` | Bits of colour kept per channel when clustering. |
| `processing.layer_difference` | 0–128 | `16` | Minimum colour distance between stacked layers. |
| `processing.corner_threshold` | 0–180 | `60` | Angle below which corners stay sharp. |
| `processing.length_threshold` | 3.5–10 | `4.0` | Shortest emitted path segment. |
| `processing.splice_threshold` | 0–180 | `45` | Angle above which curves splice rather than join. |
| `processing.max_iterations` | 1–64 | `10` | Curve-fitting refinement passes. |
| `processing.path_precision` | 0–8 | `3` | Decimal places in path data. Lower = smaller files. |
| `processing.gradients` | bool | `true` | Fit real gradients to shapes whose source pixels shade. See [Shaded artwork](#shaded-artwork-and-gradients). |
| `processing.gradients.radial` | bool | `true` | Also consider radial gradients. Shading that spreads from a point comes back flat without this. |
| `processing.gradients.merge_patches` | bool | `true` | Give neighbouring slices of one ramp a single shared gradient, so the colour runs continuously across the seam. |
| `processing.gradients.max_stops` | 2–64 | `12` | Most stops one fitted gradient may use. |
| `processing.gradients.tolerance` | 0–20 | `1.5` | How far a fitted gradient may sit from the artwork's colours, in CIELAB, before another stop is added. |

The same knobs are available on `POST /api/v1/gradients` under a
`gradients.` prefix, alongside `gradients.min_travel`,
`gradients.max_residual`, `gradients.patch_distance` and
`gradients.min_area_px`.

### Output

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `output.file_format` | `svg`, `pdf`, `eps`, `png` | `svg` | |
| `output.draw_style` | `fill_shapes`, `stroke_shapes`, `stroke_edges` | `fill_shapes` | |
| `output.shapes.stroke_width` | float | `1.0` | Used by the `stroke_*` styles. |
| `output.combine_paths` | `none`, `shapes`, `colors` | `shapes` | How much to fuse traced fragments into single objects. `shapes` merges only touching fragments, so each letter stays selectable. See [Object count](#object-count-in-illustrator-corel-inkscape). |
| `output.group_by` | `none`, `color` | `none` | `color` wraps same-coloured paths in one `<g>`. |
| `output.gap_filler.enabled` | bool | `true` | Hides hairline seams between abutting shapes. |
| `output.gap_filler.stroke_width` | float | `0.35` | |
| `output.background` | hex or `transparent` | – | Transparent inputs stay transparent by default. |
| `output.size.scale` | float | – | Uniform scale. Mutually exclusive with width/height. |
| `output.size.width` / `.height` | float | – | Target size in `output.size.unit`. |
| `output.size.unit` | `px`,`pt`,`pc`,`in`,`mm`,`cm` | `px` | |
| `output.size.aspect_ratio` | `preserve_inset`, `preserve_overflow`, `stretch` | `preserve_inset` | Used when both width and height are given. |
| `output.bitmap.dpi` | 24–1200 | `96` | PNG rasterization density. |
| `policy.retention_days` | 0–30 | `0` | Accepted for API compatibility; this server is stateless and stores nothing. |

Unknown parameter names are **rejected** with error code `1006` rather than
silently ignored, so a typo surfaces immediately.

---

## Examples

**A print-ready PDF exactly five inches wide**

```bash
curl -u demo_id:demo_secret \
  -F "image=@logo.png" \
  -F "output.file_format=pdf" \
  -F "output.size.width=5" \
  -F "output.size.unit=in" \
  -o logo.pdf http://127.0.0.1:8000/api/v1/vectorize
```

The PDF page comes out at exactly 360 × 270 pt.

**A flat 6-colour logo, grouped by colour for editing**

```bash
curl -u demo_id:demo_secret \
  -F "image=@logo.png" \
  -F "processing.max_colors=6" \
  -F "output.group_by=color" \
  -o logo.svg http://127.0.0.1:8000/api/v1/vectorize
```

**Python**

```python
import requests

with open("logo.png", "rb") as fh:
    response = requests.post(
        "http://127.0.0.1:8000/api/v1/vectorize",
        auth=("demo_id", "demo_secret"),
        files={"image": fh},
        data={
            "output.file_format": "pdf",
            "processing.max_colors": "8",
            "output.size.width": "5",
            "output.size.unit": "in",
        },
        timeout=120,
    )
response.raise_for_status()
open("logo.pdf", "wb").write(response.content)
print(response.headers["X-Path-Count"], "paths")
```

**Fetch by URL, get JSON back**

```bash
curl -u demo_id:demo_secret -H "Accept: application/json" \
  -d "image.url=https://example.com/logo.png" \
  -d "output.file_format=pdf" \
  http://127.0.0.1:8000/api/v1/vectorize
```

---

## Output notes

**PDF and EPS are true vector.** The traced shapes become native path
operators — a converted 240×180 logo yields a 3.4 KB PDF containing 76 Bézier
curve operators and no embedded bitmap, so it scales losslessly and opens for
editing in Illustrator, Inkscape or Affinity.

**PNG rasterizes the vector result**, so what you see is exactly what the
vector contains. `output.bitmap.dpi=192` renders at 2× the 96 dpi baseline.

**Sizing is resolution-independent.** The SVG `viewBox` stays in tracer pixel
space and only `width`/`height` change, so scaling never touches path data.

### Edge smoothness

Two different things make traced edges look bad, and they need different
fixes. Diagnose by zooming in:

* **Chunky, notched edges** — ragged bites out of an outline, or thin slivers
  of a slightly-different shade running alongside it. This is *colour noise*,
  not geometry, and `processing.denoise` is the fix.
* **Faceted curves** — a smooth arc rendered as visible straight segments.
  This is geometry, and `processing.smoothing` is the fix.

#### Denoise (chunky, notched edges)

JPEG compression, scans and screenshots leave thousands of near-duplicate
colours around every hard edge. The tracer faithfully emits each one as its
own ragged sliver. A median filter removes that noise while keeping hard
edges sharp — unlike a blur, which rounds them into mush.

On a JPEG-compressed sticker illustration with soft shading:

| `processing.denoise` | Paths | Distinct colours |
| --- | --- | --- |
| `none` | 127 | 64 |
| `low` (default) | 27 | 14 |
| `medium` | 17 | – |
| `high` | 13 | – |

The artwork has about four real colours, so the 64 the tracer found at `none`
were almost entirely compression noise — and every one of them was a ragged
sliver on an edge.

`low` is the default and is safe: it left a 3px rule crisp and did not lose
any real feature in testing. `medium` and above start thinning fine strokes,
so raise it only for genuinely noisy sources. Use `none` for pristine
synthetic artwork. The filter is skipped automatically on images too small
for its window, where it would erase content rather than noise.

Quantizing instead (`processing.max_colors=6`) also cleans the edges, but it
posterizes smooth shading into visible flat bands. Prefer denoise; reach for
`max_colors` when you actually want a reduced palette.

#### Transition lumps between two colours

Where anti-aliasing blends a dark stroke into its fill, the in-between pixels
form a band, and the tracer emits that band as its own shapes — pale lumps
sitting along the inside of an outline, making the edge look uneven. Worse,
it often splits the band into fills a single RGB unit apart (`#4c3e46` beside
`#4c3e47`).

**No tracer setting fixes this**, because to the tracer those really are
different colours. `filter_speckle` will not catch them either — the lumps
are often 20×60px, far above any sane speck threshold. So the cleanup happens
after tracing: `processing.color_merge` ranks fills by the area they actually
cover, then snaps each minor colour onto the nearest more prominent one
within the given RGB distance.

On outlined lettering over a gradient, saved as a mid-quality JPEG:

| `processing.color_merge` | Distinct fills | Result |
| --- | --- | --- |
| `0` (off) | 21 | Pale lumps clearly visible |
| `16` (default) | 9 | Near-duplicates gone, faint lumps remain |
| `40` | 4 | Clean — the sweet spot for flat artwork |
| `70` | 3 | **Too far** — the pink fill merged into the background |

The default is deliberately conservative. **For flat sticker or logo artwork
with visible lumps, try `processing.color_merge=40`.** Watch the gap between
40 and 70 though: push it too high and genuinely distinct colours collapse
into each other.

Ranking is by covered area rather than bounding box, which matters: a thin
ring's bounding box is as large as the shape it surrounds, so ranking by box
merges the fill into the ring instead of the other way round.

#### Smoothing (faceted curves)

`processing.smoothing` raises `corner_threshold`, `splice_threshold`,
`max_iterations` and `length_threshold` together. `corner_threshold` is the
angle below which a bend stays a hard corner instead of becoming a curve, so
turning the preset up rounds more of the outline.

| Preset | corner | splice | iterations | length |
| --- | --- | --- | --- | --- |
| `none` | 60 | 45 | 10 | 4.0 |
| `low` (default) | 80 | 50 | 16 | 4.0 |
| `medium` | 110 | 60 | 24 | 4.5 |
| `high` | 160 | 80 | 32 | 5.0 |

**`high` rounds aggressively** and will bow thin straight elements — a 3px
rule visibly tapered at its ends in testing. Step up only as far as your
artwork tolerates, and compare before committing.

The presets deliberately leave `processing.shapes.min_area_px`
(`filter_speckle`) and `processing.layer_difference` alone. Both reduce
apparent jaggedness, but the first does it by deleting small shapes — at 16 it
silently erased 12px text from a test logo — and the second by flattening
colour bands. Neither is a smoothing decision, so both stay explicit.

**If output still looks jagged**, the tracer is usually not the problem:

* **Check what you are looking at.** `output.file_format=png` rasterizes at
  `output.bitmap.dpi`, which defaults to 96 — the same size as the source, so
  it looks exactly as jagged. Ask for `output.bitmap.dpi=384`, or use SVG/PDF,
  which are resolution-independent.
* **Feed it more pixels.** Tracing follows the pixel staircase it is given.
  A 100px logo cannot produce a smooth 1000px curve.
* **Ragged notches along an outline** are colour noise, not geometry — raise
  `processing.denoise`. If noise is not the cause, the soft edge may be
  banding into separate thin shapes; `processing.layer_difference=48` merges
  those, and `processing.max_colors` is the blunter alternative.

Pre-blurring or upscaling the bitmap before tracing was tried and rejected —
it makes edges visibly lumpy, because the softened ramp gives the curve fitter
a wobbly boundary to follow.

### A pale shape coming back the colour of its background

A median despeckle is a lossy operation dressed as a cleanup. It answers what
the majority of a neighbourhood says, so anything that is a minority in its own
window gets rewritten as its surroundings — and on a file with no compression
noise, rewriting is the only thing it does.

It cost a white brush-stroke heart on a pale pink sticker its entire fill. The
stroke is 11 to 106 pixels wide and survived the filter *as pixels*, but the
filter changed the ramp along its edges enough that the tracer stopped
clustering it separately: the palest colour to come back was `#f9eef5` against
an `#f2d7ea` ground — a heart the same colour as the page it sits on. Traced
without the filter it comes back whole, and agreement with the source goes from
1.26 to 0.84.

So the despeckle now runs by default only on **lossy sources**, which is the
only place the noise it exists to remove can come from. There it keeps its
place and earns it: the same artwork at JPEG quality 70 traces to 2 objects
with the filter and 35 without, the extra 33 being ringing around the edges
rather than anything in the artwork. Setting `processing.denoise` yourself
still applies it to anything, lossless included — skipping it is a default, not
a policy.

### Ghost layers in an editor

Separate the layers of a traced JPEG and you may find a faint near-white
shape sitting behind the white one, or a grey twin behind the grey. They are
not wobbly edges on one shape — they are *extra shapes* in almost the same
colour.

The cause is upstream of the tracer. A real 1600px JPEG of six-colour
lettering contained **10,588 distinct colours**:

```
1,778,918  #666666   <- the grey background
   15,256  #656565   <- the same grey
   13,693  #676767   <- the same grey
    6,214  #686868   <- the same grey
```

Compression smears every flat colour into a cloud, and the tracer faithfully
makes each shade its own shape.

**`processing.color_merge` cannot always fix this.** It works on RGB
distance, and the intermediate greys are genuinely closer to other real
colours than to the one they belong to — in that file `#b7b7b6` was nearest
to the sage green. Raising the threshold far enough to catch the ghosts also
collapses colours you wanted to keep.

**Flat artwork gets this automatically.** With no colour settings at all,
preprocessing checks whether the image resolves to a handful of real inks. If
it does, it uses them; if it does not — a photograph, a gradient — it leaves
the image alone.

Telling an ink from the transition tone beside it is the whole difficulty,
and two conditions have to hold together before a colour is dismissed. Either
on its own gets a case badly wrong.

It has to be **explained as a blend** of two inks already accepted — the
pixels between a charcoal stroke and a cream fill lie on the line between the
two. Area cannot decide this: on the test file the band along the letter
edges covered *more* of the image (0.50%) than the sage green of the flower
leaves (0.27%), so any threshold on size keeps the wrong one.

And it has to be **thin**, holding almost nothing once a mode filter has
removed everything that is merely a thread. Blend alone cannot decide it
either, because a neutral grey sits exactly on the line between black and
white: on this artwork that dismissed the grey background — eighty percent of
the image — and took the whole palette with it.

Together they are right in all four directions. A grey background is a blend
but not thin. A charcoal hairline is thin but not a blend. An edge band is
both. A flower leaf is neither.

A gradient sliced into flat bands passes all of that, though, and this is the
one that bit hardest. Add enough bands and every pixel really is close to one
of them, so no amount of counting colours or measuring how far pixels sit from
them tells a ramp from flat artwork — on shaded lettering the residual came in
at 1.61, well inside the cutoff, and the strokes came back as hard bands with
the palest snapped onto the white of the halo.

What gives a ramp away is *where its bands sit in the picture*. A band is the
only thing separating the two colours either side of it, because that is what
a ramp is. A real ink that merely lies between two others in RGB — a grey
background between black lettering and a white halo — is not, because those
two also meet each other directly all over the artwork. Measured: the grey
background's neighbours touch 7,216 times directly against 9,199 through it,
while the gradient band's neighbours touch **zero** times except through it.
One such band anywhere declines the whole palette, and the artwork goes to the
tracer unflattened.

Counting inks is not enough on its own either, because a few bands cut through
a gradient also come back as a short list. The last test is how far the
pixels sit from the nearest ink — near zero on flat artwork, several units
once there is shading. Measured: **0.00** for a PNG logo, **1.59** for a
lettering JPEG, **2.93** for hard-edged shapes with heavy JPEG ringing,
**9.58** for a shaded sticker, **26.50** for a shaded sphere.

An explicit choice always outranks this: asking `processing.max_colors`,
`processing.palette`, `processing.detail`, `processing.color_precision` or
`processing.layer_difference` for anything other than its default turns it
off, so `detail=maximum` still keeps every hairline feature and every scrap of
compression noise the way it promises to.

**What counts is the value, not the mention.** This holds for every parameter,
not just these — the smoothing and detail presets, the denoise pass and the
colour pipeline all decide on what a field *holds*, never on whether it was
sent. Generated clients and Swagger's "Try it out" form post every field they
know about, filled in with the values the schema showed them; reading that as
an instruction gave those callers a different file from callers who sent
nothing, quietly and for no reason they could see. Posting every parameter at
its documented default now returns byte-for-byte what sending nothing returns.

The cost is that a parameter cannot be set *to* its own default as an
override. `processing.corner_threshold=60` reads as "no opinion", so the
smoothing preset's 80 still wins; `processing.smoothing=none` is how you ask
for 60.

#### Tracing at twice the resolution

A one-pixel line cannot be quantized evenly. Whether a given pixel lands on
the dark side of the boundary depends on where the line falls *within* that
pixel, so its width wanders — measured at 1–4 pixels on the reference artwork
where the source varies 1–2 — and the curve fitter follows every wobble. That
is what leaves a hairline outline looking thick in places and thin in others.

Tracing the bitmap at twice its own resolution halves that wander relative to
the line, and the outline comes out even: 194 traced shapes became 89 on the
reference file. The extra pixels buy accuracy, not size — they show up in the
`viewBox` and divide back out of the width and height, so the document is the
same size it would otherwise be.

Two of the tracer's knobs are measured in pixels, and pixels change size when
the bitmap is doubled. Left alone they quietly weaken — a 4-pixel shortest
segment becomes 2 source pixels, so the fitter starts following the staircase
it was meant to cut across, and the speckle filter loses three quarters of its
reach. Both are restored to what they mean at 1×: `length_threshold` scales
with the factor, `shapes.min_area_px` with its square. On the reference file
that took 89 traced shapes to 76 and the SVG from 683K to 506K.

Hard-quantizing a soft edge also leaves it ragged: the ramp crosses the
boundary, compression noise makes it cross back, and the fringe of single
pixels gets traced. A mode filter settles that — and eats any hairline along
with it, which is the original problem again. So it is applied only where it
cannot do harm: a pixel sits in a structure at least three pixels wide exactly
when some 3×3 window containing it holds a single ink, so mark the uniform
windows, spread the mark by one pixel, and smooth only what it covers. On the
reference file that touches 0.012% of the pixels and takes the SVG from 506K
to 423K — the same curves with a sixth fewer nodes to edit. It costs about
1.9s on a 9.3M-pixel bitmap, and runs only on the finer copy.

#### Artwork that keeps its own colours

Everything above needs a palette: the ramp is snapped onto real inks, and the
mode filter works on ink labels. Shaded artwork gets no palette — flattening a
gradient to a dozen colours would be vandalism — so until recently it got none
of this either. It was traced once, at its own resolution, with the ramp along
every edge still in it, and the tracer did what it does with a ramp: clustered
those tones into a layer of their own. Every outline came back with a sliver
of blend running beside it, and the two boundaries either side of that sliver
each followed the pixel grid. At any real zoom the line was not smooth. It
stepped, and it carried a band.

The ramp is the fix, not the problem. A soft edge records where the boundary
truly falls, at finer than a pixel, and the ramp is that record. So on a copy
enlarged 2× — bilinear, because lanczos rings and a channel that has rung past
its neighbour picks the opposite side from the other two, which stipples the
boundary with colours the artwork never had — every pixel inside a ramp is
pushed to whichever end of its own neighbourhood it sits nearer. That places
the edge within a quarter of a source pixel of where the ramp says it is, and
leaves no blend tones for the tracer to find. On a shaded illustration it took
38 paths to 20 and 57 shapes to 30, and the arcs that were ragged came back
clean.

It is deliberately narrow about when it runs, because most of the ways it
could go wrong are ways of damaging artwork it was not aimed at. It stands
down for a bitmap whose edges are already hard, where there is no sub-pixel
position to recover and enlarging only squares off an existing staircase; for
one that carries a real gradient, since the tracer cuts a ramp into more flat
layers the more pixels it is given; for a source under 128 pixels on its short
side, where the fitter's own shortest segment is a sixteenth of the picture;
for a photograph, whose noise never clears the contrast floor; and whenever
the caller is driving the colours themselves, for the same reason automatic
palette detection stands down there. Each of those keeps the plain 1× trace
unchanged. Across a corpus of flat art, hairlines, gradients, compressed
JPEGs, a photograph, a thumbnail and pixel-plotted shapes, only the shaded
illustrations came out different at all — every other file was byte-identical.

The two guarantees worth stating, because the obvious alternatives have
neither: the stage can only ever write a value that already occurs in the
pixel's own neighbourhood, so it cannot invent a colour or bow a curve between
its nodes the way moving control points does; and it only ever moves a pixel
*to* an end, never past one, so it cannot erase a feature — a one-pixel rule
comes through exactly where it was, where a median or mode filter would eat
it. It costs about 2.3s on a one-megapixel illustration.

Below one source pixel there is nothing real left for the fitter to follow —
whatever wobble survives at that scale is where the quantizer happened to put
the boundary, not something the artwork contains. So when the bitmap is traced
finer, the fitter is given more latitude to cut across it: longer segments,
curves spliced rather than cornered, and more passes to settle them. On the
reference file that took 10,488 curve segments to 8,855 and the SVG from 420K
to 346K.

`corner_threshold` is deliberately left out of that. It is the angle below
which a bend stays a hard corner, so anything above 90 rounds off a right
angle — the `medium` preset's 110 turned a test rectangle's corner into a
visible curve, while the changes above left the same rectangle pixel-identical
to the conservative setting. Everything that was raised smooths the long runs
between corners and leaves the corners themselves alone.

`processing.smoothing=medium` or `high` will go further, and is safe on
artwork with no sharp corners to lose — hand-lettering, say. It is not a
default for the reason just given.

#### Fusing the segments

What is left after all of that is a gentle waviness, and its cause is simply
that there are too many nodes. Every node the tracer emits marks somewhere the
pixel boundary turned, so a run of them along one gentle curve is a run of
chances to wander.

So neighbouring segments are fused wherever a single cubic covers both to
within **1.2 source pixels**, checked by sampling the pair and its replacement
rather than trusting the algebra. The join lands at some fraction along the
pair, so each outer handle is stretched by that fraction to span the whole
thing, and a corner is never fused across. Repeating the pass helps: every
round leaves fewer, longer segments, and a pair too curved to span before
often is not next time. On the reference file the outline went from 8,823
curve segments to 6,424 and the SVG from 347K to 259K, and a long edge reads
as one stroke rather than a chain of them.

Corners are found by measuring direction across a span of outline rather than
at a single node, because the tracer often splits one in two: a right angle
came back as a pair of 44-degree turns, neither of which looks like a corner
on its own.

Two other things were tried here and **removed**, because both quietly wrecked
thin artwork. Rotating the handles either side of a node onto the tangent a
smooth curve would have takes the kinks out, and easing nodes towards the line
between their neighbours takes out the pixel-level zigzag — but both move the
curve *between* nodes, and where two sides of a one-pixel outline run close
together that is enough to swallow the gap. On the reference lettering the
charcoal outline came out thick and lumpy at the top of an O with its white
halo gone. Fusing segments does not have that failure mode: it only ever
replaces two curves with one that provably runs where they did.

`processing.smoothing=medium` or `high` will go further, and is safe on
artwork with no sharp corners to lose — hand-lettering, say. It is not a
default for the reason just given.

#### Straightening the curve itself

What survives all of that is a *tangent break*: the segment arriving at a node
points one way and the segment leaving it points another, so the outline
visibly corners where the artwork does not. The node itself is usually right —
it sits on the boundary the tracer found — so the fix is to leave every node
exactly where it is and rotate only the two handles either side of it onto the
tangent a smooth curve would have there. Handle lengths are untouched, so the
curve keeps its shape between nodes, and a node whose tangents disagree by
more than 60° is a real corner and keeps its break.

Refitting the nodes instead — interpolating a fresh curve through them — was
tried first and is much worse. The tracer's nodes are sparse and its curvature
lives in the handles, so discarding those flattens long arcs into chords; on
the reference artwork it turned the outlines into a row of spikes. The uniform
Catmull-Rom form also gives handles five times their own chord on unevenly
spaced nodes, which loops a segment right over itself.

Aligning the handles is not the whole job, though, because the curve still has
to pass through nodes that zigzag by a pixel wherever the quantizer rounded one
way and then the other. So each node is also eased towards the line between its
neighbours — carrying its two handles with it, so that piece of outline slides
rather than swinging — under three rules that each fix something that went
wrong without them:

* **Half a step, alternating with a slightly larger one back.** A full step
  overshoots to the far side and the zigzag simply inverts, pass after pass.
  And easing every node towards its neighbours drags a curve onto its own
  chords: on a test ring it pulled every node inward by more than the wobble it
  was removing. Taubin's second step restores that without bringing the zigzag
  back.
* **A budget on where a node ends up**, not on how far it travelled getting
  there — the two steps of a pair mostly cancel, so counting both exhausts the
  allowance without the node having moved. Nothing drifts more than a pixel
  from where the tracer put it.
* **Corners measured across a span of outline**, not at a single node. The
  tracer often splits a corner in two: a right angle came back as a pair of
  44-degree turns, neither of which looks like a corner on its own, and
  smoothing duly rounded it off.

What remains after all of that is not a zigzag but a gentle waviness, and
easing nodes cannot touch it — Taubin's whole point is that it leaves the low
frequencies where they are. The cause is simply that there are too many nodes:
each one marks somewhere the pixel boundary turned, so a run of them along one
gentle curve is a run of chances to wander. So neighbouring segments are fused
wherever a single cubic covers both to within **0.8 source pixels**, measured
by sampling the pair and its replacement. Repeating that helps, because every
pass leaves fewer and longer segments and a pair too curved to span before may
not be next time. On the reference file the outline went from 8,823 curve
segments to 7,287 and the SVG from 399K to 329K, and a long edge finally reads
as one stroke rather than a chain of them.

Resampling sharpens noise as readily as geometry, though. On a heavily
compressed test image, where the tracer was already splitting one dark ring
into two shades, the same treatment took 55 traced shapes to 154. Which way a
given image goes cannot be read off the image, so it is measured rather than
guessed: both are traced and whichever came out simpler is kept. That costs a
second tracing pass — about 2.7s instead of 0.7s on a 1600px file — and it is
skipped entirely for images with no detected palette, or large enough that
doubling them would exceed the pixel budget.

`processing.denoise` is deliberately not on that list, but it does change
when it runs. A median filter is the right tool when the tracer will see raw
pixels and the wrong one when every pixel is about to be mapped onto a known
ink: it is redundant there, and it eats anything a pixel or two wide, because
a hairline is a minority in its own window. On the test artwork the default
median left the charcoal outline around the lettering thick in places, thin in
others and broken into dashes. So denoising happens only when the colours are
staying as they are, or when a level was asked for explicitly.

The same reasoning applies to the speckle pass that runs after quantization.
It clears specks of a pixel or two, but it requires seven of the nine pixels
in a window to agree before touching anything — a pixel on a one-pixel line
has two more of its own kind beside it, so only six agree, and the line
survives. A plain mode filter took 6,154 pixels off that charcoal outline;
this takes 557.

That test is also its limit. Requiring seven of nine to agree means the pass
can only reach a speck sitting in a plain field, because where two inks meet
they never do agree — so the specks that survive it are exactly the ones on a
boundary. Those are the expensive ones. A charcoal pixel that JPEG ringing
threw out into the grey beside a white outline cannot be ignored by the
tracer: the boundary running past it has to detour around it and back, and
that detour is a notch in a curve that was otherwise smooth. On the reference
artwork there were **9,969** of them in nine megapixels — a tenth of one per
cent of the bitmap, and enough to leave a notch every 250 pixels of finished
outline, which is what made the lettering look faceted at any real zoom.

Clearing them is also the only kind of smoothing that cannot open a seam.
Every shape is traced separately, so nudging one shape's outline moves it away
from the shape that abutted it and lets the background show through the crack
— which is what ruled out relaxing the traced nodes, a treatment that looked
excellent and doubled the seams. A stray taken out of the bitmap is taken out
for both shapes at once, and both stop detouring in the same place. It took
the notches down by a fifth and *closed* an eighth of the seams. It costs
about 0.75s on a 9.3M-pixel bitmap of six inks.

Having no company of its own cannot be the whole test, though, because a thin
feature is made of lonely pixels too. A one-pixel white line on a dark ground
does not quantize to white: the ramp puts some of its pixels on white and the
rest on the mid-grey between the two, so pixel by pixel the line is alone
among its own kind. Judged on company alone it is a run of strays, and
outline-only lettering duly disappeared.

What tells the two apart is where the colour sits. Mixing two inks can only
ever land *between* them in brightness, so that mid-grey — a blend of the line
and the ground — is never the darkest or the lightest thing in its window.
Ringing is the opposite: it overshoots past everything around it, which is why
the charcoal speck is darker than both the grey it sits in and the white it
rang off. So a pixel goes only when it is alone **and** an extreme, and
anything that could be a blend of what surrounds it stays. Nothing else in the
corpus moved by a pixel.

**A colour budget does the same thing on demand.** `processing.max_colors`
derives that many inks and maps both the pixels and the traced fills onto
them. On the test file:

```
processing.max_colors=6
```

took 18 fills and 135 objects down to **6 fills and 36 objects**, recovering
grey, cream, charcoal, pink, sage and the white outline. Ask for roughly the
number of inks the artwork actually has: a budget larger than that spends the
spare slots on the transition tones you were trying to remove.

**Pinning the palette is the strongest form of it.** Every pixel snaps to a
colour you chose, so there is no derivation to second-guess:

```
processing.palette=#666666,#fbf7da,#2d2c2a,#f09ec2,#c7cda0,#ffffff
```

On that file: 18 fills and 135 objects became **6 fills and 26 objects**, with
the artwork visually unchanged — better preserved than the alternatives,
which shifted the whites toward cream.

Note this needs the output fills to be snapped as well as the input pixels.
The tracer averages the pixels inside each cluster, so boundary clusters come
back as blends: pinning six colours produced eighteen until the result was
snapped back to the palette too.

#### Edge pixels resolve to the two colours they lie between

A pinned palette does *not* map each pixel to the nearest palette colour.
Nearest is wrong along an anti-aliased edge, and wrong in a way you can see:
halfway between the charcoal stroke and the cream fill is `(148, 146, 130)`,
whose nearest entry in the palette above is the **grey background** — a colour
that touches that edge nowhere in the artwork.

On this file that mapped a one-to-two pixel grey ribbon along every letter,
and the tracer turned the ribbon into grey shapes lying against the white
outline. Separate the layers in an editor and the white one had grey jagged
lines running through it; the outline was no longer a clean white edge.

So the input is quantized against the palette *plus* samples taken along the
segment between every pair of its colours, and each sample resolves to the
colour it sits nearer to. An edge pixel then lands on one of the two colours
that actually meet there, never on a third that merely sits nearby in RGB.
On the test file that took the artwork from 279 traced shapes to 156, with
the white outline continuous all the way round.

The probe palette has the same 256-entry ceiling as any other, so beyond
about 22 pinned colours there is no room for the ramps and mapping falls back
to plain nearest. Palettes that large are unusual — the feature is for
pinning a handful of ink colours.

To find a palette for your own artwork:

```powershell
.venv\Scripts\python.exe tools\tune.py samples\yourfile.jpg --palette 6
```

Treat its output as a starting point — it reports the most-used distinct
colours, which can include an anti-aliasing shade rather than a real ink
colour. Edit the list before using it.

### Checking a new image

Most of what the pipeline does is decided automatically and is invisible in
the finished file. `tools/audit.py` prints those decisions, so a new image can
be checked in one line rather than opened in an editor and squinted at:

```powershell
.venv\Scripts\python.exe tools\audit.py samples\
```

```
  file                         size        inks  resid  objects  fills     ms
  sample_bump.jpeg             1600x1459      6   1.59       13      6    691
      #666666 #fdf9dc #2b2a28 #ffffff #f19fc3 #c9cfa1
  photoish.jpg                 600x600        -    nan        1      1    296
```

*inks* is the palette detected from the artwork and *resid* how far the
average pixel sits from the nearest one; a dash means the image was read as
continuous-tone and left alone, which is what should happen to a photograph.
It warns about the failure modes worth knowing about — a palette that lost a
colour the artwork clearly uses, an object count high enough to mean
anti-aliasing is still being traced, and a residual close to the cutoff, where
a slightly noisier version of the same image would be treated differently.

Pass `--write DIR` to save each result as an SVG at the same time.

### Shaded artwork and gradients

The tracer only ever emits flat fills, so shaded artwork had nowhere to go.
Banding it into narrow strips reads as stripes; collapsing each region to one
colour throws the shading away. Neither is what the artwork says.

So after the document is built, a second stage goes back to the pixels. It
rasterizes the finished vector, so it knows exactly which source pixels each
shape covers, fits a colour model to those pixels, and where they turn out to
lie on a ramp replaces the flat fill with a real SVG gradient along it:

```xml
<defs><linearGradient id="shade0" gradientUnits="userSpaceOnUse"
      x1="419.6" y1="102" x2="419.6" y2="498">
  <stop offset="0" stop-color="#ce8c9e"/><stop offset="1" stop-color="#efd5db"/>
</linearGradient></defs>
<path d="…" fill="url(#shade0)"/>
```

Three things it does that comparing fill colours to each other cannot:

**It knows where each shape is.** Attributing pixels by "nearest fill colour"
mixes every shape of one colour together, wherever it sits, so a ramp fitted
to them describes nothing in particular. A geometric mask is what makes a
per-shape fit mean anything — and it is what lets a picture that traced to a
*single* shape be fitted at all, which is the case a fill histogram has
nothing to compare and used to leave as one averaged colour.

**It fits curves, not only lines.** Shading is rarely linear in sRGB —
falloff across a sphere certainly is not — so the ramp is profiled along its
own axis and reduced to as many stops as it takes to stay inside tolerance.
Two stops where two will do, more where the shading needs them. Shading that
spreads from a point gets a `radialGradient`, found by solving for the centre
in closed form and refining it by Nelder-Mead.

**It repairs patches.** Neighbouring shapes that are slices of one ramp are
fitted together and share a single gradient in user space, so the colour runs
continuously across the seam between them rather than stepping at it. Whether
two neighbours really are one ramp is decided by whether their colours agree
*along the edge they share*, then confirmed by fitting them together: a shared
ramp is only used if it describes the group about as well as the separate
answers did. No geometry is changed; only the paint.

A fill still has to earn a gradient. The colour must travel at least
`gradients.min_travel` in CIELAB from one end of the shape to the other; the
ramp must explain the pixels at least a fifth better than a flat fill does,
*and* better by a full CIELAB unit, so a fill that is a whisker outside the
visible threshold and made a whisker better is left as it is; and the shape
must be bigger than `gradients.min_area_px`. A fill already inside the
threshold of visibility is never touched — there is nothing there for a
gradient to put right, and one would cost a definition and an object the
editor has to carry. Stops are rationed by the evidence available too, so a
sliver of a hundred pixels cannot buy a twelve-stop curve. Flat artwork comes
back with the flat fills it had.

#### Shading inside artwork that is otherwise flat

A palette the **caller** chose is a promise, and a gradient would break it:
asking for six inks and getting a ramp is not what was ordered. So
`processing.palette` and `processing.max_colors` keep the fills flat.

A palette the **detector** derived is not a promise — it is a guess that the
artwork is flat, and the guess is only ever wrong in one direction. Sticker
lettering is the usual case: a grey ground, a white halo, a near-black
outline and one colour on the letters all read as flat, so inks are found and
the bitmap is mapped onto them — and the one part that was *not* flat, the
shading on the letters, gets cut into two or three shades of the same colour.
Every shape it crosses then comes back as hard-edged patches of those shades
with a ragged step between them, which is exactly what a gradient exists to
prevent.

So the guess is checked rather than trusted. The shapes are judged against
the artwork's own pixels from **before** they were mapped onto the palette,
which is the only copy that still has the ramp in it; where they really are
flat, nothing changes and the file comes back byte for byte as it did before.

Two things keep that check honest on artwork made of thin inks:

- **A shape thinner than the blend around it is not judged at all.** A traced
  outline a few pixels wide has a different ink on each side, so most of what
  it covers is the ramp between them and eroding the mask cannot remove it
  all. Fitted as it stands, a black outline beside a white halo comes back as
  a ramp running from black to nearly white — a fine description of that
  boundary and nothing to do with the shape, which is one flat black.
- **Pixels that resolve to another ink are set aside.** The palette says what
  every pixel was meant to be, so the evidence about a shape can be separated
  from the evidence about its neighbours. It is a *family* of inks rather than
  one, because the case this serves is a ramp the quantizer had to cut in two:
  insisting on the exact ink would throw away half of the shape's own ramp.

**When no ramp fits at all, the structure gets one colour.** A run of
neighbouring shapes can be found to be slices of one thing -- they agree along
the edges they share and they are the same ink -- and still have no single ramp
that describes them, with each of them flat on its own. Left alone that is
precisely a patchwork: one letter arriving as blocks of two barely different
colours with a ragged step between them, which is worse to look at than either
colour would be on its own. So the whole structure is painted in the **darkest**
of the inks it already uses -- darker rather than lighter because these are
stickers and lettering, where the shape reads against its background and the
paler choice thins it, and one of its own inks rather than an average, because
an average is a colour the artwork does not contain. Every member ends up with
the same fill, so the shapes fuse into a single object with nothing visible
inside it. This is a fallback, never a preference: where the pixels really do
describe a ramp, the ramp wins. And because it is the one step here that
throws information away, it is refused for any group spanning more than one
family width in CIELAB however far `gradients.patch_distance` is opened --
without that ceiling, opening the width to 45 took the reference lettering
from 0.57 to 29.11 against its source.

The same reasoning applies to merging. Neighbours have to agree along the edge
they share *and* be the same ink, because groups grow by chaining and a chain
is continuous across a boundary it should never cross — a pink fill meets the
sliver of pink-white blend beside it, which meets a paler sliver, which meets
the white halo. Without the second test that chain painted the halo with the
letter's ramp.

Measured over a corpus of shaded artwork — a linear banner, a diagonal badge,
a shaded sphere, a landscape and a full-frame ramp — mean CIEDE2000 against
the source fell from **4.33 to 0.29**, with the two worst cases (the sphere at
5.35 and the full-frame ramp at 15.14) coming down to 0.33 and 0.11. Flat
artwork was byte-identical before and after.

**Format support is uneven, and it is reportlab's, not ours.** Its PDF backend
writes a real `/Shading`, so PDF and SVG keep the gradient. Its PNG and
PostScript backends have no gradient support at all — they do not ignore one,
they raise partway through drawing — so for those two each gradient fill is
replaced by the average colour along the ramp, weighted by how much of the
ramp each stop governs. The PNG is a preview and the flat stand-in is honest
about that; if you need EPS with real shading, that needs a different renderer.

#### Running the stage on its own

`POST /api/v1/gradients` is the same stage, exposed for a document you already
hold — to re-fit an SVG whose gradients were flattened somewhere downstream,
or to see what it makes of one image without re-tracing. `/vectorize` runs it
for you on every conversion, so nothing has to be called here to get gradients
out of the main endpoint, and the route is kept out of the published schema
for that reason.

It takes the original bitmap exactly as `/vectorize` does (`image`,
`image.base64` or `image.url`) plus the vector as a `vector` file part,
`vector.svg` text or `vector.base64`, and returns the refined SVG:

```bash
curl -u demo_id:demo_secret http://127.0.0.1:8000/api/v1/gradients   -F image=@artwork.png -F vector=@artwork.svg -o refined.svg
```

It re-fits from the source rather than compounding, so running it on its own
output is a no-op rather than a second layer of definitions — and a gradient
that the pixels do not support is removed as readily as one is added.

### Object count in Illustrator, Corel, Inkscape

The tracer emits every fragment as its own path, and editors count each path
as an object. A six-colour illustration opened in CorelDRAW as **"Group of
761 Objects"** — impossible to select or recolour by hand. But merging too
hard is just as bad: fuse a whole colour and a word becomes one object you
cannot pick letters out of.

`output.combine_paths` chooses where to sit between those:

| Mode | What merges | Objects (test file) | Use when |
| --- | --- | --- | --- |
| `none` | nothing | 34 | you want every traced fragment |
| `shapes` (default) | fragments that touch | **20** | normal editing — one object per letter or motif |
| `colors` | everything of one fill | 5 | you only ever recolour whole layers |

`shapes` groups by overlapping bounds, which is an approximation: two tightly
kerned letters whose boxes intersect will merge. That is far less disruptive
than either extreme.

**Merging forces `processing.hierarchical=cutout`, and that is not optional.**
It reorders shapes, which destroys the back-to-front paint order `stacked`
depends on — letter counters and holes get painted over solid. Cutout shapes
do not overlap, so order carries no meaning. Choosing `stacked` turns merging
off rather than failing the request; clients that echo every default back
(Swagger's "Try it out" does exactly that) would otherwise all trip an error
they never asked for.

Cutout output runs roughly 50% larger than stacked, since each shape carries
its own full outline instead of sitting on top of its neighbour.

The response reports the outcome: `X-Combined-Paths` is the mode actually
applied, `X-Path-Count` is objects in the file, and `X-Shape-Count` is traced
shapes before merging.

### Detail (thin lines and small features)

`processing.detail` sets how small a shape has to be before the tracer throws
it away, and how many decimal places survive in the path data.

| Preset | Discards below | Path precision |
| --- | --- | --- |
| `low` | 8 px | 2 dp |
| `standard` (default) | 4 px | 3 dp |
| `high` | 1 px | 4 dp |
| `maximum` | nothing | 5 dp |

**The cliff here is sharp, and worth understanding before you pick.** On a
200px test card, a 2px rule and a 3px dot were discarded at `standard` *and*
at `high` — only `maximum` kept them. But `maximum` also keeps every
single-pixel scrap of compression noise: on a noisy JPEG the path count went
from 27 to 2098 and the SVG from 1.5 KB to 44 KB.

So `maximum` is a deliberate trade, not a better default. Use it when your
artwork genuinely contains hairline strokes or tiny marks, and expect a large
file.

**Do not reach for denoise to clean up after it.** Denoising cannot bring
that path count down — the survivors are real pixel differences, not isolated
outliers — and it washes out the very thin features `maximum` exists to
preserve. In testing, `maximum` + `denoise=medium` recovered the 2px rule but
rendered it pale and broken. Turn denoise *down* when you turn detail up.

### Detail and smoothness pull against each other

There is no setting that gives both. Denoising and speckle-filtering buy
clean edges by discarding small variation, and small variation is also what
fine detail is made of. Pick per image:

| Your artwork | Try |
| --- | --- |
| Clean logo, flat colour | defaults |
| Noisy JPEG / screenshot, chunky edges | `processing.denoise=medium` |
| Hairline strokes, tiny marks | `processing.detail=maximum`, `processing.denoise=none` |
| Noisy *and* detailed | `processing.detail=high`, `processing.denoise=low`, then accept a compromise |

Supersampling — tracing an upscaled copy to recover sub-pixel edges — was
tried three ways (plain upscale, upscale + blur, and upscale followed by
snapping back to the source palette). All three made things worse: the
interpolated ramp invents intermediate colours that the tracer bands into
extra slivers, taking the sticker test from 27 paths to 93–141. Not shipped.

### What vectorizes well

Flat-colour artwork is the sweet spot — logos, icons, line art, screenshots,
scanned text, stickers. Photographs and smooth gradients get **posterized**
into flat regions, because the tracer emits solid-filled paths and does not
synthesise gradient meshes. For photographic input, set
`processing.max_colors` (8–24 is a reasonable range) and raise
`processing.shapes.min_area_px` to suppress noise specks — but expect a
stylised result rather than a faithful one. This is a genuine limitation of
the tracing engine, not a configuration mistake.

---

## Configuration

Environment variables, all prefixed `VECTOR_` — see `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `VECTOR_REQUIRE_AUTH` | `true` | Turn authentication off for local use. |
| `VECTOR_API_KEYS` | `demo_id:demo_secret` | Comma-separated `id:secret` pairs. |
| `VECTOR_MAX_UPLOAD_BYTES` | 32 MiB | Rejected from `Content-Length` before the body is read. |
| `VECTOR_MAX_INPUT_PIXELS` | 30 M | Hard ceiling on input resolution. |
| `VECTOR_MAX_OUTPUT_PIXELS` | 33 M | Ceiling on PNG output. |
| `VECTOR_JOB_TIMEOUT_SECONDS` | 120 | Per-job wall clock limit. |
| `VECTOR_MAX_CONCURRENT_JOBS` | CPU count | Simultaneous tracing jobs. |
| `VECTOR_RATE_LIMIT_PER_MINUTE` | 60 | Per key; `0` disables. |
| `VECTOR_ALLOW_URL_FETCH` | `true` | Enable `image.url`. |
| `VECTOR_ALLOW_PRIVATE_NETWORK_FETCH` | `false` | Keep `false` in production. |
| `VECTOR_CREDITS_ENABLED` | `true` | Credit accounting. |

---

## Errors

Every failure returns the same envelope:

```json
{ "error": { "status": 400, "code": 1006, "message": "Unknown parameter 'output.file_formatt'.", "request_id": "a1b2c3" } }
```

| Code | Meaning | Code | Meaning |
| --- | --- | --- | --- |
| 1001 | No image supplied | 1008 | URL fetch failed |
| 1002 | More than one image supplied | 1009 | URL not allowed (private address) |
| 1003 | Image could not be decoded | 2000/2001 | Unauthorized / bad credentials |
| 1004 | Too many input pixels | 2002 | Insufficient credits |
| 1005 | File too large | 3000 | Rate limited |
| 1006 | Bad parameter | 5001 | Vectorization failed |
| 1007 | Unsupported output format | 5003 | Job timed out |

---

## Security

`image.url` makes the server an HTTP client on the caller's behalf, so every
URL — **and every redirect hop** — is resolved and checked against private,
loopback, link-local, multicast and reserved address space before connecting.
This blocks the usual SSRF targets, including cloud metadata endpoints at
`169.254.169.254`. A determined DNS-rebinding attack remains theoretically
possible since the check and the connection resolve separately; run the fetcher
through an egress proxy if you accept URLs from untrusted callers.

Uploads are capped by `Content-Length` before the body is read, decoded images
are capped by pixel count, and Pillow's own decompression-bomb heuristic is
replaced by an explicit limit that returns a clear `1004` error.

---

## Testing

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
```

(or plain `pip install -r requirements-dev.txt` and `pytest` with the
environment activated)

203 tests cover every output format, all three image-input styles, parameter
validation and rejection, geometry and unit conversion, colour quantization
and palette pinning, draw styles, transparency, watermarking, credits,
authentication, rate limiting, SSRF policy and the error envelope — plus the
gradient stage end to end and up close: where a shape sits once its transform
is applied, which pixels its holes exclude, whether a ramp whose channels move
apart is still found, and whether neighbouring slices of one gradient end up
sharing it.

---

## Project layout

```
app/
  main.py              app factory, middleware, request ids
  config.py            settings
  api/
    parsing.py         request -> (image bytes, validated params)
    deps.py            auth, throttling, injection
    v1/vectorize.py    the conversion endpoint
    v1/gradients.py    the gradient stage, exposed on its own
    v1/meta.py         health, formats, parameters, account
  core/
    errors.py          error taxonomy and JSON envelope
    security.py        credential verification
    credits.py         credit accounting
    ratelimit.py       sliding-window limiter
  schemas/
    params.py          every vectorize parameter, with validation
    gradients.py       the gradient stage's own parameters
  services/
    preprocess.py      decode, EXIF, alpha, downscale, quantize
    engine.py          VTracer wrapper
    svgdoc.py          viewBox, sizing, draw styles, grouping, watermark
    gradients.py       fits gradients to the source pixels under each shape
    render.py          SVG -> PDF / EPS / PNG
    pipeline.py        orchestration, threading, timeouts
    fetch.py           SSRF-guarded URL fetching
tests/                 203 tests
```

---

## Production notes

The default credit store and rate limiter are **in-memory and per-process**.
Running `uvicorn --workers 4` gives each worker its own balances and counters.
Back both with Redis or a database before scaling out — `CreditStore` is a
`Protocol` and `RateLimiter` a single class, so both are drop-in replacements.

Tracing is CPU-bound; it runs in a worker thread behind a capacity limiter
sized to the CPU count, which keeps the event loop responsive. For heavy
traffic, move conversion onto a task queue and add an async job endpoint
(`202 Accepted` + polling) rather than raising the request timeout.

### Not implemented

Honest gaps against vectorizer.ai's feature set: gradient meshes, centreline
(stroke) tracing of line art, DXF output, and automatic parameter selection
from image content. `policy.retention_days` is accepted but inert — nothing is
ever written to disk.
