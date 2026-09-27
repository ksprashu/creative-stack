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

"""Annotate a screenshot from a declarative YAML spec.

Replaces the manual Snagit pass: numbered step callouts, highlight boxes,
arrows, and redaction of secrets — all reproducible, diffable and re-runnable
when the UI changes.

Coordinates are fractions of image width/height (0.0-1.0) by default, so a spec
survives a resolution change. Pass --pixels to use absolute coordinates.

    python3 annotate.py --input raw.png --spec steps.yaml --output annotated.png

Spec format:

    # steps.yaml
    annotations:
      - type: box
        at: [0.05, 0.12, 0.40, 0.22]     # x, y, w, h
        label: "1"                        # optional numbered badge
      - type: arrow
        from: [0.50, 0.30]
        to:   [0.62, 0.44]
      - type: text
        at: [0.55, 0.20]
        text: "Click Deploy"
      - type: redact
        at: [0.70, 0.05, 0.28, 0.06]
        style: blur                       # blur | block | pixelate
"""

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("CREATIVE_STACK_ROOT",
                                Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import brand  # noqa: E402
import hostenv  # noqa: E402


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def _font(size):
    """Find a real UI font; PIL's default bitmap font is unusably small."""
    from PIL import ImageFont
    path = hostenv.font_file("Roboto", bold=True)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


class Canvas:
    """Wraps a PIL image with fraction->pixel coordinate resolution."""

    def __init__(self, image, use_pixels, tokens, mode):
        from PIL import ImageDraw
        self.img = image
        self.draw = ImageDraw.Draw(image, "RGBA")
        self.use_pixels = use_pixels
        self.w, self.h = image.size
        palette = tokens["color"][mode]
        self.accent = palette["accent"]
        self.on_accent = "#FFFFFF"
        # Stroke scales with image size so a 4K shot is not hairlined.
        self.stroke = max(3, round(min(self.w, self.h) / 320))

    def pt(self, xy):
        x, y = xy
        return (round(x if self.use_pixels else x * self.w),
                round(y if self.use_pixels else y * self.h))

    def rect(self, box):
        x, y, w, h = box
        if self.use_pixels:
            return [round(x), round(y), round(x + w), round(y + h)]
        return [round(x * self.w), round(y * self.h),
                round((x + w) * self.w), round((y + h) * self.h)]


def draw_box(c, ann):
    box = c.rect(ann["at"])
    color = ann.get("color", c.accent)
    c.draw.rounded_rectangle(box, radius=c.stroke * 2, outline=color, width=c.stroke)
    if ann.get("label"):
        _badge(c, (box[0], box[1]), str(ann["label"]), color)


def _badge(c, anchor, text, color):
    """Numbered circle pinned to a box corner, centred on the corner itself."""
    r = max(16, c.stroke * 7)
    cx, cy = anchor
    c.draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    font = _font(int(r * 1.15))
    bbox = c.draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    c.draw.text((cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1]),
                text, fill=c.on_accent, font=font)


def draw_arrow(c, ann):
    import math
    x0, y0 = c.pt(ann["from"])
    x1, y1 = c.pt(ann["to"])
    color = ann.get("color", c.accent)
    c.draw.line([x0, y0, x1, y1], fill=color, width=c.stroke)

    # Arrowhead as a filled triangle rotated to match the line.
    head = c.stroke * 5
    angle = math.atan2(y1 - y0, x1 - x0)
    spread = math.radians(26)
    c.draw.polygon([
        (x1, y1),
        (x1 - head * math.cos(angle - spread), y1 - head * math.sin(angle - spread)),
        (x1 - head * math.cos(angle + spread), y1 - head * math.sin(angle + spread)),
    ], fill=color)


def draw_text(c, ann):
    x, y = c.pt(ann["at"])
    color = ann.get("color", c.accent)
    size = ann.get("size") or max(18, round(c.h / 30))
    font = _font(size)
    text = ann["text"]

    # Pill background keeps the label legible over any screenshot content.
    pad = size // 2
    bbox = c.draw.textbbox((x, y), text, font=font)
    c.draw.rounded_rectangle(
        [bbox[0] - pad, bbox[1] - pad // 2, bbox[2] + pad, bbox[3] + pad // 2],
        radius=(bbox[3] - bbox[1]) // 2 + pad // 2, fill=color)
    c.draw.text((x, y), text, fill=c.on_accent, font=font)


def draw_redact(c, ann):
    from PIL import ImageFilter
    box = c.rect(ann["at"])
    style = ann.get("style", "blur")
    region = c.img.crop(box)

    if style == "block":
        c.draw.rectangle(box, fill="#202124")
        return
    if style == "pixelate":
        small = region.resize((max(1, region.width // 20), max(1, region.height // 20)))
        region = small.resize(region.size, 0)  # NEAREST
    else:
        # Radius proportional to region size — a fixed radius fails to hide
        # text in large regions and destroys small ones.
        region = region.filter(
            ImageFilter.GaussianBlur(max(6, min(region.size) // 6)))
    c.img.paste(region, box[:2])


HANDLERS = {"box": draw_box, "arrow": draw_arrow, "text": draw_text, "redact": draw_redact}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--spec", "-s", required=True, help="YAML annotation spec")
    ap.add_argument("--pixels", action="store_true",
                    help="treat coordinates as absolute pixels, not fractions")
    ap.add_argument("--mode", choices=["light", "dark"], default="light")
    ap.add_argument("--brand", help="brand profile")
    args = ap.parse_args()

    try:
        import yaml
        from PIL import Image
    except ImportError as exc:
        die(f"missing dependency: {exc.name}. Run: uv pip install -r requirements.txt")

    src, spec_path = Path(args.input), Path(args.spec)
    if not src.exists():
        die(f"input not found: {src}")
    if not spec_path.exists():
        die(f"spec not found: {spec_path}")

    spec = yaml.safe_load(spec_path.read_text()) or {}
    annotations = spec.get("annotations", [])
    if not annotations:
        die(f"{spec_path} has no 'annotations:' list")

    try:
        tokens = brand.load(args.brand)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        die(str(exc))

    image = Image.open(src).convert("RGBA")
    canvas = Canvas(image, args.pixels, tokens, args.mode)

    # Redactions first: later annotations must draw on top, never be blurred.
    ordered = sorted(annotations, key=lambda a: a.get("type") != "redact")

    for i, ann in enumerate(ordered, 1):
        kind = ann.get("type")
        handler = HANDLERS.get(kind)
        if handler is None:
            die(f"annotation {i}: unknown type '{kind}'. "
                f"Valid: {', '.join(sorted(HANDLERS))}")
        try:
            handler(canvas, ann)
        except KeyError as exc:
            die(f"annotation {i} ({kind}): missing required field {exc}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(out)
    print(f"✓ {out}  ({len(annotations)} annotation(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
