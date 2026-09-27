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

"""Deterministic web screenshots for documentation.

The point is repeatability: same URL, same viewport, same hidden cookie banner,
same pixels — so docs screenshots can be regenerated in CI when the UI changes
instead of being re-taken by hand.

    # full page at retina
    python3 capture.py --url https://cloud.google.com/run --output run.png

    # just one element, dark mode, 1440 viewport
    python3 capture.py --url https://example.com --selector "main .hero" \\
        --width 1440 --dark --output hero.png

    # wait for an app to settle, then capture
    python3 capture.py --url http://localhost:3000 --wait-for ".chart-loaded" \\
        --output chart.png
"""

import argparse
import sys
from pathlib import Path

# Overlays that ruin documentation screenshots. Hidden, not clicked: clicking
# consent buttons varies per vendor and per region, hiding always works.
NOISE_SELECTORS = [
    "[id*='cookie' i]", "[class*='cookie' i]",
    "[id*='consent' i]", "[class*='consent' i]",
    "[id*='onetrust' i]", "#CybotCookiebotDialog",
    "[class*='banner' i][class*='privacy' i]",
    "[role='dialog'][aria-label*='cookie' i]",
]


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--selector", help="capture only this element")
    ap.add_argument("--wait-for", help="CSS selector to await before capturing")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--scale", type=int, default=2, help="device scale factor")
    ap.add_argument("--full-page", action="store_true")
    ap.add_argument("--dark", action="store_true", help="emulate dark colour scheme")
    ap.add_argument("--keep-banners", action="store_true",
                    help="do not hide cookie/consent overlays")
    ap.add_argument("--delay", type=float, default=0.4,
                    help="settle time in seconds after load")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright, Error as PWError
    except ImportError:
        die("playwright not installed. Run: uv pip install -r requirements.txt "
            "&& python -m playwright install chromium")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=args.scale,
            color_scheme="dark" if args.dark else "light",
        )
        # Freeze animations so repeat runs produce identical pixels.
        page.add_style_tag(content="""
            *, *::before, *::after {
              animation-duration: 0s !important;
              animation-delay: 0s !important;
              transition-duration: 0s !important;
              transition-delay: 0s !important;
            }
            html { scroll-behavior: auto !important; }
        """)

        try:
            page.goto(args.url, wait_until="networkidle", timeout=45_000)
        except PWError as exc:
            # networkidle never settles on pages with polling/websockets.
            print(f"⚠️  networkidle timed out, falling back to 'load': {str(exc)[:70]}")
            page.goto(args.url, wait_until="load", timeout=45_000)

        if args.wait_for:
            try:
                page.wait_for_selector(args.wait_for, timeout=30_000)
            except PWError:
                die(f"--wait-for selector never appeared: {args.wait_for}")

        if not args.keep_banners:
            page.eval_on_selector_all(
                ",".join(NOISE_SELECTORS),
                "els => els.forEach(e => e.style.setProperty('display','none','important'))",
            )

        page.wait_for_timeout(int(args.delay * 1000))

        if args.selector:
            locator = page.locator(args.selector).first
            if locator.count() == 0:
                die(f"selector matched nothing: {args.selector}")
            locator.screenshot(path=str(out))
        else:
            page.screenshot(path=str(out), full_page=args.full_page)

        browser.close()

    print(f"✓ {out}  ({out.stat().st_size // 1024}KB, {args.scale}x)")
    print(f"  Next: frame it →  python3 frame.py -i {out} -o {out.stem}-framed.png "
          f"--chrome browser --url {args.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
