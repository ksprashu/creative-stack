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

"""Lay music and effects under a voice track and master the result.

    python3 mix.py -v voice.wav -m bed.wav -o episode.wav
    python3 mix.py -v voice.wav -m bed.wav -o intro.m4a --lead 3 --tail 4
    python3 mix.py -v voice.wav -m bed.wav -s whoosh.wav@12.5 -s ding.wav@40 -o final.wav

The music ducks under the voice automatically — a sidechain compressor keyed
off the voice, not a static fader — so it stays audible in the gaps and gets
out of the way the moment anyone speaks. `--lead` and `--tail` give the bed
room to breathe on its own at either end.

Feed it a track that has already been through `clean.py`. This stage mixes and
masters; it does not repair.
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import soundlib as S  # noqa: E402

# Everything is forced through this before it meets another stream.
# sidechaincompress and amix both refuse to run on inputs whose sample rate or
# channel layout disagree, and a 44.1kHz stock music loop under a 48kHz
# voiceover is the normal case, not the exception.
FMT = f"aformat=sample_fmts=fltp:sample_rates={S.RATE}:channel_layouts=stereo"


def speech_intervals(path, noise_db, min_silence, merge_gap, offset=0.0):
    """[(start, end)] of everything that is not silence, in seconds.

    ffmpeg's silencedetect reports the gaps; the speech is what is left over.
    Intervals closer together than `merge_gap` are fused, because a bed that
    dips back up for the third of a second between two sentences is more
    distracting than one that simply stays down through the paragraph.
    """
    duration = S.probe(path)["duration"]
    proc = S.run([S.need("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path),
                  "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
                  "-f", "null", "-"], capture=True)
    silences, start = [], None
    for line in (proc.stderr or "").splitlines():
        if "silence_start:" in line:
            start = float(line.rsplit("silence_start:", 1)[1].split()[0])
        elif "silence_end:" in line and start is not None:
            silences.append((start, float(line.rsplit("silence_end:", 1)[1]
                                          .split()[0])))
            start = None
    if start is not None:
        silences.append((start, duration))

    speech, cursor = [], 0.0
    for begin, end in silences:
        if begin > cursor:
            speech.append((cursor, begin))
        cursor = end
    if cursor < duration:
        speech.append((cursor, duration))

    merged = []
    for begin, end in speech:
        if merged and begin - merged[-1][1] < merge_gap:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((begin, end))
    return [(a + offset, b + offset) for a, b in merged]


def duck_envelope(intervals, duck_db, attack, release):
    """A `volume` expression that is 1.0 in the gaps and -duck_db under speech.

    This is deliberately not `sidechaincompress`. That filter's gain reduction
    is a function of how far the key runs over an opaque linear threshold, and
    measuring it across the parameter space gives anything from 0 to 30dB for
    the same nominal settings — there is no way to ask it for "6dB under the
    voice" and get 6dB. An explicit envelope does exactly what the flag says,
    every time, and the ramps are visible in the expression.
    """
    floor = 10 ** (-duck_db / 20)
    expr = "1"
    for begin, end in reversed(intervals):
        fade_in = max(0.0, begin - attack)
        fade_out = end + release
        # Ramp down over `attack` before the phrase, hold, ramp back over
        # `release` after it; 1.0 everywhere else. Segments are combined with
        # min() so overlapping ramps take the deeper of the two.
        seg = (f"if(lt(t,{fade_in:.3f}),1,"
               f"if(lt(t,{begin:.3f}),1-{1 - floor:.5f}*(t-{fade_in:.3f})/{attack:.3f},"
               f"if(lt(t,{end:.3f}),{floor:.5f},"
               f"if(lt(t,{fade_out:.3f}),{floor:.5f}+{1 - floor:.5f}*(t-{end:.3f})/{release:.3f},"
               f"1))))")
        expr = f"min({seg},{expr})"
    return expr


def parse_sfx(spec):
    """'file.wav@12.5' or 'file.wav@12.5:-6' -> (Path, seconds, gain_db)."""
    path, _, rest = spec.rpartition("@")
    if not path:
        S.die(f"--sfx wants FILE@SECONDS, got '{spec}'")
    when, _, gain = rest.partition(":")
    try:
        when = float(when)
        gain = float(gain) if gain else 0.0
    except ValueError:
        S.die(f"--sfx wants FILE@SECONDS[:GAIN_DB], got '{spec}'")
    source = Path(path)
    if not source.is_file():
        S.die(f"no such effect: {path}")
    return source, when, gain


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--voice", "-v", required=True, help="the cleaned voice track")
    ap.add_argument("--music", "-m", help="music bed; looped if it is shorter "
                                          "than the voice")
    ap.add_argument("--sfx", "-s", action="append", default=[],
                    metavar="FILE@SECONDS[:GAIN_DB]",
                    help="a one-shot effect at a timestamp; repeatable")
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--lead", type=float, default=0.0,
                    help="seconds of music alone before the voice starts")
    ap.add_argument("--tail", type=float, default=0.0,
                    help="seconds of music alone after the voice ends")
    ap.add_argument("--music-gain", type=float, default=-16.0,
                    help="music level in dB relative to the voice, before "
                         "ducking (default -16)")
    ap.add_argument("--duck", type=float, default=12.0,
                    help="how far the music drops under speech, in dB. 0 turns "
                         "ducking off and leaves a flat bed")
    ap.add_argument("--attack", type=float, default=0.25,
                    help="seconds the bed takes to drop as a phrase starts")
    ap.add_argument("--release", type=float, default=0.6,
                    help="seconds the bed takes to come back after a phrase")
    ap.add_argument("--duck-floor", type=float, default=-38.0,
                    help="dB below which the voice counts as silence for "
                         "duck detection. Raise it toward -30 if a noisy "
                         "recording keeps the bed permanently down")
    ap.add_argument("--duck-hold", type=float, default=0.4,
                    help="seconds of quiet before the bed is allowed to rise")
    ap.add_argument("--fade", type=float, default=2.0,
                    help="music fade in/out in seconds")
    ap.add_argument("--target", type=float, help="LUFS (default: brand token)")
    ap.add_argument("--true-peak", type=float, dest="true_peak",
                    help="dBTP ceiling (default: brand token)")
    ap.add_argument("--lra", type=float, default=9.0)
    ap.add_argument("--brand", help="brand profile")
    args = ap.parse_args()

    voice = Path(args.voice)
    vinfo = S.probe(voice)
    dst = Path(args.output)
    dst.parent.mkdir(parents=True, exist_ok=True)

    sfx = [parse_sfx(spec) for spec in args.sfx]
    total = args.lead + vinfo["duration"] + args.tail
    overrun = [(p, t) for p, t, _ in sfx if t > total]
    if overrun:
        S.die(f"{overrun[0][0].name} is placed at {overrun[0][1]}s but the mix "
              f"is only {total:.1f}s long.")

    tokens = S.tokens(args.brand)
    target_i, target_tp = S.targets(tokens, args.target, args.true_peak)

    inputs, chain, mix_in = [], [], []

    # -- voice ---------------------------------------------------------------
    inputs += ["-i", str(voice)]
    lead_ms = int(args.lead * 1000)
    delay = f",adelay={lead_ms}|{lead_ms}" if lead_ms else ""
    chain.append(f"[0:a]{FMT}{delay}[voice]")
    mix_in.append("[voice]")

    # -- music ---------------------------------------------------------------
    if args.music:
        music = Path(args.music)
        minfo = S.probe(music)
        # -stream_loop has to precede its own -i, and -1 means forever; atrim
        # is what actually ends the mix. Looping a bed that is already long
        # enough is harmless and saves branching on the duration.
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        if minfo["duration"] < total:
            print(f"  {S.DIM}looping {music.name} "
                  f"({minfo['duration']:.1f}s) to cover {total:.1f}s{S.RESET}")
        fade_out = max(0.0, total - args.fade)
        chain.append(
            f"[1:a]{FMT},atrim=0:{total:.3f},asetpts=N/SR/TB,"
            f"volume={args.music_gain}dB,"
            f"afade=t=in:st=0:d={args.fade},"
            f"afade=t=out:st={fade_out:.3f}:d={args.fade}[bed]")
        phrases = []
        if args.duck > 0:
            phrases = speech_intervals(voice, args.duck_floor, args.duck_hold,
                                       args.attack + args.release + 0.25,
                                       offset=args.lead)
            if not phrases:
                print(f"  {S.YELLOW}!{S.RESET} no speech found in "
                      f"{voice.name} — leaving the bed flat")
        if phrases:
            print(f"  {S.DIM}ducking {args.duck}dB under "
                  f"{len(phrases)} phrase(s){S.RESET}")
            expr = duck_envelope(phrases, args.duck, args.attack, args.release)
            # eval=frame, not the default eval=once: the whole point is a gain
            # that changes over time, and `once` would bake in the value at
            # t=0 and hold it for the entire mix.
            chain.append(f"[bed]volume=volume='{expr}':eval=frame[music]")
        else:
            chain.append("[bed]anull[music]")
        mix_in.append("[music]")

    # -- effects -------------------------------------------------------------
    for index, (path, when, gain) in enumerate(sfx):
        stream = (2 if args.music else 1) + index
        inputs += ["-i", str(path)]
        offset = int((when + args.lead) * 1000)
        chain.append(f"[{stream}:a]{FMT},volume={gain}dB,"
                     f"adelay={offset}|{offset}[sfx{index}]")
        mix_in.append(f"[sfx{index}]")

    # -- sum -----------------------------------------------------------------
    # normalize=0 is not optional: amix's default divides every input by the
    # number of inputs, so adding a music bed would quietly drop the voice by
    # 6dB and adding a second effect by another 1.5dB.
    chain.append(f"{''.join(mix_in)}amix=inputs={len(mix_in)}:"
                 f"duration=longest:normalize=0,"
                 f"atrim=0:{total:.3f},asetpts=N/SR/TB[out]")

    print(f"◈ {voice.name}"
          f"{' + ' + Path(args.music).name if args.music else ''}"
          f"{f' + {len(sfx)} sfx' if sfx else ''} → {dst.name}  "
          f"{S.DIM}({total:.1f}s, duck {args.duck}dB){S.RESET}")

    staging = Path(tempfile.mkdtemp(prefix="cs-mix-"))
    try:
        summed = S.intermediate("summed", staging)
        S.ffmpeg([*inputs, "-filter_complex", ";".join(chain),
                  "-map", "[out]", "-ar", str(S.RATE), "-c:a", "pcm_s24le",
                  str(summed)])

        stats = S.measure(summed)
        S.ffmpeg(["-i", str(summed),
                  "-af", S.loudnorm_filter(stats, target_i, target_tp, args.lra),
                  *S.encode_args(dst), str(dst)])

        S.report(dst, f"{total:.1f}s")
        S.report_loudness(dst, target_i, target_tp)
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
