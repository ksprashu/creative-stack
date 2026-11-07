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

"""Apply the same edit to one image or a thousand.

An ordered pipeline of named operations, so a batch is reproducible and
reviewable instead of a shell one-liner nobody can read a month later.

    python3 edit.py -i shots/ -o out/ --ops "trim,resize:1600,round:16,shadow"
    python3 edit.py -i logo.png -o out/logo-dark.png --ops "tint:#E8EAED"
    python3 edit.py -i raw/ -o out/ --ops "crop:16x9,resize:1200" --recursive

Operations, applied left to right:

    trim                remove uniform border (screenshots with dead margin)
    crop:16x9           centre-crop to an aspect ratio
    crop:x,y,w,h        crop an explicit box in pixels
    resize:1600         fit inside 1600px on the long edge, never upscale
    resize:1600x900     fit inside a box
    pad:40              add a transparent margin
    pad:40:#FFFFFF      add a coloured margin
    round:16            round the corners (needs alpha, so writes PNG)
    shadow              drop shadow using brand shadow tokens
    border:2:#DADCE0    a hairline border
    grayscale           desaturate
    tint:#E8EAED        duotone-ish tint toward a colour
    flatten:#FFFFFF     composite alpha onto a solid background
    rotate:90           rotate clockwise
    autoorient          apply the EXIF orientation and drop the tag
    strip               remove all metadata (do this before publishing)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rasterlib as R  # noqa: E402

sys.path.insert(0, str(R.REPO_ROOT / "scripts"))
import brand  # noqa: E402

GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"

ASPECTS = {"16x9": 16 / 9, "4x3": 4 / 3, "3x2": 3 / 2, "1x1": 1.0,
           "9x16": 9 / 16, "2x1": 2.0, "21x9": 21 / 9}

# Operations that require an alpha channel in the output.
NEEDS_ALPHA = {"round", "shadow", "pad"}


def parse_ops(spec):
    ops = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        # `pad:40:#FFFFFF` and `crop:16x9` both split on ':' — keep the tail
        # whole so a hex colour containing no ':' survives either shape.
        name, _, rest = chunk.partition(":")
        ops.append((name.strip().lower(), rest))
    if not ops:
        R.die("no operations given. See --help for the list.")
    return ops


def build_args(ops, tokens, src_w, src_h):
    """Translate the op list into one ImageMagick argv.

    One process per image, not one per operation: a 12-step pipeline on a
    thousand files is 12,000 processes otherwise, and ImageMagick re-encodes
    at every hop, which is both slow and lossy on JPEG.
    """
    argv = []
    alpha = False
    shadow_tokens = tokens.get("shadow") or {}

    for name, arg in ops:
        if name == "trim":
            argv += ["-trim", "+repage"]
        elif name == "autoorient":
            argv += ["-auto-orient"]
        elif name == "strip":
            argv += ["-strip"]
        elif name == "grayscale":
            argv += ["-colorspace", "Gray"]
        elif name == "rotate":
            argv += ["-rotate", arg or "90"]
        elif name == "resize":
            if not arg:
                R.die("resize needs a size, e.g. resize:1600")
            # '>' means "shrink only". Upscaling a screenshot just blurs it.
            argv += ["-resize", f"{arg}>"]
        elif name == "crop":
            argv += _crop(arg, src_w, src_h)
        elif name == "pad":
            size, _, colour = arg.partition(":")
            argv += ["-bordercolor", colour or "none", "-border", size or "40"]
            alpha = alpha or not colour
        elif name == "border":
            width, _, colour = arg.partition(":")
            argv += ["-bordercolor", colour or "#DADCE0", "-border", width or "1"]
        elif name == "flatten":
            argv += ["-background", arg or "#FFFFFF", "-flatten", "-alpha", "off"]
            alpha = False
            continue
        elif name == "tint":
            if not arg:
                R.die("tint needs a colour, e.g. tint:#E8EAED")
            argv += ["-colorspace", "Gray", "-fill", arg, "-tint", "80"]
        elif name == "round":
            argv += _round(arg or "16", src_w, src_h)
            alpha = True
        elif name == "shadow":
            colour = shadow_tokens.get("color", "#00000040")
            argv += ["(", "+clone", "-background", colour, "-shadow", "40x18+0+10",
                     ")", "+swap", "-background", "none", "-layers", "merge",
                     "+repage"]
            alpha = True
        else:
            R.die(f"unknown operation '{name}'. See --help for the list.")

        if name in NEEDS_ALPHA:
            alpha = True

    return argv, alpha


def _crop(arg, src_w, src_h):
    if not arg:
        R.die("crop needs an aspect or a box, e.g. crop:16x9 or crop:0,0,800,600")
    if "," in arg:
        try:
            x, y, w, h = (int(v) for v in arg.split(","))
        except ValueError:
            R.die(f"crop box must be x,y,w,h — got '{arg}'")
        return ["-crop", f"{w}x{h}+{x}+{y}", "+repage"]

    ratio = ASPECTS.get(arg)
    if ratio is None:
        R.die(f"unknown aspect '{arg}'. Known: {', '.join(sorted(ASPECTS))}")
    if src_w / src_h > ratio:
        w, h = round(src_h * ratio), src_h
    else:
        w, h = src_w, round(src_w / ratio)
    return ["-gravity", "center", "-crop", f"{w}x{h}+0+0", "+repage"]


def _round(radius, width, height):
    """Rounded corners via a drawn mask.

    ImageMagick has no rounded-rect operator, so the idiom is to draw one into
    the alpha channel. The mask is sized from the *source*, which is why any
    resize has to come before the round in the op list.
    """
    r = int(radius)
    return ["-alpha", "set", "(", "+clone", "-alpha", "extract", "-draw",
            f"fill black polygon 0,0 0,{r} {r},0 "
            f"fill white circle {r},{r} {r},0", "(", "+clone", "-flip", ")",
            "-compose", "Multiply", "-composite",
            "(", "+clone", "-flop", ")", "-compose", "Multiply", "-composite",
            ")", "-alpha", "off", "-compose", "CopyOpacity", "-composite",
            "-compose", "Over"]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, nargs="+",
                    help="image, glob or directory")
    ap.add_argument("--output", "-o", required=True,
                    help="output file, or a directory for batch input")
    ap.add_argument("--ops", required=True, help="comma-separated operations")
    ap.add_argument("--format", help="force an output format (png, webp, jpg …)")
    ap.add_argument("--quality", type=int, help="encoder quality")
    ap.add_argument("--recursive", "-r", action="store_true")
    ap.add_argument("--brand", help="brand profile")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the ImageMagick command and stop")
    args = ap.parse_args()

    ops = parse_ops(args.ops)
    tokens = brand.load(args.brand)
    sources = R.inputs(args.input, args.recursive)

    out = Path(args.output)
    batch = len(sources) > 1 or args.output.endswith("/") or out.is_dir()
    if batch:
        out.mkdir(parents=True, exist_ok=True)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)

    for src in sources:
        src_w, src_h, _ = R.probe(src)
        argv, alpha = build_args(ops, tokens, src_w, src_h)

        if batch:
            suffix = f".{args.format.lstrip('.')}" if args.format else src.suffix
            dst = out / f"{src.stem}{suffix}"
        else:
            suffix = out.suffix
            dst = out

        if alpha and suffix.lower() in (".jpg", ".jpeg"):
            # JPEG has no alpha channel: rounding a corner and writing .jpg
            # gives black corners, not transparent ones. Retarget rather than
            # produce that quietly.
            dst = dst.with_suffix(".png")
            print(f"    {YELLOW}!{RESET} {src.name}: this pipeline needs alpha — "
                  f"writing {dst.name} instead of {suffix}")
        cmd = R.magick() + [str(src)] + argv
        if args.quality:
            cmd += ["-quality", str(args.quality)]
        cmd.append(str(dst))

        if args.dry_run:
            print(" ".join(cmd))
            continue

        R.run(cmd, f"edit {src.name}")
        w, h, _ = R.probe(dst)
        print(f"  {GREEN}✓{RESET} {dst}  {DIM}{src_w}x{src_h} → {w}x{h}"
              f"  {R.human(dst.stat().st_size)}{RESET}")

    if not args.dry_run:
        print(f"\n{DIM}  {len(sources)} image(s), ops: {args.ops}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
