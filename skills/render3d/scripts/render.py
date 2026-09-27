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

"""Render a 3D model headlessly, framed and lit, against the brand.

    python3 render.py -i model.glb -o out/hero.png
    python3 render.py -i model.glb -o out/iso.png --view iso --transparent
    python3 render.py -i model.glb -o out/spin.mp4 --turntable --frames 90
    python3 render.py -i part.stl -o out/part.png --material blue --backdrop hero

The model is scaled and centred automatically, so the same rig frames a
millimetre-scale CAD part and a metre-scale game asset without tuning.

Output format comes from the extension: `.png` is a still, `.mp4`/`.webm`/`.gif`
is a turntable. `--turntable` on its own defaults to MP4.
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
    ap.add_argument("--input", "-i", required=True, help="model file")
    ap.add_argument("--output", "-o", required=True, help="output .png or .mp4")
    ap.add_argument("--view", choices=["three-quarter", "front", "iso"],
                    default="three-quarter",
                    help="camera framing (iso is a true isometric orthographic)")
    ap.add_argument("--turntable", action="store_true",
                    help="render a seamless 360 spin instead of a still")
    ap.add_argument("--frames", type=int, default=90,
                    help="turntable frame count (default 90)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--samples", type=int, default=96,
                    help="render samples; 32 previews, 256 publishes")
    ap.add_argument("--engine", choices=["cycles", "eevee"], default="cycles",
                    help="cycles is slower and correct; eevee needs a GPU")
    ap.add_argument("--gpu", action="store_true",
                    help="let Cycles use the GPU (off by default: headless "
                         "boxes and containers do not have one)")
    ap.add_argument("--distance", type=float, default=6.0,
                    help="camera distance, or ortho scale for --view iso")
    ap.add_argument("--material", help="override every material: a brand "
                                       "colour name (blue, accent) or #RRGGBB")
    ap.add_argument("--backdrop", help="named brand gradient for the world")
    ap.add_argument("--ambient", type=float, default=0.5,
                    help="ambient world strength on top of the three-point "
                         "rig. Raise it to flatten and brighten, drop it "
                         "toward 0.2 for a moodier, higher-contrast render")
    ap.add_argument("--transparent", action="store_true",
                    help="render on alpha instead of the brand background")
    ap.add_argument("--mode", choices=["light", "dark"], default="light")
    ap.add_argument("--keep-frames", action="store_true",
                    help="keep the PNG sequence next to the video")
    ap.add_argument("--brand", help="brand profile")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="show Blender's full output")
    args = ap.parse_args()

    model = B.resolve_model(args.input)
    dst = Path(args.output)
    dst.parent.mkdir(parents=True, exist_ok=True)

    animated = args.turntable or dst.suffix.lower() in B.VIDEO_SUFFIXES
    if animated and dst.suffix.lower() == ".png":
        dst = dst.with_suffix(".mp4")
        print(f"  {B.YELLOW}!{B.RESET} --turntable writes a video — "
              f"using {dst.name}")

    tokens = brand.load(args.brand)
    major, minor = B.blender_version()
    if major < 3:
        B.die(f"Blender {major}.{minor} is too old — this needs 3.0 or newer "
              f"for the import operators and the shadow catcher.")

    studio = B.ensure_studio()
    job = {
        "action": "render", "studio": str(studio), "model": str(model),
        "view": args.view, "width": args.width, "height": args.height,
        "samples": args.samples, "engine": args.engine, "gpu": args.gpu,
        "distance": args.distance, "material": args.material,
        "transparent": args.transparent, "world_strength": args.ambient,
        "palette": B.palette(tokens, args.mode),
        "gradient": B.gradient_stops(tokens, args.backdrop) if args.backdrop else None,
    }
    if args.backdrop and not job["gradient"]:
        B.die(f"no gradient named '{args.backdrop}' in the brand tokens")

    print(f"◈ {model.name} → {dst.suffix.lstrip('.').upper()}  "
          f"{B.DIM}({args.engine}, {args.samples} samples, {args.view})"
          f"{B.RESET}")

    # Blender always renders onto alpha so the cove can catch a shadow; the
    # brand background is laid in underneath afterwards unless the caller
    # asked to keep the transparency.
    staging = Path(tempfile.mkdtemp(prefix="cs-render3d-"))
    backdrop = None
    if not args.transparent:
        backdrop = B.backdrop_png(staging / "backdrop.png", args.width,
                                args.height, job["palette"]["bg"],
                                job["gradient"])

    try:
        if not animated:
            raw = dst if args.transparent else staging / "raw.png"
            job["output"] = str(raw)
            B.run_job(job, "render", args.verbose)
            if backdrop:
                B.flatten(raw, backdrop, dst)
            B.report(dst, f"{args.width}x{args.height}")
            return 0

        frames_dir = (dst.parent / f"{dst.stem}-frames" if args.keep_frames
                      else staging / "frames")
        frames_dir.mkdir(parents=True, exist_ok=True)
        job["frames"] = args.frames
        job["output"] = str(frames_dir / f"{dst.stem}")

        B.run_job(job, "turntable render", args.verbose)
        rendered = sorted(frames_dir.glob(f"{dst.stem}*.png"))
        if len(rendered) < args.frames:
            B.die(f"expected {args.frames} frames, found {len(rendered)} in "
                  f"{frames_dir}")

        B.mux(frames_dir, dst.stem, dst, args.fps, backdrop)
        B.report(dst, f"{args.frames} frames @ {args.fps}fps, "
                      f"{args.frames / args.fps:.1f}s loop")
        if args.keep_frames:
            print(f"  {B.DIM}frames kept in {frames_dir} "
                  f"(still transparent — the backdrop goes on at mux){B.RESET}")
        return 0
    finally:
        # --keep-frames puts the sequence outside the staging directory, so
        # this only ever removes scratch.
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
