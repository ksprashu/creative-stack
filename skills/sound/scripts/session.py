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

"""Build a Reaper session (.RPP) from stems, for when the mix needs hands on it.

    python3 session.py -o show.rpp -t voice.wav -t bed.wav
    python3 session.py -o show.rpp -t voice.wav -t "bed.wav@4.0:-16" --duck
    python3 session.py -o show.rpp -t voice.wav -t bed.mid --render show.wav

`mix.py` is the autonomous path and it is the one to reach for first. This is
the escape hatch: when a mix needs a human ear — riding a fader through a
tricky passage, retiming a music hit, comping two takes — this puts every stem
on its own track at the right offset with the right gain, so the session opens
ready to work instead of ready to be assembled.

An .RPP is emitted rather than a ReaScript because a project file just opens.
A ReaScript needs Reaper already running, the script installed, and an action
triggered — three manual steps to reach the same place.
"""

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import soundlib as S  # noqa: E402


def parse_track(spec):
    """'file.wav', 'file.wav@4.0' or 'file.wav@4.0:-16' -> (Path, start, gain)."""
    path, _, rest = spec.partition("@")
    source = Path(path).expanduser()
    if not source.is_file():
        S.die(f"no such stem: {path}")
    start, _, gain = rest.partition(":")
    try:
        start = float(start) if start else 0.0
        gain = float(gain) if gain else 0.0
    except ValueError:
        S.die(f"--track wants FILE[@START][:GAIN_DB], got '{spec}'")
    return source, start, gain


def source_block(path, length, indent):
    """The <SOURCE ...> chunk for one media item."""
    pad = " " * indent
    if path.suffix.lower() in (".mid", ".midi"):
        # Reaper can reference an external .mid as a MIDIFILE source. Inline
        # MIDI would mean re-encoding every event into Reaper's own text
        # format; a reference keeps bed.py as the single source of truth.
        return [f"{pad}<SOURCE MIDIFILE", f'{pad}  FILE "{path}"', f"{pad}>"]
    return [f"{pad}<SOURCE WAVE", f'{pad}  FILE "{path}"', f"{pad}>"]


MIDI_CAVEAT = ("a MIDI item makes no sound in Reaper until the track has an "
               "instrument on it — add one, or render the .mid to audio with "
               "bed.py first")


def build_rpp(dst, tracks, sample_rate, render_to=None):
    """Write a Reaper project. The format is line-oriented text: tokens, then
    optional `<CHUNK ... >` blocks nested by two spaces."""
    longest = 0.0
    lines = [
        '<REAPER_PROJECT 0.1 "7.0" 0',
        "  RIPPLE 0",
        "  RECORD_PATH \"\" \"\"",
        f"  SAMPLERATE {sample_rate} 0 0",
        "  TEMPO 120 4 4",
    ]
    if render_to:
        # Without these, -renderproject still renders — to the project's own
        # directory under the project's name, which is not where the caller
        # asked for it. RENDER_PATTERN is the file stem; RENDER_FILE is the
        # directory it lands in.
        lines += [
            f'  RENDER_FILE "{render_to.parent.resolve()}"',
            f'  RENDER_PATTERN "{render_to.stem}"',
            # Third field is the render sample rate. Leaving it 0 does not
            # inherit SAMPLERATE above — Reaper falls back to its own global
            # preference, which is 44100 out of the box, and the render comes
            # back resampled without saying so.
            f"  RENDER_FMT 0 2 {sample_rate}",
            "  RENDER_RANGE 1 0 0 18 1000",
            "  RENDER_STEMS 0",
            "  RENDER_ADDTOPROJ 0",
        ]
    for index, (path, start, gain) in enumerate(tracks, 1):
        if path.suffix.lower() in (".mid", ".midi"):
            # ffprobe reads MIDI duration only with the right demuxer on some
            # builds; two minutes is a safe visible length and the user will
            # trim it anyway.
            length = 120.0
        else:
            length = S.probe(path)["duration"]
        longest = max(longest, start + length)
        volume = 10 ** (gain / 20)
        lines += [
            "  <TRACK",
            f'    NAME "{path.stem}"',
            f"    VOLPAN {volume:.6f} 0 -1 -1 1",
            "    MUTESOLO 0 0 0",
            "    <ITEM",
            f"      POSITION {start:.6f}",
            f"      LENGTH {length:.6f}",
            "      LOOP 0",
            f'      NAME "{path.name}"',
            *source_block(path.resolve(), length, 6),
            "    >",
            "  >",
        ]
    lines.append(">")

    dst.write_text("\n".join(lines) + "\n")
    return longest


def render(project, dst):
    """Ask Reaper to render the project from the command line.

    -renderproject is the only genuinely headless thing Reaper does; it still
    launches the app, renders, and exits. There is no exit-code contract, so
    the output file is the only thing worth believing.
    """
    binary = shutil.which("reaper")
    if not binary:
        app = Path("/Applications/REAPER.app/Contents/MacOS/REAPER")
        binary = str(app) if app.exists() else None
    if not binary:
        S.die("reaper not found. " + S.hostenv.package_hint("reaper"))

    before = dst.stat().st_mtime if dst.exists() else 0
    subprocess.run([binary, "-renderproject", str(project)],
                   capture_output=True, text=True, timeout=300)
    # Reaper returns before the render has been flushed on some versions.
    for _ in range(60):
        if dst.exists() and dst.stat().st_mtime > before and dst.stat().st_size:
            return True
        time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", "-o", required=True, help="the .rpp to write")
    ap.add_argument("--track", "-t", action="append", default=[], required=True,
                    metavar="FILE[@START][:GAIN_DB]",
                    help="a stem, in track order; repeatable")
    ap.add_argument("--rate", type=int, default=S.RATE)
    ap.add_argument("--duck", action="store_true",
                    help="drop every track after the first by --duck-gain, as "
                         "a starting point for riding them by hand")
    ap.add_argument("--duck-gain", type=float, default=-16.0)
    ap.add_argument("--render", help="also render the project to this file "
                                     "(needs Reaper installed)")
    ap.add_argument("--open", action="store_true",
                    help="open the finished project in Reaper")
    args = ap.parse_args()

    dst = Path(args.output)
    if dst.suffix.lower() != ".rpp":
        dst = dst.with_suffix(".rpp")
    dst.parent.mkdir(parents=True, exist_ok=True)

    tracks = [parse_track(spec) for spec in args.track]
    if args.duck:
        tracks = [(path, start, gain if index == 0 else gain + args.duck_gain)
                  for index, (path, start, gain) in enumerate(tracks)]

    print(f"◈ {len(tracks)} stem(s) → {dst.name}")
    if any(p.suffix.lower() in (".mid", ".midi") for p, _, _ in tracks):
        print(f"  {S.YELLOW}!{S.RESET} {MIDI_CAVEAT}")
    length = build_rpp(dst, tracks, args.rate,
                       Path(args.render) if args.render else None)
    for path, start, gain in tracks:
        print(f"  {S.DIM}{path.name:<28} @{start:>7.2f}s  "
              f"{gain:+.1f}dB{S.RESET}")
    S.report(dst, f"{length:.1f}s timeline")

    if args.render:
        target = Path(args.render)
        if not render(dst, target):
            S.die(f"Reaper did not write {target}. Open {dst} and check the "
                  f"render settings (File -> Render).")
        S.report(target)
    if args.open:
        opener = "open" if S.hostenv.IS_MAC else "xdg-open"
        subprocess.run([opener, str(dst)], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
