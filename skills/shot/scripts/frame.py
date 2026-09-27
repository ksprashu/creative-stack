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

"""Composite a raw screenshot onto a branded, publication-ready backdrop.

Adds rounded corners, a drop shadow, optional macOS window or browser chrome,
and a gradient backdrop pulled from brand/tokens.json — so every screenshot in
a post looks like it came from the same studio.

    # macOS window frame on the default brand backdrop
    python3 frame.py --input raw.png --output hero.png

    # browser chrome with a URL, dark brand backdrop
    python3 frame.py --input app.png --output app-framed.png \\
        --chrome browser --url "https://console.cloud.google.com" --backdrop dusk

    # no chrome, just radius + shadow, tighter padding
    python3 frame.py --input diagram.png --output diagram-framed.png \\
        --chrome none --padding 48
"""

import argparse
import base64
import mimetypes
import os
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("CREATIVE_STACK_ROOT",
                                Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import brand  # noqa: E402  - path set above

TEMPLATE = REPO_ROOT / "templates/frames/frame.html"


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def data_uri(path):
    """Inline the image so Playwright needs no file:// origin permissions."""
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def build_css_vars(tokens, args):
    """Map brand tokens onto the template's custom properties."""
    mode = args.mode
    palette = tokens["color"][mode]
    gradients = tokens.get("gradient", {})
    shot_cfg = tokens.get("screenshot", {})

    backdrop = args.backdrop or shot_cfg.get("defaultBackdrop", "subtle")
    # A backdrop may be a named gradient, a raw CSS value, or a flat hex colour.
    backdrop_css = gradients.get(backdrop, backdrop)

    padding = args.padding if args.padding is not None else shot_cfg.get("backdropPadding", 96)
    radius = args.radius if args.radius is not None else shot_cfg.get("cornerRadius", 12)

    return {
        "--backdrop": backdrop_css,
        "--pad": f"{padding}px",
        "--radius": f"{radius}px",
        "--shadow": tokens.get("shadow", {}).get("hero", "0 24px 70px rgba(0,0,0,.35)"),
        "--chrome-bg": palette["surfaceAlt"],
        "--chrome-border": palette["border"],
        "--chrome-text": palette["textMuted"],
        "--caption-color": palette["textMuted"],
        "--font-body": tokens["font"]["body"],
    }


def render(args, tokens):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        die("playwright not installed. Run: uv pip install playwright && playwright install chromium")

    src = Path(args.input)
    if not src.exists():
        die(f"input not found: {src}")
    if not TEMPLATE.exists():
        die(f"frame template missing: {TEMPLATE}")

    scale = args.scale or tokens.get("screenshot", {}).get("retinaScale", 2)
    css_vars = build_css_vars(tokens, args)
    style = ";".join(f"{k}:{v}" for k, v in css_vars.items())

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        # deviceScaleFactor drives retina output; the viewport is a starting
        # size only, since we screenshot the #stage element, not the page.
        page = browser.new_page(viewport={"width": 1600, "height": 1000},
                                device_scale_factor=scale)
        page.goto(TEMPLATE.as_uri())

        page.eval_on_selector(":root", "(el, s) => el.style.cssText = s", style)
        page.eval_on_selector("#shot", "(el, src) => el.src = src", data_uri(src))

        if args.chrome == "none":
            page.eval_on_selector("#window", "el => el.classList.add('bare')")
        elif args.chrome == "window":
            page.eval_on_selector("#urlbar", "el => el.style.display = 'none'")
            page.eval_on_selector("#title", "(el, t) => el.textContent = t",
                                  args.title or "")
        elif args.chrome == "browser":
            page.eval_on_selector("#title", "(el, t) => el.textContent = t",
                                  args.title or "")
            page.eval_on_selector("#url", "(el, u) => el.textContent = u",
                                  args.url or "")
            if not args.url:
                page.eval_on_selector("#urlbar", "el => el.style.display = 'none'")

        if args.caption:
            page.eval_on_selector("#caption", "(el, c) => el.textContent = c", args.caption)
        else:
            page.eval_on_selector("#caption", "el => el.remove()")

        # Wait for the inlined image to decode before measuring the stage.
        page.wait_for_function("() => { const i = document.getElementById('shot');"
                               " return i && i.complete && i.naturalWidth > 0; }")
        page.locator("#stage").screenshot(path=str(out))
        browser.close()

    size_kb = out.stat().st_size // 1024
    print(f"✓ {out}  ({size_kb}KB, {scale}x)")
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, help="raw screenshot")
    ap.add_argument("--output", "-o", required=True, help="framed PNG to write")
    ap.add_argument("--chrome", choices=["window", "browser", "none"], default="window",
                    help="macOS window bar, browser URL bar, or neither")
    ap.add_argument("--title", help="window title text")
    ap.add_argument("--url", help="URL shown in browser chrome")
    ap.add_argument("--caption", help="caption rendered under the frame")
    ap.add_argument("--backdrop", help="named gradient from brand tokens, or raw CSS")
    ap.add_argument("--mode", choices=["light", "dark"], default="light")
    ap.add_argument("--brand", help="brand profile (default: tokens.json default)")
    ap.add_argument("--padding", type=int, help="backdrop padding in px")
    ap.add_argument("--radius", type=int, help="corner radius in px")
    ap.add_argument("--scale", type=int, help="device scale factor (default 2 = retina)")
    args = ap.parse_args()

    try:
        tokens = brand.load(args.brand)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        die(str(exc))

    render(args, tokens)
    return 0


if __name__ == "__main__":
    sys.exit(main())
