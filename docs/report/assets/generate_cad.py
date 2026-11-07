# Copyright 2026 Creative Stack Contributors
# SPDX-License-Identifier: Apache-2.0

import math
from pathlib import Path
import FreeCAD
import Part
import Mesh

HERE = Path(__file__).resolve().parent
doc = FreeCAD.newDocument("PlanetaryMount")

# 1. Outer scalloped flange base (R=42mm, H=8mm)
base = Part.makeCylinder(42.0, 8.0)

# Cut 6 bolt holes + outer scallops around perimeter (R=31mm)
for i in range(6):
    angle = math.radians(i * 60.0)
    bx = 31.0 * math.cos(angle)
    by = 31.0 * math.sin(angle)
    bolt_hole = Part.makeCylinder(4.2, 20.0, FreeCAD.Vector(bx, by, -5.0))
    counterbore = Part.makeCylinder(7.5, 4.0, FreeCAD.Vector(bx, by, 5.0))
    base = base.cut(bolt_hole).cut(counterbore)

    # Scallop between bolt bosses
    s_angle = math.radians(i * 60.0 + 30.0)
    sx = 46.0 * math.cos(s_angle)
    sy = 46.0 * math.sin(s_angle)
    scallop = Part.makeCylinder(12.0, 20.0, FreeCAD.Vector(sx, sy, -5.0))
    base = base.cut(scallop)

# 2. Central bearing turret (R=22mm, H=28mm) + stepped collar (R=26mm, H=14mm)
collar = Part.makeCylinder(26.0, 14.0)
turret = Part.makeCylinder(22.0, 28.0)
body = base.fuse(collar).fuse(turret)

# 3. Stiffening ribs (6 radial gussets)
for i in range(6):
    angle = math.radians(i * 60.0)
    rib = Part.makeBox(16.0, 4.0, 18.0, FreeCAD.Vector(18.0, -2.0, 6.0))
    rib.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), i * 60.0)
    body = body.fuse(rib)

# 4. Precision central bore (R=13mm) + keyway slot
bore = Part.makeCylinder(13.0, 40.0, FreeCAD.Vector(0, 0, -5.0))
keyway = Part.makeBox(6.0, 5.0, 40.0, FreeCAD.Vector(-3.0, 10.5, -5.0))
body = body.cut(bore).cut(keyway)

# 5. Top ring groove
groove_outer = Part.makeCylinder(19.5, 3.0, FreeCAD.Vector(0, 0, 26.0))
groove_inner = Part.makeCylinder(16.5, 3.0, FreeCAD.Vector(0, 0, 26.0))
body = body.cut(groove_outer.cut(groove_inner))

part_obj = doc.addObject("Part::Feature", "PlanetaryMount")
part_obj.Shape = body
doc.recompute()

# Export STEP (ISO 10303-21) and high-resolution STL mesh
Part.export([part_obj], str(HERE / "planetary-mount.step"))
Mesh.export([part_obj], str(HERE / "planetary-mount.stl"))
print(f"✓ FreeCAD exported STEP ({len(body.Faces)} faces, {len(body.Edges)} edges) and STL")
