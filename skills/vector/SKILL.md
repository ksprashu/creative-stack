---
name: vector
description: Architecture and flow diagrams from text, raster-to-SVG tracing, icon sprites, and SVG optimise/export/recolour — all brand-themed and resolution-independent. Trigger via /vector.
disable-model-invocation: true
---

# Vector: Diagrams, Traces and SVG

Anything that should stay sharp at any size: the architecture diagram in a blog post, a logo
someone only sent you as a PNG, an icon set, the SVG that has to survive being blown up onto a
conference screen.

---

## 🎯 Goal

Diagrams are written as text and rendered, so they live in git, diff properly, and restyle when
the brand does. Logos get traced once and reused everywhere. Every output carries the brand
palette without anyone hand-picking a hex.

---

## 📋 Step-by-Step Workflow

1. **Write the diagram as text.** The renderer is picked from the extension, so choose the
   language that fits the shape of what you are drawing:

   | Write | Renderer | Best at |
   | --- | --- | --- |
   | `.d2` | d2 | architecture — the best automatic layout of the four |
   | `.mmd` | mermaid | sequence, state, gantt, ER; what everyone already writes |
   | `.dot` | graphviz | dependency graphs, anything genuinely graph-shaped |
   | `.puml` | plantuml | UML proper, when the audience expects UML |

2. **Render it.** Output format comes from the extension — SVG for the web, PNG for slides:

   ```bash
   python3 scripts/diagram.py -i arch.d2 -o out/arch.svg --direction right
   python3 scripts/diagram.py -i flow.mmd -o out/flow.png --theme dark --scale 2
   ```

   `--direction right` is worth reaching for on architecture diagrams: the default top-down flow
   produces a very tall image that reads badly in a blog post.

3. **Trace a raster** when the only copy of a mark is a PNG:

   ```bash
   python3 scripts/trace.py -i logo.png -o out/logo.svg --fill brand.blue
   python3 scripts/trace.py -i illustration.png -o out/art.svg --mode color --colors 6
   ```

   `bw` thresholds to one flat shape — right for logos, marks, line art. `color` posterises to N
   colours and traces each as a layer. If two colours merge into one, raise `--colors`; if the
   edges are ragged, raise `--despeckle`.

4. **Build an icon sprite** instead of shipping twenty files:

   ```bash
   python3 scripts/sprite.py -i icons/ -o out/icons.svg --currentcolor \
     --contact-sheet out/icons.png
   ```

   Emits `<symbol id="icon-name">` per file with internal ids namespaced, plus a labelled PNG of
   the whole set so you can see what you have.

5. **Finish the SVG** before it ships:

   ```bash
   python3 scripts/svg.py -i out/ -o out/min/ --op optimise
   python3 scripts/svg.py -i logo.svg -o out/logo.png --op export --width 1200
   python3 scripts/svg.py -i third-party.svg -o out/on-brand.svg --op recolor --to-brand
   python3 scripts/svg.py -i poster.svg -o out/poster.svg --op text-to-path
   ```

   | Op | What it does |
   | --- | --- |
   | `optimise` | svgo, with the `viewBox` protected |
   | `export` | PNG via resvg; PDF/EPS via Inkscape |
   | `recolor` | `--map '#FF0000=brand.red'`, or `--to-brand` to snap everything to the nearest brand colour |
   | `text-to-path` | outlines the text so the file stops depending on an installed font |

---

## 💡 Concrete Example

An architecture diagram and an icon set for a blog post, from nothing to publishable:

```bash
# 1. the diagram, wide rather than tall, at 2x for retina
python3 scripts/diagram.py -i docs/arch.d2 -o out/arch.png --direction right --scale 2

# 2. and a dark-mode twin for the same post
python3 scripts/diagram.py -i docs/arch.d2 -o out/arch-dark.png --direction right \
  --theme dark --scale 2

# 3. the partner logo arrived as a 400px PNG — trace it once
python3 scripts/trace.py -i assets/partner.png -o out/partner.svg --mode color --colors 5

# 4. the icon set, themeable, with a sheet to eyeball
python3 scripts/sprite.py -i assets/icons/ -o out/icons.svg --currentcolor \
  --contact-sheet out/icons-sheet.png

# 5. an off-brand icon pack dragged onto the palette
python3 scripts/svg.py -i vendor/icons/ -o out/vendor/ --op recolor --to-brand --tolerance 40
```

The SVGs go straight into the post; `/raster` builds the responsive PNG ladder from the rendered
diagrams if the CMS will not take SVG.

---

## 🚫 Hard Constraints

*   **NEVER** run svgo over a finished sprite. `cleanupIds` deletes ids nothing in the same
    document references — in a sprite that is every single `<symbol id>`, so the file survives
    the pass and every `<use>` breaks. `sprite.py` optimises each icon *before* assembly for
    exactly this reason.
*   **NEVER** let svgo strip the `viewBox`. Without it an SVG cannot scale and renders at its
    intrinsic size or at nothing. `vectorlib.svgo_optimise()` puts it back; use that rather than
    calling `svgo` directly, because svgo 3 and svgo 4 disagree about both the default and the
    flag that changes it.
*   **NEVER** use `--currentcolor` on a two-tone icon. It collapses every paint onto one colour,
    so a white glyph knocked out of a dark circle becomes an invisible glyph on a same-coloured
    circle. Check the contact sheet.
*   **NEVER** run `trace.py --mode color` on a photograph. Posterising needs flat regions; a
    photo has none and you get megabytes of path soup that renders slower than the JPEG.
*   **NEVER** hand-edit the hex values in a rendered diagram. They come from `brand/tokens.json`
    via `--brand`; editing the output means the next render silently loses the change.
*   **NEVER** call `inkscape` for a PNG export. resvg is a static binary, roughly 20x faster on a
    batch, and does not need a display. Inkscape is for PDF, EPS and text-to-path only — the
    three things nothing else here can do.
*   **NEVER** write an optimise or recolor pass back over its own input. There is no undo, and a
    traced or hand-drawn source is usually not reproducible. `svg.py` refuses in-place output.
