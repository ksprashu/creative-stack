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

Animated styles, word by word, still one libass pass rather than a rendered
overlay: `karaoke` sweeps the brand accent through each line as it is spoken
(ASS \\kf); `highlight` lights only the current word, with a slight pop.

    python3 captions.py -i cut.mp4 -s cut.srt -o out.mp4 --style highlight \\
      --words talk.json

Word timings come from whisper.cpp's full JSON (`-ojf`) via --words. Without it,
or where the .srt was edited so its words no longer match, each cue's time is
shared across its words by length — close enough to read naturally, and the
edited .srt always wins over the transcript.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from vidlib import die, ffmpeg, parse_srt, probe, tokens
import hostenv  # noqa: E402  - importable once vidlib has set sys.path

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{primary},{secondary},{outline},{back},{bold},0,0,0,100,100,0,0,{border_style},{outline_w},{shadow},{align},{margin_h},{margin_h},{margin_v},1

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


def tag_color(hex_color):
    """#RRGGBB -> the &HBBGGRR& form an inline \\c override wants."""
    return "&H" + ass_color(hex_color)[4:] + "&"


def ass_word(text):
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def load_words(path):
    """[[start, end, word]] from whisper.cpp's -ojf JSON.

    whisper splits words into BPE tokens; a token that starts with a space
    begins a word, anything else (`through` after ` Walk`, a trailing `.`)
    continues the previous one. `[_BEG_]`-style specials are dropped.
    """
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    words = []
    for segment in doc.get("transcription", []):
        for token in segment.get("tokens", []):
            text = token.get("text", "")
            if text.startswith("[_") or not text.strip():
                continue
            start = token["offsets"]["from"] / 1000
            end = token["offsets"]["to"] / 1000
            if text.startswith(" ") or not words:
                words.append([start, end, text.strip()])
            else:
                words[-1][1] = end
                words[-1][2] += text
    if not words:
        die(f"{path} has no token timings. It needs whisper's full JSON: "
            "whisper-cli … -ojf (transcribe.py's plain -oj has none).")
    return words


def time_words(cue, words):
    """[(start, end, word)] for one cue.

    The transcript's timings are used only when they line up word-for-word
    with the cue — after autocut shifts the timeline, or after a hand edit of
    the .srt, they would highlight the wrong word, which is worse than an
    estimate. The estimate shares the cue by word length, plus one for the gap.
    """
    text = re.sub(r"</?[bius]>", "", cue["text"]).split()
    if not text:
        return []
    if words:
        hits = [w for w in words if cue["start"] - 0.15 <= w[0] < cue["end"]]
        if len(hits) == len(text):
            return [(max(w[0], cue["start"]), min(w[1], cue["end"]), word)
                    for w, word in zip(hits, text)]
    weights = [len(word) + 1 for word in text]
    span, at, out = cue["end"] - cue["start"], cue["start"], []
    for word, weight in zip(text, weights):
        length = span * weight / sum(weights)
        out.append((at, at + length, word))
        at += length
    return out


def wrap_words(words, max_chars):
    """Line-break indices for a word list, mirroring textwrap's greedy fill."""
    breaks, width = set(), 0
    for index, (_, _, word) in enumerate(words):
        if width and width + 1 + len(word) > max_chars:
            breaks.add(index)
            width = len(word)
        else:
            width += len(word) + (1 if width else 0)
    return breaks


def animated_events(cue, words, style, accent, max_chars):
    """Dialogue texts with start/end for one cue in karaoke or highlight style."""
    timed = time_words(cue, words)
    if not timed:
        return []
    breaks = wrap_words(timed, max_chars)

    def joined(parts):
        out = ""
        for index, part in enumerate(parts):
            if index:
                out += "\\N" if index in breaks else " "
            out += part
        return out

    if style == "karaoke":
        # \kf sweeps SecondaryColour -> PrimaryColour over the word. Each word
        # holds until the next starts, so the sweep never stalls mid-line.
        lead = round((timed[0][0] - cue["start"]) * 100)
        parts = []
        for index, (start, end, word) in enumerate(timed):
            until = timed[index + 1][0] if index + 1 < len(timed) else end
            parts.append(f"{{\\kf{max(1, round((until - start) * 100))}}}{ass_word(word)}")
        text = (f"{{\\k{lead}}}" if lead > 0 else "") + joined(parts)
        return [(cue["start"], cue["end"], text)]

    # highlight: one event per word, the whole line visible, the live word lit.
    pop = f"{{\\c{tag_color(accent)}\\fscx108\\fscy108}}"
    plain = [ass_word(w) for _, _, w in timed]
    events = []
    if timed[0][0] > cue["start"] + 0.01:
        events.append((cue["start"], timed[0][0], joined(plain)))
    for index, (start, _, _) in enumerate(timed):
        until = timed[index + 1][0] if index + 1 < len(timed) else cue["end"]
        lit = [f"{pop}{w}{{\\r}}" if i == index else w for i, w in enumerate(plain)]
        events.append((start, until, joined(lit)))
    return events


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

    text_color = ass_color(opts["color"] or palette["text"])
    accent = opts.get("accent") or palette["accent"]
    animated = style in ("karaoke", "highlight")
    if style == "karaoke":
        # Sung words take PrimaryColour, unsung ones SecondaryColour.
        primary, secondary = ass_color(accent), text_color
    else:
        primary = secondary = text_color

    lines = [ASS_HEADER.format(
        width=width, height=height,
        font=opts["font"], size=size,
        primary=primary, secondary=secondary,
        outline=back if border_style == 3 else ass_color("#000000", 0x20),
        back=back,
        bold=-1 if opts["bold"] else 0,
        border_style=border_style, outline_w=outline_w, shadow=shadow,
        align=8 if opts["position"] == "top" else 2,
        margin_h=round(video["safeMargin"] * scale),
        margin_v=margin_v,
    )]
    for cue in cues:
        if animated:
            for start, end, text in animated_events(
                    cue, opts.get("words"), style, accent, opts["max_chars"]):
                lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},"
                             f"Default,,0,0,0,,{text}")
            continue
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
    ap.add_argument("--style", default="outline",
                    choices=["outline", "box", "pill", "karaoke", "highlight"])
    ap.add_argument("--words", help="whisper.cpp full JSON (-ojf) for real word "
                    "timings in karaoke/highlight; estimated without it")
    ap.add_argument("--accent", default=None,
                    help="karaoke/highlight colour, #RRGGBB (default: brand accent)")
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

    words = load_words(args.words) if args.words else None
    if words is not None and args.style not in ("karaoke", "highlight"):
        print("⚠️  --words only affects --style karaoke / highlight")

    ass = build_ass(cues, meta, args.style, {
        "words": words, "accent": args.accent,
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
