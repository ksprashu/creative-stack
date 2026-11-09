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

"""Find secrets in a screen recording and blur them for as long as they show.

/shot's scan.py, run over time: frames are sampled, each one is OCR'd with
the same rules (keys, labelled values, emails, project ids, cards, random
strings), and a hit seen at the same place in consecutive samples becomes one
track with a start, an end and a box.

    python3 vidscan.py -i out/cut.mp4 --json out/scan.json        # report; review
    python3 vidscan.py -i out/cut.mp4 --from-json out/scan.json --render out/redacted.mp4
    python3 vidscan.py -i out/captioned.mp4 --check               # exit 1 if anything is readable

Sampling is plain `--fps` (default 1): a secret on screen for less than one
sample interval can fall between samples, so raise it for fast-moving demos
or run `--check` at a higher rate on the result. Each track is widened by a
whole interval on both sides, because the text may have appeared any time
after the previous sample — the blur arrives early rather than late.

As with scan.py, matched text is never printed or written in full.
"""

import argparse
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

from vidlib import REPO_ROOT, die, ffmpeg, filter_script_args, hms, need, probe

sys.path.insert(0, str(REPO_ROOT / "skills/shot/scripts"))
import scan  # noqa: E402

GREEN, YELLOW, DIM, RESET = scan.GREEN, scan.YELLOW, scan.DIM, scan.RESET

# Samples of one secret move a little (scrolling, OCR jitter); boxes that
# share at least this much of the smaller one's area are the same place.
SAME_PLACE = 0.5
# A track survives this many empty samples: OCR misses a word on one frame
# and finds it on the next, which is still one secret, not two.
GAP_SAMPLES = 1
# boxblur's radius may not exceed half the blurred area's short side (a
# quarter, for the half-size chroma planes); past this it looks no different.
MAX_BLUR_RADIUS = 20


# ------------------------------------------------------------- tracking ---

def overlap(a, b):
    """Shared area as a fraction of the smaller box's area."""
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    smaller = min(a[2] * a[3], b[2] * b[3])
    return ix * iy / smaller if smaller > 0 else 0.0


def tracks(samples, interval):
    """Join per-frame hits into tracks.

    `samples` is [(time, findings)] in time order, each finding a scan.py
    {kind, preview, box}. A hit continues a track of the same kind whose box
    it overlaps, if that track was seen within GAP_SAMPLES samples; otherwise
    it starts a new one. Returns [{kind, preview, start, end, box}] with
    start/end the first and last sample times."""
    done, live = [], []
    for t, findings in samples:
        for track in [k for k in live if t - k["end"] > interval * (GAP_SAMPLES + 1) + 1e-6]:
            live.remove(track)
            done.append(track)
        for f in findings:
            match = next((k for k in live if k["kind"] == f["kind"]
                          and overlap(k["box"], f["box"]) >= SAME_PLACE), None)
            if match:
                match["box"] = scan.union([match["box"], f["box"]])
                match["end"] = t
            else:
                live.append({"kind": f["kind"], "preview": f["preview"],
                             "start": t, "end": t, "box": list(f["box"])})
    return sorted(done + live, key=lambda k: (k["start"], k["box"][1]))


def widen(track, interval, duration, size):
    """Pad the track in time (one interval each side, clamped) and in space
    (scan.py's padding, rounded out to even pixels for yuv420 crops).
    A `duration` of 0 means the container did not say (common for WebM and
    MKV captures), so the end is left unclamped."""
    x, y, w, h = scan.pad(track["box"], size)
    x0, y0 = x - x % 2, y - y % 2
    x1 = min(size[0] - size[0] % 2, x + w + (x + w) % 2)
    y1 = min(size[1] - size[1] % 2, y + h + (y + h) % 2)
    return {**track,
            "start": round(max(0.0, track["start"] - interval), 3),
            "end": round(min(duration or math.inf, track["end"] + interval), 3),
            "box": [x0, y0, max(2, x1 - x0), max(2, y1 - y0)]}


# ------------------------------------------------------------- render ---

def blur_graph(found):
    """An ffmpeg filter graph that blurs each track's box during its span.

    One crop+boxblur branch per track, overlaid back in place only while
    `between(t, start, end)`. Output label: [vout]."""
    n = len(found)
    lines = [f"[0:v]split={n + 1}[base]" + "".join(f"[c{i}]" for i in range(n)) + ";"]
    prev = "base"
    for i, k in enumerate(found):
        x, y, w, h = k["box"]
        luma = max(1, min(MAX_BLUR_RADIUS, min(w, h) // 2 - 1))
        chroma = max(1, min(luma, min(w, h) // 4 - 1))
        out = "vout" if i == n - 1 else f"v{i}"
        lines.append(f"[c{i}]crop={w}:{h}:{x}:{y},"
                     f"boxblur=luma_radius={luma}:luma_power=3:"
                     f"chroma_radius={chroma}:chroma_power=3[b{i}];")
        lines.append(f"[{prev}][b{i}]overlay={x}:{y}:"
                     f"enable='between(t,{k['start']},{k['end']})'[{out}];")
        prev = out
    lines[-1] = lines[-1].rstrip(";")
    return "\n".join(lines) + "\n"


def render(src, found, out, meta):
    """Encode `src` with every track blurred; audio is copied untouched."""
    with tempfile.TemporaryDirectory(prefix="vidscan-") as tmp:
        graph = Path(tmp) / "graph.txt"
        graph.write_text(blur_graph(found))
        args = ["-y", "-i", str(src), *filter_script_args(graph), "-map", "[vout]"]
        args += ["-map", "0:a", "-c:a", "copy"] if meta["has_audio"] else ["-an"]
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
        ffmpeg(args)


# --------------------------------------------------------------- scan ---

def sample(src, fps, tmp):
    """[(time, png path)] at `fps` frames per second."""
    ffmpeg(["-i", str(src), "-vf", f"fps={fps}", str(Path(tmp) / "f%06d.png")])
    frames = sorted(Path(tmp).glob("f*.png"))
    # The fps filter's first output frame is t=0, then one every 1/fps.
    # Unrounded: tracks() measures gaps against 1/fps, and a rounded time
    # (1/3 s -> 0.333) can push a one-sample gap past the bridging limit.
    return [(i / fps, p) for i, p in enumerate(frames)]


def scan_video(src, fps, meta):
    size = (meta["width"], meta["height"])
    samples = []
    with tempfile.TemporaryDirectory(prefix="vidscan-") as tmp:
        frames = sample(src, fps, tmp)
        for n, (t, frame) in enumerate(frames, 1):
            print(f"\r  {DIM}OCR {n}/{len(frames)}{RESET}", end="", file=sys.stderr)
            _, lines = scan.ocr(frame)
            samples.append((t, scan.scan_lines(lines)))
        print("\r\033[K", end="", file=sys.stderr)
    interval = 1 / fps
    return [widen(k, interval, meta["duration"], size) for k in tracks(samples, interval)]


def report(found):
    for k in found:
        x, y, w, h = k["box"]
        span = f"{hms(k['start'], digits=1)}–{hms(k['end'], digits=1)}"
        print(f"  {YELLOW}{k['kind']:<22}{RESET} {k['preview']:<28} "
              f"{span:<24} {DIM}at {x},{y} {w}×{h}{RESET}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, help="screen recording")
    ap.add_argument("--fps", type=float, default=1.0,
                    help="frames sampled per second (default 1)")
    ap.add_argument("--json", help="write the tracks here, to review before --render")
    ap.add_argument("--from-json", help="render a reviewed tracks file; no OCR")
    ap.add_argument("--render", "-o", help="write a copy with every track blurred")
    ap.add_argument("--check", action="store_true",
                    help="write nothing; exit 1 if anything is found (pre-publish gate)")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.is_file():
        die(f"no such file: {src}")
    if args.fps <= 0:
        die("--fps must be positive")
    if args.check and (args.render or args.from_json):
        die("--check only reads; run it on the rendered file")
    need("ffmpeg")
    meta = probe(src)
    if not meta["has_video"]:
        die(f"no video stream in {src}")

    if args.from_json:
        found = json.loads(Path(args.from_json).read_text())
    else:
        found = scan_video(src, args.fps, meta)
    report(found)
    if args.json:
        Path(args.json).write_text(json.dumps(found, indent=2) + "\n")

    if args.check:
        if found:
            print(f"❌ {len(found)} possible secret(s) still readable in {src}")
            return 1
        print(f"{GREEN}✓{RESET} nothing readable flagged in {src} at {args.fps:g} fps")
        return 0
    if not found:
        print(f"{GREEN}✓{RESET} nothing flagged in {src} — still watch it before publishing")
        if args.render:
            # The next step reads the --render path, so it must exist even
            # when there is nothing to blur.
            shutil.copyfile(src, args.render)
            print(f"  {DIM}copied unchanged to {args.render}{RESET}")
        return 0
    if args.render:
        render(src, found, args.render, meta)
        print(f"{GREEN}✓{RESET} {len(found)} track(s) blurred into {args.render}")
        print(f"  {DIM}then: python3 vidscan.py -i {args.render} --check{RESET}")
    else:
        print(f"  {DIM}review, then: --from-json <file> --render out/redacted.mp4{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
