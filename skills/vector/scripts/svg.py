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

"""The four things you always end up doing to an SVG before shipping it.

    optimise      svgo, with the viewBox kept — the usual cause of an icon
                  that renders at 0x0 in production is svgo removing it
    export        rasterise to PNG, or hand off to PDF/EPS for print
    text-to-path  convert live text to outlines, so the file does not depend
                  on a font the reader does not have
    recolor       snap an SVG's colours onto the brand palette

    python3 svg.py -i icons/ -o out/icons/ --op optimise
    python3 svg.py -i logo.svg -o out/logo.png --op export --width 1200
    python3 svg.py -i slide.svg -o out/slide.svg --op text-to-path
    python3 svg.py -i third-party.svg -o out/on-brand.svg --op recolor --to-brand

Export deliberately prefers `resvg` over Inkscape: it is a static binary with
no GUI to start, it is ~20x faster on a batch, and it renders the SVG 2 subset
correctly. Inkscape is the fallback, and the only tool here that can do
text-to-path.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vectorlib import (GREEN, DIM, RESET, REPO_ROOT, die, run,  # noqa: E402
                       svg_sources, svgo_optimise, validate_svg_resources)

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import brand  # noqa: E402
import hostenv  # noqa: E402

# `fill="#abc"`, `stroke:#AABBCC`, `stop-color="#123456"` — one pattern covers
# both the attribute and the inline-style spellings because we only rewrite the
# colour literal, never the property around it.
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")


def inkscape():
    found = hostenv.which("inkscape")
    if not found:
        die("inkscape not found. Run: scripts/install_bridges.py (it links the "
            "app's headless binary into ~/.local/bin), or "
            f"{hostenv.package_hint('inkscape')}")
    return found


# --------------------------------------------------------------- operations ---

def op_optimise(src, dst, args, tokens):
    return svgo_optimise(src, dst, args.precision, required=True)


def op_export(src, dst, args, tokens):
    try:
        validate_svg_resources(src)
    except ValueError as error:
        die(str(error))
    fmt = dst.suffix.lower().lstrip(".")
    if fmt in ("png",) and hostenv.which("resvg"):
        cmd = [hostenv.which("resvg"), "--resources-dir", str(src.resolve().parent)]
        if args.width:
            cmd += ["--width", str(args.width)]
        elif args.height:
            cmd += ["--height", str(args.height)]
        else:
            cmd += ["--zoom", str(args.scale)]
        if args.background:
            cmd += ["--background", args.background]
        run(cmd + [str(src), str(dst)], "resvg")
        return "resvg"

    if fmt == "png" and hostenv.which("rsvg-convert"):
        cmd = [hostenv.which("rsvg-convert"), "-f", "png"]
        if args.width:
            cmd += ["-w", str(args.width)]
        elif args.height:
            cmd += ["-h", str(args.height)]
        else:
            cmd += ["-z", str(args.scale)]
        if args.background:
            cmd += ["-b", args.background]
        run(cmd + ["-o", str(dst), str(src)], "rsvg-convert")
        return "rsvg-convert"

    # PDF and EPS have to go through Inkscape — it is the only one of the three
    # that writes real vector output rather than an embedded raster.
    cmd = [inkscape(), f"--export-type={fmt}", f"--export-filename={dst}"]
    if args.width:
        cmd.append(f"--export-width={args.width}")
    if args.height:
        cmd.append(f"--export-height={args.height}")
    if args.background:
        cmd.append(f"--export-background={args.background}")
    run(cmd + [str(src)], f"inkscape export to {fmt}")
    return "inkscape"


def op_text_to_path(src, dst, args, tokens):
    # --export-text-to-path only applies on export, so the round trip is
    # SVG in, plain SVG out, with every glyph now a path.
    run([inkscape(), "--export-type=svg", "--export-plain-svg",
         "--export-text-to-path", f"--export-filename={dst}", str(src)],
        "inkscape text-to-path")
    remaining = len(re.findall(r"<text\b", dst.read_text(errors="replace")))
    return "no live text left" if not remaining else f"{remaining} <text> left"


def op_recolor(src, dst, args, tokens):
    text = src.read_text(errors="replace")
    mapping = explicit_map(args.map, tokens)
    palette = brand_palette(tokens) if args.to_brand else {}

    changed = {}

    def swap(match):
        found = normalise(match.group(0))
        target = mapping.get(found)
        if target is None and palette:
            target = nearest(found, palette, args.tolerance)
        if target is None or target.upper() == found:
            return match.group(0)
        changed[found] = target
        return target

    dst.write_text(HEX_RE.sub(swap, text))
    if not changed:
        return "no colours changed"
    pairs = ", ".join(f"{a}→{b}" for a, b in list(changed.items())[:4])
    more = f" +{len(changed) - 4}" if len(changed) > 4 else ""
    return pairs + more


# ----------------------------------------------------------------- colours ---

def normalise(hex_colour):
    value = hex_colour.lstrip("#").upper()
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return "#" + value


def rgb(hex_colour):
    value = normalise(hex_colour)[1:]
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def brand_palette(tokens):
    """Every brand colour worth snapping to, flattened to a list of hexes."""
    colours = tokens.get("color", {})
    out = list((colours.get("brand") or {}).values())
    for scheme in ("light", "dark"):
        out += [v for v in (colours.get(scheme) or {}).values()
                if isinstance(v, str) and v.startswith("#")]
    seen = [normalise(c) for c in out if isinstance(c, str) and c.startswith("#")]
    if not seen:
        die("the brand tokens define no colours to snap to — fill in "
            "brand/tokens.json, or use --map instead of --to-brand")
    return list(dict.fromkeys(seen))


def explicit_map(spec, tokens):
    """'#FF0000=brand.red,#000=light.text' into {normalised: hex}."""
    mapping = {}
    for pair in (spec or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        source, _, target = pair.partition("=")
        if not target:
            die(f"--map wants from=to pairs — got '{pair}'")
        mapping[normalise(source)] = resolve(target.strip(), tokens)
    return mapping


def resolve(spec, tokens):
    if spec.startswith("#"):
        return normalise(spec)
    node = tokens.get("color", {})
    for part in spec.split("."):
        node = (node or {}).get(part)
        if node is None:
            die(f"no colour '{spec}' in the brand tokens")
    return normalise(node)


def nearest(hex_colour, palette, tolerance):
    """Closest brand colour, or None if nothing is close enough.

    Weighted RGB rather than plain Euclidean: the eye is roughly twice as
    sensitive to green as to blue, and an unweighted distance happily maps a
    mid grey onto a saturated brand blue.
    """
    r, g, b = rgb(hex_colour)
    best, best_distance = None, None
    for candidate in palette:
        cr, cg, cb = rgb(candidate)
        distance = (2 * (r - cr) ** 2 + 4 * (g - cg) ** 2 + 3 * (b - cb) ** 2) ** 0.5
        if best_distance is None or distance < best_distance:
            best, best_distance = candidate, distance
    # Max weighted distance is sqrt(9 * 255^2) = 765; express the tolerance as
    # a percentage of that so the flag means something to a human.
    return best if best_distance <= tolerance / 100 * 765 else None


OPS = {"optimise": op_optimise, "export": op_export,
       "text-to-path": op_text_to_path, "recolor": op_recolor}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, nargs="+",
                    help="SVG, glob or directory")
    ap.add_argument("--output", "-o", required=True,
                    help="output file, or a directory for batch input")
    ap.add_argument("--op", required=True, choices=sorted(OPS))
    ap.add_argument("--width", type=int, help="export width in pixels")
    ap.add_argument("--height", type=int, help="export height in pixels")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="export zoom when no width/height is given")
    ap.add_argument("--background", help="export background, e.g. '#FFFFFF'")
    ap.add_argument("--precision", type=int, default=3,
                    help="optimise: decimal places to keep (default 3)")
    ap.add_argument("--map", help="recolor: '#FF0000=brand.red,#000=light.text'")
    ap.add_argument("--to-brand", action="store_true",
                    help="recolor: snap every colour to its nearest brand colour")
    ap.add_argument("--tolerance", type=float, default=30,
                    help="recolor: how far --to-brand may reach, 0-100 (default 30)")
    ap.add_argument("--recursive", "-r", action="store_true")
    ap.add_argument("--brand", help="brand profile")
    args = ap.parse_args()

    if args.op == "recolor" and not (args.map or args.to_brand):
        die("recolor needs --map, --to-brand, or both")

    tokens = brand.load(args.brand)
    files = svg_sources(args.input, args.recursive)

    out = Path(args.output)
    batch = len(files) > 1 or args.output.endswith("/") or out.is_dir()
    if batch:
        out.mkdir(parents=True, exist_ok=True)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)

    suffix = ".png" if args.op == "export" else ".svg"
    for src in files:
        dst = (out / f"{src.stem}{suffix}") if batch else out
        if src.resolve() == dst.resolve():
            die(f"{src} would be overwritten in place. Write to a separate "
                f"-o path; there is no undo on an optimise pass.")
        note = OPS[args.op](src, dst, args, tokens)
        print(f"  {GREEN}✓{RESET} {dst}  {DIM}{note}{RESET}")

    print(f"\n{DIM}  {len(files)} file(s), op: {args.op}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
