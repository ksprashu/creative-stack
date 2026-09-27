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

"""Remove a background, then optionally drop the subject onto a brand backdrop.

Runs locally — the image never leaves the machine, which matters when the
subject is an unreleased product or an internal console.

    python3 cutout.py -i product.jpg -o out/product.png
    python3 cutout.py -i product.jpg -o out/hero.png --backdrop hero --pad 80
    python3 cutout.py -i shots/ -o out/cut/ --recursive --shadow

The first run downloads the u2net model (~170MB) into the rembg cache. That is
a one-time cost; later runs are offline.
"""

import argparse
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rasterlib as R  # noqa: E402

sys.path.insert(0, str(R.REPO_ROOT / "scripts"))
import brand  # noqa: E402

GREEN, DIM, RESET = "\033[32m", "\033[2m", "\033[0m"

# u2netp is a quarter the size and good enough for a clean product shot; u2net
# is the default because a soft edge on a hero image is what people notice.
MODELS = ("u2net", "u2netp", "isnet-general-use", "silueta")


def load_rembg():
    try:
        from rembg import new_session, remove          # noqa: PLC0415
    except ImportError:
        R.die("rembg not installed. Run: uv pip install --python .venv/bin/python rembg")
    return new_session, remove


def parse_gradient(spec, tokens):
    """(angle_degrees, [rgb, ...]) from a brand name or a literal.

    Brand tokens store gradients as the CSS the deck and frame templates use
    verbatim — `linear-gradient(135deg, #4285F4 0%, #34A853 100%)`. Parsing that
    here keeps one definition of "hero" across /shot, /deck and /raster instead
    of a second, drifting copy in a different shape.
    """
    gradients = tokens.get("gradient") or {}
    css = gradients.get(spec, spec)

    angle = 135.0
    match = re.search(r"(-?[\d.]+)deg", css)
    if match:
        angle = float(match.group(1))

    stops = [_rgb(h) for h in re.findall(r"#(?:[0-9a-fA-F]{3}){1,2}\b", css)]
    if not stops:
        R.die(f"unknown backdrop '{spec}'. Named: "
              f"{', '.join(sorted(gradients)) or '(none in tokens.json)'}; "
              f"or pass '#0B57D0,#1A73E8' or a full linear-gradient(...)")
    if len(stops) == 1:
        stops = stops * 2
    return angle, stops


def gradient_backdrop(size, spec, tokens):
    """Render a linear gradient as the backing layer."""
    from PIL import Image                               # noqa: PLC0415

    angle, stops = parse_gradient(spec, tokens)
    width, height = size

    # CSS angles run clockwise from "to top"; 135deg is the top-left → bottom-
    # right diagonal everything in this repo uses. Project each pixel onto that
    # direction, normalise to 0..1, and interpolate between the stops.
    theta = math.radians(angle - 90.0)
    dx, dy = math.cos(theta), math.sin(theta)
    extent = abs(dx) * (width - 1) + abs(dy) * (height - 1) or 1
    x0 = (width - 1) if dx < 0 else 0
    y0 = (height - 1) if dy < 0 else 0

    # Render small and upscale: a per-pixel python loop on a 4000px hero is
    # seconds, and a gradient has no detail to lose.
    sw, sh = max(2, min(width, 256)), max(2, min(height, 256))
    small = Image.new("RGB", (sw, sh))
    pixels = small.load()
    last = len(stops) - 1
    for y in range(sh):
        py = y * (height - 1) / max(sh - 1, 1)
        for x in range(sw):
            px = x * (width - 1) / max(sw - 1, 1)
            t = min(max((dx * (px - x0) + dy * (py - y0)) / extent, 0.0), 1.0)
            pos = t * last
            i = min(int(pos), last - 1)
            f = pos - i
            a, b = stops[i], stops[i + 1]
            pixels[x, y] = tuple(round(a[c] + (b[c] - a[c]) * f) for c in range(3))
    return small.resize((width, height), Image.BICUBIC)


def _rgb(hex_colour):
    value = hex_colour.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def add_shadow(subject, blur=24, offset=(0, 14), opacity=110):
    """A contact shadow under the cut-out subject, from its own alpha."""
    from PIL import Image, ImageFilter                   # noqa: PLC0415

    alpha = subject.split()[-1]
    shadow = Image.new("RGBA", subject.size, (0, 0, 0, 0))
    shadow.putalpha(alpha.point(lambda a: min(a, opacity)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))

    canvas = Image.new("RGBA", subject.size, (0, 0, 0, 0))
    canvas.alpha_composite(shadow, dest=(max(offset[0], 0), max(offset[1], 0)))
    canvas.alpha_composite(subject)
    return canvas


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, nargs="+",
                    help="image, glob or directory")
    ap.add_argument("--output", "-o", required=True,
                    help="output PNG, or a directory for batch input")
    ap.add_argument("--model", default="u2net", choices=MODELS,
                    help="segmentation model (default u2net)")
    ap.add_argument("--backdrop", help="named brand gradient, or '#hex,#hex'")
    ap.add_argument("--pad", type=int, default=0,
                    help="padding around the subject when using a backdrop")
    ap.add_argument("--shadow", action="store_true",
                    help="add a contact shadow under the subject")
    ap.add_argument("--alpha-matting", action="store_true",
                    help="slower, much better on hair and soft edges")
    ap.add_argument("--recursive", "-r", action="store_true")
    ap.add_argument("--brand", help="brand profile")
    args = ap.parse_args()

    from PIL import Image                                # noqa: PLC0415

    new_session, remove = load_rembg()
    tokens = brand.load(args.brand)
    session = new_session(args.model)

    sources = R.inputs(args.input, args.recursive)
    out = Path(args.output)
    batch = len(sources) > 1 or args.output.endswith("/") or out.is_dir()
    if batch:
        out.mkdir(parents=True, exist_ok=True)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix.lower() != ".png":
            R.die("a cut-out needs an alpha channel — write a .png")

    for src in sources:
        cut = remove(Image.open(src).convert("RGBA"), session=session,
                     alpha_matting=args.alpha_matting).convert("RGBA")

        if args.shadow:
            cut = add_shadow(cut)

        if args.backdrop:
            pad = args.pad
            canvas_size = (cut.width + pad * 2, cut.height + pad * 2)
            canvas = gradient_backdrop(canvas_size, args.backdrop,
                                       tokens).convert("RGBA")
            canvas.alpha_composite(cut, dest=(pad, pad))
            result = canvas
        else:
            result = cut

        dst = (out / f"{src.stem}.png") if batch else out
        result.save(dst)
        print(f"  {GREEN}✓{RESET} {dst}  {DIM}{result.width}x{result.height}"
              f"  {R.human(dst.stat().st_size)}{RESET}")

    if not args.backdrop:
        print(f"\n{DIM}  Transparent PNG — composite it with:"
              f"  --backdrop hero --pad 80 --shadow{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
