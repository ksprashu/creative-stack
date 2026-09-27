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

"""Generate a royalty-free music bed or logo sting, as MIDI or audio.

    python3 bed.py -o bed.wav --style ambient --duration 90
    python3 bed.py -o sting.wav --style corporate --sting
    python3 bed.py -o bed.mid --style lofi --bars 16 --key F --seed 7
    python3 bed.py -o bed.wav --style upbeat --bpm 112 --duration 45

Nothing here is sampled or scraped: the notes are written by this script and
played by a General MIDI synth, so the result is yours to publish. It will
never be as good as a real composer. It is background, and it is free of
licensing questions when a video is going onto a company blog.

Writing a `.mid` gives you the raw MIDI to drag into a DAW instead — see also
`reaper.py`, which builds the session around it.
"""

import argparse
import math
import random
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import soundlib as S  # noqa: E402

TICKS = 480
BEATS_PER_BAR = 4

SEMITONES = {"C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4,
             "F": 5, "F#": 6, "GB": 6, "G": 7, "G#": 8, "AB": 8, "A": 9,
             "A#": 10, "BB": 10, "B": 11}

SCALES = {
    "major":  [0, 2, 4, 5, 7, 9, 11],
    "minor":  [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11],
}

# Each style is a progression in scale degrees (0 = tonic), the scale it sits
# in, a default tempo, and the General MIDI voices that play it. `role` picks
# the pattern the voice is given in render_bar().
STYLES = {
    "ambient": {
        "scale": "minor", "bpm": 72, "progression": [0, 5, 2, 6],
        "voices": [
            {"program": 89, "role": "pad", "octave": 4, "velocity": 54},
            {"program": 88, "role": "pad", "octave": 3, "velocity": 40},
            {"program": 98, "role": "sparkle", "octave": 6, "velocity": 44},
        ],
    },
    "corporate": {
        "scale": "major", "bpm": 100, "progression": [0, 4, 5, 3],
        "voices": [
            {"program": 0, "role": "arp", "octave": 5, "velocity": 62},
            {"program": 48, "role": "pad", "octave": 4, "velocity": 46},
            {"program": 33, "role": "bass", "octave": 3, "velocity": 70},
        ],
    },
    "upbeat": {
        "scale": "major", "bpm": 118, "progression": [0, 5, 3, 4],
        "voices": [
            {"program": 4, "role": "arp", "octave": 5, "velocity": 66},
            {"program": 24, "role": "pluck", "octave": 4, "velocity": 58},
            {"program": 33, "role": "bass", "octave": 3, "velocity": 74},
            {"program": None, "role": "drums", "octave": 0, "velocity": 78},
        ],
    },
    "lofi": {
        "scale": "dorian", "bpm": 84, "progression": [0, 3, 5, 4],
        "voices": [
            {"program": 4, "role": "arp", "octave": 4, "velocity": 56},
            {"program": 89, "role": "pad", "octave": 3, "velocity": 38},
            {"program": 32, "role": "bass", "octave": 3, "velocity": 66},
        ],
    },
    "tension": {
        "scale": "minor", "bpm": 92, "progression": [0, 1, 0, 6],
        "voices": [
            {"program": 49, "role": "pad", "octave": 4, "velocity": 58},
            {"program": 42, "role": "pluck", "octave": 3, "velocity": 50},
            {"program": 43, "role": "bass", "octave": 3, "velocity": 76},
        ],
    },
}


def load_mido():
    try:
        import mido  # noqa: PLC0415
    except ImportError:
        S.die("mido not installed. Run: "
              "uv pip install --python .venv/bin/python mido\n"
              "   then re-run this with .venv/bin/python instead of python3.")
    return mido


def chord(scale, tonic, degree, octave, seventh=False):
    """Triad (or 7th) built on a scale degree, as MIDI note numbers."""
    steps = [degree, degree + 2, degree + 4] + ([degree + 6] if seventh else [])
    notes = []
    for step in steps:
        octave_shift, index = divmod(step, len(scale))
        notes.append(tonic + 12 * (octave + octave_shift) + scale[index])
    return notes


def render_bar(track, mido, voice, notes, rng, bar_ticks):
    """Append one bar of `voice`'s pattern over `notes` to a mido track."""
    role = voice["role"]
    base = voice["velocity"]

    def emit(pitch, start, length, velocity):
        events.append((start, "on", pitch, max(1, min(127, velocity))))
        events.append((start + length, "off", pitch, 0))

    events = []
    if role == "pad":
        for pitch in notes:
            emit(pitch, 0, bar_ticks, base + rng.randint(-4, 4))
    elif role == "bass":
        # The chord root as written, not an octave below it. The voice's own
        # octave already places it; dropping another 12 puts the root under
        # MIDI 24, where GM basses have no usable samples and the note reads
        # as mud rather than pitch.
        for beat in (0, 2):
            emit(notes[0], beat * TICKS, TICKS - 20, base + rng.randint(-5, 5))
    elif role == "arp":
        # Eighth notes walking up and back down the chord. Ties the bar to a
        # pulse without needing drums.
        pattern = notes + notes[-2:0:-1]
        for step in range(BEATS_PER_BAR * 2):
            pitch = pattern[step % len(pattern)]
            emit(pitch, step * TICKS // 2, TICKS // 2 - 15,
                 base + rng.randint(-8, 6))
    elif role == "pluck":
        for beat in (1, 3):
            emit(notes[rng.randrange(len(notes))], beat * TICKS,
                 TICKS // 2, base + rng.randint(-6, 6))
    elif role == "sparkle":
        if rng.random() < 0.6:
            emit(notes[rng.randrange(len(notes))],
                 rng.choice([0, 2, 3]) * TICKS, TICKS, base)
    elif role == "drums":
        # GM percussion keys: 36 kick, 38 snare, 42 closed hat.
        for beat in range(BEATS_PER_BAR):
            emit(42, beat * TICKS, 60, base - 20)
            emit(42, beat * TICKS + TICKS // 2, 60, base - 28)
            if beat in (0, 2):
                emit(36, beat * TICKS, 120, base)
            else:
                emit(38, beat * TICKS, 120, base - 6)

    clock = 0
    for when, kind, pitch, velocity in sorted(events, key=lambda e: (e[0], e[1])):
        track.append(mido.Message(
            "note_on" if kind == "on" else "note_off",
            note=pitch, velocity=velocity, time=when - clock,
            channel=voice["channel"]))
        clock = when
    if clock < bar_ticks:
        track.append(mido.Message("note_off", note=notes[0], velocity=0,
                                  time=bar_ticks - clock,
                                  channel=voice["channel"]))


def build_midi(path, style, key, bars, bpm, seed, sting):
    mido = load_mido()
    rng = random.Random(seed)
    spec = STYLES[style]
    scale = SCALES[spec["scale"]]
    tonic = SEMITONES[key.upper()]
    bar_ticks = TICKS * BEATS_PER_BAR

    midi = mido.MidiFile(ticks_per_beat=TICKS)
    meta = mido.MidiTrack()
    meta.append(mido.MetaMessage("set_tempo",
                                 tempo=mido.bpm2tempo(bpm), time=0))
    midi.tracks.append(meta)

    # Channel 9 is percussion in General MIDI and nothing else may use it; the
    # melodic voices take the remaining channels in order.
    melodic = (c for c in range(16) if c != 9)
    # Copy, rather than annotating STYLES in place — the module-level table is
    # shared, and a second build_midi() call in the same process must not see
    # channels left over from the first.
    voices = [dict(v) for v in spec["voices"]]
    for voice in voices:
        voice["channel"] = 9 if voice["role"] == "drums" else next(melodic)

    progression = spec["progression"]
    hold_bars = 0
    if sting:
        # A sting is a gesture, not a loop: one turn away from home and a
        # landing on the tonic. Anything longer stops being a sting and starts
        # being an intro that the viewer has to sit through.
        progression = [progression[3], 0]
        bars = len(progression)
        hold_bars = 1

    for voice in voices:
        track = mido.MidiTrack()
        midi.tracks.append(track)
        if voice["program"] is not None:
            track.append(mido.Message("program_change",
                                      program=voice["program"],
                                      channel=voice["channel"], time=0))
        for index in range(bars):
            degree = progression[index % len(progression)]
            notes = chord(scale, tonic, degree, voice["octave"],
                          seventh=style in ("lofi", "ambient"))
            if hold_bars and index == len(progression) - 1 \
                    and voice["role"] != "drums":
                # Land on the tonic and let it ring. `hold_bars` is counted
                # into the returned length so the trim downstream does not
                # slice the decay back off.
                for pitch in notes:
                    track.append(mido.Message("note_on", note=pitch,
                                              velocity=voice["velocity"],
                                              channel=voice["channel"], time=0))
                track.append(mido.Message("note_off", note=notes[0], velocity=0,
                                          channel=voice["channel"],
                                          time=bar_ticks * hold_bars))
                continue
            render_bar(track, mido, voice, notes, rng, bar_ticks)

        # fluidsynth stops rendering the instant the last MIDI event passes, so
        # without a trailing beat of nothing every release envelope and the
        # whole reverb tail are chopped off mid-decay. One silent note_off two
        # bars out is enough to keep the synth running through the tail.
        track.append(mido.Message("note_off", note=0, velocity=0,
                                  channel=voice["channel"], time=bar_ticks * 2))

    midi.save(str(path))
    total_bars = bars + hold_bars
    return total_bars, total_bars * BEATS_PER_BAR * 60.0 / bpm


def render_audio(midi_path, wav_path, soundfont, gain, reverb):
    cmd = [S.need("fluidsynth"),
           # -n no MIDI input, -i no interactive shell. Without both, a
           # headless run opens a prompt on stdin and hangs forever.
           "-ni", "-F", str(wav_path), "-r", str(S.RATE), "-g", str(gain),
           "-o", f"synth.reverb.active={'yes' if reverb else 'no'}"]
    if reverb:
        cmd += ["-o", "synth.reverb.room-size=0.75",
                "-o", "synth.reverb.level=0.7"]
    cmd += [str(soundfont), str(midi_path)]
    S.run(cmd, capture=True)   # fluidsynth prints a licence banner on every run
    if not wav_path.exists() or wav_path.stat().st_size < 1000:
        S.die(f"fluidsynth produced no audio. The SoundFont at {soundfont} may "
              f"be a placeholder rather than a General MIDI bank.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", "-o", required=True,
                    help=".wav/.mp3/.flac/... to render, or .mid for raw MIDI")
    ap.add_argument("--style", choices=sorted(STYLES), default="ambient")
    ap.add_argument("--sting", action="store_true",
                    help="a short resolving gesture for a logo, not a loop")
    ap.add_argument("--bars", type=int, help="length in bars (overrides --duration)")
    ap.add_argument("--duration", type=float, default=60.0,
                    help="target length in seconds, rounded up to a whole bar")
    ap.add_argument("--bpm", type=int, help="tempo (default: per style)")
    ap.add_argument("--key", default="C", help="tonic: C, F#, Bb ...")
    ap.add_argument("--seed", type=int, default=0,
                    help="same seed and flags give byte-identical MIDI")
    ap.add_argument("--soundfont", help="path to a .sf2/.sf3 bank")
    ap.add_argument("--gain", type=float, default=0.6,
                    help="fluidsynth output gain; above 1.0 it clips")
    ap.add_argument("--no-reverb", dest="reverb", action="store_false")
    ap.add_argument("--fade", type=float,
                    help="fade-out in seconds; 0 for a hard end. Default 3 for "
                         "a bed, 0 for a sting — a sting already ends on a "
                         "chord that is decaying on its own")
    ap.add_argument("--target", type=float,
                    help="LUFS. A bed that goes under a voice does not need "
                         "to be loud — mix.py sets the relative level")
    ap.add_argument("--true-peak", type=float, dest="true_peak")
    ap.add_argument("--brand", help="brand profile")
    args = ap.parse_args()

    if args.key.upper() not in SEMITONES:
        S.die(f"unknown key '{args.key}'. Use one of: "
              f"{', '.join(sorted(set(SEMITONES)))}")

    spec = STYLES[args.style]
    # A sting rides noticeably faster than the same style's bed. Three bars at
    # a bed tempo is a twelve-second sting, which is not a sting.
    bpm = args.bpm or (round(spec["bpm"] * 1.4) if args.sting else spec["bpm"])
    fade = args.fade if args.fade is not None else (0.0 if args.sting else 3.0)
    seconds_per_bar = BEATS_PER_BAR * 60.0 / bpm
    bars = args.bars or max(1, math.ceil(args.duration / seconds_per_bar))

    dst = Path(args.output)
    dst.parent.mkdir(parents=True, exist_ok=True)

    print(f"◈ {args.style}{' sting' if args.sting else ''} in "
          f"{args.key.upper()} {spec['scale']} → {dst.name}  "
          f"{S.DIM}({bpm}bpm, seed {args.seed}){S.RESET}")

    if dst.suffix.lower() == ".mid":
        bars, length = build_midi(dst, args.style, args.key, bars, bpm,
                                  args.seed, args.sting)
        S.report(dst, f"{length:.1f}s of MIDI, {bars} bars")
        return 0

    tokens = S.tokens(args.brand)
    target_i, target_tp = S.targets(tokens, args.target, args.true_peak)
    soundfont = S.soundfont(args.soundfont)

    staging = Path(tempfile.mkdtemp(prefix="cs-bed-"))
    try:
        midi = staging / "bed.mid"
        bars, length = build_midi(midi, args.style, args.key, bars, bpm,
                                  args.seed, args.sting)

        raw = S.intermediate("raw", staging)
        render_audio(midi, raw, soundfont, args.gain, args.reverb)

        trimmed = S.intermediate("trimmed", staging)
        chain = [f"atrim=0:{length:.3f}", "asetpts=N/SR/TB"]
        if fade and fade < length:
            chain.append(f"afade=t=out:st={length - fade:.3f}:d={fade}")
        S.ffmpeg(["-i", str(raw), "-af", ",".join(chain),
                  "-ar", str(S.RATE), "-c:a", "pcm_s24le", str(trimmed)])

        stats = S.measure(trimmed)
        S.ffmpeg(["-i", str(trimmed),
                  "-af", S.loudnorm_filter(stats, target_i, target_tp),
                  *S.encode_args(dst), str(dst)])

        S.report(dst, f"{length:.1f}s, {bars} bars")
        S.report_loudness(dst, target_i, target_tp)
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
