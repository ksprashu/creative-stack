---
name: shot
description: Produce publication-grade screenshots — device frames, annotations, redaction, code and terminal images, deterministic web capture. Trigger via /shot.
disable-model-invocation: true
---

# Shot: Publication-Grade Screenshot Studio

Turn a raw capture into a branded, blog-ready image — framed, annotated, and safe to publish.

---

## 🎯 Goal

Every screenshot in a post looks like it came from the same studio: consistent backdrop, corner
radius, shadow and callout style, with no leaked credentials. All steps are scripted, so a
screenshot can be regenerated when the UI changes instead of re-taken by hand.

---

## 📋 Step-by-Step Workflow

1. **Capture** — pick the source:
   ```bash
   # a live web page, deterministically (animations frozen, cookie banners hidden)
   python3 scripts/capture.py --url https://cloud.google.com/run --output out/raw.png

   # a local app, after it finishes loading
   python3 scripts/capture.py --url http://localhost:3000 --wait-for ".chart-loaded" \
     --selector "main .hero" --output out/raw.png
   ```
   For a desktop app window, capture with `screencapture -w out/raw.png` first.

2. **Redact and annotate** — with **anot8**. Its spec takes percentages and live anchors (`"@submit.right"`), and every render
   hands back each annotation's pixel `bounds` and a preview, so placement is checked by
   looking, not guessed once. The `anot8` skill has the spec reference; the `anot8_*` MCP
   tools are the same engine with the PNG returned straight into context.
   ```bash
   # brand colours as anot8 palette names (red/green/amber/cyan/purple are anot8 presets, so
   # the names must differ)
   jq '.profiles[.default].color | {brand: .light.accent, warn: .brand.red,
       ok: .brand.green, note: .brand.yellow}' brand/tokens.json > out/palette.json
   anot8 init out/raw.png -o out/raw.anot8.json --palette out/palette.json   # natural size filled in
   python3 scripts/scan.py -i out/raw.png --spec out/raw.anot8.json          # blurs for secrets, first
   anot8 render out/raw.anot8.json -o out/annotated.png --md out/notes.md
   # look at out/annotated.png and the printed bounds, then nudge and re-render:
   anot8 apply out/raw.anot8.json --in-place --ops '[{"op":"update","id":"deploy","set":{"y":312}}]'
   ```
   Then `"color": "brand"` (or `defaults.color`) keeps every callout on brand. `blur` and
   `highlight` change the pixels and draw no border, so a redaction reads as part of the
   image, not as a callout.

   `scan.py` OCRs the screenshot and adds a `blur` (id `scan-N`, note `scan: <kind>`) for every
   known key format, labelled value (`password:`, `GITHUB_TOKEN=`), email, GCP project id, card
   number and long random string, leaving the label itself readable. Its report shows only the
   first four characters of each hit. `--ai` adds a Gemini pass for what OCR can't read: faces,
   avatars, names, account pickers. Review the `scan-*` blurs in the render: delete a false
   positive from the spec, and blur anything it missed by hand.

   Still a secret it can't find? `/raster`'s `segment.py -i out/raw.png --query "the API key" --json hits.json`
   finds it by description; its `box_2d` is `[ymin, xmin, ymax, xmax]` in 0–1000, so a `blur`
   at `x: xmin/10 %`, `y: ymin/10 %`, `w: (xmax - xmin)/10 %`, `h: (ymax - ymin)/10 %` covers it.

   A solid redaction is a `box` with `"fillOpacity": 1`. To pixelate rather than blur, use
   `/raster`'s `segment.py --query "..." --op pixelate -o out.png`.

3. **Frame** — composite onto the brand backdrop:
   ```bash
   python3 scripts/frame.py -i out/annotated.png -o out/final.png \
     --chrome browser --url "https://console.cloud.google.com" --backdrop subtle
   ```

4. **Check, then optimize before publishing** — `--check` writes nothing and exits 1 if anything
   is still readable:
   ```bash
   python3 scripts/scan.py -i out/final.png --check && oxipng -o 4 --strip safe out/final.png
   ```

---

## 💡 Concrete Example

### A three-step annotated console walkthrough

`out/raw.anot8.json`, after `anot8 init` (image and palette filled in):
```json
{
  "version": 1,
  "image": { "source": "raw.png", "width": 2880, "height": 1800 },
  "palette": { "brand": "#1A73E8", "warn": "#EA4335", "ok": "#34A853", "note": "#FBBC04" },
  "defaults": { "color": "warn" },
  "annotations": [
    { "type": "blur", "id": "account", "x": "62%", "y": "4%", "width": "34%", "height": "5%",
      "badge": false },
    { "type": "box", "id": "source", "x": "5%", "y": "18%", "width": "38%", "height": "10%",
      "note": "Pick the source repository" },
    { "type": "box", "id": "deploy", "x": "5%", "y": "34%", "width": "38%", "height": "10%",
      "note": "Deploy runs here" },
    { "type": "arrow", "from": "@deploy.right", "to": ["70%", "52%"], "badge": false }
  ]
}
```
Blurs go first, so later marks draw on top. Badges number 1..N and `--md` writes the matching
notes list.

```bash
python3 scripts/capture.py --url https://console.cloud.google.com --output out/raw.png
anot8 init out/raw.png -o out/raw.anot8.json --palette out/palette.json
python3 scripts/scan.py -i out/raw.png --spec out/raw.anot8.json
# add the callouts above, then:
anot8 render out/raw.anot8.json -o out/annotated.png --md out/notes.md
python3 scripts/frame.py  -i out/annotated.png -o out/final.png --chrome browser \
  --url "console.cloud.google.com" --caption "Deploying a service from the console"
python3 scripts/scan.py -i out/final.png --check
oxipng -o 4 --strip safe out/final.png
```

### Code and terminal images (no real recording needed)

```bash
# syntax-highlighted code image
silicon main.py -o out/code.png --theme "Dracula" --background "#00000000"

# scripted terminal session -> GIF, re-renderable when the CLI output changes
cat > demo.tape <<'TAPE'
Output out/demo.gif
Set FontFamily "JetBrains Mono"
Set FontSize 22
Set Width 1200
Set Height 600
Type "gcloud run deploy my-service --source ."
Enter
Sleep 3s
TAPE
vhs demo.tape
```

---

## 🚫 Hard Constraints

*   **NEVER** publish a screenshot without checking for secrets first — project IDs, emails,
    tokens, account pickers. Run `scan.py` (add `--ai` when faces or account pickers are
    in shot), look at the render, then `scan.py --check` the final image. A clean scan is
    not proof: OCR misses text it can't read, so look too. Blur, do not crop and hope.
*   **NEVER** use a solid box over text you intend to stay unreadable *and* look natural — use
    `blur`; use `"fillOpacity": 1` only when total removal is the point.
*   **NEVER** ship an anot8 render without opening the PNG it wrote. The returned `bounds`
    say where a box *is*, not whether it covers the thing; a blur 10px short of a token's
    last characters leaves the suffix readable.
*   **NEVER** hardcode absolute machine paths in a spec or in committed markdown image embeds.
*   **NEVER** re-take a documentation screenshot by hand when a `capture.py` invocation exists —
    edit the invocation so the next UI change is a one-command regeneration.
*   **NEVER** mix backdrops within a single article; pass the same `--backdrop` to every frame.
