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

"""Shared helpers for the /sound scripts.

Probing, running ffmpeg quietly, the two-pass EBU R128 loudness path that every
stage ends with, and the codec table that turns an output extension into encoder
flags. Nothing here needs a Python audio library — the measuring and the
normalising are both done by ffmpeg, so the core path runs on a bare system
python3. Only bed.py reaches for mido, and it does that lazily.
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

BOLD, DIM, GREEN, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m")

# 48kHz throughout. loudnorm runs at 192kHz internally and will happily emit a
# 192kHz file if nothing pins the rate; every downstream muxer then resamples it
# again. Pinning once here means one resample, at a rate video actually uses.
RATE = 48000


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def need(tool, hint=""):
    """Resolve a binary or fail with something the user can act on."""
    if tool in ("ffmpeg", "ffprobe"):
        resolved = hostenv.ffmpeg_bin(tool)
        if resolved:
            return resolved
        die(f"{tool} not found. {hint or hostenv.ffmpeg_install_hint()}")
    path = shutil.which(tool)
    if not path:
        die(f"{tool} not found on PATH. {hint or hostenv.package_hint(tool)}")
    return path


def run(cmd, capture=False, check=True):
    proc = subprocess.run(
        [str(c) for c in cmd],
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-12:]
        die(f"{Path(str(cmd[0])).name} failed (exit {proc.returncode}):\n  "
            + "\n  ".join(tail))
    return proc


def ffmpeg(args, capture=False, check=True):
    return run([need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
                "-y", *args], capture=capture, check=check)


def has_filter(name):
    proc = run([need("ffmpeg"), "-hide_banner", "-filters"], capture=True,
               check=False)
    return bool(re.search(rf"^\s*\S+\s+{re.escape(name)}\s", proc.stdout or "",
                          re.M))


# ------------------------------------------------------------------ probing ---

def probe(path):
    """{duration, rate, channels, codec, bitrate} for an audio or A/V file."""
    path = Path(path)
    if not path.is_file():
        die(f"no such file: {path}")
    proc = run([need("ffprobe"), "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path)], capture=True)
    doc = json.loads(proc.stdout)
    stream = next((s for s in doc["streams"] if s["codec_type"] == "audio"), None)
    if stream is None:
        die(f"{path.name} has no audio stream. "
            f"If this is a silent screen recording there is nothing to work on.")
    return {
        "duration": float(doc["format"].get("duration")
                          or stream.get("duration") or 0.0),
        "rate": int(stream.get("sample_rate", RATE)),
        "channels": int(stream.get("channels", 1)),
        "codec": stream.get("codec_name", "?"),
        "bitrate": int(doc["format"].get("bit_rate", 0) or 0),
    }


# ----------------------------------------------------------------- loudness ---

# loudnorm prints its measurement as a JSON object on stderr, but not last:
# the muxing-overhead and `size=` progress lines follow it, and the block is
# introduced by a `[Parsed_loudnorm_0 @ 0x...]` prefix that is not valid JSON.
# So scan every brace-block and keep the one that has the fields we came for,
# rather than trying to anchor on either end of the stream.
_JSON_BLOCK = re.compile(r"\{[^{}]*\}", re.S)


def measure(path, dual_mono=None):
    """Measured loudness of a file: {i, tp, lra, thresh, offset}.

    `dual_mono` matters and defaults to sensing the file. EBU R128 sums
    channels, so a mono track measures about 3 LU quieter than the identical
    material panned to both sides — normalise it without this and every mono
    voiceover in the project comes out 3dB hot relative to the stereo music.
    """
    info = probe(path)
    if dual_mono is None:
        dual_mono = info["channels"] == 1
    args = ["-i", str(path), "-map", "a:0", "-af",
            f"loudnorm=I=-14:TP=-1:LRA=11:dual_mono={'true' if dual_mono else 'false'}"
            ":print_format=json", "-f", "null", "-"]
    proc = run([need("ffmpeg"), "-hide_banner", "-nostdin", *args],
               capture=True, check=True)
    raw = None
    for block in _JSON_BLOCK.findall(proc.stderr or ""):
        try:
            candidate = json.loads(block)
        except ValueError:
            continue
        if "input_i" in candidate:
            raw = candidate
    if raw is None:
        die(f"could not read a loudness measurement from {Path(path).name}. "
            f"The file may be entirely silent.")
    stats = {
        "i": float(raw["input_i"]),
        "tp": float(raw["input_tp"]),
        "lra": float(raw["input_lra"]),
        "thresh": float(raw["input_thresh"]),
        "offset": float(raw["target_offset"]),
        "dual_mono": dual_mono,
    }
    # Digital silence measures as -inf, which serialises into the second-pass
    # filter as the literal string "-inf" and makes ffmpeg fail with an
    # unparseable-option error three steps later. Catch it here, where the
    # cause is still obvious.
    if any(v == float("-inf") for v in (stats["i"], stats["tp"])):
        die(f"{path.name if isinstance(path, Path) else Path(path).name} is "
            f"digitally silent — there is nothing to normalise. Check that the "
            f"recording captured the right input device.")
    return stats


def loudnorm_filter(stats, target_i, target_tp, lra=11.0):
    """Second-pass loudnorm, primed with the first pass's measurement.

    `linear=true` asks loudnorm to apply one constant gain rather than riding
    the level dynamically. That is what you want for a finished mix: dynamic
    mode re-compresses the material and audibly pumps on speech. It silently
    falls back to dynamic mode when the required gain would clip, which is the
    filter telling you the source needs a limiter, not a fader.
    """
    return (f"loudnorm=I={target_i}:TP={target_tp}:LRA={lra}"
            f":measured_I={stats['i']}:measured_TP={stats['tp']}"
            f":measured_LRA={stats['lra']}:measured_thresh={stats['thresh']}"
            f":offset={stats['offset']}"
            f":dual_mono={'true' if stats['dual_mono'] else 'false'}"
            f":linear=true:print_format=summary")


def targets(tokens, target_i=None, target_tp=None):
    """(I, TP) in LUFS/dBTP — the brand tokens unless the caller overrode them."""
    video = tokens.get("video", {})
    return (float(target_i if target_i is not None
                  else video.get("loudnessTarget", -14)),
            float(target_tp if target_tp is not None
                  else video.get("truePeak", -1.0)))


# ------------------------------------------------------------------ codecs ---

# Extension -> encoder flags. Everything lands at 48kHz; see RATE.
_CODECS = {
    ".wav":  ["-c:a", "pcm_s24le"],
    ".flac": ["-c:a", "flac", "-compression_level", "8"],
    ".mp3":  ["-c:a", "libmp3lame", "-b:a", "192k"],
    ".m4a":  ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
    ".aac":  ["-c:a", "aac", "-b:a", "192k"],
    ".opus": ["-c:a", "libopus", "-b:a", "128k"],
    ".ogg":  ["-c:a", "libvorbis", "-q:a", "6"],
}


def encode_args(dst, channels=None):
    """Encoder flags for an output path, or die listing what is supported."""
    suffix = Path(dst).suffix.lower()
    if suffix not in _CODECS:
        die(f"unsupported output format '{suffix or Path(dst).name}'. "
            f"Use one of: {', '.join(sorted(_CODECS))}")
    args = ["-ar", str(RATE), *_CODECS[suffix]]
    if channels:
        args += ["-ac", str(channels)]
    return args


def intermediate(stem, staging):
    """A lossless scratch file. Every stage writes 24-bit WAV between steps —
    round-tripping through a lossy codec three times to reach one MP3 is how a
    clean voiceover picks up a swirl of artefacts."""
    return Path(staging) / f"{stem}.wav"


# --------------------------------------------------------------- soundfonts ---

def soundfont(override=None):
    """The General MIDI bank, or die with the way to get one."""
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            die(f"no such SoundFont: {override}")
        return path
    found = hostenv.soundfont()
    if not found:
        die(hostenv.soundfont_hint())
    return found


# ----------------------------------------------------------------- reporting ---

def human(path):
    size = Path(path).stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


def report(path, note=""):
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        die(f"{path} was not written — the filter graph produced nothing.")
    detail = f"  {DIM}{note}{RESET}" if note else ""
    print(f"  {GREEN}✓{RESET} {path}  {DIM}({human(path)}){RESET}{detail}")


def report_loudness(path, target_i, target_tp):
    """Measure a finished file and say whether it actually hit the target."""
    stats = measure(path)
    ok_i = abs(stats["i"] - target_i) <= 1.0
    ok_tp = stats["tp"] <= target_tp + 0.3
    mark = f"{GREEN}✓{RESET}" if (ok_i and ok_tp) else f"{YELLOW}!{RESET}"
    print(f"  {mark} {stats['i']:.1f} LUFS / {stats['tp']:.1f} dBTP "
          f"{DIM}(target {target_i} / {target_tp}, LRA {stats['lra']:.1f}){RESET}")
    if not ok_i:
        print(f"    {DIM}off target — the source is probably near-silent or "
              f"heavily clipped already{RESET}")
    return stats


def tokens(profile=None):
    return brand.load(profile)
