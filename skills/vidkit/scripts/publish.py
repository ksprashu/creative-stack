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

"""Final pass: loudness, chapters, encode ladder, thumbnail.

The part everyone skips and every viewer notices. Audio gets normalised to the
brand loudness target so your video is not 6 dB quieter than the one before it
in the playlist; the ladder gives a blog embed something small to start with.

    python3 publish.py -i captioned.mp4 -o out/publish --chapters chapters.txt

Writes out/publish/<name>-1080p.mp4, -720p.mp4, -480p.mp4, a poster frame, and
manifest.json listing every artefact with its size and bitrate.

Loudness is measured, not guessed: a first pass runs EBU R128 analysis and the
second pass applies the exact correction, which is why a single -af loudnorm
(one-pass) is not good enough for published audio.
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from vidlib import die, ffmpeg, hms, need, probe, tokens

LADDER = {
    2160: {"crf": 20, "maxrate": "40M"},
    1440: {"crf": 20, "maxrate": "20M"},
    1080: {"crf": 20, "maxrate": "10M"},
    720: {"crf": 22, "maxrate": "5M"},
    480: {"crf": 24, "maxrate": "2M"},
}


def measure_loudness(source, target, true_peak):
    """Pass 1 of EBU R128: measure, so pass 2 can correct exactly."""
    proc = subprocess.run(
        [need("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(source),
         "-af", f"loudnorm=I={target}:TP={true_peak}:LRA=11:print_format=json",
         "-f", "null", "-"],
        stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)

    match = re.search(r"\{[^{}]*input_i[^{}]*\}", proc.stderr, re.S)
    if not match:
        print("⚠️  loudness analysis produced no JSON — falling back to "
              "single-pass normalisation.")
        return None
    return json.loads(match.group(0))


def loudnorm_filter(target, true_peak, measured):
    base = f"loudnorm=I={target}:TP={true_peak}:LRA=11"
    if not measured:
        return base
    return (f"{base}:measured_I={measured['input_i']}"
            f":measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}"
            f":measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true:print_format=summary")


def chapter_metadata(chapters, duration):
    """ffmetadata chapter block — what makes the scrubber show sections."""
    lines = [";FFMETADATA1"]
    for index, (start, title) in enumerate(chapters):
        end = chapters[index + 1][0] if index + 1 < len(chapters) else duration
        if end <= start:
            continue
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(start * 1000)}", f"END={int(end * 1000) - 1}",
                  f"title={title}"]
    return "\n".join(lines) + "\n"


def parse_chapters(path):
    from timeline import parse_chapters as _parse
    return _parse(path)


def youtube_stamp(seconds):
    """YouTube only turns a description into chapters if the first stamp is
    exactly 0:00 and the form is M:SS or H:MM:SS — no leading zero on the hour."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def rungs(height, requested):
    """Never upscale: a 720p source gets a 720p top rung, not a fake 1080p."""
    if requested:
        wanted = requested
    else:
        wanted = [h for h in sorted(LADDER, reverse=True) if h <= 1080]
    usable = [h for h in wanted if h <= height]
    if not usable:
        usable = [min(LADDER, key=lambda h: abs(h - height))]
    return sorted(set(usable), reverse=True)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--output", "-o", required=True, help="output directory")
    ap.add_argument("--name", default=None, help="basename (default: input stem)")
    ap.add_argument("--chapters", help="YouTube-style 'MM:SS Title' file")
    ap.add_argument("--heights", type=int, nargs="*", default=None,
                    help="ladder rungs, e.g. --heights 1080 720")
    ap.add_argument("--thumb-at", type=float, default=None,
                    help="poster frame time in seconds (default: 1/3 in)")
    ap.add_argument("--webm", action="store_true", help="also emit VP9/Opus")
    ap.add_argument("--brand", default=None)
    ap.add_argument("--no-loudnorm", dest="loudnorm", action="store_false",
                    default=True)
    ap.add_argument("--copy-video", action="store_true",
                    help="losslessly copy the video stream (-c:v copy) when converting .mov/.mkv to .mp4")
    ap.add_argument("--preset", default="slow",
                    help="x264 preset (slow is worth it for a final render)")
    args = ap.parse_args()

    source = Path(args.input)
    if not source.exists():
        die(f"input not found: {source}")

    meta = probe(source)
    if not meta["has_video"]:
        die(f"{source} has no video track.")

    theme = tokens(args.brand)
    target = theme["video"]["loudnessTarget"]
    true_peak = theme["video"]["truePeak"]

    out_arg = Path(args.output)
    single_file_mode = out_arg.suffix.lower() == ".mp4"
    out_dir = out_arg.parent if single_file_mode else out_arg
    out_dir.mkdir(parents=True, exist_ok=True)
    name = args.name or (out_arg.stem if single_file_mode else source.stem)

    audio_filter = None
    if args.loudnorm and meta["has_audio"]:
        print(f"🔊 measuring loudness (target {target} LUFS) …")
        measured = measure_loudness(source, target, true_peak)
        audio_filter = loudnorm_filter(target, true_peak, measured)
        if measured:
            print(f"   input {float(measured['input_i']):.1f} LUFS → {target} LUFS, "
                  f"peak {float(measured['input_tp']):.1f} → {true_peak} dBTP")
    elif args.loudnorm:
        print("⚠️  no audio track — skipping loudness normalisation.")

    metadata_path = None
    chapters = []
    if args.chapters:
        chapters = parse_chapters(args.chapters)
        metadata_path = out_dir / f"{name}.ffmetadata"
        metadata_path.write_text(chapter_metadata(chapters, meta["duration"]))
        print(f"🔖 {len(chapters)} chapters")

    if single_file_mode:
        cmd = ["-i", str(source)]
        if metadata_path:
            cmd += ["-i", str(metadata_path), "-map_metadata", "1"]
        cmd += ["-map", "0:v:0"]
        if meta["has_audio"]:
            cmd += ["-map", "0:a:0"]
        if args.copy_video or meta.get("video_codec") == "h264":
            cmd += ["-c:v", "copy"]
        else:
            cmd += ["-c:v", "libx264", "-preset", args.preset, "-crf", "20",
                    "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p"]
        cmd += ["-movflags", "+faststart"]
        if meta["has_audio"]:
            cmd += ["-c:a", "aac", "-b:a", "256k", "-ar", "48000"]
            if audio_filter:
                cmd += ["-af", audio_filter]
        else:
            cmd += ["-an"]
        print(f"🎬 remuxing & normalising → {out_arg.name} …")
        ffmpeg([*cmd, "-y", str(out_arg)])
        print(f"✓ {out_arg} ({out_arg.stat().st_size // 1024}KB)")
        return 0

    artifacts = []
    for height in rungs(meta["height"], args.heights):
        rung = LADDER[height]
        destination = out_dir / f"{name}-{height}p.mp4"
        cmd = ["-i", str(source)]
        if metadata_path:
            cmd += ["-i", str(metadata_path), "-map_metadata", "1"]
        cmd += ["-map", "0:v:0"]
        if meta["has_audio"]:
            cmd += ["-map", "0:a:0"]
        # -2 keeps width even and preserves the aspect ratio; x264 rejects odd.
        cmd += ["-vf", f"scale=-2:{height}:flags=lanczos",
                "-c:v", "libx264", "-preset", args.preset, "-crf", str(rung["crf"]),
                "-maxrate", rung["maxrate"], "-bufsize", rung["maxrate"],
                "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart"]
        if meta["has_audio"]:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
            if audio_filter:
                cmd += ["-af", audio_filter]
        else:
            cmd += ["-an"]
        print(f"🎬 encoding {height}p …")
        ffmpeg([*cmd, "-y", str(destination)])
        artifacts.append(destination)

    if args.webm:
        destination = out_dir / f"{name}-1080p.webm"
        height = min(1080, meta["height"])
        cmd = ["-i", str(source), "-vf", f"scale=-2:{height}:flags=lanczos",
               "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
               "-row-mt", "1", "-pix_fmt", "yuv420p"]
        cmd += (["-c:a", "libopus", "-b:a", "128k"] +
                (["-af", audio_filter] if audio_filter else [])) \
            if meta["has_audio"] else ["-an"]
        print("🎬 encoding webm (VP9) …")
        ffmpeg([*cmd, "-y", str(destination)])
        artifacts.append(destination)

    poster_at = args.thumb_at if args.thumb_at is not None else meta["duration"] / 3
    poster = out_dir / f"{name}-poster.png"
    ffmpeg(["-ss", f"{poster_at:.3f}", "-i", str(source), "-frames:v", "1",
            "-vf", "scale=1280:-2:flags=lanczos", "-y", str(poster)])
    artifacts.append(poster)

    if chapters:
        youtube = out_dir / f"{name}-chapters.txt"
        youtube.write_text("\n".join(f"{youtube_stamp(start)} {title}"
                                     for start, title in chapters) + "\n")
        artifacts.append(youtube)

    manifest = out_dir / "manifest.json"
    manifest.write_text(json.dumps({
        "source": str(source.resolve()),
        "duration": meta["duration"],
        "loudnessTarget": target if audio_filter else None,
        "chapters": [{"start": s, "title": t} for s, t in chapters],
        "artifacts": [{"path": str(a.relative_to(out_dir)),
                       "bytes": a.stat().st_size} for a in artifacts],
    }, indent=2) + "\n")

    print(f"✓ {out_dir}/")
    for artifact in artifacts:
        print(f"  {artifact.name:<32} {artifact.stat().st_size // 1024:>7}KB")
    print(f"  {manifest.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
