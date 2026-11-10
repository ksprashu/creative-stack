#!/usr/bin/env python3
# Copyright 2026 Creative Stack Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Convert between CAD formats (.step, .iges, .fcstd, .stl, .3mf).

Uses build123d (OpenCASCADE) for STEP/STL/3MF, and falls back to headless
`freecadcmd` when reading or writing FreeCAD `.fcstd` or `.iges` files.

    .venv/bin/python convert.py -i vendor_part.step -o out/vendor_part.stl
    .venv/bin/python convert.py -i housing.fcstd -o out/housing.step
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cadlib as C  # noqa: E402

FREECAD_SNIPPET = """\
import os, sys
import FreeCAD, Part, Mesh

src = os.environ["CS_CAD_SRC"]
dst = os.environ["CS_CAD_DST"]
tol = float(os.environ.get("CS_CAD_TOL", "0.02"))

ext_in = os.path.splitext(src)[1].lower()
ext_out = os.path.splitext(dst)[1].lower()

if ext_in == ".fcstd":
    doc = FreeCAD.openDocument(src)
    shapes = [
        o.Shape for o in doc.Objects
        if hasattr(o, "Shape") and not o.Shape.isNull() and o.Shape.Solids
    ]
    if not shapes:
        raise RuntimeError(f"no solid shapes found in {src}")
    shape = Part.makeCompound(shapes) if len(shapes) > 1 else shapes[0]
else:
    shape = Part.Shape()
    shape.read(src)

if ext_out in (".step", ".stp", ".iges", ".igs"):
    shape.exportStep(dst) if ext_out in (".step", ".stp") else shape.exportIges(dst)
elif ext_out == ".stl":
    mesh = Mesh.Mesh()
    mesh.addFacets(shape.tessellate(tol))
    mesh.write(dst)
else:
    raise RuntimeError(f"freecadcmd unsupported target format: {ext_out}")
"""


def convert_with_freecad(src, dst, tolerance):
    fc = C.hostenv.find_app("freecadcmd")
    if not fc:
        C.die(f"{src.suffix} conversion requires FreeCAD (`freecadcmd`). "
              f"{C.hostenv.pkg_install_hint('freecad')}")
    with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8", delete=False) as tmp:
        tmp.write(FREECAD_SNIPPET)
        script_path = Path(tmp.name)
    env = dict(os.environ, CS_CAD_SRC=str(src.resolve()), CS_CAD_DST=str(dst.resolve()),
               CS_CAD_TOL=str(tolerance))
    env.setdefault("FREECAD_USER_HOME", tempfile.gettempdir())
    try:
        proc = subprocess.run([str(fc), str(script_path)], capture_output=True, text=True, env=env)
        if proc.returncode != 0 or not dst.exists():
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-10:]
            C.die("freecadcmd conversion failed:\n  " + "\n  ".join(tail))
    finally:
        script_path.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True,
                    help="input .step/.stp, .iges/.igs, .fcstd, .stl, or .3mf")
    ap.add_argument("--output", "-o", required=True,
                    help="output .stl, .3mf, or .step")
    ap.add_argument("--tolerance", type=float, default=0.01,
                    help="linear tessellation deflection in mm (default 0.01)")
    args = ap.parse_args()

    src = Path(args.input)
    dst = Path(args.output)
    if not src.exists():
        C.die(f"input file not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    ext_in = src.suffix.lower()
    ext_out = dst.suffix.lower()

    if ext_in in (".fcstd", ".iges", ".igs") or ext_out in (".iges", ".igs"):
        if ext_out == ".3mf":
            with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as tmp:
                mid_step = Path(tmp.name)
            try:
                convert_with_freecad(src, mid_step, args.tolerance)
                src, ext_in = mid_step, ".step"
            except Exception:
                mid_step.unlink(missing_ok=True)
                raise
        else:
            convert_with_freecad(src, dst, args.tolerance)
            print(f"✓ {dst}  (via freecadcmd)")
            return 0

    try:
        from build123d import Compound, Mesher, Unit, export_step, export_stl, import_step, import_stl
    except ImportError:
        C.die("build123d is not installed. Run: uv pip install --python .venv/bin/python -r requirements.txt")

    if ext_in in (".step", ".stp"):
        shape = import_step(str(src))
    elif ext_in == ".stl":
        shape = import_stl(str(src))
    elif ext_in == ".3mf":
        mesher_in = Mesher()
        shapes = mesher_in.read(str(src))
        shape = Compound(children=shapes) if len(shapes) > 1 else shapes[0]
    else:
        C.die(f"unsupported input format: {ext_in}")

    if ext_out == ".stl":
        export_stl(shape, str(dst), tolerance=args.tolerance)
    elif ext_out == ".3mf":
        mesher_out = Mesher(unit=Unit.MM)
        mesher_out.add_shape(shape, linear_deflection=args.tolerance)
        mesher_out.write(str(dst))
    elif ext_out in (".step", ".stp"):
        export_step(shape, str(dst))
    else:
        C.die(f"unsupported output format: {ext_out} (expected .stl, .3mf, or .step)")

    print(f"✓ {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
