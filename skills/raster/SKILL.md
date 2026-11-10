---
name: raster
description: Batch image editing, responsive size and format ladders, background removal, select-by-description masking for images and video, and brand compositing — reproducible pipelines instead of one-off GUI passes. Trigger via /raster.
disable-model-invocation: true
---

# Raster: Batch Image Work

The image jobs that are tedious once and unbearable a hundred times: resizing, cropping to
aspect, rounding corners, stripping metadata, building a `srcset`, cutting a subject out of
its background, and blurring or lifting out *one particular thing* you can describe but not
draw a box around.

---

## 🎯 Goal

Every edit is a named operation in an ordered list, so the same pipeline runs on one image or a
thousand and the command is still readable in six months. Nothing here opens a GUI, nothing
uploads, and nothing upscales behind your back.

For layer work that genuinely needs a raster editor — masks, channels, a real composite —
`gimp-console` is on PATH and takes Script-Fu on stdin. Reach for it when the pipeline stops
being a pipeline.

---

## 📋 Step-by-Step Workflow

1. **Look before you edit.** Aspect crops and rounded corners are computed from the source
   dimensions, so know what they are:

   ```bash
   python3 -c "import sys; sys.path.insert(0,'scripts'); import rasterlib as R; print(R.probe('hero.png'))"
   ```

2. **Dry-run the pipeline.** It prints the ImageMagick command and stops — cheap insurance
   before it runs across a directory:

   ```bash
   python3 scripts/edit.py -i shots/ -o out/ --ops "trim,resize:1600,round:16,shadow" --dry-run
   ```

3. **Run it.** Operations apply left to right, in one process per image:

   ```bash
   python3 scripts/edit.py -i shots/ -o out/ --ops "trim,resize:1600,round:16,shadow"
   ```

   | Op | What it does |
   | --- | --- |
   | `trim` | remove a uniform border — the dead margin on a window capture |
   | `crop:16x9` / `crop:x,y,w,h` | centre-crop to an aspect, or an explicit box |
   | `resize:1600` / `resize:1600x900` | fit inside; **never** upscales |
   | `pad:40` / `pad:40:#FFF` | transparent or coloured margin |
   | `round:16` | rounded corners (needs alpha) |
   | `shadow` | drop shadow from brand shadow tokens |
   | `border:2:#DADCE0` | hairline border |
   | `grayscale`, `tint:#E8EAED` | desaturate, or tint toward a colour |
   | `flatten:#FFFFFF` | composite alpha onto a solid |
   | `rotate:90`, `autoorient` | rotate; apply the EXIF orientation |
   | `strip` | remove all metadata |

4. **Build the responsive ladder** for anything going on a page:

   ```bash
   python3 scripts/ladder.py -i out/hero.png -o out/img/ \
     --widths 480,960,1440 --formats webp,png --base-url /img/
   ```

   Writes `<stem>-<width>.<ext>`, runs `oxipng` over the PNGs, and emits both a `manifest.json`
   and the `<img srcset>` line to paste in.

5. **Cut a subject out** when it needs to sit on a brand backdrop rather than its own room:

   ```bash
   python3 scripts/cutout.py -i product.jpg -o out/product.png
   python3 scripts/cutout.py -i product.jpg -o out/hero.png \
     --backdrop hero --pad 80 --shadow
   ```

   The default model is BiRefNet (`birefnet-general`, MIT), which keeps hair, fur and thin
   cables that u2net haloes. The first run downloads ~930MB; `--model birefnet-general-lite`
   (~220MB) or `u2netp` when size or speed matters more than the edge.

6. **Select by description** when the subject is not "whatever is in front", but *the mug on
   the left*, *every face*, *the token in the terminal*:

   ```bash
   python3 scripts/segment.py -i desk.png -q "the red mug" --cutout out/mug.png --refine sam2
   python3 scripts/segment.py -i shot.png -q "the API key" --op fill -o out/safe.png --grow 8
   python3 scripts/segment.py -i demo.mp4 -q "the email address" --at 2 --op blur -o out/demo.mp4
   python3 scripts/segment.py -i photo.jpg -q "the bin by the door" --op inpaint -o out/clean.jpg
   ```

   Gemini finds the matches and writes `<output>.segments.json` (box and polygon per match,
   0-1000) *before* touching pixels. A wrong match is an edit to that file, or `--only 0,2`,
   then a `--from-json` re-run — no second model call. Ops are `blur`, `pixelate`, `fill`,
   `erase` (to transparency); `--mask` writes the matte itself. Blur, pixelate and fill cover
   each match's whole box by default (`--shape mask` to follow the outline), and pixelate never
   uses blocks smaller than half the region — a word's outline is often enough to read it.

   `--op inpaint` **removes** the selection rather than hiding it: the region is filled from its
   surroundings, following the outline grown by at least 4px. `--inpaint local` (default) is
   OpenCV's Telea fill: offline and deterministic, right for text, logos, stray cursors and
   small objects on flat or gently varying backgrounds, and it also runs per frame on video.
   `--inpaint gemini` paints the region magenta (the model never sees what was there), asks
   the `/artgen` image model to fill the hole, tone-matches the answer to a band around it and
   pastes back only the region, so nothing outside the mask moves. Use it for large objects on
   texture, where a local fill smears; it is stills only.

   Gemini's polygons are fine for blur and fill. For a cutout edge, `--refine sam2` hands each
   box to SAM 2 locally. **Video is always SAM 2**: objects are found on the frame at `--at`
   seconds and tracked forwards and backwards through the clip; audio is copied untouched.
   SAM 2 needs the optional ML deps once:
   `uv pip install --python .venv/bin/python -r requirements-ml.txt`.

7. **Drop to GIMP** only for the genuinely layered case:

   ```bash
   gimp-console -idf --batch-interpreter python-fu-eval \
     -b 'pdb.gimp_image_flatten(img)' -b 'pdb.gimp_quit(1)'
   ```

---

## 💡 Concrete Example

Thirty console screenshots for a tutorial, from raw captures to what the CMS wants:

```bash
# 1. see what one pipeline does before it touches thirty files
python3 scripts/edit.py -i raw/ -o out/clean/ \
  --ops "autoorient,trim,crop:16x9,resize:1600,round:12,shadow,strip" --dry-run

# 2. run it
python3 scripts/edit.py -i raw/ -o out/clean/ \
  --ops "autoorient,trim,crop:16x9,resize:1600,round:12,shadow,strip" --recursive

# 3. three widths, two formats, optimised, with a manifest
python3 scripts/ladder.py -i out/clean/ -o out/img/ \
  --widths 480,960,1440 --formats webp,png --base-url /blog/cloudrun/

# 4. the product shot on a brand gradient for the hero
python3 scripts/cutout.py -i raw/device.jpg -o out/img/hero.png \
  --backdrop hero --pad 96 --shadow --alpha-matting

# 5. one capture shows a colleague's email in a sidebar — find it, check the JSON, blur it
python3 scripts/segment.py -i out/clean/step-07.png -q "the email address in the sidebar" \
  --op blur --grow 10 -o out/clean/step-07-safe.png
```

`out/img/manifest.json` then carries every output, its byte size and the `srcset` string per
source image.

Screenshots of a UI should go through `/shot` **first** — that is what adds device chrome and
redacts tokens. `/raster` is the batch pass afterwards.

---

## 🚫 Hard Constraints

*   **NEVER** call `magick` directly in a script. Ubuntu 24.04 still ships ImageMagick 6, where
    the binary is `convert` and `magick` does not exist. Use `rasterlib.magick()`, which resolves
    both.
*   **NEVER** put `resize` after `round`. The corner mask is drawn at the size the image is when
    the op runs; resizing afterwards scales the mask and gives you soft, wrong-radius corners.
*   **NEVER** write an alpha pipeline to `.jpg`. JPEG has no alpha, so rounded corners come out
    black. `edit.py` retargets to `.png` and tells you — do not "fix" that by flattening unless
    you actually want a background.
*   **NEVER** upscale to hit a target width. `resize` is shrink-only on purpose; a blurry 2x
    asset looks worse than a sharp 1x one.
*   **NEVER** publish an image without `strip`. Camera and screenshot metadata carries GPS,
    device names, and on macOS sometimes the window title of whatever was behind it.
*   **NEVER** run `cutout.py` against a remote service. rembg runs locally and the image never
    leaves the machine — that is the entire reason it is the tool here rather than an API.
*   **NEVER** pixelate or blur a secret that has to stay secret. Both keep some of the
    original signal and both have been reversed in public; `--op fill` keeps none.
*   **NEVER** use `--op inpaint` to redact. It is for removing clutter, not secrets: a fill
    can carry the original's colour and shape into the hole, and nothing checks that it
    didn't. Hide secrets with `fill` (or blur, with the caveat above) and re-check with
    `/shot`'s `scan.py --check`.
*   **NEVER** trust `segment.py` as the only redaction pass on something going public. A
    description can miss a second occurrence; `/shot` redacts by pattern, and the
    `.segments.json` is there to be read before the image ships.
*   **NEVER** add Ultralytics/YOLO for detection. It is AGPL-3.0, which would reach every
    script that imports it; Gemini finds the objects and SAM 2 (Apache-2.0) draws the edges.
*   **NEVER** offer `bria-rmbg` in `cutout.py` even though rembg ships it. Its weights are
    CC BY-NC, and a default should never quietly make an output non-commercial.
*   **NEVER** batch-edit in place. Always write to a separate `-o` directory; there is no undo
    and the source capture is usually unrepeatable.
