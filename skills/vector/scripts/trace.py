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

"""Trace a raster image into clean SVG paths.

The usual reason: someone sent a logo as a 400px PNG and it has to go on a
slide, a poster and a favicon. Tracing it once beats re-drawing it.

    python3 trace.py -i logo.png -o out/logo.svg
    python3 trace.py -i logo.png -o out/logo.svg --fill brand.blue
    python3 trace.py -i mark.jpg -o out/mark.svg --mode color --colors 4

Two modes:

    bw      one flat shape from a threshold. Right for logos, marks, icons,
            signatures, line art — anything that is really one colour.
    color   posterise to N colours and trace each as its own layer, stacked
            back into one SVG. Right for flat illustrations; wrong for photos,
            which have no flat regions to find and produce path soup.

potrace only reads 1-bit bitmaps, so every path here goes through ImageMagick
first to threshold or to mask a single quantised colour.
"""

import argparse
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vectorlib import (GREEN, YELLOW, DIM, RESET, REPO_ROOT, die,  # noqa: E402
                       magick, need, run, svgo_optimise)

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import brand  # noqa: E402

# potrace writes a whole standalone SVG per run. In colour mode we want only
# the drawing, so the layers can be stacked into one document. Greedy on
# purpose: `--group` nests an inner <g> per shape, and a lazy match would stop
# at the first </g> and leave the outer one — with its transform — unbalanced.
GROUP_RE = re.compile(r"<g\b[^>]*>.*</g>", re.S)
VIEWBOX_RE = re.compile(r'viewBox="([^"]+)"')
FILL_RE = re.compile(r'\bfill="[^"]*"')


def resolve_colour(spec, tokens):
    """'brand.blue' or 'light.accent' into a hex, or pass a literal through."""
    if not spec:
        return None
    if spec.startswith("#"):
        return spec
    node = tokens.get("color", {})
    for part in spec.split("."):
        node = (node or {}).get(part)
        if node is None:
            die(f"no colour '{spec}' in the brand tokens. Try brand.blue, "
                f"light.accent, or a literal #RRGGBB.")
    return node


def quantised_colours(src, count, tmp):
    """The N dominant flat colours, largest area first.

    Area, not luminance: SVG has no z-index, so the layers paint in document
    order and whichever is last wins. The biggest region is almost always the
    background, and everything else is detail sitting on it — so sorting by
    pixel count descending puts the backdrop down first and the line work on
    top. Sorting by brightness instead works for dark-ink-on-paper and hides
    the entire drawing under a white plate for anything else.
    """
    # -depth 8 matters: without it a Q16 build prints 16-bit-per-channel hex
    # (#4251858FF4F5, not #4285F4) and every colour comes out wrong.
    txt = run(magick() + [str(src), "-alpha", "remove", "-alpha", "off",
                          "-colors", str(count), "-depth", "8", "-format", "%c",
                          "histogram:info:-"], "quantise", capture=True)
    counted = {}
    for line in txt.splitlines():
        match = re.search(r"^\s*(\d+):.*?#([0-9A-Fa-f]{6})\b", line)
        if match:
            colour = "#" + match.group(2).upper()
            counted[colour] = counted.get(colour, 0) + int(match.group(1))
    return sorted(counted, key=counted.get, reverse=True)


def mask_for(src, colour, fuzz, dst, count):
    """A 1-bit bitmap with everything close to `colour` in black.

    Black, not white: potrace traces the black pixels of a PBM and treats
    white as the hole. Inverting this gives you a full-canvas rectangle with
    the shape punched out of it, which looks right until you stack two layers.

    The image is quantised to the same palette first, so 'close to' means
    'landed in this bucket' rather than a fuzzy match against the original
    gradient — which is what keeps the layers from overlapping badly.
    """
    run(magick() + [str(src), "-alpha", "remove", "-alpha", "off",
                    "-colors", str(count), "-depth", "8",
                    "-fuzz", f"{fuzz}%", "-fill", "black", "-opaque", colour,
                    "-fill", "white", "+opaque", "black",
                    str(dst)], f"mask {colour}")


def potrace_svg(bitmap, opts):
    """potrace's SVG for one bitmap, as text."""
    cmd = [need("potrace"), "-s", "--group",
           "-o", "-", str(bitmap)] + opts
    return run(cmd, "potrace", capture=True)


def trace_options(args):
    opts = ["-t", str(args.despeckle), "-a", str(args.corner),
            "-O", str(args.tolerance)]
    if args.no_curve_optimise:
        opts.append("-n")
    return opts


def trace_bw(src, args, tmpdir):
    bitmap = tmpdir / "mask.pbm"
    # -flatten against white first: a transparent PNG thresholds to nothing,
    # because alpha is not luminance and potrace never sees the subject.
    run(magick() + [str(src), "-background", "white", "-alpha", "remove",
                    "-alpha", "off", "-colorspace", "Gray",
                    "-threshold", f"{args.threshold}%",
                    str(bitmap)], "threshold")
    svg = potrace_svg(bitmap, trace_options(args))
    groups = GROUP_RE.findall(svg)
    if not groups:
        die("potrace found no shapes. Try a different --threshold (it is "
            f"{args.threshold}% now) — or the image may be near-uniform.")
    return svg, [(groups[0], None)]


def trace_color(src, args, tmpdir):
    colours = quantised_colours(src, args.colors, tmpdir)
    print(f"  {DIM}palette: {' '.join(colours)}{RESET}")

    layers, template = [], None
    for index, colour in enumerate(colours):
        bitmap = tmpdir / f"layer{index}.pbm"
        mask_for(src, colour, args.fuzz, bitmap, args.colors)
        svg = potrace_svg(bitmap, trace_options(args))
        template = template or svg
        groups = GROUP_RE.findall(svg)
        if groups:
            layers.append((groups[0], colour))
        else:
            print(f"  {YELLOW}!{RESET} {colour}: no shapes, skipped")
    if not layers:
        die("no traceable layers. --mode color needs flat regions; a "
            "photograph has none. Use --mode bw, or posterise first.")
    return template, layers


def assemble(template, layers, width, height, fill):
    """One SVG document from the per-colour groups potrace produced.

    Every layer came from a bitmap of identical dimensions, so potrace gave
    each the same translate/scale transform and they stack in register.
    """
    viewbox = VIEWBOX_RE.search(template)
    viewbox = viewbox.group(1) if viewbox else f"0 0 {width} {height}"

    body = []
    for group, colour in layers:
        target = colour or fill
        if target:
            group = FILL_RE.sub(f'fill="{target}"', group, count=1)
        body.append(group)

    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="{viewbox}" width="{width}" height="{height}">\n'
            + "\n".join(body) + "\n</svg>\n")


def optimise(path):
    result = svgo_optimise(path, path)
    if result is None:
        print(f"  {YELLOW}!{RESET} svgo not found — SVG left unoptimised")
    else:
        print(f"  {DIM}svgo: {result}{RESET}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, help="raster image")
    ap.add_argument("--output", "-o", required=True, help="output .svg")
    ap.add_argument("--mode", choices=["bw", "color"], default="bw")
    ap.add_argument("--colors", type=int, default=6,
                    help="colour-mode layer count (default 6)")
    ap.add_argument("--fuzz", type=int, default=12,
                    help="colour-mode match tolerance %% (default 12)")
    ap.add_argument("--threshold", type=int, default=50,
                    help="bw-mode black/white split %% (default 50)")
    ap.add_argument("--fill", help="recolour a bw trace: brand.blue, or #RRGGBB")
    ap.add_argument("--despeckle", type=int, default=2, metavar="PX",
                    help="drop specks smaller than this many pixels (potrace -t)")
    ap.add_argument("--corner", type=float, default=1.0,
                    help="corner threshold: 0 is all corners, 1.33 all curves")
    ap.add_argument("--tolerance", type=float, default=0.2,
                    help="curve-fitting tolerance (potrace -O)")
    ap.add_argument("--no-curve-optimise", action="store_true",
                    help="keep every Bezier segment — larger, more faithful")
    ap.add_argument("--no-optimise", action="store_true",
                    help="skip the svgo pass")
    ap.add_argument("--brand", help="brand profile")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.is_file():
        die(f"no such file: {src}")
    dst = Path(args.output)
    if dst.suffix.lower() != ".svg":
        die(f"trace output is SVG — got {dst.suffix or 'no extension'}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    tokens = brand.load(args.brand)
    fill = resolve_colour(args.fill, tokens)

    size = run(magick() + [str(src), "-format", "%w %h", "info:-"],
               "probe", capture=True).split()
    width, height = (int(v) for v in size[:2])
    print(f"◈ {src.name} → SVG  {DIM}({args.mode}, {width}x{height}){RESET}")

    with tempfile.TemporaryDirectory(prefix="cs-trace-") as tmp:
        tmpdir = Path(tmp)
        if args.mode == "color":
            template, layers = trace_color(src, args, tmpdir)
        else:
            template, layers = trace_bw(src, args, tmpdir)

    dst.write_text(assemble(template, layers, width, height, fill))
    if not args.no_optimise:
        optimise(dst)

    print(f"  {GREEN}✓{RESET} {dst}  {DIM}{len(layers)} layer(s), "
          f"{dst.stat().st_size // 1024}KB{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
