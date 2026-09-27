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

"""Style and burn captions using the brand tokens.

Most social platforms autoplay muted, so burned-in captions are the difference
between a watched video and a scrolled-past one. YouTube and blog embeds prefer
a soft track instead — this does both.

    # burned in, brand-styled
    python3 captions.py -i cut.mp4 -s cut.srt -o captioned.mp4

    # soft track for YouTube (no re-encode of the video)
    python3 captions.py -i cut.mp4 -s cut.srt -o captioned.mp4 --soft

    # just look at the subtitle file the burn would use
    python3 captions.py -s cut.srt --ass-only -o captions.ass

Styles: `outline` (white text, dark outline — safest over busy screen
recordings), `box` (solid brand surface behind the text), `pill` (box, rounded
by margin padding — the social-video look).
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import hostenv  # noqa: E402
from vidlib import die, ffmpeg, parse_srt, probe, tokens

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{primary},{primary},{outline},{back},{bold},0,0,0,100,100,0,0,{border_style},{outline_w},{shadow},{align},{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_color(hex_color, alpha=0):
    """#RRGGBB -> &HAABBGGRR. ASS alpha is inverted: 00 opaque, FF transparent."""
    value = hex_color.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        die(f"not a hex colour: {hex_color}")
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H{alpha:02X}{blue}{green}{red}".upper()


def ass_time(seconds):
    """ASS wants H:MM:SS.cc — centiseconds, single-digit hour."""
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def escape_text(text, max_chars):
    """SRT markup out, ASS line breaks in."""
    text = re.sub(r"</?[bius]>", "", text)
    text = text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")
    lines = []
    for line in text.split("\n"):
        lines.extend(textwrap.wrap(line, max_chars) or [""])
    return "\\N".join(lines[:3])          # 3 lines is already too many on screen


def build_ass(cues, meta, style, opts):
    palette = opts["tokens"]["color"]["dark"]
    video = opts["tokens"]["video"]
    height = meta["height"] or video["resolution"][1]
    width = meta["width"] or video["resolution"][0]

    # Token sizes are authored against a 1080p frame; scale to the real one.
    scale = height / video["resolution"][1]
    size = opts["size"] or round(video["captionSize"] * scale)
    margin_v = opts["margin"] or round(video["safeMargin"] * scale * 1.4)

    border_style, outline_w, shadow, back = 1, max(2, round(3 * scale)), 0, ass_color("#000000", 0x60)
    if style in ("box", "pill"):
        border_style = 3
        outline_w = round((10 if style == "pill" else 6) * scale)
        shadow = 0
        back = ass_color(palette["bg"], 0x1A)

    lines = [ASS_HEADER.format(
        width=width, height=height,
        font=opts["font"], size=size,
        primary=ass_color(opts["color"] or palette["text"]),
        outline=back if border_style == 3 else ass_color("#000000", 0x20),
        back=back,
        bold=-1 if opts["bold"] else 0,
        border_style=border_style, outline_w=outline_w, shadow=shadow,
        align=8 if opts["position"] == "top" else 2,
        margin_h=round(video["safeMargin"] * scale),
        margin_v=margin_v,
    )]
    for cue in cues:
        lines.append(f"Dialogue: 0,{ass_time(cue['start'])},{ass_time(cue['end'])},"
                     f"Default,,0,0,0,,{escape_text(cue['text'], opts['max_chars'])}")
    return "\n".join(lines) + "\n"


def filter_path(path):
    """Escape a path for use inside an ffmpeg filter argument."""
    return str(path).replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")


def check_font(name):
    """A missing font silently falls back to something ugly; say so up front."""
    if not shutil.which("fc-match"):
        return
    proc = subprocess.run(["fc-match", "-f", "%{family}", name],
                          capture_output=True, text=True)
    resolved = proc.stdout.strip()
    if resolved and name.split(",")[0].strip().lower() not in resolved.lower():
        print(f"⚠️  font '{name}' not installed — libass will substitute "
              f"'{resolved}'. Install it: {hostenv.font_install_hint(name)}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", help="video to caption (omit with --ass-only)")
    ap.add_argument("--srt", "-s", required=True)
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--style", default="outline", choices=["outline", "box", "pill"])
    ap.add_argument("--position", default="bottom", choices=["bottom", "top"])
    ap.add_argument("--soft", action="store_true",
                    help="mux a selectable subtitle track instead of burning in")
    ap.add_argument("--ass-only", action="store_true",
                    help="write the .ass and stop")
    ap.add_argument("--brand", default=None, help="brand profile")
    ap.add_argument("--font", default=None, help="override the brand caption font")
    ap.add_argument("--size", type=int, default=0, help="override caption size in px")
    ap.add_argument("--color", default=None, help="override text colour, #RRGGBB")
    ap.add_argument("--margin", type=int, default=0,
                    help="override distance from the frame edge in px")
    ap.add_argument("--max-chars", type=int, default=42)
    ap.add_argument("--no-bold", dest="bold", action="store_false", default=True)
    ap.add_argument("--crf", type=int, default=18, help="quality of the burn re-encode")
    args = ap.parse_args()

    srt = Path(args.srt)
    if not srt.exists():
        die(f"transcript not found: {srt}")
    cues = parse_srt(srt)
    if not cues:
        die(f"{srt} has no cues.")

    theme = tokens(args.brand)
    if args.input:
        meta = probe(args.input)
    elif args.ass_only:
        meta = {"width": theme["video"]["resolution"][0],
                "height": theme["video"]["resolution"][1], "has_audio": False}
    else:
        die("--input is required unless --ass-only is set")

    font = args.font or theme["video"]["captionFont"]
    check_font(font)

    ass = build_ass(cues, meta, args.style, {
        "tokens": theme, "font": font, "size": args.size, "color": args.color,
        "margin": args.margin, "max_chars": args.max_chars,
        "position": args.position, "bold": args.bold,
    })

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    if args.ass_only:
        output.write_text(ass, encoding="utf-8")
        print(f"✓ {output}  ({len(cues)} cues, style={args.style})")
        return 0

    if args.soft:
        # mov_text for .mp4/.mov, WebVTT for .webm — anything else gets srt.
        codec = {".mp4": "mov_text", ".m4v": "mov_text", ".mov": "mov_text",
                 ".webm": "webvtt"}.get(output.suffix.lower(), "srt")
        ffmpeg(["-i", args.input, "-i", str(srt), "-map", "0", "-map", "1",
                "-c", "copy", "-c:s", codec,
                "-metadata:s:s:0", "language=eng",
                "-disposition:s:0", "default", "-y", str(output)])
        print(f"✓ {output}  (soft {codec} track, video stream copied)")
        return 0

    with tempfile.TemporaryDirectory(prefix="vidkit-ass-") as tmp:
        ass_path = Path(tmp) / "captions.ass"
        ass_path.write_text(ass, encoding="utf-8")
        args_ff = ["-i", args.input,
                   "-vf", f"ass='{filter_path(ass_path)}'",
                   "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf),
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        args_ff += ["-c:a", "copy"] if meta["has_audio"] else ["-an"]
        print(f"🔥 burning {len(cues)} cues at {meta['width']}x{meta['height']} …")
        ffmpeg([*args_ff, "-y", str(output)])

    print(f"✓ {output}  ({output.stat().st_size // 1024}KB, style={args.style})")
    print(f"  Next: encode ladder + thumbnail →  python3 publish.py -i {output} "
          "-o out/publish")
    return 0


if __name__ == "__main__":
    sys.exit(main())
