# Copyright 2026 Creative Stack Contributors
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import FreeCAD
import Part
import TechDraw

HERE = Path(__file__).resolve().parent
shape = Part.Shape()
shape.read(str(HERE / "planetary-mount.step"))

# Project isometric and top views via TechDraw.projectToSVG
iso_svg = TechDraw.projectToSVG(shape, FreeCAD.Vector(1.0, -1.0, 0.85))
# Wrap inside a clean technical blueprint SVG viewport
svg_doc = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="-55 -55 110 110" width="960" height="640" style="background:#0D1117">
  <defs>
    <pattern id="grid" width="10" height="10" patternUnits="userSpaceOnUse">
      <path d="M 10 0 L 0 0 0 10" fill="none" stroke="#1E293B" stroke-width="0.25"/>
    </pattern>
  </defs>
  <rect x="-90" y="-60" width="180" height="120" fill="#0D1117"/><rect x="-90" y="-60" width="180" height="120" fill="url(#grid)"/>
  <circle cx="0" cy="0" r="42" fill="none" stroke="#334155" stroke-width="0.25" stroke-dasharray="1.5,1"/>
  <circle cx="0" cy="0" r="31" fill="none" stroke="#38BDF8" stroke-width="0.25" stroke-dasharray="2,1"/>
  <g stroke="#38BDF8" stroke-width="0.45" fill="none" stroke-linecap="round" stroke-linejoin="round">
    {iso_svg}
  </g>
</svg>"""
with open(HERE / "cad-blueprint.svg", "w") as f:
    f.write(svg_doc)
print("✓ FreeCAD TechDraw exported B-Rep engineering blueprint SVG")
