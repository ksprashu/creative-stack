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

"""Turn a raw voice recording into a publishable track.

    python3 clean.py -i raw.wav -o voice.wav
    python3 clean.py -i screencast.mov -o voice.wav --denoise 0,1.5
    python3 clean.py -i raw.wav -o voice.mp3 --target -16 --room-tone 0.4

The chain, in the order it has to run: rumble filter, de-noise, de-ess,
gate, compress, then a two-pass EBU R128 normalise to the brand target. The
normalise is last because every stage before it changes the loudness, so
measuring earlier measures the wrong thing.

Input can be a video file — the audio stream is pulled out and the video is
ignored. Pair it back up with `/vidkit publish.py`.
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import soundlib as S  # noqa: E402


def noise_profile(src, window, staging, amount):
    """Learn a noise fingerprint from a quiet window with sox, return a filter-
    free cleaned WAV.

    sox's `noisered` is spectral subtraction against a real sample of the room,
    which beats ffmpeg's blind `afftdn` whenever the recording actually has a
    second of silence to learn from. It needs that second: aim the window at
    the pause before you started talking, never at a breath.
    """
    sox = S.need("sox")
    start, _, duration = window.partition(",")
    start, duration = float(start), float(duration or 1.0)

    # sox cannot read most container formats, so hand it a WAV either way.
    wav = S.intermediate("pre-sox", staging)
    S.ffmpeg(["-i", str(src), "-map", "a:0", "-ac", "1", "-ar", str(S.RATE),
              "-c:a", "pcm_s24le", str(wav)])

    prof = Path(staging) / "noise.prof"
    S.run([sox, str(wav), "-n", "trim", str(start), str(duration),
           "noiseprof", str(prof)])
    if prof.stat().st_size == 0:
        S.die(f"the window at {start}s+{duration}s produced an empty noise "
              f"profile — it is probably past the end of the file.")

    out = S.intermediate("denoised", staging)
    S.run([sox, str(wav), str(out), "noisered", str(prof), str(amount)])
    return out


def build_chain(args, info):
    """The filter graph, minus the normalise pass."""
    chain = []

    # 80Hz is below the lowest fundamental in speech and above desk thumps,
    # HVAC and handling noise. Nothing you want to keep lives down there.
    if args.highpass:
        chain.append(f"highpass=f={args.highpass}")
    if args.lowpass:
        chain.append(f"lowpass=f={args.lowpass}")

    if args.denoise == "auto":
        # nf is the noise floor estimate in dB. -25 is gentle enough not to
        # give speech the underwater, phasey quality that aggressive spectral
        # gating produces — that artefact is far more noticeable to a listener
        # than the hiss it removes.
        chain.append("afftdn=nf=-25:nr=12:tn=1")

    if args.deess and S.has_filter("deesser"):
        chain.append("deesser=i=0.4")

    if args.gate:
        # Closes between sentences rather than mid-word: a 250ms release is
        # long enough that trailing consonants survive.
        chain.append("agate=threshold=0.015:ratio=2:attack=10:release=250")

    if args.compress:
        # Modest 3:1 with a slow-ish attack, so plosives are tamed but the
        # transient that makes speech intelligible is still there.
        chain.append("acompressor=threshold=-18dB:ratio=3:attack=15:"
                     "release=180:makeup=2")

    if args.room_tone:
        chain.append(f"adelay={int(args.room_tone * 1000)}:all=1")

    if info["channels"] > 1 and args.mono:
        chain.append("pan=mono|c0=0.5*c0+0.5*c1")

    return chain


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True,
                    help="audio or video file; the first audio stream is used")
    ap.add_argument("--output", "-o", required=True,
                    help=".wav .flac .mp3 .m4a .opus")
    ap.add_argument("--target", type=float,
                    help="integrated loudness in LUFS (default: brand token, "
                         "-14 for streaming; use -16 for podcast)")
    ap.add_argument("--true-peak", type=float, dest="true_peak",
                    help="ceiling in dBTP (default: brand token, -1.0)")
    ap.add_argument("--lra", type=float, default=11.0,
                    help="target loudness range; lower is more even (11 is the "
                         "EBU default, 7 suits narration over music)")
    ap.add_argument("--denoise", default="auto",
                    help="'auto' for ffmpeg's spectral denoiser, 'off', or "
                         "START,DURATION of a silent window to learn a sox "
                         "noise profile from (e.g. 0,1.5)")
    ap.add_argument("--denoise-amount", type=float, default=0.21,
                    help="how hard sox subtracts the learned profile, 0-1. "
                         "Past about 0.3 it starts taking the quiet parts of "
                         "the voice with it (default 0.21)")
    ap.add_argument("--highpass", type=int, default=80,
                    help="rumble filter in Hz; 0 disables")
    ap.add_argument("--lowpass", type=int, default=0,
                    help="hiss filter in Hz; 0 disables. 12000 tames a harsh mic")
    ap.add_argument("--no-deess", dest="deess", action="store_false",
                    help="skip sibilance reduction")
    ap.add_argument("--no-gate", dest="gate", action="store_false",
                    help="keep the room tone between sentences")
    ap.add_argument("--no-compress", dest="compress", action="store_false")
    ap.add_argument("--mono", action="store_true",
                    help="fold to mono — right for a single voice, and it "
                         "halves the size of every downstream file")
    ap.add_argument("--room-tone", type=float, default=0.0,
                    help="seconds of silence to prepend, so an editor has "
                         "something to cut on")
    ap.add_argument("--brand", help="brand profile")
    args = ap.parse_args()

    src = Path(args.input)
    info = S.probe(src)
    dst = Path(args.output)
    dst.parent.mkdir(parents=True, exist_ok=True)

    tokens = S.tokens(args.brand)
    target_i, target_tp = S.targets(tokens, args.target, args.true_peak)

    print(f"◈ {src.name} → {dst.name}  {S.DIM}({info['duration']:.1f}s, "
          f"{info['channels']}ch @ {info['rate']}Hz){S.RESET}")
    before = S.measure(src)
    print(f"  {S.DIM}in:  {before['i']:.1f} LUFS / {before['tp']:.1f} dBTP"
          f"{S.RESET}")

    staging = Path(tempfile.mkdtemp(prefix="cs-sound-"))
    try:
        source = src
        if args.denoise not in ("auto", "off"):
            source = noise_profile(src, args.denoise, staging,
                                   args.denoise_amount)
            info = S.probe(source)

        chain = build_chain(args, info)
        repaired = S.intermediate("repaired", staging)
        S.ffmpeg(["-i", str(source), "-map", "a:0",
                  *(["-af", ",".join(chain)] if chain else []),
                  "-ar", str(S.RATE), "-c:a", "pcm_s24le", str(repaired)])

        # Two passes, and both are necessary. The first measures what the
        # repaired file actually is; the second applies one exact gain from
        # that measurement. A single pass has to guess as it goes and lands
        # up to 2 LU off, which is audible when the track is cut against
        # music that was normalised properly.
        stats = S.measure(repaired)
        S.ffmpeg(["-i", str(repaired),
                  "-af", S.loudnorm_filter(stats, target_i, target_tp, args.lra),
                  *S.encode_args(dst), str(dst)])

        S.report(dst, f"{info['duration'] + args.room_tone:.1f}s")
        S.report_loudness(dst, target_i, target_tp)
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
