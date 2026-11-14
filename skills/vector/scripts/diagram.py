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

"""Render a text diagram to brand-themed SVG, PNG or PDF.

Four renderers, one interface, because the right one depends on the diagram:

    d2          architecture — the best automatic layout of the four
    mermaid     sequence, state, gantt, ER; the format everyone already writes
    graphviz    dependency graphs, anything genuinely graph-shaped
    plantuml    UML proper, when the audience expects UML

The renderer is detected from the file extension or the content, so you write
the diagram and this works out how to draw it.

    python3 diagram.py -i arch.d2 -o out/arch.svg
    python3 diagram.py -i flow.mmd -o out/flow.png --theme dark
    python3 diagram.py -i deps.dot -o out/deps.svg --optimise
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(os.environ.get("CREATIVE_STACK_ROOT",
                                Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import brand  # noqa: E402
import hostenv  # noqa: E402

GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"

EXT_RENDERER = {".d2": "d2", ".mmd": "mermaid", ".mermaid": "mermaid",
                ".dot": "graphviz", ".gv": "graphviz", ".puml": "plantuml",
                ".plantuml": "plantuml", ".uml": "plantuml"}

# Content sniffing for a bare .txt or stdin. Ordered: the first match wins, so
# the most distinctive keyword for each language goes first.
CONTENT_HINTS = [
    ("mermaid", re.compile(r"^\s*(graph|flowchart|sequenceDiagram|classDiagram|"
                           r"stateDiagram|erDiagram|gantt|journey|pie|mindmap)\b",
                           re.M)),
    ("graphviz", re.compile(r"^\s*(strict\s+)?(di)?graph\b.*\{", re.M)),
    ("plantuml", re.compile(r"^\s*@startuml\b", re.M)),
    ("d2", re.compile(r"^\s*\w[\w. -]*\s*(->|--|<->)\s*\w", re.M)),
]

BIN_HINT = {"d2": "d2", "mermaid": "mmdc", "graphviz": "dot", "plantuml": "plantuml"}


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def detect(path, text):
    renderer = EXT_RENDERER.get(path.suffix.lower())
    if renderer:
        return renderer
    for name, pattern in CONTENT_HINTS:
        if pattern.search(text):
            return name
    die(f"cannot tell what kind of diagram {path.name} is. "
        f"Use a known extension ({', '.join(sorted(EXT_RENDERER))}) or --renderer.")
    return None


def need(binary, renderer):
    found = hostenv.which(binary)
    if not found:
        hint = {"macos": f"brew install {renderer}",
                "arch": f"sudo pacman -S {renderer}",
                "debian": f"sudo apt install {renderer}"}.get(hostenv.FAMILY, "")
        if renderer == "mermaid":
            hint = "npm install -g @mermaid-js/mermaid-cli"
        die(f"{binary} not found (needed for {renderer} diagrams). {hint}")
    return found


def palette(tokens, mode):
    """The handful of colours every renderer theme needs."""
    colours = tokens.get("color", {})
    scheme = colours.get(mode, {})
    accents = colours.get("brand", {})
    return {
        "bg": scheme.get("bg", "#FFFFFF"),
        "surface": scheme.get("surface", "#F8F9FA"),
        "border": scheme.get("border", "#DADCE0"),
        "text": scheme.get("text", "#202124"),
        "muted": scheme.get("textMuted", "#5F6368"),
        "accent": scheme.get("accent", "#1A73E8"),
        "blue": accents.get("blue", "#4285F4"),
        "red": accents.get("red", "#EA4335"),
        "yellow": accents.get("yellow", "#FBBC04"),
        "green": accents.get("green", "#34A853"),
        "font": (tokens.get("font", {}) or {}).get("sans", "Roboto"),
        "mono": (tokens.get("font", {}) or {}).get("mono", "Roboto Mono"),
    }


# ------------------------------------------------------------- per renderer ---

def d2_brand_source(src, text, pal, direction):
    """Write a temp .d2 that applies brand colours over the user's diagram.

    d2's `--theme` only selects one of its built-in palettes — the dark one is
    mauve, which is nobody's brand. `theme-overrides` remaps the colour slots,
    so this prepends a vars block rather than asking every diagram author to
    paste one in. The temp file goes next to the source so relative `...@file`
    imports still resolve.
    """
    overrides = {
        # neutrals: N1 darkest text through N7 lightest background
        "N1": pal["text"], "N2": pal["text"], "N3": pal["muted"],
        "N4": pal["border"], "N5": pal["border"], "N6": pal["surface"],
        "N7": pal["bg"],
        # primary shape scale
        "B1": pal["accent"], "B2": pal["accent"], "B3": pal["blue"],
        "B4": pal["blue"], "B5": pal["surface"], "B6": pal["surface"],
        # the two accents d2 rotates through for secondary shapes
        "AA2": pal["green"], "AA4": pal["green"], "AA5": pal["surface"],
        "AB4": pal["yellow"], "AB5": pal["surface"],
    }
    lines = ["vars: {", "  d2-config: {", "    theme-overrides: {"]
    lines += [f'      {slot}: "{value}"' for slot, value in overrides.items()]
    lines += ["    }", "  }", "}"]
    if direction:
        lines.append(f"direction: {direction}")
    lines.append("")

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".d2", prefix=f".{src.stem}-brand-",
        dir=src.parent, delete=False)
    handle.write("\n".join(lines) + text)
    handle.close()
    return Path(handle.name)


def render_d2(src, dst, pal, mode, scale, direction=None, text=""):
    # Theme 0 is d2's neutral default and 200 its dark counterpart; the brand
    # overrides sit on top of whichever. ELK lays out architecture diagrams far
    # better than the default dagre.
    src = d2_brand_source(src, text, pal, direction)
    cmd = [need("d2", "d2"), "--layout", "elk", "--pad", "32",
           "--theme", "0" if mode == "light" else "200"]

    # d2 wants *paths to .ttf files*, not family names, and it has no fallback
    # if the path is wrong — so only pass a font we actually resolved. strict
    # matters here: silently rendering in Andale Mono is worse than d2's own
    # bundled Source Sans Pro.
    regular = hostenv.font_file(pal["font"], bold=False, strict=True)
    bold = hostenv.font_file(pal["font"], bold=True, strict=True)
    mono = hostenv.font_file(pal["mono"], bold=False, strict=True)
    if regular:
        cmd += ["--font-regular", regular]
    if bold:
        cmd += ["--font-bold", bold]
    if mono:
        cmd += ["--font-mono", mono]

    if scale and dst.suffix.lower() != ".svg":
        cmd += ["--scale", str(scale)]
    cmd += [str(src), str(dst)]
    return cmd


def render_mermaid(src, dst, pal, mode, scale, direction=None, text=""):
    # mmdc takes its theme as a JSON blob, which is how brand colours get in.
    config = {
        "theme": "base",
        "themeVariables": {
            "background": pal["bg"],
            "primaryColor": pal["surface"],
            "primaryTextColor": pal["text"],
            "primaryBorderColor": pal["border"],
            "lineColor": pal["muted"],
            "secondaryColor": pal["surface"],
            "tertiaryColor": pal["bg"],
            "fontFamily": f'"{pal["font"]}", sans-serif',
            "fontSize": "15px",
        },
        "flowchart": {"curve": "basis", "padding": 16},
        "sequence": {"actorMargin": 48},
    }
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(config, handle)
    handle.close()

    cmd = [need("mmdc", "mermaid"), "-i", str(src), "-o", str(dst),
           "-c", handle.name, "-b", pal["bg"]]
    if scale:
        cmd += ["-s", str(scale)]
    # Chromium inside a container has no usable sandbox; mmdc needs telling.
    if not hostenv.IS_MAC and not hostenv.has_display():
        puppeteer = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"args": ["--no-sandbox", "--disable-dev-shm-usage"]}, puppeteer)
        puppeteer.close()
        cmd += ["-p", puppeteer.name]
    return cmd


def render_graphviz(src, dst, pal, mode, scale, direction=None, text=""):
    fmt = dst.suffix.lstrip(".").lower()
    rankdir = {"right": "LR", "left": "RL", "down": "TB", "up": "BT"}.get(
        direction or "", "TB")
    return [need("dot", "graphviz"), f"-T{fmt}", f"-Grankdir={rankdir}",
            f"-Gbgcolor={pal['bg']}", f"-Gfontname={pal['font']}",
            f"-Nfontname={pal['font']}", f"-Efontname={pal['font']}",
            f"-Nfillcolor={pal['surface']}", f"-Ncolor={pal['border']}",
            f"-Nfontcolor={pal['text']}", f"-Ecolor={pal['muted']}",
            "-Nstyle=filled,rounded", "-Nshape=box", "-Gpad=0.4",
            str(src), "-o", str(dst)]


def reject_graphviz_resources(text):
    """Fail closed on DOT attributes that make Graphviz load local files."""
    blocked = {"image", "imagepath", "shapefile"}
    length = len(text)

    def skip_space_and_comments(index):
        while index < length:
            if text[index].isspace():
                index += 1
            elif text.startswith("/*", index):
                end = text.find("*/", index + 2)
                if end < 0:
                    return length
                index = end + 2
            elif text.startswith("//", index):
                end = text.find("\n", index + 2)
                if end < 0:
                    return length
                index = end + 1
            elif (text[index] == "#"
                  and not text[text.rfind("\n", 0, index) + 1:index].strip()):
                end = text.find("\n", index + 1)
                if end < 0:
                    return length
                index = end + 1
            else:
                break
        return index

    index = 0
    while index < length:
        index = skip_space_and_comments(index)
        if index >= length:
            break

        if text[index] == '"':
            start = index + 1
            index = start
            value = []
            while index < length:
                if text[index] == "\\" and index + 1 < length:
                    value.extend((text[index], text[index + 1]))
                    index += 2
                elif text[index] == '"':
                    break
                else:
                    value.append(text[index])
                    index += 1
            index = min(index + 1, length)
            key = "".join(value).lower()
            equals = skip_space_and_comments(index)
            if key in blocked and equals < length and text[equals] == "=":
                raise ValueError(
                    "Graphviz local image resources are disabled; remove image, "
                    "shapefile, or imagepath attributes"
                )
            continue

        if text[index] == "<":
            start = index
            depth = 1
            quote = None
            index += 1
            while index < length:
                if text.startswith("<!--", index):
                    end = text.find("-->", index + 4)
                    index = length if end < 0 else end + 3
                    continue
                char = text[index]
                if quote:
                    if char == quote:
                        quote = None
                elif char in ('"', "'"):
                    quote = char
                elif char == "<":
                    depth += 1
                elif char == ">":
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                index += 1
            html = text[start:index]
            html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
            if re.search(r"<\s*IMG\b[^>]*\bSRC\s*=", html, re.I | re.S):
                raise ValueError(
                    "Graphviz local image resources are disabled; remove HTML IMG SRC"
                )
            continue

        if text[index].isalpha() or text[index] == "_":
            end = index + 1
            while end < length and (text[end].isalnum() or text[end] == "_"):
                end += 1
            key = text[index:end].lower()
            index = end
            equals = skip_space_and_comments(index)
            if key in blocked and equals < length and text[equals] == "=":
                raise ValueError(
                    "Graphviz local image resources are disabled; remove image, "
                    "shapefile, or imagepath attributes"
                )
            continue

        index += 1


def render_plantuml(src, dst, pal, mode, scale, direction=None, text=""):
    fmt = dst.suffix.lstrip(".").lower()
    return [need("plantuml", "plantuml"), f"-t{fmt}",
            "-o", str(dst.parent.resolve()), str(src)]


RENDERERS = {"d2": render_d2, "mermaid": render_mermaid,
             "graphviz": render_graphviz, "plantuml": render_plantuml}

SUPPORTED = {"d2": {".svg", ".png", ".pdf"},
             "mermaid": {".svg", ".png", ".pdf"},
             "graphviz": {".svg", ".png", ".pdf"},
             "plantuml": {".svg", ".png"}}


def optimise_svg(path):
    svgo = hostenv.which("svgo")
    if not svgo:
        return False
    subprocess.run([svgo, "--quiet", "--multipass", str(path), "-o", str(path)],
                   check=False)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, help="diagram source file")
    ap.add_argument("--output", "-o", required=True, help="output .svg, .png or .pdf")
    ap.add_argument("--renderer", choices=sorted(RENDERERS),
                    help="override the auto-detected renderer")
    ap.add_argument("--theme", choices=["light", "dark"], default="light")
    ap.add_argument("--scale", type=float, default=2.0,
                    help="raster scale factor (default 2 = retina)")
    ap.add_argument("--direction", choices=["right", "left", "down", "up"],
                    help="layout direction (d2 and graphviz). Architecture "
                         "diagrams usually want 'right' — the default top-down "
                         "flow produces very tall images.")
    ap.add_argument("--optimise", action="store_true",
                    help="run svgo over SVG output")
    ap.add_argument("--brand", help="brand profile")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        die(f"no such file: {src}")
    dst = Path(args.output)
    dst.parent.mkdir(parents=True, exist_ok=True)

    text = src.read_text(errors="replace")
    renderer = args.renderer or detect(src, text)

    if renderer == "graphviz":
        try:
            reject_graphviz_resources(text)
        except ValueError as error:
            die(str(error))

    if dst.suffix.lower() not in SUPPORTED[renderer]:
        die(f"{renderer} cannot write {dst.suffix}. "
            f"Supported: {', '.join(sorted(SUPPORTED[renderer]))}")

    pal = palette(brand.load(args.brand), args.theme)
    cmd = RENDERERS[renderer](src, dst, pal, args.theme, args.scale,
                              args.direction, text)

    print(f"◈ {src.name} → {dst.suffix[1:].upper()}  {DIM}({renderer}, {args.theme}){RESET}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        die(f"{renderer} failed:\n{detail}")

    # render_d2 writes a temp brand overlay next to the source; it has served
    # its purpose by now whether the render worked or not.
    for leftover in src.parent.glob(f".{src.stem}-brand-*.d2"):
        leftover.unlink(missing_ok=True)

    if not dst.exists():
        die(f"{renderer} reported success but wrote no {dst}")

    if args.optimise and dst.suffix.lower() == ".svg":
        before = dst.stat().st_size
        if optimise_svg(dst):
            after = dst.stat().st_size
            print(f"  {DIM}svgo: {before // 1024}KB → {after // 1024}KB{RESET}")
        else:
            print(f"  {YELLOW}!{RESET} svgo not installed — skipping optimisation")

    print(f"  {GREEN}✓{RESET} {dst}  {DIM}{dst.stat().st_size // 1024}KB{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
