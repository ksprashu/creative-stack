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

"""Render brand-styled motion graphics — title cards, lower thirds, end cards.

Templates are plain HTML/CSS in templates/motion/. Headless Chromium renders
them frame by frame and ffmpeg encodes the frames, so the output is exactly
reproducible: every CSS animation is paused and seeked to each frame's time
through the Web Animations API rather than played in real time.

    # a title card
    python3 motion.py -t title -o out/title.mp4 \\
      --param kicker="Cloud Run" --param title="Deploy in 90 seconds"

    # a lower third as a transparent overlay for an NLE (ProRes 4444)
    python3 motion.py -t lower-third -o out/lt.mov --alpha \\
      --param name="Ada Lovelace" --param role="Developer Advocate"

    # ... or composited straight onto the footage at 3.5s
    python3 motion.py -t lower-third --overlay-on out/cut.mp4 --at 3.5 \\
      -o out/cut-lt.mp4 --param name="Ada Lovelace" --param role="DevRel"

    # review the resolved spec, edit it, render from it
    python3 motion.py -t end-card --param title="Thanks for watching" \\
      --spec-out out/end.json
    python3 motion.py --spec out/end.json -o out/end.mp4

Outputs by extension: .mp4 is opaque H.264; .mov is ProRes (4444 with alpha
under --alpha); .webm with --alpha is VP9 with an alpha channel. Every render
writes its resolved spec next to the output as <name>.motion.json first.

Templates: title (kicker, title, subtitle, bg), lower-third (name, role, side),
end-card (logo, title, cta, url). Write your own: any HTML in templates/motion/
that links motion.css and motion.js gets the brand tokens as CSS variables and
its `data-param` elements filled.
"""

import argparse
import base64
import json
import mimetypes
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from vidlib import REPO_ROOT, die, ffmpeg, hms, need, probe, tokens

TEMPLATES = REPO_ROOT / "templates/motion"
DEFAULT_DURATION = {"title": 4.0, "lower-third": 5.0, "end-card": 5.0}

# Pause everything, then seek. Animations are re-collected on every frame
# because one can start mid-clip (a class added from script, an element
# un-hidden) and would otherwise play in real time and break determinism.
SEEK_JS = """async (t) => {
  for (const a of document.getAnimations()) { a.pause(); a.currentTime = t * 1000; }
  if (window.seek) await window.seek(t);
}"""


def data_uri(path):
    """Inline images so the file:// page needs no extra origin permissions."""
    path = Path(path).expanduser()
    if not path.exists():
        die(f"image not found: {path}")
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def css_vars(theme, mode):
    """Brand tokens -> the custom properties every motion template reads."""
    palette = theme["color"][mode]
    brand_colors = [v for k, v in theme["color"].get("brand", {}).items()
                    if not k.startswith("$")]
    brand_colors = (brand_colors or [palette["accent"]] * 4)
    brand_colors = (brand_colors * 4)[:4]
    out = {
        "--bg": palette["bg"],
        "--surface": palette["surface"],
        "--on-bg": palette["text"],
        "--on-surface": palette["text"],
        "--muted": palette["textMuted"],
        "--accent": palette["accent"],
        "--on-accent": palette["bg"],
        "--font-display": theme["font"]["display"],
        "--font-body": theme["font"]["body"],
        "--font-mono": theme["font"]["mono"],
        "--radius": str(theme.get("radius", {}).get("lg", 16)),
        "--safe": str(theme.get("video", {}).get("safeMargin", 64)),
    }
    for index, color in enumerate(brand_colors, 1):
        out[f"--brand-{index}"] = color
    for name, value in theme.get("gradient", {}).items():
        if not name.startswith("$"):
            out[f"--gradient-{name}"] = value
    return out


def parse_params(pairs):
    params = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep:
            die(f"--param wants key=value, got: {pair}")
        params[key.strip()] = value
    return params


def build_spec(args):
    """Everything the render needs, resolved — the reviewable intermediate."""
    theme = tokens(args.brand)
    video = theme.get("video", {})
    width, height = video.get("resolution", [1920, 1080])
    fps = video.get("fps", 30)
    if args.overlay_on:
        meta = probe(args.overlay_on)
        width, height, fps = meta["width"], meta["height"], meta["fps"]
    if args.size:
        try:
            width, height = (int(v) for v in args.size.lower().split("x"))
        except ValueError:
            die(f"--size wants WIDTHxHEIGHT, got: {args.size}")
    fps = args.fps or fps

    params = {}
    if args.params:
        params.update(json.loads(Path(args.params).read_text()))
    params.update(parse_params(args.param))
    logo = params.get("logo")
    if logo and not str(logo).startswith("data:"):
        params["logo"] = data_uri(logo)
    elif not logo and args.template == "end-card":
        mark = REPO_ROOT / theme.get("logo", {}).get("mark", "")
        if theme.get("logo", {}).get("mark") and mark.is_file():
            params["logo"] = data_uri(mark)

    variables = css_vars(theme, args.mode)
    if params.get("bg"):
        bg = params["bg"]
        variables["--title-bg"] = ("transparent" if bg == "none"
                                   else variables.get(f"--gradient-{bg}", bg))

    return {
        "version": 1,
        "template": args.template,
        "duration": args.duration or DEFAULT_DURATION.get(args.template, 5.0),
        "fps": fps,
        "width": width - width % 2,      # yuv420p needs even dimensions
        "height": height - height % 2,
        "mode": args.mode,
        "alpha": bool(args.alpha or args.overlay_on),
        "params": params,
        "vars": variables,
    }


# JPEG frames decode as full-range yuvj420p, and `-pix_fmt yuv420p` alone keeps
# that range tag — players that honour it show the title washed out next to
# the camera footage it is cut against. Squeeze to broadcast range and say so.
RANGE_ARGS = ["-vf", "scale=out_range=tv:out_color_matrix=bt709",
              "-color_range", "tv", "-colorspace", "bt709",
              "-color_primaries", "bt709", "-color_trc", "bt709"]


def encoder_args(output, alpha):
    suffix = output.suffix.lower()
    if suffix == ".mov":
        if alpha:
            return ["-c:v", "prores_ks", "-profile:v", "4444",
                    "-pix_fmt", "yuva444p10le", "-vendor", "apl0"]
        return ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le",
                "-vendor", "apl0"]
    if suffix == ".webm":
        return ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "28", "-row-mt", "1",
                "-pix_fmt", "yuva420p" if alpha else "yuv420p"]
    if alpha:
        die(f"{suffix or 'that extension'} cannot carry alpha — use .mov "
            "(ProRes 4444, for an NLE) or .webm (VP9, for the web).")
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "16",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart"]


def render(spec, output):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        die("playwright not installed. Run: uv pip install playwright && "
            "playwright install chromium")

    template = TEMPLATES / f"{spec['template']}.html"
    if not template.exists():
        names = sorted(p.stem for p in TEMPLATES.glob("*.html"))
        die(f"no template '{spec['template']}' — have: {', '.join(names)}")

    frames = max(1, round(spec["duration"] * spec["fps"]))
    page_spec = {k: spec[k] for k in ("vars", "params", "duration", "fps", "alpha")}
    if not spec["alpha"]:
        page_spec["vars"] = {**spec["vars"], "--page-bg": spec["vars"]["--bg"]}

    # PNG only when the alpha channel has to survive. Chromium spends ~250ms
    # PNG-compressing a frame-sized gradient, and the frames are re-encoded to
    # H.264 anyway, so opaque renders ship q95 JPEG: ~4x faster, no visible loss.
    shot = ({"type": "png", "omit_background": True} if spec["alpha"]
            else {"type": "jpeg", "quality": 95})
    cmd = [need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "image2pipe", "-framerate", str(spec["fps"]),
           "-c:v", "png" if spec["alpha"] else "mjpeg",
           "-i", "-", *([] if spec["alpha"] else RANGE_ARGS),
           *encoder_args(output, spec["alpha"]),
           "-r", str(spec["fps"]), "-y", str(output)]

    started = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": spec["width"], "height": spec["height"]},
            device_scale_factor=1)
        page.add_init_script(f"window.MOTION = {json.dumps(page_spec)};")
        page.goto(template.as_uri())
        page.evaluate("document.fonts.ready")
        encoder = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        try:
            for index in range(frames):
                page.evaluate(SEEK_JS, index / spec["fps"])
                encoder.stdin.write(page.screenshot(**shot))
                if index % 30 == 0:
                    print(f"\r  frame {index + 1}/{frames}", end="", flush=True)
        except BrokenPipeError:
            pass
        finally:
            encoder.stdin.close()
            err = encoder.stderr.read().decode(errors="replace")
            encoder.wait()
            browser.close()
    print(f"\r  frame {frames}/{frames}")
    if encoder.returncode != 0:
        die(f"ffmpeg failed encoding {output.name}:\n  " + err.strip()[-800:])
    return time.monotonic() - started


def composite(video, overlay, at, output):
    """Lay a rendered alpha clip over footage, starting `at` seconds in."""
    meta = probe(video)
    graph = (f"[1:v]setpts=PTS-STARTPTS+{at}/TB[ov];"
             "[0:v][ov]overlay=eof_action=pass:format=auto,format=yuv420p[v]")
    args = ["-i", str(video), "-i", str(overlay), "-filter_complex", graph,
            "-map", "[v]", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-movflags", "+faststart"]
    if meta["has_audio"]:
        args += ["-map", "0:a", "-c:a", "copy"]
    ffmpeg([*args, "-y", str(output)])


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--template", "-t", help="template name in templates/motion/")
    ap.add_argument("--output", "-o", help=".mp4, .mov or .webm")
    ap.add_argument("--param", "-p", action="append", metavar="KEY=VALUE",
                    help="template parameter (repeatable)")
    ap.add_argument("--params", help="JSON file of template parameters")
    ap.add_argument("--duration", "-d", type=float, default=0,
                    help="seconds (default: per template)")
    ap.add_argument("--fps", type=float, default=0, help="default: brand video fps")
    ap.add_argument("--size", help="WIDTHxHEIGHT (default: brand video resolution)")
    ap.add_argument("--mode", default="dark", choices=["dark", "light"])
    ap.add_argument("--brand", default=None, help="brand profile")
    ap.add_argument("--alpha", action="store_true",
                    help="transparent background (.mov or .webm)")
    ap.add_argument("--overlay-on", metavar="VIDEO",
                    help="composite onto this video instead of writing a bare clip")
    ap.add_argument("--at", type=float, default=0.0,
                    help="with --overlay-on: start time in seconds")
    ap.add_argument("--spec", help="render from a reviewed spec JSON")
    ap.add_argument("--spec-out", help="write the resolved spec and stop")
    args = ap.parse_args()

    if args.spec:
        spec = json.loads(Path(args.spec).read_text())
        if spec.get("version") != 1:
            die(f"{args.spec}: unknown spec version {spec.get('version')}")
        if args.overlay_on or args.alpha:
            spec["alpha"] = True
    else:
        if not args.template:
            die("--template is required (or --spec)")
        spec = build_spec(args)

    if args.spec_out:
        Path(args.spec_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.spec_out).write_text(json.dumps(spec, indent=2) + "\n")
        print(f"✓ {args.spec_out}  (edit, then: motion.py --spec {args.spec_out} -o …)")
        return 0
    if not args.output:
        die("--output is required unless --spec-out is set")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not args.spec:
        sidecar = output.with_suffix(".motion.json")
        sidecar.write_text(json.dumps(spec, indent=2) + "\n")

    print(f"🎬 {spec['template']}  {spec['width']}x{spec['height']} "
          f"@{spec['fps']:g}fps  {spec['duration']:g}s"
          f"{'  alpha' if spec['alpha'] else ''}")

    if args.overlay_on:
        with tempfile.TemporaryDirectory(prefix="vidkit-motion-") as tmp:
            clip = Path(tmp) / "overlay.mov"
            took = render(spec, clip)
            print(f"🧩 compositing onto {Path(args.overlay_on).name} at {hms(args.at)}")
            composite(args.overlay_on, clip, args.at, output)
    else:
        took = render(spec, output)

    print(f"✓ {output}  ({output.stat().st_size // 1024}KB, rendered in {took:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
