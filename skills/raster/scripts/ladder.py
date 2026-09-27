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

"""Build a responsive size/format ladder from one source image.

A blog hero needs six files, not one: three widths across two formats, each
optimised, plus the `<img srcset>` line that ties them together. Doing that by
hand is where the 4MB PNG in production comes from.

    python3 ladder.py -i hero.png -o out/img/          # 480/960/1440, webp+png
    python3 ladder.py -i hero.png -o out/img/ --widths 400,800,1600 --formats webp,avif
    python3 ladder.py -i shots/ -o out/img/ --widths 800 --formats webp --recursive

Writes `<stem>-<width>.<ext>` plus a manifest.json carrying the srcset string.
Never upscales: a width above the source is skipped and said so, because a
blurry 2x asset is worse than none.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rasterlib as R  # noqa: E402

GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"

# Quality per format. AVIF and WebP are perceptually stronger per bit than JPEG,
# so matching JPEG's number would waste bytes for no visible gain.
QUALITY = {"webp": 82, "avif": 55, "jpg": 85, "jpeg": 85, "png": None}


def encode(src, dst, width, fmt, quality):
    """Resize + encode. vips is several times faster and uses far less memory
    on large images, so prefer it and fall back to ImageMagick."""
    quality = QUALITY.get(fmt) if quality is None else quality

    if R.have("vips") and fmt != "png":
        cmd = ["vips", "thumbnail", str(src),
               f"{dst}[Q={quality}]" if quality else str(dst), str(width)]
        R.run(cmd, f"vips thumbnail {src.name}")
        return

    cmd = R.magick() + [str(src), "-resize", f"{width}x", "-strip"]
    if quality:
        cmd += ["-quality", str(quality)]
    if fmt == "png":
        # PNG quality is a filter/compression hint, not a lossy knob; oxipng
        # below does the real work.
        cmd += ["-define", "png:compression-level=9"]
    cmd.append(str(dst))
    R.run(cmd, f"magick {src.name} -> {width}px {fmt}")


def optimise(path, fmt):
    """Lossless post-pass. Skipped silently when the tool is absent — a missing
    optimiser should cost bytes, not the build."""
    if fmt == "png" and R.have("oxipng"):
        R.run(["oxipng", "-o", "4", "--strip", "safe", "-q", str(path)],
              f"oxipng {path.name}")
        return "oxipng"
    if fmt in ("jpg", "jpeg") and R.have("jpegoptim"):
        R.run(["jpegoptim", "--strip-all", "-q", str(path)], f"jpegoptim {path.name}")
        return "jpegoptim"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, nargs="+",
                    help="image, glob or directory")
    ap.add_argument("--output", "-o", required=True, help="output directory")
    ap.add_argument("--widths", default="480,960,1440",
                    help="comma-separated target widths (default 480,960,1440)")
    ap.add_argument("--formats", default="webp,png",
                    help="comma-separated output formats (default webp,png)")
    ap.add_argument("--quality", type=int, default=None,
                    help="override the per-format default quality")
    ap.add_argument("--recursive", "-r", action="store_true",
                    help="descend into subdirectories")
    ap.add_argument("--base-url", default="",
                    help="URL prefix for the generated srcset, e.g. /img/")
    args = ap.parse_args()

    widths = sorted({int(w) for w in args.widths.split(",") if w.strip()})
    formats = [f.strip().lower().lstrip(".") for f in args.formats.split(",") if f.strip()]
    for fmt in formats:
        if f".{fmt}" not in R.RASTER_SUFFIXES:
            R.die(f"unsupported format '{fmt}'. "
                  f"Use: {', '.join(sorted(s[1:] for s in R.RASTER_SUFFIXES))}")

    sources = R.inputs(args.input, args.recursive)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {}
    for src in sources:
        src_w, src_h, _ = R.probe(src)
        print(f"\n▸ {src.name}  {DIM}{src_w}x{src_h}{RESET}")
        entries = []

        for width in widths:
            if width > src_w:
                print(f"    {YELLOW}!{RESET} {width}px skipped — source is only "
                      f"{src_w}px wide, upscaling would just blur it")
                continue
            for fmt in formats:
                dst = out_dir / f"{src.stem}-{width}.{fmt}"
                encode(src, dst, width, fmt, args.quality)
                tool = optimise(dst, fmt)
                size = dst.stat().st_size
                note = f"{DIM} ({tool}){RESET}" if tool else ""
                print(f"    {GREEN}✓{RESET} {dst.name:<34} {R.human(size)}{note}")
                entries.append({"file": dst.name, "width": width,
                                "format": fmt, "bytes": size})

        if not entries:
            continue

        # srcset uses the widest format listed first — browsers pick the first
        # type they support, so order is the negotiation.
        primary = formats[0]
        srcset = ", ".join(
            f"{args.base_url}{e['file']} {e['width']}w"
            for e in entries if e["format"] == primary)
        manifest[src.name] = {"source": {"width": src_w, "height": src_h},
                              "outputs": entries, "srcset": srcset}

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\n{GREEN}✓{RESET} {manifest_path}")

    if len(manifest) == 1:
        only = next(iter(manifest.values()))
        widest = max((e for e in only["outputs"]), key=lambda e: e["width"])
        print(f"\n{DIM}  <img src=\"{args.base_url}{widest['file']}\"")
        print(f"       srcset=\"{only['srcset']}\"")
        print(f"       sizes=\"(max-width: 960px) 100vw, 960px\" alt=\"\">{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
