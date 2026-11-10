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

"""Verify that a 3D mesh or CAD model is watertight, single-body, and slicer-ready.

    .venv/bin/python check.py -i out/bracket/bracket.stl
    .venv/bin/python check.py -i out/bracket/bracket.3mf --bed 220x220x250 --json out/report.json
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cadlib as C  # noqa: E402


def load_triangles(path):
    suffix = path.suffix.lower()
    if suffix == ".stl":
        return C.parse_stl(path.read_bytes())

    try:
        from build123d import Mesher, export_stl, import_step
    except ImportError:
        C.die("build123d is required to inspect non-STL formats (.3mf/.step). "
              "Run: uv pip install --python .venv/bin/python -r requirements.txt")

    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as tmp:
        tmp_stl = Path(tmp.name)
    try:
        if suffix in (".step", ".stp"):
            shape = import_step(str(path))
            export_stl(shape, str(tmp_stl))
        elif suffix == ".3mf":
            mesher = Mesher()
            shapes = mesher.read(str(path))
            from build123d import Compound
            export_stl(Compound(children=shapes) if len(shapes) > 1 else shapes[0], str(tmp_stl))
        else:
            C.die(f"unsupported input format '{suffix}'. Expected .stl, .3mf, or .step/.stp")
        return C.parse_stl(tmp_stl.read_bytes())
    finally:
        tmp_stl.unlink(missing_ok=True)


def evaluate(report, expected_parts=1, allow_multipart=False):
    """Return (failures: list[str], warnings: list[str])."""
    failures, warnings = [], []
    if not report["watertight"]:
        failures.append(
            f"non-manifold mesh ({report['boundary_edges']} open boundary edge(s), "
            f"{report['non_manifold_edges']} non-manifold edge(s))"
        )
    if report["inverted_normals"]:
        failures.append("negative signed volume — triangle winding / normals are inverted")
    if not report["fits_bed"]:
        sx, sy, sz = report["bbox_mm"]["size"]
        bx, by, bz = report["bed_mm"]
        failures.append(f"part size {sx:.2f}×{sy:.2f}×{sz:.2f} mm exceeds bed {bx:g}×{by:g}×{bz:g} mm")
    if not allow_multipart and report["components"] != expected_parts:
        failures.append(
            f"found {report['components']} disconnected solid(s), expected {expected_parts} "
            "(ensure bosses/ribs overlap or fuse with the main body)"
        )
    if report["unit_warning"]:
        warnings.append(report["unit_warning"])
    if report["bed_contact_mm2"] < 4.0:
        warnings.append(
            f"low print-bed contact area ({report['bed_contact_mm2']:.2f} mm² at Z_min) — "
            "reorient a flat face onto Z=0 or plan a brim/supports in your slicer"
        )
    ov = report["overhang"]
    if ov["fraction"] > 0.15:
        warnings.append(
            f"{ov['fraction'] * 100:.1f}% of surface ({ov['area_mm2']:.1f} mm²) overhangs steeper "
            f"than {ov['max_deg']:g}° — consider 45° chamfers or slicer supports"
        )
    return failures, warnings


def print_summary(path, report, failures, warnings):
    sx, sy, sz = report["bbox_mm"]["size"]
    zmin = report["bbox_mm"]["min"][2]
    status = f"{C.RED}FAIL{C.RESET}" if failures else f"{C.GREEN}PASS{C.RESET}"
    print(f"▸ Printability check [{status}] — {path.name}")
    print(f"  Size:        {sx:.2f} × {sy:.2f} × {sz:.2f} mm  (Z_min = {zmin:.3f} mm)")
    print(f"  Volume:      {report['volume_cm3']:.3f} cm³  ({report['triangles']} triangles)")
    print(f"  Topology:    {'watertight' if report['watertight'] else 'OPEN/NON-MANIFOLD'}, "
          f"{report['components']} solid(s)")
    print(f"  Bed contact: {report['bed_contact_mm2']:.1f} mm²  |  "
          f"Overhang >{report['overhang']['max_deg']:g}°: "
          f"{report['overhang']['area_mm2']:.1f} mm² "
          f"({report['overhang']['fraction'] * 100:.1f}%)")
    for w in warnings:
        print(f"  {C.YELLOW}!{C.RESET} {w}")
    for f in failures:
        print(f"  {C.RED}✗{C.RESET} {f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, help="input .stl, .3mf, or .step")
    ap.add_argument("--bed", default="256x256x256",
                    help="printer build volume WxDxH in mm (default 256x256x256)")
    ap.add_argument("--expected-parts", type=int, default=1,
                    help="expected connected solid count (default 1)")
    ap.add_argument("--allow-multipart", action="store_true",
                    help="do not fail when multiple disconnected solids are present")
    ap.add_argument("--max-overhang", type=float, default=45.0,
                    help="steep overhang threshold in degrees from vertical (default 45)")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="optional path to write machine-readable report JSON")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        C.die(f"file not found: {src}")

    bed = C.parse_bed(args.bed)
    tris = load_triangles(src)
    report = C.analyze_mesh(tris, bed_size=bed, max_overhang_deg=args.max_overhang)
    failures, warnings = evaluate(report, args.expected_parts, args.allow_multipart)
    report["passed"] = not failures
    report["failures"] = failures
    report["warnings"] = warnings

    if args.json_out:
        out_p = Path(args.json_out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print_summary(src, report, failures, warnings)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
