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
| `processing.max_colors` | 0–256 | `0` (auto) | Quantize the input palette. The single most useful quality knob. |
| `processing.palette` | hex list | – | Pin output colours exactly, e.g. `#ff0000,#00ff00`. Output fills are snapped to this list, so you get exactly this many layers. The strongest fix for ghost layers — see [Ghost layers](#ghost-layers-in-an-editor). |
| `processing.color_merge` | 0–160 | `16` | Snap minor fills onto the nearest prominent one within this RGB distance. Removes the pale lumps left where anti-aliasing blends two flat regions. See [Transition lumps](#transition-lumps-between-two-colours). |
| `processing.detail` | `low`, `standard`, `high`, `maximum` | `standard` | Smallest feature kept, and path coordinate precision. See [Detail](#detail-thin-lines-and-small-features). |
| `processing.denoise` | `none`, `low`, `medium`, `high` | `low` | Median despeckle before tracing. The main defence against chunky, notched edges. See [Edge smoothness](#edge-smoothness). |
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

**Pinning the palette does fix it.** Every pixel snaps to a colour you chose,
so the ghost shades never exist:

```
processing.palette=#666666,#fbf7da,#2d2c2a,#f09ec2,#c7cda0,#ffffff
```

On that file: 18 fills and 135 objects became **6 fills and 75 objects**, with
the artwork visually unchanged — better preserved than the alternatives,
which shifted the whites toward cream.

Note this needs the output fills to be snapped as well as the input pixels.
The tracer averages the pixels inside each cluster, so boundary clusters come
back as blends: pinning six colours produced eighteen until the result was
snapped back to the palette too.

To find a palette for your own artwork:

```powershell
.venv\Scripts\python.exe tools\tune.py samples\yourfile.jpg --palette 6
```

Treat its output as a starting point — it reports the most-used distinct
colours, which can include an anti-aliasing shade rather than a real ink
colour. Edit the list before using it.

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

64 tests cover every output format, all three image-input styles, parameter
validation and rejection, geometry and unit conversion, colour quantization
and palette pinning, draw styles, transparency, watermarking, credits,
authentication, rate limiting, SSRF policy and the error envelope.

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
    v1/meta.py         health, formats, parameters, account
  core/
    errors.py          error taxonomy and JSON envelope
    security.py        credential verification
    credits.py         credit accounting
    ratelimit.py       sliding-window limiter
  schemas/params.py    every parameter, with validation
  services/
    preprocess.py      decode, EXIF, alpha, downscale, quantize
    engine.py          VTracer wrapper
    svgdoc.py          viewBox, sizing, draw styles, grouping, watermark
    render.py          SVG -> PDF / EPS / PNG
    pipeline.py        orchestration, threading, timeouts
    fetch.py           SSRF-guarded URL fetching
tests/                 64 tests
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
