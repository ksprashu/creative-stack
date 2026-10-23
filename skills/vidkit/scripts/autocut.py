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

"""Cut the dead air out of a screen recording.

Detects silence, builds the list of segments worth keeping, and writes it to
segments.json — the contract the rest of /vidkit speaks. Rendering is optional:
the JSON alone is enough to emit an NLE timeline, so you can review the cut
before spending encode time on it.

    # analyse only — prints how much would be removed
    python3 autocut.py -i raw.mov -o out/cut.json

    # analyse, render, and slide the transcript onto the new timeline
    python3 autocut.py -i raw.mov -o out/cut.json --render out/cut.mp4 \\
        --srt out/talk.srt --srt-out out/cut.srt

Tuning, in the order worth reaching for:
    --threshold   quieter room noise needs a lower dB (default -35)
    --padding     breathing room kept around speech (default 0.15s)
    --min-silence shorter gaps are left alone (default 0.5s)
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from vidlib import (die, ffmpeg, filter_script_args, hms, need, parse_srt,
                    probe, run, write_chapters, write_segments, write_srt)

SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def detect_silence(source, threshold_db, min_silence):
    """Return [(start, end)] of silent spans, via ffmpeg's silencedetect."""
    # silencedetect reports on stderr at 'info' level, so this one call cannot
    # use the quiet ffmpeg() wrapper.
    proc = subprocess.run(
        [need("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(source),
         "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence}",
         "-f", "null", "-"],
        stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True,
    )
    if proc.returncode != 0:
        die("silence detection failed:\n  "
            + "\n  ".join(proc.stderr.strip().splitlines()[-8:]))

    spans, open_start = [], None
    for line in proc.stderr.splitlines():
        start = SILENCE_START.search(line)
        if start:
            open_start = float(start.group(1))
            continue
        end = SILENCE_END.search(line)
        if end and open_start is not None:
            spans.append((open_start, float(end.group(1))))
            open_start = None
    if open_start is not None:          # recording ends in silence
        spans.append((open_start, float("inf")))
    return spans


def keep_segments(silences, duration, padding, min_clip):
    """Invert the silence list into segments worth keeping."""
    keeps, cursor = [], 0.0
    for start, end in silences:
        start, end = max(0.0, start), min(duration, end)
        if start > cursor:
            keeps.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < duration:
        keeps.append([cursor, duration])

    # Pad outward so words are not clipped, then merge anything that now touches.
    padded = [[max(0.0, s - padding), min(duration, e + padding)] for s, e in keeps]
    merged = []
    for segment in padded:
        if merged and segment[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], segment[1])
        else:
            merged.append(segment)

    return [(s, e) for s, e in merged if e - s >= min_clip]


def subtract(segments, cuts, min_clip):
    """Remove analysis.json's cut spans from the kept segments.

    Applied after silence removal, so Gemini only has to find what silence
    detection cannot — false starts, retakes, dead waits with a fan running.
    """
    out = []
    for start, end in segments:
        pieces = [(start, end)]
        for cut_start, cut_end in cuts:
            pieces = [p for s, e in pieces for p in
                      ((s, min(e, cut_start)), (max(s, cut_end), e))
                      if p[1] - p[0] > 0]
        out += [(s, e) for s, e in pieces if e - s >= min_clip]
    return out


def remap(segments):
    """Return f(source_time) -> output_time for the cut timeline.

    Times inside a removed span collapse onto the start of the next kept
    segment; times after the last segment return None so callers can drop them.
    """
    offsets, elapsed = [], 0.0
    for start, end in segments:
        offsets.append((start, end, elapsed))
        elapsed += end - start

    def fn(time):
        for start, end, base in offsets:
            if time < start:
                return base                      # snapped forward out of a cut
            if time <= end:
                return base + (time - start)
        return None                              # past the end of the cut
    return fn


def remap_cues(cues, segments):
    """Slide transcript cues onto the cut timeline, dropping fully-cut ones."""
    fn = remap(segments)
    total = sum(end - start for start, end in segments)
    out = []
    for cue in cues:
        start = fn(cue["start"])
        if start is None:
            continue                             # cue begins after the last cut
        # A cue that overruns the end of the timeline is clipped to it, not
        # dropped — the words were spoken, only the tail was trimmed.
        end = fn(cue["end"])
        if end is None:
            end = total
        if end - start < 0.12:
            continue                             # cue lived inside a removed span
        out.append({"start": start, "end": end, "text": cue["text"]})
    return out


def render(source, segments, meta, output):
    """Concatenate the kept segments losslessly-accurately with ffmpeg."""
    chunks, labels = [], []
    for index, (start, end) in enumerate(segments):
        chunks.append(f"[0:v]trim=start={start:.4f}:end={end:.4f},"
                      f"setpts=PTS-STARTPTS[v{index}];")
        labels.append(f"[v{index}]")
        if meta["has_audio"]:
            chunks.append(f"[0:a]atrim=start={start:.4f}:end={end:.4f},"
                          f"asetpts=PTS-STARTPTS[a{index}];")
            labels.append(f"[a{index}]")

    streams = 1 if not meta["has_audio"] else 2
    # Interleave v/a labels in the order concat expects: v0 a0 v1 a1 ...
    if meta["has_audio"]:
        ordered = "".join(f"[v{i}][a{i}]" for i in range(len(segments)))
    else:
        ordered = "".join(f"[v{i}]" for i in range(len(segments)))
    chunks.append(f"{ordered}concat=n={len(segments)}:v=1:a="
                  f"{1 if meta['has_audio'] else 0}[outv]"
                  f"{'[outa]' if meta['has_audio'] else ''}")

    graph = "\n".join(chunks)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write(graph)
        graph_path = handle.name

    try:
        args = ["-i", str(source), *filter_script_args(graph_path),
                "-map", "[outv]"]
        if streams == 2:
            args += ["-map", "[outa]", "-c:a", "aac", "-b:a", "192k"]
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                 "-y", str(output)]
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        ffmpeg(args)
    finally:
        Path(graph_path).unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--output", "-o", required=True, help="segments JSON path")
    ap.add_argument("--render", help="also encode the cut video to this path")
    ap.add_argument("--srt", help="transcript to slide onto the cut timeline")
    ap.add_argument("--srt-out", help="where to write the remapped transcript")
    ap.add_argument("--threshold", type=float, default=-35.0,
                    help="silence threshold in dBFS (default -35)")
    ap.add_argument("--min-silence", type=float, default=0.5,
                    help="ignore silences shorter than this, in seconds")
    ap.add_argument("--padding", type=float, default=0.15,
                    help="seconds of air kept on each side of speech")
    ap.add_argument("--min-clip", type=float, default=0.3,
                    help="drop kept segments shorter than this")
    ap.add_argument("--analysis", help="analysis.json from analyze.py — its "
                    "cuts are removed on top of the silences")
    args = ap.parse_args()

    source = Path(args.input)
    if not source.exists():
        die(f"input not found: {source}")

    meta = probe(source)
    if not meta["has_audio"]:
        die(f"{source} has no audio track — autocut has nothing to measure. "
            "Use zoom.py/publish.py directly.")

    print(f"🔎 scanning {source.name}  ({hms(meta['duration'])}, "
          f"{meta['width']}x{meta['height']} @ {meta['fps']:g}fps)")
    silences = detect_silence(source, args.threshold, args.min_silence)
    segments = keep_segments(silences, meta["duration"], args.padding, args.min_clip)
    cuts, analysis = [], None
    if args.analysis:
        analysis = json.loads(Path(args.analysis).read_text())
        if Path(analysis.get("source", "")).resolve() != source.resolve():
            print(f"⚠️  {args.analysis} was made from {analysis.get('source')}, "
                  f"not {source} — its times may not line up.")
        cuts = [(c["start"], c["end"]) for c in analysis.get("cuts", [])]
        segments = subtract(segments, cuts, args.min_clip)
        print(f"✂️  applied {len(cuts)} cuts from {args.analysis}")

    if not segments:
        die(f"everything was below {args.threshold}dB — the whole recording "
            "looks silent. Try --threshold -45.")

    kept = sum(e - s for s, e in segments)
    removed = meta["duration"] - kept
    percent = removed / meta["duration"] * 100 if meta["duration"] else 0

    write_segments(args.output, source, meta, segments, extra={
        "tool": "autocut",
        "threshold_db": args.threshold,
        "padding": args.padding,
        "analysis": str(Path(args.analysis).resolve()) if args.analysis else None,
        "analysis_cuts": len(cuts),
        "kept": round(kept, 3),
        "removed": round(removed, 3),
    })
    print(f"✓ {args.output}  ({len(segments)} segments, "
          f"{hms(kept)} kept, {hms(removed)} removed — {percent:.0f}% shorter)")

    if analysis and analysis.get("chapters"):
        # analyze.py speaks source time; timeline.py and publish.py read the
        # cut timeline. A chapter whose start was cut lands where its content
        # resumes; two that collapse onto the same second keep the first.
        fn, seen, moved = remap(segments), set(), []
        for chapter in analysis["chapters"]:
            at = fn(chapter["start"])
            if at is not None and int(at) not in seen:
                seen.add(int(at))
                moved.append({"start": at, "title": chapter["title"]})
        chapters_out = Path(args.output).with_suffix(".chapters.txt")
        write_chapters(chapters_out, moved)
        print(f"✓ {chapters_out}  ({len(moved)} chapters on the cut timeline)")

    if args.srt:
        cues = remap_cues(parse_srt(args.srt), segments)
        destination = args.srt_out or str(Path(args.srt).with_suffix(".cut.srt"))
        write_srt(destination, cues)
        print(f"✓ {destination}  ({len(cues)} cues remapped)")
    elif args.srt_out:
        die("--srt-out needs --srt")

    if args.render:
        print(f"🎬 rendering {len(segments)} segments …")
        render(source, segments, meta, args.render)
        print(f"✓ {args.render}  ({Path(args.render).stat().st_size // 1024}KB)")
        print(f"  Next: burn captions →  python3 captions.py -i {args.render} "
              f"-s {args.srt_out or 'out/cut.srt'} -o out/captioned.mp4")
    else:
        print(f"  Next: review in Resolve →  python3 timeline.py -s {args.output} "
              "-o out/edit.xml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
