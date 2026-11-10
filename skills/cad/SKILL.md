---
name: cad
description: Parametric 3D CAD part design in millimetres — build123d B-rep scripts, watertight manifold and overhang gates, slicer-ready .stl/.3mf/.step exports, and FreeCAD/STEP conversion. Trigger via /cad.
disable-model-invocation: true
---

# CAD: Parametric Parts → Slicer-Ready STL / 3MF / STEP

Design dimensionally exact mechanical parts from code and hand your slicer a verified, watertight mesh — plus a `.step` solid for FreeCAD and an isometric studio preview.

---

## 🎯 Goal

A plain-language part requirement becomes a reviewable `params.json` + `part.py` in millimetres, compiled via OpenCASCADE (`build123d`) onto the `Z = 0` print bed as `.stl`, `.3mf`, and `.step`. Every build passes an automatic manifold, single-solid, bounding-box, bed-adhesion, and overhang check, followed by a `/render3d` visual check before you open OrcaSlicer, PrusaSlicer, or Bambu Studio.

---

## 📋 Step-by-Step Workflow

1. **Write `params.json` first** so clearances, wall thicknesses, and hole spacings are explicit and editable without rewriting geometry:

   | Feature (FDM, 0.4 mm nozzle) | Recommended Default (`mm`) |
   | --- | --- |
   | Minimum structural wall | `1.6` – `2.4` (`4–6` perimeters) |
   | Floor / lid thickness | `1.2` – `2.0` (`6–10` layers at `0.2 mm`) |
   | Slip-fit / snap-lid XY clearance | `0.20` – `0.30` per side |
   | M2 / M3 / M4 / M5 through-hole | `2.3` / `3.3` / `4.4` / `5.4` Ø |
   | M3 heat-set brass insert bore | `4.0` Ø × `5.0` deep |

2. **Author `part.py` with `def build(params: dict):`** using `build123d` in **millimetres** with **`+Z` as the print-head up direction**:

   ```python
   from build123d import (
       Axis, BuildLine, BuildPart, BuildSketch, CounterBoreHole,
       Locations, Plane, Polyline, chamfer, extrude, fillet, make_face,
   )

   DEFAULT_PARAMS = {
       "width_mm": 60.0,
       "depth_mm": 32.0,
       "base_mm": 4.0,
       "wall_mm": 4.0,
       "height_mm": 28.0,
       "m3_hole_mm": 3.3,
       "m3_cbore_mm": 6.2,
   }

   def build(p: dict):
       w, d, base, wall, h = (
           p["width_mm"], p["depth_mm"], p["base_mm"], p["wall_mm"], p["height_mm"]
       )
       with BuildPart() as bp:
           with BuildSketch(Plane.YZ):
               with BuildLine():
                   Polyline(
                       (0, 0), (d, 0), (d, base), (wall, base), (wall, h), (0, h),
                       close=True,
                   )
               make_face()
           extrude(amount=w / 2, both=True)
           front_corners = bp.edges().filter_by(Axis.Z).group_by(Axis.Y)[-1]
           fillet(front_corners, radius=5.0)
           with Locations(Plane.XY.offset(base)):
               with Locations((-w / 3, d * 0.62), (w / 3, d * 0.62)):
                   CounterBoreHole(
                       radius=p["m3_hole_mm"] / 2,
                       counter_bore_radius=p["m3_cbore_mm"] / 2,
                       counter_bore_depth=1.8,
                       depth=base,
                   )
           chamfer(bp.faces().sort_by(Axis.Z)[0].outer_wire().edges(), length=0.5)
       return bp.part
   ```

3. **Compile, drop onto `Z = 0`, verify printability, and render a preview** in one command:

   ```bash
   $CREATIVE_STACK_ROOT/.venv/bin/python $CREATIVE_STACK_ROOT/skills/cad/scripts/build.py \
     -s out/bracket/part.py \
     -p out/bracket/params.json \
     -o out/bracket/bracket \
     --bed 256x256x256 \
     --preview out/bracket/preview.png
   ```

   Inspect `out/bracket/preview.png` to confirm hole directions, fillets, and bed orientation match the requirement.

4. **Check or convert existing CAD files** (`.stl`, `.3mf`, `.step`, `.iges`, `.fcstd`) standalone:

   ```bash
   $CREATIVE_STACK_ROOT/.venv/bin/python $CREATIVE_STACK_ROOT/skills/cad/scripts/check.py \
     -i out/bracket/bracket.stl --bed 220x220x250 --json out/bracket/report.json

   $CREATIVE_STACK_ROOT/.venv/bin/python $CREATIVE_STACK_ROOT/skills/cad/scripts/convert.py \
     -i vendor_enclosure.step -o out/enclosure.stl
   ```

---

## 💡 Concrete Example

Designing a parametric desk cable-clip mount (`48 × 24 × 16 mm`) with two countersunk M3 screw holes, exporting slicer files, and checking the render:

```bash
mkdir -p out/cable-clip

cat > out/cable-clip/params.json <<'EOF'
{
  "width_mm": 48.0,
  "depth_mm": 24.0,
  "base_mm": 3.6,
  "channel_mm": 6.5,
  "m3_clearance_mm": 3.3
}
EOF

# Write out/cable-clip/part.py, then build + verify + render preview:
$CREATIVE_STACK_ROOT/.venv/bin/python $CREATIVE_STACK_ROOT/skills/cad/scripts/build.py \
  -s out/cable-clip/part.py \
  -p out/cable-clip/params.json \
  -o out/cable-clip/cable-clip \
  --preview out/cable-clip/preview.png

# Override one dimension without touching code:
$CREATIVE_STACK_ROOT/.venv/bin/python $CREATIVE_STACK_ROOT/skills/cad/scripts/build.py \
  -s out/cable-clip/part.py \
  -p out/cable-clip/params.json \
  --set channel_mm=8.0 \
  -o out/cable-clip/cable-clip-8mm
```

`out/cable-clip/` now holds `cable-clip.stl`, `cable-clip.3mf` (drop straight into OrcaSlicer / PrusaSlicer), `cable-clip.step` (open in FreeCAD for manual edits), `cable-clip.report.json`, and `preview.png`.

---

## 🚫 Hard Constraints

*   **NEVER** author CAD dimensions in metres or inches. Slicers interpret raw STL coordinates strictly as **millimetres** — a `0.05` metre part imports as a `0.05 mm` speck on the build plate.
*   **NEVER** put a `fillet()` on the bottom print-bed face (`Z = 0`). The bottom tangent of a fillet starts near `90°` overhang on layers 1–3 and curls upward off the bed; use a `45°` `chamfer(..., length=0.4..0.8)` on bed edges instead, which prints cleanly and prevents elephant's foot.
*   **NEVER** butt a boss, rib, or tab flush against a face with `0.0 mm` overlap when floating-point coordinates are involved — overlap boolean additions by `0.01 mm` into the parent solid or union inside a single `BuildPart` so OpenCASCADE fuses `1` connected solid rather than `2` detached shells that fall apart after printing.
*   **NEVER** size screw holes or mating slots to exact nominal dimensions (`3.0 mm` for an M3 screw). FDM inner perimeters shrink by `0.15–0.30 mm`; add explicit clearance in `params.json` (`3.3 mm` for M3 through-holes, `0.25 mm` per side for sliding fits).
*   **NEVER** hand an `.stl` or `.3mf` to the user without running `check.py` (automatic in `build.py`) **and** visually inspecting `--preview` when Blender is available. Passing `is_valid` in B-rep does not prove a through-hole went through the intended wall or that the largest flat face sits on `Z = 0`.
*   **NEVER** apply `fillet()` or `chamfer()` before all primary boolean cuts and holes are complete. OpenCASCADE edge indices and face adjacencies shift after booleans, and filleting an edge thicker than its remaining adjacent wall throws `StdFail_NotDone`.
