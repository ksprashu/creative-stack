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

"""Compile a parametric build123d part script into slicer-ready .stl, .3mf, and .step.

    .venv/bin/python build.py -s out/bracket/part.py -o out/bracket/bracket \
      --params out/bracket/params.json --preview out/bracket/preview.png
"""

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cadlib as C  # noqa: E402
import check as checker  # noqa: E402


def _coerce_value(raw):
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def load_part_module(script_path):
    spec = importlib.util.spec_from_file_location("cs_cad_part", str(script_path))
    if spec is None or spec.loader is None:
        C.die(f"cannot load Python part script: {script_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cs_cad_part"] = mod
    spec.loader.exec_module(mod)
    return mod


def resolve_output_stem(raw_out):
    p = Path(raw_out)
    if p.suffix.lower() in (".stl", ".3mf", ".step", ".stp"):
        return p.with_suffix("")
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--script", "-s", required=True,
                    help="Python part module defining build(params) -> Shape")
    ap.add_argument("--output", "-o", required=True,
                    help="output path stem (writes <stem>.stl, <stem>.3mf, <stem>.step)")
    ap.add_argument("--params", "-p", default=None,
                    help="optional JSON parameter file")
    ap.add_argument("--set", dest="overrides", action="append", default=[],
                    metavar="KEY=VAL", help="override a parameter on the CLI (repeatable)")
    ap.add_argument("--formats", default="stl,3mf,step",
                    help="comma-separated export formats (default: stl,3mf,step)")
    ap.add_argument("--tolerance", type=float, default=0.01,
                    help="linear mesh deflection in mm (default 0.01)")
    ap.add_argument("--angular-tolerance", type=float, default=0.1,
                    help="angular mesh deflection in radians (default 0.1)")
    ap.add_argument("--keep-z", action="store_true",
                    help="do not auto-centre X/Y or drop Z_min onto the Z=0 print bed")
    ap.add_argument("--bed", default="256x256x256",
                    help="printer build volume WxDxH in mm (default 256x256x256)")
    ap.add_argument("--expected-parts", type=int, default=1,
                    help="expected connected solid count (default 1)")
    ap.add_argument("--allow-multipart", action="store_true",
                    help="allow multi-body assemblies without failing the check")
    ap.add_argument("--max-overhang", type=float, default=45.0,
                    help="steep overhang warning threshold in degrees (default 45)")
    ap.add_argument("--no-check", action="store_true",
                    help="skip the post-export printability gate")
    ap.add_argument("--preview", default=None,
                    help="optional PNG path to render an isometric preview via /render3d")
    ap.add_argument("--material", default="blue",
                    help="material colour name or #RRGGBB for --preview (default blue)")
    args = ap.parse_args()

    script_path = Path(args.script)
    if not script_path.exists():
        C.die(f"part script not found: {script_path}")

    try:
        from build123d import Mesher, Pos, Unit, export_step, export_stl
    except ImportError:
        C.die("build123d is not installed in this Python environment. "
              "Run: uv pip install --python .venv/bin/python -r requirements.txt")

    mod = load_part_module(script_path)
    params = dict(getattr(mod, "DEFAULT_PARAMS", {}))
    if args.params:
        params_path = Path(args.params)
        if not params_path.exists():
            C.die(f"params file not found: {params_path}")
        params.update(json.loads(params_path.read_text(encoding="utf-8")))
    for item in args.overrides:
        if "=" not in item:
            C.die(f"expected KEY=VAL for --set, got: {item!r}")
        k, _, v = item.partition("=")
        params[k.strip()] = _coerce_value(v.strip())

    if callable(getattr(mod, "build", None)):
        raw_shape = mod.build(params)
    elif hasattr(mod, "part"):
        raw_shape = mod.part
    elif hasattr(mod, "result"):
        raw_shape = mod.result
    else:
        C.die(f"{script_path}: define `def build(params):` returning a build123d Part/Shape")

    shape = getattr(raw_shape, "part", raw_shape)
    if shape is None:
        C.die(f"{script_path}: build() returned None")

    if hasattr(shape, "is_valid") and not shape.is_valid:
        C.die(f"{script_path}: OpenCASCADE BRepCheck reports invalid geometry "
              "(often a fillet/chamfer radius larger than the adjacent face)")

    if not args.keep_z:
        bb = shape.bounding_box()
        cx = 0.5 * (bb.min.X + bb.max.X)
        cy = 0.5 * (bb.min.Y + bb.max.Y)
        shape = Pos(-cx, -cy, -bb.min.Z) * shape

    stem = resolve_output_stem(args.output)
    stem.parent.mkdir(parents=True, exist_ok=True)
    formats = [f.strip().lower().lstrip(".") for f in args.formats.split(",") if f.strip()]
    unknown = set(formats) - {"stl", "3mf", "step"}
    if unknown:
        C.die(f"unsupported --formats {sorted(unknown)}; choose from stl, 3mf, step")

    exports = {}
    stl_for_check = stem.with_suffix(".stl")
    export_stl(
        shape,
        str(stl_for_check),
        tolerance=args.tolerance,
        angular_tolerance=args.angular_tolerance,
    )
    if "stl" in formats:
        exports["stl"] = str(stl_for_check)
        print(f"✓ {stl_for_check}")

    if "3mf" in formats:
        threemf_path = stem.with_suffix(".3mf")
        mesher = Mesher(unit=Unit.MM)
        mesher.add_shape(
            shape,
            linear_deflection=args.tolerance,
            angular_deflection=args.angular_tolerance,
        )
        mesher.write(str(threemf_path))
        exports["3mf"] = str(threemf_path)
        print(f"✓ {threemf_path}")

    if "step" in formats:
        step_path = stem.with_suffix(".step")
        export_step(shape, str(step_path))
        exports["step"] = str(step_path)
        print(f"✓ {step_path}")

    exit_code = 0
    if not args.no_check:
        bed = C.parse_bed(args.bed)
        tris = C.parse_stl(stl_for_check.read_bytes())
        report = C.analyze_mesh(tris, bed_size=bed, max_overhang_deg=args.max_overhang)
        failures, warnings = checker.evaluate(
            report, args.expected_parts, args.allow_multipart
        )
        report["passed"] = not failures
        report["failures"] = failures
        report["warnings"] = warnings
        report["params"] = params
        report["exports"] = exports
        report_path = Path(f"{stem}.report.json")
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        checker.print_summary(stl_for_check, report, failures, warnings)
        if failures:
            exit_code = 1

    if "stl" not in formats:
        stl_for_check.unlink(missing_ok=True)

    if args.preview and exit_code == 0:
        render_script = C.REPO_ROOT / "skills/render3d/scripts/render.py"
        preview_src = exports.get("stl")
        if not preview_src:
            preview_src = str(stem.with_suffix(".preview.tmp.stl"))
            export_stl(shape, preview_src, tolerance=args.tolerance,
                       angular_tolerance=args.angular_tolerance)
        try:
            if C.hostenv.find_app("blender") and render_script.exists():
                cmd = [
                    sys.executable, str(render_script),
                    "-i", preview_src,
                    "-o", str(args.preview),
                    "--view", "iso",
                    "--samples", "48",
                    "--material", str(args.material),
                ]
                proc = subprocess.run(cmd)
                if proc.returncode != 0:
                    print(f"  {C.YELLOW}!{C.RESET} /render3d preview exited {proc.returncode}")
            else:
                print(f"  {C.YELLOW}!{C.RESET} Blender not found — skipping --preview render")
        finally:
            if preview_src.endswith(".preview.tmp.stl"):
                Path(preview_src).unlink(missing_ok=True)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
