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

"""Extrude a flat SVG logo into 3D and render it, still or as a sting.

    python3 logo3d.py -i logo.svg -o out/logo3d.png
    python3 logo3d.py -i logo.svg -o out/sting.mp4 --spin --frames 60
    python3 logo3d.py -i logo.svg -o out/logo.png --depth 0.2 --material red

The sting is a slow ease-in/ease-out rock through 60 degrees rather than a
full rotation: a logo that spins all the way round spends half the clip facing
away from the viewer, which is fine for a product and wrong for a wordmark.

The SVG has to contain paths, not live `<text>` — Blender's importer reads
curves and silently ignores text elements. Outline the type first:

    python3 ../../vector/scripts/svg.py -i logo.svg -o flat.svg --op text-to-path
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blendlib as B  # noqa: E402

sys.path.insert(0, str(B.REPO_ROOT / "scripts"))
import brand  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, help="flat .svg logo")
    ap.add_argument("--output", "-o", required=True,
                    help="output .png, or .mp4/.webm/.gif for a sting")
    ap.add_argument("--spin", action="store_true",
                    help="animate a rock through --sweep degrees")
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--depth", type=float, default=0.08,
                    help="extrusion depth as a fraction of the logo's size "
                         "(default 0.08 — thicker than about 0.25 and the "
                         "counters in letterforms start to close up)")
    ap.add_argument("--bevel", type=float, default=0.004,
                    help="edge bevel; this is what catches the key light and "
                         "gives the extrusion a visible edge")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--samples", type=int, default=96)
    ap.add_argument("--engine", choices=["cycles", "eevee"], default="cycles")
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--distance", type=float, default=5.0)
    ap.add_argument("--material", default="accent",
                    help="brand colour name or #RRGGBB (default accent)")
    ap.add_argument("--backdrop", help="named brand gradient for the backdrop")
    ap.add_argument("--ambient", type=float, default=0.5)
    ap.add_argument("--transparent", action="store_true",
                    help="render on alpha — what you want for an overlay")
    ap.add_argument("--mode", choices=["light", "dark"], default="light")
    ap.add_argument("--keep-frames", action="store_true")
    ap.add_argument("--brand", help="brand profile")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    source = Path(args.input)
    if not source.is_file():
        B.die(f"no such SVG: {args.input}")
    if source.suffix.lower() != ".svg":
        B.die(f"this takes an SVG — got {source.suffix or 'no extension'}. "
              f"Trace a raster first with /vector `trace.py`.")

    dst = Path(args.output)
    dst.parent.mkdir(parents=True, exist_ok=True)

    animated = args.spin or dst.suffix.lower() in B.VIDEO_SUFFIXES
    if animated and dst.suffix.lower() == ".png":
        dst = dst.with_suffix(".mp4")
        print(f"  {B.YELLOW}!{B.RESET} --spin writes a video — "
              f"using {dst.name}")

    tokens = brand.load(args.brand)
    major, minor = B.blender_version()
    if major < 3:
        B.die(f"Blender {major}.{minor} is too old — this needs 3.0 or newer.")

    studio = B.ensure_studio()
    job = {
        "action": "logo", "studio": str(studio), "svg": str(source.resolve()),
        "width": args.width, "height": args.height, "samples": args.samples,
        "engine": args.engine, "gpu": args.gpu, "distance": args.distance,
        "depth": args.depth, "bevel": args.bevel, "material": args.material,
        "transparent": args.transparent, "world_strength": args.ambient,
        "palette": B.palette(tokens, args.mode),
        "gradient": (B.gradient_stops(tokens, args.backdrop)
                     if args.backdrop else None),
    }
    if args.backdrop and not job["gradient"]:
        B.die(f"no gradient named '{args.backdrop}' in the brand tokens")

    print(f"◈ {source.name} → {dst.suffix.lstrip('.').upper()}  "
          f"{B.DIM}(extrude {args.depth}, {args.samples} samples){B.RESET}")

    staging = Path(tempfile.mkdtemp(prefix="cs-logo3d-"))
    backdrop = None
    if not args.transparent:
        backdrop = B.backdrop_png(staging / "backdrop.png", args.width,
                                  args.height, job["palette"]["bg"],
                                  job["gradient"])

    try:
        if not animated:
            raw = dst if args.transparent else staging / "raw.png"
            job["output"] = str(raw)
            B.run_job(job, "logo render", args.verbose)
            if backdrop:
                B.flatten(raw, backdrop, dst)
            B.report(dst, f"{args.width}x{args.height}")
            return 0

        frames_dir = (dst.parent / f"{dst.stem}-frames" if args.keep_frames
                      else staging / "frames")
        frames_dir.mkdir(parents=True, exist_ok=True)
        job["frames"] = args.frames
        job["output"] = str(frames_dir / f"{dst.stem}")

        B.run_job(job, "logo sting", args.verbose)
        rendered = sorted(frames_dir.glob(f"{dst.stem}*.png"))
        if len(rendered) < args.frames:
            B.die(f"expected {args.frames} frames, found {len(rendered)} in "
                  f"{frames_dir}")

        B.mux(frames_dir, dst.stem, dst, args.fps, backdrop)
        B.report(dst, f"{args.frames} frames @ {args.fps}fps, "
                      f"{args.frames / args.fps:.1f}s")
        if args.keep_frames:
            print(f"  {B.DIM}frames kept in {frames_dir}{B.RESET}")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
