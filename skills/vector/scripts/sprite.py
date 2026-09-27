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

"""Fold a directory of icons into one SVG sprite.

Twenty icons is twenty requests and twenty chances for one to 404. A sprite is
one file, cached once, and every icon is `<use href="#icon-name">` after that.

    python3 sprite.py -i icons/ -o out/icons.svg
    python3 sprite.py -i icons/ -o out/icons.svg --prefix ui- --currentcolor
    python3 sprite.py -i icons/ -o out/icons.svg --contact-sheet out/icons.png

`--currentcolor` is the one that matters for a themed site: it strips the
hardcoded fills so each icon inherits the CSS `color` of wherever it is used,
which is how one sprite serves both light and dark mode.
"""

import argparse
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vectorlib import (GREEN, YELLOW, DIM, RESET, REPO_ROOT, die,  # noqa: E402
                       run, svg_sources, svgo_optimise)

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import brand  # noqa: E402
import hostenv  # noqa: E402

VIEWBOX_RE = re.compile(r'\bviewBox\s*=\s*"([^"]+)"')
WIDTH_RE = re.compile(r'\bwidth\s*=\s*"([\d.]+)')
HEIGHT_RE = re.compile(r'\bheight\s*=\s*"([\d.]+)')
SVG_BLOCK_RE = re.compile(r"<svg\b[^>]*>(.*)</svg\s*>", re.S | re.I)
XML_DECL_RE = re.compile(r"<\?xml.*?\?>|<!DOCTYPE[^>]*>|<!--.*?-->", re.S)
# Colour literals that are a real paint, as opposed to "none" or a gradient
# reference — those have to survive `--currentcolor` untouched.
PAINT_RE = re.compile(r'\b(fill|stroke)\s*=\s*"(#[0-9a-fA-F]{3,8}|'
                      r'rgb\([^"]*\)|[a-z]+)"')

# id/class collide across files once everything lives in one document, so they
# get namespaced per symbol.
ID_DEF_RE = re.compile(r'\bid\s*=\s*"([^"]+)"')
ID_REF_RE = re.compile(r'url\(#([^)]+)\)|(?:xlink:)?href\s*=\s*"#([^"]+)"')


def slug(name):
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return cleaned or "icon"


def symbol_for(path, prefix, currentcolor, stem=None):
    """One <symbol> from one icon file, with its ids namespaced."""
    text = XML_DECL_RE.sub("", path.read_text(errors="replace"))

    body = SVG_BLOCK_RE.search(text)
    if not body:
        return None, f"{path.name}: no <svg> element"
    inner = body.group(1).strip()

    header = text[:body.start(1)]
    viewbox = VIEWBOX_RE.search(header)
    if viewbox:
        viewbox = viewbox.group(1)
    else:
        width = WIDTH_RE.search(header)
        height = HEIGHT_RE.search(header)
        if not (width and height):
            return None, (f"{path.name}: no viewBox and no width/height — "
                          f"cannot work out its coordinate system")
        viewbox = f"0 0 {width.group(1)} {height.group(1)}"

    name = f"{prefix}{slug(stem or path.stem)}"

    # Namespace every internal id so two icons that both define "a" — which
    # svgo produces constantly — do not overwrite each other in the sprite.
    local = set(ID_DEF_RE.findall(inner))
    for ident in sorted(local, key=len, reverse=True):
        scoped = f"{name}-{ident}"
        inner = inner.replace(f'id="{ident}"', f'id="{scoped}"')
        inner = inner.replace(f"url(#{ident})", f"url(#{scoped})")
        inner = inner.replace(f'href="#{ident}"', f'href="#{scoped}"')

    if currentcolor:
        inner = PAINT_RE.sub(
            lambda m: m.group(0) if m.group(2) in ("none", "transparent")
            or m.group(2).startswith("url")
            else f'{m.group(1)}="currentColor"', inner)

    return (f'  <symbol id="{name}" viewBox="{viewbox}">\n'
            f'    {inner}\n  </symbol>'), None


def contact_sheet(symbols, names, dst, columns, cell):
    """A PNG showing every icon with its id, so the set can be eyeballed."""
    rows = (len(names) + columns - 1) // columns
    pad, label = 16, 14
    width = columns * (cell + pad * 2)
    height = rows * (cell + pad * 2 + label)

    tiles = []
    for index, name in enumerate(names):
        x = (index % columns) * (cell + pad * 2) + pad
        y = (index // columns) * (cell + pad * 2 + label) + pad
        tiles.append(
            f'<use href="#{name}" x="{x}" y="{y}" width="{cell}" '
            f'height="{cell}"/>\n'
            f'<text x="{x + cell / 2}" y="{y + cell + 12}" font-size="10" '
            f'text-anchor="middle" fill="#5F6368" '
            f'font-family="sans-serif">{name}</text>')

    sheet = dst.with_suffix(".sheet.svg")
    sheet.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        f'<rect width="100%" height="100%" fill="#FFFFFF"/>\n'
        f'<g color="#202124">\n'
        + "<defs>\n" + "\n".join(symbols) + "\n</defs>\n"
        + "\n".join(tiles) + "\n</g>\n</svg>\n")

    renderer = hostenv.which("resvg") or hostenv.which("rsvg-convert")
    if not renderer:
        print(f"  {YELLOW}!{RESET} no resvg/rsvg-convert — contact sheet left "
              f"as {sheet}")
        return
    if renderer.endswith("resvg"):
        run([renderer, str(sheet), str(dst)], "resvg contact sheet")
    else:
        run([renderer, "-f", "png", "-o", str(dst), str(sheet)],
            "rsvg contact sheet")
    sheet.unlink(missing_ok=True)
    print(f"  {GREEN}✓{RESET} {dst}  {DIM}contact sheet, {len(names)} icons{RESET}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, nargs="+",
                    help="icon directory, glob or files")
    ap.add_argument("--output", "-o", required=True, help="output sprite .svg")
    ap.add_argument("--prefix", default="icon-",
                    help="id prefix (default 'icon-')")
    ap.add_argument("--currentcolor", action="store_true",
                    help="replace hardcoded fills with currentColor")
    ap.add_argument("--contact-sheet", metavar="PNG",
                    help="also render a labelled PNG of the whole set")
    ap.add_argument("--columns", type=int, default=6)
    ap.add_argument("--cell", type=int, default=48,
                    help="contact-sheet icon size in px (default 48)")
    ap.add_argument("--no-optimise", action="store_true")
    ap.add_argument("--recursive", "-r", action="store_true")
    ap.add_argument("--brand", help="brand profile")
    args = ap.parse_args()

    brand.load(args.brand)          # validates the profile early
    files = svg_sources(args.input, args.recursive)

    dst = Path(args.output)
    if dst.suffix.lower() != ".svg":
        die(f"a sprite is an SVG — got {dst.suffix or 'no extension'}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    symbols, names = [], []
    tmp = tempfile.TemporaryDirectory(prefix="cs-sprite-")
    for path in files:
        # Optimise each icon on its own rather than the finished sprite. svgo's
        # cleanupIds drops ids nothing in the same document references, and in
        # a sprite that is *every symbol id* — running it at the end quietly
        # deletes the only thing making the file useful.
        source = path
        if not args.no_optimise:
            source = Path(tmp.name) / f"{len(names)}-{path.name}"
            if svgo_optimise(path, source) is None:
                source = path
        symbol, problem = symbol_for(source, args.prefix, args.currentcolor, path.stem)
        if problem:
            print(f"  {YELLOW}!{RESET} {problem}")
            continue
        symbols.append(symbol)
        names.append(f"{args.prefix}{slug(path.stem)}")
    tmp.cleanup()

    if not symbols:
        die("no usable icons — every input was skipped")

    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        die(f"two icons slug to the same id: {', '.join(sorted(duplicates))}. "
            f"Rename the files — <use> can only reach one of them.")

    # display:none on the sprite root: without it the browser lays the whole
    # thing out inline and you get a blank block above the page content.
    dst.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" style="display:none">\n'
        + "\n".join(symbols) + "\n</svg>\n")

    print(f"  {GREEN}✓{RESET} {dst}  {DIM}{len(names)} symbols, "
          f"{dst.stat().st_size // 1024 or 1}KB{RESET}")

    if args.contact_sheet:
        sheet = Path(args.contact_sheet)
        sheet.parent.mkdir(parents=True, exist_ok=True)
        contact_sheet(symbols, names, sheet, args.columns, args.cell)

    print(f"\n{DIM}  Inline the sprite once, then:{RESET}\n"
          f'    <svg class="icon"><use href="#{names[0]}"/></svg>')
    if args.currentcolor:
        print(f"{DIM}    .icon {{ width: 24px; height: 24px; "
              f"color: var(--text); }}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
