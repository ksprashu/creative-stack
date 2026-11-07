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

"""Shared helpers for the /vidkit scripts.

Only the things every stage needs: probing, running ffmpeg without drowning the
console in banner output, SRT parsing, and the segments.json contract that
autocut -> timeline -> publish all speak.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("CREATIVE_STACK_ROOT",
                                Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import brand  # noqa: E402
import hostenv  # noqa: E402

CACHE = Path(os.environ.get("CREATIVE_STACK_CACHE", hostenv.cache_dir()))


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def need(tool, hint=""):
    """Fail fast with an actionable message instead of a raw OSError.

    ffmpeg and ffprobe go through hostenv, which knows that Homebrew's plain
    `ffmpeg` has no libass/freetype (so `ass`, `subtitles` and `drawtext` are
    missing) and picks the keg-only `ffmpeg-full` instead. On Linux the distro
    build already has everything, so this is a straight PATH lookup.
    """
    if tool in ("ffmpeg", "ffprobe"):
        resolved = hostenv.ffmpeg_bin(tool)
        if resolved:
            return resolved
        die(f"{tool} not found. {hint or hostenv.ffmpeg_install_hint()}")
    path = shutil.which(tool)
    if not path:
        die(f"{tool} not found on PATH. {hint or 'Run scripts/bootstrap.sh.'}")
    return path


def ffmpeg_version():
    """(major, minor) of the resolved ffmpeg, or (0, 0) for a git build."""
    proc = run([need("ffmpeg"), "-hide_banner", "-version"],
               capture=True, check=False)
    match = re.search(r"ffmpeg version n?(\d+)\.(\d+)", proc.stdout or "")
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def filter_script_args(path):
    """Read a filter graph from a file — a few hundred trim segments blow past
    the shell's argument limit.

    ffmpeg 7.1 replaced `-filter_complex_script FILE` with the generic
    `-/filter_complex FILE` form and removed the old spelling in 9.0, so pick
    the one this build actually understands.
    """
    if ffmpeg_version() >= (7, 1):
        return ["-/filter_complex", str(path)]
    return ["-filter_complex_script", str(path)]


def has_filter(name):
    """True if the resolved ffmpeg build actually has this filter compiled in."""
    proc = run([need("ffmpeg"), "-hide_banner", "-filters"],
               capture=True, check=False)
    return bool(re.search(rf"^\s*\S+\s+{re.escape(name)}\s", proc.stdout or "",
                          re.M))


def run(cmd, capture=False, check=True, quiet=True):
    """Run a command. ffmpeg's banner and per-frame progress are noise here, so
    stderr is captured and only replayed when something actually fails."""
    proc = subprocess.run(
        [str(c) for c in cmd],
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if (capture or quiet) else None,
        text=True,
    )
    if check and proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-12:]
        die(f"{Path(str(cmd[0])).name} failed (exit {proc.returncode}):\n  "
            + "\n  ".join(tail))
    return proc


def ffmpeg(args, **kw):
    return run([need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", *args], **kw)


def probe(path):
    """Return {duration, fps, width, height, has_audio, has_video} for a file."""
    need("ffprobe")
    proc = run(["ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path)], capture=True)
    doc = json.loads(proc.stdout)
    video = next((s for s in doc["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in doc["streams"] if s["codec_type"] == "audio"), None)

    fps = 30.0
    if video and video.get("r_frame_rate", "0/0") != "0/0":
        num, _, den = video["r_frame_rate"].partition("/")
        if float(den or 1):
            fps = float(num) / float(den or 1)

    return {
        "duration": float(doc["format"].get("duration", 0.0)),
        "fps": round(fps, 6),
        "width": int(video["width"]) if video else 0,
        "height": int(video["height"]) if video else 0,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def hms(seconds, sep=":", ms_sep=".", digits=3):
    """Format seconds as HH:MM:SS.mmm (SRT wants a comma; pass ms_sep=',')."""
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    whole = int(secs)
    frac = int(round((secs - whole) * (10 ** digits)))
    if frac >= 10 ** digits:          # 59.9996 must not render as :60.000
        frac, whole = 0, whole + 1
    return (f"{int(hours):02d}{sep}{int(minutes):02d}{sep}{whole:02d}"
            f"{ms_sep}{frac:0{digits}d}")


# --- segments.json: the contract between autocut, timeline and publish -------

SEGMENTS_VERSION = 1


def write_segments(path, source, meta, segments, extra=None):
    doc = {
        "version": SEGMENTS_VERSION,
        "source": str(Path(source).resolve()),
        "duration": meta["duration"],
        "fps": meta["fps"],
        "width": meta["width"],
        "height": meta["height"],
        "segments": [{"start": round(s, 4), "end": round(e, 4)} for s, e in segments],
    }
    doc.update(extra or {})
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(doc, indent=2) + "\n")
    return doc


def read_segments(path):
    doc = json.loads(Path(path).read_text())
    if doc.get("version") != SEGMENTS_VERSION:
        die(f"{path}: segments version {doc.get('version')} != {SEGMENTS_VERSION}. "
            "Re-run autocut.py.")
    if not doc.get("segments"):
        die(f"{path}: no segments — nothing to build a timeline from.")
    return doc


# --- SRT ----------------------------------------------------------------------

_SRT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def parse_srt(path):
    """Return [{'start': s, 'end': s, 'text': str}]. Tolerates VTT-style dots."""
    cues, pending = [], None
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        match = _SRT_TIME.search(line)
        if match:
            g = [int(x) for x in match.groups()]
            pending = {
                "start": g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000,
                "end": g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000,
                "text": "",
            }
            cues.append(pending)
        elif pending is not None:
            if line.strip():
                pending["text"] = (pending["text"] + "\n" + line.strip()).strip()
            else:
                pending = None
    return [c for c in cues if c["text"]]


def write_srt(path, cues):
    blocks = []
    for index, cue in enumerate(cues, 1):
        blocks.append(f"{index}\n{hms(cue['start'], ms_sep=',')} --> "
                      f"{hms(cue['end'], ms_sep=',')}\n{cue['text']}\n")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(blocks), encoding="utf-8")


def write_vtt(path, cues):
    blocks = ["WEBVTT\n"]
    for cue in cues:
        blocks.append(f"{hms(cue['start'])} --> {hms(cue['end'])}\n{cue['text']}\n")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(blocks), encoding="utf-8")


def clamp_cues(cues, duration, min_len=0.2):
    """Whisper routinely runs the last cue far past the end of the audio and
    pads text with a leading space. Fix both before anything downstream sees it."""
    out = []
    for cue in cues:
        start = min(max(0.0, cue["start"]), duration)
        end = min(cue["end"], duration)
        text = cue["text"].strip()
        if not text or end - start < min_len:
            continue
        out.append({"start": start, "end": end, "text": text})
    return out


def write_chapters(path, chapters):
    """[{start, title}] -> a YouTube-style chapters file."""
    # YouTube wants M:SS, or H:MM:SS past an hour — the same shape
    # timeline.py --chapters and publish.py --chapters read.
    lines = []
    for c in chapters:
        total = int(c["start"])
        hours, rest = divmod(total, 3600)
        minutes, seconds = divmod(rest, 60)
        stamp = (f"{hours}:{minutes:02d}:{seconds:02d}" if hours
                 else f"{minutes}:{seconds:02d}")
        lines.append(f"{stamp} {c['title']}")
    Path(path).write_text("\n".join(lines) + "\n")


def tokens(profile=None):
    return brand.load(profile)
