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

"""Generate instrumental music from a text prompt with Lyria.

    python3 music.py -p "warm lo-fi beat, mellow rhodes, soft vinyl crackle" -o out/bed.wav
    python3 music.py -p "bright corporate strings, optimistic" -o out/bed.wav --duration 180
    python3 music.py -p "tense low pulse" -o out/tension.wav --negative "drums, vocals"

Lyria writes one fixed-length clip of about 33 seconds, 48kHz stereo. Asking
for longer generates several clips and crossfades them, which works because
the prompt holds the style steady across generations even though the material
differs — that is what a ten-minute bed under a tutorial wants anyway.

This is the counterpart to `/sound bed.py`, not a replacement for it. bed.py
writes MIDI you can edit, renders offline, costs nothing and is deterministic.
Lyria sounds like a recording. Reach for bed.py when the music has to sit
politely under a voice; reach for this when it has to be heard.
"""

import argparse
import base64
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genailib as G  # noqa: E402

# lyria-002 is the generally available model. The lyria-3 previews are
# allowlist-only on most projects, so they sit behind --model rather than in
# a chain that would 404 on every run for almost everyone.
DEFAULT_MODEL = "lyria-002"

# Measured, not documented: a lyria-002 response is 6,291,544 bytes of 48kHz
# 16-bit stereo WAV, which is 32.768s exactly. Treated as a hint for planning
# how many clips to ask for — the real length is read back from each file.
CLIP_SECONDS = 32.7
CROSSFADE = 2.0


def ffmpeg():
    binary = shutil.which("ffmpeg")
    if not binary:
        G.die("ffmpeg not found. " + G.hostenv.package_hint("ffmpeg"))
    return binary


def duration(path):
    out = subprocess.run(
        [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
         "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        G.die(f"could not read a duration from {path}")


def generate_clip(prompt, negative, seed, model):
    """One Lyria clip as raw WAV bytes."""
    instance = {"prompt": prompt}
    if negative:
        instance["negative_prompt"] = negative
    if seed is not None:
        # Lyria rejects a request carrying both a seed and a sample count > 1;
        # we always ask for one clip at a time, so the two never collide here.
        instance["seed"] = seed

    response = G.vertex_predict(model, {"instances": [instance],
                                        "parameters": {"sample_count": 1}})
    predictions = response.get("predictions") or []
    if not predictions:
        # An empty prediction list is how Lyria reports a filtered prompt.
        G.die(f"{model} returned no audio. The prompt may have been filtered — "
              "Lyria refuses artist names, song titles and lyrics.")
    encoded = predictions[0].get("bytesBase64Encoded")
    if not encoded:
        G.die(f"{model} returned a prediction with no audio payload: "
              f"{sorted(predictions[0])}")
    return base64.b64decode(encoded)


def stitch(clips, target_seconds, dst):
    """Crossfade clips into one file of at most `target_seconds`.

    acrossfade, not concat: two independently generated clips butted together
    click audibly at the seam, and a two-second overlap hides both the click
    and the fact that the second clip starts on a different phrase.
    """
    args = []
    for clip in clips:
        args += ["-i", str(clip)]

    if len(clips) == 1:
        chain = "[0:a]anull[mix]"
    else:
        steps, label = [], "[0:a]"
        for index in range(1, len(clips)):
            out = "[mix]" if index == len(clips) - 1 else f"[x{index}]"
            steps.append(f"{label}[{index}:a]"
                         f"acrossfade=d={CROSSFADE}:c1=tri:c2=tri{out}")
            label = out
        chain = ";".join(steps)

    # Trim last, and fade the tail: a bed that stops dead at the target length
    # is more obviously machine-made than anything in the notes.
    fade_start = max(0.0, target_seconds - 3.0)
    chain += (f";[mix]atrim=0:{target_seconds:.3f},asetpts=N/SR/TB,"
              f"afade=t=out:st={fade_start:.3f}:d=3[out]")

    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-y", *args, "-filter_complex", chain, "-map", "[out]",
                    "-ar", "48000", "-c:a", "pcm_s24le", str(dst)], check=True)


def encode(src, dst):
    """Copy or transcode the stitched WAV to whatever the caller asked for."""
    codec = {".wav": ["-c:a", "pcm_s24le"],
             ".flac": ["-c:a", "flac"],
             ".mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
             ".m4a": ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
             ".opus": ["-c:a", "libopus", "-b:a", "128k"]}
    suffix = dst.suffix.lower()
    if suffix not in codec:
        G.die(f"unsupported audio format '{suffix}'. Use one of: "
              f"{', '.join(sorted(codec))}")
    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-y", "-i", str(src), "-ar", "48000", *codec[suffix],
                    str(dst)], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt", "-p", required=True,
                    help="instruments, mood and tempo — not artists or songs")
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--duration", "-d", type=float,
                    help=f"seconds wanted; clips are ~{CLIP_SECONDS:.0f}s each "
                         f"and are crossfaded to reach this")
    ap.add_argument("--negative", help="what must not be in it")
    ap.add_argument("--seed", type=int,
                    help="fixed seed for the first clip; later clips step from it")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    out = G.prepare(args.output)
    target = args.duration or CLIP_SECONDS
    # Overlaps eat CROSSFADE seconds per join, so n clips cover
    # n*CLIP - (n-1)*CROSSFADE. Solve for n and round up.
    needed = max(1, math.ceil((target - CROSSFADE)
                              / (CLIP_SECONDS - CROSSFADE)))

    print(f"◈ {needed} clip(s) → {out.name}  "
          f"{G.DIM}({target:.0f}s, {args.model}){G.RESET}")

    staging = Path(tempfile.mkdtemp(prefix="cs-lyria-"))
    try:
        clips = []
        for index in range(needed):
            seed = None if args.seed is None else args.seed + index
            data = generate_clip(args.prompt, args.negative, seed, args.model)
            clip = staging / f"clip{index:02d}.wav"
            clip.write_bytes(data)
            clips.append(clip)
            print(f"  {G.DIM}clip {index + 1}/{needed}: "
                  f"{duration(clip):.1f}s{G.RESET}")

        available = sum(duration(c) for c in clips) - CROSSFADE * (len(clips) - 1)
        if target > available + 0.5:
            G.warn(f"asked for {target:.0f}s but the clips only cover "
                   f"{available:.0f}s — writing what there is")
            target = available

        stitched = staging / "stitched.wav"
        stitch(clips, min(target, available), stitched)
        encode(stitched, out)

        G.provenance(out, args.prompt, args.model, "vertex")
        G.report(out, f"{duration(out):.1f}s, 48kHz stereo")
        print(f"  {G.DIM}not loudness-normalised — run it through "
              f"/sound mix.py, which will duck and master it{G.RESET}")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
