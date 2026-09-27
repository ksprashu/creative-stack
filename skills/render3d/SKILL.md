---
name: render3d
description: Headless Blender product shots, turntables, isometric diagram renders and extruded 3D logo stings — auto-framed, brand-lit, on a seamless studio sweep. Trigger via /render3d.
disable-model-invocation: true
---

# Render3D: Product Shots, Turntables and Logo Stings

The hero image for a post about a physical thing, a spinning 3D asset for a slide, an isometric
render of a part, a 3D wordmark to open a video. All from the command line, no Blender window.

---

## 🎯 Goal

A model file goes in and a publishable image comes out, framed and lit without anyone touching a
viewport. The rig is fixed and the model is normalised into it, so a millimetre-scale CAD export
and a metre-scale game asset land identically. Colours come from `brand/tokens.json`.

---

## 📋 Step-by-Step Workflow

1. **Render the still.** Everything is auto-scaled and auto-framed — there is nothing to tune on
   the first run:

   ```bash
   python3 scripts/render.py -i model.glb -o out/hero.png
   python3 scripts/render.py -i part.stl -o out/part.png --material blue
   ```

   Blender imports `.blend .obj .gltf .glb .fbx .stl .ply .usd* .dae .abc .svg` without an add-on.
   Anything else: convert to `.glb` first.

2. **Pick the framing.**

   | `--view` | Use for |
   | --- | --- |
   | `three-quarter` | the default; a product shot, a hero image |
   | `front` | comparisons, anything where a straight-on silhouette matters |
   | `iso` | true isometric orthographic — architecture and diagram renders |

   `--distance` moves the camera, and doubles as the ortho scale under `--view iso`.

3. **Spin it** when a still will not show the shape:

   ```bash
   python3 scripts/render.py -i model.glb -o out/spin.mp4 --turntable --frames 90
   python3 scripts/render.py -i model.glb -o out/spin.gif --turntable --frames 48 --fps 24
   ```

   The subject rotates, not the camera, so the lighting stays put and the loop is seamless —
   frame `frames + 1` is the same pose as frame 1, so the render stops one short.

4. **Extrude a logo** for a title card or a video sting:

   ```bash
   python3 scripts/logo3d.py -i logo.svg -o out/logo3d.png --material blue
   python3 scripts/logo3d.py -i logo.svg -o out/sting.mp4 --spin --frames 60
   ```

   `--depth` is a fraction of the logo's own width, so it means the same thing on a 24px icon and
   a 2400px wordmark.

5. **Choose the background.** Every render is made on transparent film and the backdrop is
   composited in afterwards, so it is exactly the brand hex with no lighting in the loop:

   ```bash
   --mode dark               # the dark-scheme background instead of the light one
   --backdrop hero           # a named gradient from brand/tokens.json
   --transparent             # keep the alpha, composite it yourself later
   ```

6. **Turn the samples up for the final.** `--samples 32` previews in seconds, 96 is the default,
   256 is what you publish. `--engine eevee` is faster still but see the constraints.

---

## 💡 Concrete Example

A product post: a hero still, a turntable for the top of the page, an isometric for the
architecture section, and a title card.

```bash
# 1. preview the framing cheaply before committing to a long render
python3 scripts/render.py -i assets/device.glb -o out/preview.png --samples 24 --width 800

# 2. the hero, at publish quality
python3 scripts/render.py -i assets/device.glb -o out/hero.png --samples 256 --width 2400

# 3. a 3-second loop for the top of the post
python3 scripts/render.py -i assets/device.glb -o out/spin.mp4 \
  --turntable --frames 90 --fps 30 --samples 96

# 4. the exploded part, isometric, to sit beside the diagram
python3 scripts/render.py -i assets/mount.stl -o out/mount.png \
  --view iso --distance 4 --material blue

# 5. the title card, on the brand gradient
python3 scripts/logo3d.py -i brand/logo.svg -o out/title.png \
  --backdrop hero --material accent --width 1920 --height 1080

# 6. a transparent copy of the hero to drop onto a slide
python3 scripts/render.py -i assets/device.glb -o out/hero-alpha.png --transparent
```

`/vidkit` takes `out/spin.mp4` straight into an edit; `/deck` embeds `out/title.png`; `/raster`
builds the responsive ladder from `out/hero.png`.

---

## 🚫 Hard Constraints

*   **NEVER** render the backdrop as lit geometry. The cove is metres across and the lamps are
    aimed at a subject the size of a mug, so the floor by the camera blows past white while the
    wall behind sits at 75% grey — and the exposure that fixes the wall clips the contact shadow
    to nothing. The cove is a shadow catcher on transparent film and the background is composited
    underneath. This was arrived at the hard way; do not "simplify" it back.
*   **NEVER** turn off `film_transparent` while the cove is a shadow catcher. Cycles renders a
    shadow catcher as an ordinary diffuse plane when the film is opaque, which puts a grey band
    across a render that was meant to be white.
*   **NEVER** write the composited backdrop with ImageMagick's default PNG coder. A flat white or
    any neutral canvas is written out as *Grayscale*, and both `-composite` and ffmpeg's `overlay`
    take their pixel format from the first input — the render silently loses all its colour. The
    `PNG24:` prefix is what forces sRGB; `-type TrueColor` does not.
*   **NEVER** use `--engine eevee` for anything you are going to publish. It ignores the shadow
    catcher, so the contact shadow is dropped entirely — the render comes back clean but floating.
    It is a framing preview, nothing more.
*   **NEVER** import anything from this repo inside `_inblender.py`. It runs in Blender's own
    interpreter, which has no idea this repo exists. The two sides talk in JSON through
    `$CREATIVE_STACK_JOB`, and that is the whole contract.
*   **NEVER** feed `logo3d.py` an SVG with live `<text>`. Blender's importer reads curves and
    ignores text elements, so the logo silently comes back missing its wordmark. Outline it first
    with `/vector`: `svg.py --op text-to-path`.
*   **NEVER** commit `templates/blender/studio.blend`. A `.blend` is an opaque binary that cannot
    be reviewed in a diff; it is generated on first use from `action_build_studio` in about a
    second, and regenerating it is how a change to the rig actually takes effect.
*   **NEVER** pass a raw brand hex into a Blender colour socket. Those sockets are linear and the
    tokens are sRGB — `#4285F4` fed in raw renders as roughly `#7FB0FF`. Use `srgb_to_linear()`.
