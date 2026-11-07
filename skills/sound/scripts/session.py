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

"""Build and produce a Reaper session (.RPP) from stems, karaoke tracks, or vocal takes.

    # basic multi-stem session
    python3 session.py -o show.rpp -t voice.wav -t bed.wav
    python3 session.py -o show.rpp -t voice.wav -t "bed.wav@4.0:-16" --duck
    python3 session.py -o show.rpp -t voice.wav -t bed.mid --render show.wav

    # live karaoke recording session: record-armed mic + gain-staged backing + muted lyric video
    python3 session.py -o out/song/song.rpp --arm-mic --monitoring on \\
        --karaoke-backing out/song/backing-track.wav \\
        --video out/song/karaoke-video.mp4 --markers out/song/lyrics.md \\
        --tempo 98 --open

    # vocal production & mastering inside Reaper (native FX + buses + automation + live Lua)
    python3 session.py -o out/song/song.rpp \\
        --karaoke-backing out/song/backing-track.wav \\
        --produce-vocal "out/song/recordings/01-Lead Vocals (Mic).wav" \\
        --video out/song/karaoke-video.mp4 --markers out/song/lyrics.md \\
        --tempo 98 --apply-live

`mix.py` is the autonomous path and it is the one to reach for first. This is
the escape hatch: when a mix needs a human ear — riding a fader through a
tricky passage, retiming a music hit, comping two takes, or producing a vocal
take inside Reaper with native FX — this puts every stem, bus, automation lane,
and FX chain on its own track so the session opens ready to review or render.

An .RPP is emitted first so the project opens cleanly on any machine; when
`--produce-vocal` is used, a companion `.produce.json` plan and `.produce.lua`
ReaScript are also emitted so `REAPER -nonewinst` can set exact `ReaEQ`,
`ReaDelay`, and `ReaVerbate` parameters live and write `fx-report.txt`.
"""

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import soundlib as S  # noqa: E402


def db_to_linear_gain(db):
    """Convert dB gain to linear amplitude ratio (`10 ^ (dB / 20)`).

    Reaper's `.rpp` `VOLPAN` / `AUXRECV` / `VOLENV` tokens AND ReaScript's
    `reaper.TrackFX_SetEQParam(tr, fx, bandtype, bandidx, 1, val, false)`
    all expect linear amplitude ratios rather than raw dB. Passing `0.0` to
    `TrackFX_SetEQParam` with `isnorm=false` sets the band to `-inf dB`.
    """
    return 10.0 ** (float(db) / 20.0)


def reaper_delay_musical_norm(eighth_notes=2.0):
    """Convert delay length in 1/8th notes to ReaDelay's normalized param 4.

    ReaDelay maps parameter 4 (`Length (musical)`) linearly across `[0, 256]`
    eighth notes, so a quarter-note echo (`2.0` eighth notes) is `2 / 256 =
    0.0078125`, and a full 4/4 bar (`8.0` eighth notes) is `8 / 256 = 0.03125`.
    """
    val = float(eighth_notes) / 256.0
    return max(0.0, min(1.0, val))


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


def parse_timestamp_seconds(stamp):
    """Parse 'MM:SS', 'H:MM:SS', or float seconds string into float seconds."""
    text = str(stamp).strip().strip("`*")
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60.0 + float(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600.0 + int(parts[1]) * 60.0 + float(parts[2])
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_markers_text(text):
    """Extract `[(seconds, label), ...]` from a `lyrics.md` table or `MM:SS Label` lines."""
    markers = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("|---") or line.startswith("| :"):
            continue
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2:
                sec = parse_timestamp_seconds(cells[0])
                if sec is not None:
                    sec_name = re.sub(r"[*_`]", "", cells[1]).strip()
                    lyric = re.sub(r"[*_`\"]", "", cells[2]).strip() if len(cells) >= 3 else ""
                    label = f"{sec_name}: {lyric}".strip(": ") if lyric else sec_name
                    markers.append((sec, label[:80]))
            continue
        m = re.match(r"^(\d+:\d{2}(?::\d{2})?(?:\.\d+)?)\s+(.+)$", line)
        if m:
            sec = parse_timestamp_seconds(m.group(1))
            if sec is not None:
                markers.append((sec, m.group(2).strip()[:80]))
    return sorted(markers, key=lambda item: item[0])


def parse_markers_file(path):
    """Read timeline markers from a `.md` or `.txt` file."""
    p = Path(path).expanduser()
    if not p.is_file():
        S.die(f"markers file not found: {path}")
    return parse_markers_text(p.read_text(encoding="utf-8", errors="replace"))


def compute_vocal_automation_points(window_rms_db, silence_floor_db=-42.0,
                                    max_boost_db=4.8, max_cut_db=-2.8):
    """Compute Pre-FX `<VOLENV>` points `[(time_sec, linear_gain), ...]` from windowed RMS.

    Phrases that sit quieter than the vocal's median active RMS get a smooth
    Pre-FX boost (up to `max_boost_db`) so soft low-register verses feed the
    compressor consistently; phrases that jump well above the median get a
    gentle Pre-FX trim (down to `max_cut_db`).
    """
    active = [(t, db) for t, db in window_rms_db
              if db is not None and not math.isinf(db) and db > silence_floor_db]
    if not active:
        return [(0.0, 1.0)]
    sorted_dbs = sorted(db for _, db in active)
    median_db = sorted_dbs[len(sorted_dbs) // 2]

    points = [(0.0, 1.0)]
    last_gain_db = 0.0
    for t, db in window_rms_db:
        if db is None or math.isinf(db) or db <= silence_floor_db:
            target_db = 0.0
        else:
            delta = median_db - db
            # Apply 60% correction toward median, clamped to [max_cut_db, max_boost_db]
            target_db = max(float(max_cut_db), min(float(max_boost_db), delta * 0.6))
        if abs(target_db - last_gain_db) >= 0.6:
            points.append((round(float(t), 2), round(db_to_linear_gain(target_db), 6)))
            last_gain_db = target_db
    return points


def detect_active_bounds(window_rms_db, duration, silence_floor_db=-42.0,
                         pad_in=0.8, pad_out=1.5):
    """Return `(start_sec, length_sec)` trimming dead air before/after the vocal take."""
    active_times = [t for t, db in window_rms_db
                    if db is not None and not math.isinf(db) and db > silence_floor_db]
    if not active_times:
        return 0.0, float(duration)
    start = max(0.0, round(min(active_times) - pad_in, 2))
    end = min(float(duration), round(max(active_times) + pad_out, 2))
    length = max(1.0, round(end - start, 2))
    return start, length


def measure_window_rms(path, window_sec=2.0):
    """Measure RMS level (dBFS) in `window_sec` buckets across an audio file."""
    info = S.probe(path)
    samples_per_win = max(int(info["rate"] * window_sec), 1024)
    proc = S.run([
        S.need("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path),
        "-af", f"asetnsamples={samples_per_win},astats=metadata=1:reset=1,"
               "ametadata=print:key=lavfi.astats.Overall.RMS_level",
        "-f", "null", "-"
    ], capture=True, check=False)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    pts_re = re.compile(r"pts_time:([0-9.]+)")
    rms_re = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[0-9.]+|-inf)")
    windows = []
    cur_t = 0.0
    for line in out.splitlines():
        m_t = pts_re.search(line)
        if m_t:
            cur_t = float(m_t.group(1))
            continue
        m_r = rms_re.search(line)
        if m_r:
            val = m_r.group(1)
            db = float("-inf") if val == "-inf" else float(val)
            windows.append((round(cur_t, 2), db))
    return windows


def analyze_vocal_take(vocal_path, backing_path=None):
    """Measure vocal LUFS/TP/LRA, phrasing RMS windows, and compute production targets."""
    vocal_info = S.probe(vocal_path)
    vocal_lufs = S.measure(vocal_path)
    windows = measure_window_rms(vocal_path, window_sec=2.0)
    trim_start, trim_len = detect_active_bounds(windows, vocal_info["duration"])
    auto_pts = compute_vocal_automation_points(windows)

    backing_lufs = None
    backing_gain_db = -5.0
    if backing_path and Path(backing_path).is_file():
        backing_lufs = S.measure(backing_path)
        # Commercial karaoke tracks are often brickwalled at -8 to -10 LUFS.
        # Pull the instrumental down to around -14 LUFS so the vocal sits on top
        # without clipping the master bus.
        backing_gain_db = round(max(-8.0, min(0.0, -14.0 - backing_lufs["i"])), 1)

    return {
        "vocal_file": str(Path(vocal_path).resolve()),
        "duration": vocal_info["duration"],
        "rate": vocal_info["rate"],
        "channels": vocal_info["channels"],
        "vocal_lufs": vocal_lufs,
        "backing_lufs": backing_lufs,
        "backing_gain_db": backing_gain_db,
        "trim_start": trim_start,
        "trim_length": trim_len,
        "automation_points": auto_pts,
        "eq": {
            "hpf_hz": 95.0,
            "low_shelf_hz": 180.0,
            "low_shelf_db": 1.2,
            "mud_hz": 330.0,
            "mud_db": -4.0,
            "mud_bw": 0.75,
            "presence_hz": 3400.0,
            "presence_db": 4.2,
            "presence_bw": 1.1,
            "air_hz": 10500.0,
            "air_db": 3.2,
        },
        "echo": {
            "send_db": -12.5,
            "eighth_notes": 2.0,
            "feedback_norm": 0.3548,
            "lowpass_hz": 3800.0,
            "highpass_hz": 220.0,
        },
        "reverb": {
            "send_db": -10.5,
            "room_size": 0.72,
            "predelay_norm": 0.070,
        },
    }


def source_block(path, length, indent):
    """The <SOURCE ...> chunk for one media item."""
    pad = " " * indent
    suf = path.suffix.lower()
    if suf in (".mid", ".midi"):
        # Reaper can reference an external .mid as a MIDIFILE source. Inline
        # MIDI would mean re-encoding every event into Reaper's own text
        # format; a reference keeps bed.py as the single source of truth.
        return [f"{pad}<SOURCE MIDIFILE", f'{pad}  FILE "{path}"', f"{pad}>"]
    if suf in (".mp4", ".mov", ".mkv", ".webm", ".avi"):
        return [f"{pad}<SOURCE VIDEO", f'{pad}  FILE "{path}"', f"{pad}>"]
    return [f"{pad}<SOURCE WAVE", f'{pad}  FILE "{path}"', f"{pad}>"]


MIDI_CAVEAT = ("a MIDI item makes no sound in Reaper until the track has an "
               "instrument on it — add one, or render the .mid to audio with "
               "bed.py first")


def build_produce_lua(rpp_path, report_path, plan, tempo=98.0):
    """Generate the Lua ReaScript that configures native Reaper FX & writes fx-report.txt."""
    eq = plan["eq"]
    echo = plan["echo"]
    rev = plan["reverb"]
    musical_norm = reaper_delay_musical_norm(echo["eighth_notes"])
    rpp_escaped = json.dumps(str(Path(rpp_path).resolve()))
    report_escaped = json.dumps(str(Path(report_path).resolve()))

    return f"""-- Generated by creative-stack /sound session.py
-- Configures ReaEQ (with linear amplitude conversion), ReaDelay, ReaVerbate & Master FX
reaper.Main_openProject("noprompt:" .. {rpp_escaped})

local report_path = {report_escaped}

local function db_to_gain(db)
  return 10.0 ^ (db / 20.0)
end

local function set_eq(tr, fx, hpf_hz, ls_hz, ls_db, b1_hz, b1_db, b1_bw, b2_hz, b2_db, b2_bw, hs_hz, hs_db)
  -- bandtype: 0=HPF, 1=LowShelf, 2=Band, 3=Notch, 4=HighShelf, 5=LPF
  -- paramtype: 0=Freq(Hz), 1=Gain(linear amplitude ratio when isnorm=false), 2=BW(oct)
  if hpf_hz and hpf_hz > 0 then
    reaper.TrackFX_SetEQBandEnabled(tr, fx, 0, 0, true)
    reaper.TrackFX_SetEQParam(tr, fx, 0, 0, 0, hpf_hz, false)
    reaper.TrackFX_SetEQParam(tr, fx, 0, 0, 1, 1.0, false)
    reaper.TrackFX_SetEQParam(tr, fx, 0, 0, 2, 1.4, false)
  else
    reaper.TrackFX_SetEQBandEnabled(tr, fx, 0, 0, false)
  end

  reaper.TrackFX_SetEQBandEnabled(tr, fx, 1, 0, true)
  reaper.TrackFX_SetEQParam(tr, fx, 1, 0, 0, ls_hz or 120, false)
  reaper.TrackFX_SetEQParam(tr, fx, 1, 0, 1, db_to_gain(ls_db or 0.0), false)
  reaper.TrackFX_SetEQParam(tr, fx, 1, 0, 2, 1.0, false)

  reaper.TrackFX_SetEQBandEnabled(tr, fx, 2, 0, true)
  reaper.TrackFX_SetEQParam(tr, fx, 2, 0, 0, b1_hz or 330, false)
  reaper.TrackFX_SetEQParam(tr, fx, 2, 0, 1, db_to_gain(b1_db or 0.0), false)
  reaper.TrackFX_SetEQParam(tr, fx, 2, 0, 2, b1_bw or 1.0, false)

  reaper.TrackFX_SetEQBandEnabled(tr, fx, 2, 1, true)
  reaper.TrackFX_SetEQParam(tr, fx, 2, 1, 0, b2_hz or 3400, false)
  reaper.TrackFX_SetEQParam(tr, fx, 2, 1, 1, db_to_gain(b2_db or 0.0), false)
  reaper.TrackFX_SetEQParam(tr, fx, 2, 1, 2, b2_bw or 1.1, false)

  reaper.TrackFX_SetEQBandEnabled(tr, fx, 4, 0, true)
  reaper.TrackFX_SetEQParam(tr, fx, 4, 0, 0, hs_hz or 10000, false)
  reaper.TrackFX_SetEQParam(tr, fx, 4, 0, 1, db_to_gain(hs_db or 0.0), false)
  reaper.TrackFX_SetEQParam(tr, fx, 4, 0, 2, 1.2, false)
end

-- 1. Master Track EQ (FX 0)
local master_tr = reaper.GetMasterTrack(0)
set_eq(master_tr, 0, 30.0, 80.0, 0.8, 300.0, -0.8, 1.0, 3500.0, 0.5, 1.2, 12000.0, 1.0)

-- 2. Track 1: Lead Vocals (Mic) -> FX 1 is ReaEQ (Vocal Presence & Air)
local vocal_tr = reaper.GetTrack(0, 0)
if vocal_tr then
  set_eq(vocal_tr, 1, {eq['hpf_hz']}, {eq['low_shelf_hz']}, {eq['low_shelf_db']}, {eq['mud_hz']}, {eq['mud_db']}, {eq['mud_bw']}, {eq['presence_hz']}, {eq['presence_db']}, {eq['presence_bw']}, {eq['air_hz']}, {eq['air_db']})
end

-- 3. Track 2: Vocal Echo (Stereo Delay Bus) -> FX 0 is ReaDelay, FX 1 is ReaEQ
local echo_tr = reaper.GetTrack(0, 1)
if echo_tr then
  reaper.TrackFX_SetParam(echo_tr, 0, 0, 0.5)        -- Wet: +0.0 dB (100% wet bus)
  reaper.TrackFX_SetParam(echo_tr, 0, 1, 0.0)        -- Dry: -inf dB
  reaper.TrackFX_SetParam(echo_tr, 0, 2, 1.0)        -- Tap 1: Enabled ON
  reaper.TrackFX_SetParam(echo_tr, 0, 3, 0.0)        -- Length (time): 0.0 ms (use musical tempo)
  reaper.TrackFX_SetParam(echo_tr, 0, 4, {musical_norm:.8f}) -- Length (musical): {echo['eighth_notes']:.2f} (1/8 notes at {tempo:.0f} BPM)
  reaper.TrackFX_SetParam(echo_tr, 0, 5, {echo['feedback_norm']:.4f}) -- Feedback
  reaper.TrackFX_SetParam(echo_tr, 0, 6, 0.190)      -- Lowpass: ~3800 Hz
  reaper.TrackFX_SetParam(echo_tr, 0, 7, 0.011)      -- Hipass: ~220 Hz
  reaper.TrackFX_SetParam(echo_tr, 0, 9, 1.0)        -- Stereo width: 1.00
  reaper.TrackFX_SetParam(echo_tr, 0, 10, 0.5)       -- Tap 1 Volume: +0.0 dB
  set_eq(echo_tr, 1, 220.0, 250.0, 0.0, 500.0, 0.0, 1.0, 3200.0, -2.5, 1.0, 4500.0, -8.0)
end

-- 4. Track 3: Vocal Reverb (Lush Plate Bus) -> FX 0 is ReaVerbate, FX 1 is ReaEQ
local rev_tr = reaper.GetTrack(0, 2)
if rev_tr then
  reaper.TrackFX_SetParam(rev_tr, 0, 0, 0.5)         -- Wet: +0.0 dB
  reaper.TrackFX_SetParam(rev_tr, 0, 1, 0.0)         -- Dry: -inf dB
  reaper.TrackFX_SetParam(rev_tr, 0, 2, {rev['room_size']:.2f})      -- Room size
  reaper.TrackFX_SetParam(rev_tr, 0, 5, {rev['predelay_norm']:.3f})     -- Initial pre-delay
  set_eq(rev_tr, 1, 220.0, 250.0, 0.0, 350.0, -2.5, 0.9, 2500.0, 0.0, 1.0, 7500.0, -6.0)
end

-- 5. Track 4: Backing Track (Instrumental) -> FX 0 is ReaEQ (Vocal Pocket Carve)
local back_tr = reaper.GetTrack(0, 3)
if back_tr then
  set_eq(back_tr, 0, 0.0, 100.0, 0.0, 200.0, -1.0, 1.0, 3200.0, -2.2, 1.2, 10000.0, 0.0)
end

local f = io.open(report_path, "w")
local function log(line)
  if f then f:write(line .. "\\n") end
end

local function dump_track_fx(label, tr)
  local fx_count = reaper.TrackFX_GetCount(tr)
  log(string.format("=== %s (FX count: %d) ===", label, fx_count))
  for fx = 0, fx_count - 1 do
    local _, fx_name = reaper.TrackFX_GetFXName(tr, fx, "")
    log(string.format("  [FX %d] %s", fx + 1, fx_name))
    local p_count = reaper.TrackFX_GetNumParams(tr, fx)
    for p = 0, math.min(p_count - 1, 18) do
      local _, p_name = reaper.TrackFX_GetParamName(tr, fx, p, "")
      local _, p_val = reaper.TrackFX_GetFormattedParamValue(tr, fx, p, "")
      log(string.format("    - %-24s : %s", p_name, p_val))
    end
  end
end

dump_track_fx("MASTER TRACK", master_tr)
local n_tracks = reaper.CountTracks(0)
for i = 0, n_tracks - 1 do
  local tr = reaper.GetTrack(0, i)
  local _, t_name = reaper.GetTrackName(tr)
  dump_track_fx(string.format("Track %d: %s", i + 1, t_name), tr)
end

if f then f:close() end
reaper.Main_SaveProject(0, false)
"""


def build_produced_rpp(dst, plan, backing_path, video_path=None, markers=None,
                       sample_rate=S.RATE, tempo=98.0, render_to=None):
    """Write a 5-track produced & mastered Reaper `.rpp` project with native FX chains."""
    vocal_path = Path(plan["vocal_file"])
    back_path = Path(backing_path) if backing_path else None
    vid_path = Path(video_path) if video_path else None
    back_len = S.probe(back_path)["duration"] if back_path and back_path.is_file() else plan["duration"]
    longest = max(plan["duration"], back_len)

    echo_send_vol = db_to_linear_gain(plan["echo"]["send_db"])
    rev_send_vol = db_to_linear_gain(plan["reverb"]["send_db"])
    back_vol = db_to_linear_gain(plan.get("backing_gain_db", -5.0))
    vocal_vol = db_to_linear_gain(1.0)

    lines = [
        '<REAPER_PROJECT 0.1 "7.0" 0',
        "  <NOTES 0 0",
        f"    |{dst.stem} (Produced & Mastered Session)",
        "    |",
        "    |FX & MIX OVERVIEW:",
        "    |  1. Lead Vocals (Mic): Pre-FX Volume Automation + ReaGate + ReaEQ + Dual ReaComp + De-esser",
        "    |  2. Vocal Echo (Stereo Delay Bus): ReaDelay (tempo-synced echo) + Analog Filter ReaEQ",
        "    |  3. Vocal Reverb (Lush Plate Bus): ReaVerbate (pre-delay plate) + Abbey Road ReaEQ",
        "    |  4. Karaoke Backing Track: Gain-staged instrumental + ReaEQ (-2.2 dB vocal pocket @ 3.2 kHz)",
        "    |  5. Master Bus: Mastering ReaEQ + ReaComp Bus Glue + Master Limiter (-1.0 dB) + Loudness Meter",
        "  >",
        "  RIPPLE 0",
        '  RECORD_PATH "recordings" ""',
        f"  SAMPLERATE {sample_rate} 0 0",
        f"  TEMPO {float(tempo):g} 4 4",
    ]
    if render_to:
        lines += [
            f'  RENDER_FILE "{render_to.parent.resolve()}"',
            f'  RENDER_PATTERN "{render_to.stem}"',
            f"  RENDER_FMT 0 2 {sample_rate}",
            "  RENDER_RANGE 1 0 0 18 1000",
            "  RENDER_STEMS 0",
            "  RENDER_ADDTOPROJ 0",
        ]
    lines += [
        "  MASTER_VOLUME 1 0 -1 -1 1",
        "  MASTER_FX 1",
        "  <MASTERFXLIST",
        "    SHOW 0",
        "    LASTSEL 0",
        "    DOCKED 0",
        "    BYPASS 0 0 0",
        '    <VST "VST: ReaEQ (Cockos) - Mastering Tone" reaeq.vst.dylib 0 "" 1919247729<56535472656571726561657100000000> ""',
        "    >",
        "    BYPASS 0 0 0",
        '    <VST "VST: ReaComp (Cockos) - Master Bus Glue" reacomp.vst.dylib 0 "" 1919247213<5653547265636D726561636F6D700000> ""',
        "    >",
        "    BYPASS 0 0 0",
        '    <JS loser/masterLimiter ""',
        "      -2.5 200 100 0 250 -1 0 - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -",
        "    >",
        "    BYPASS 0 0 0",
        '    <JS analysis/loudness_meter ""',
        "      1 0 0 2 1 1 1 1 -14 -9 1 0 0 1.8 0 0 - - - - - - - - - - - - - 0 -150 0 -150 -150 0 0 - - - - - - - - - - - - - - - - - - - - - - - - - - - -",
        "    >",
        "  >",
    ]
    for idx, (sec, label) in enumerate(markers or [], 1):
        safe_label = label.replace('"', "'")
        lines.append(f'  MARKER {idx} {sec:.3f} "{safe_label}" 0')

    # Track 1: Lead Vocals (Mic)
    lines += [
        "  <TRACK",
        '    NAME "Lead Vocals (Mic)"',
        f"    VOLPAN {vocal_vol:.6f} 0 -1 -1 1",
        "    MUTESOLO 0 0 0",
        "    REC 0 1 1 0 0 0 0 0",
        "    NCHAN 2",
        "    FX 1",
        "    MAINSEND 1 0",
        "    <VOLENV",
        "      ACT 1 -1",
        "      VIS 1 1 1",
        "      LANEHEIGHT 56 0",
        "      ARM 0",
        "      DEFSHAPE 0 -1 -1",
        "      VOLTYPE 1",
    ]
    for pt_t, pt_gain in plan.get("automation_points", [(0.0, 1.0)]):
        lines.append(f"      PT {pt_t:g} {pt_gain:.6f} 0")
    lines += [
        "    >",
        "    <FXCHAIN",
        "      SHOW 0",
        "      LASTSEL 1",
        "      DOCKED 0",
        "      BYPASS 0 0 0",
        '      <VST "VST: ReaGate (Cockos) - Vocal Silence Gate" reagate.vst.dylib 0 "" 1919248244<56535472656774726561676174650000> ""',
        "      >",
        "      BYPASS 0 0 0",
        '      <VST "VST: ReaEQ (Cockos) - Vocal Presence & Air" reaeq.vst.dylib 0 "" 1919247729<56535472656571726561657100000000> ""',
        "      >",
        "      BYPASS 0 0 0",
        '      <VST "VST: ReaComp (Cockos) - Peak Catcher (4:1)" reacomp.vst.dylib 0 "" 1919247213<5653547265636D726561636F6D700000> ""',
        "      >",
        "      BYPASS 0 0 0",
        '      <VST "VST: ReaComp (Cockos) - Smooth Leveler (2.5:1)" reacomp.vst.dylib 0 "" 1919247213<5653547265636D726561636F6D700000> ""',
        "      >",
        "      BYPASS 0 0 0",
        '      <JS Liteon/deesser ""',
        "        1 1 0 6200 1.2 -25 4 1 0 - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -",
        "      >",
        "    >",
        "    <ITEM",
        f"      POSITION {plan['trim_start']:.3f}",
        f"      SOFFS {plan['trim_start']:.3f}",
        f"      LENGTH {plan['trim_length']:.3f}",
        "      LOOP 0",
        "      FADEIN 1 0.4 0 1 0 0 0",
        "      FADEOUT 1 1.2 0 1 0 0 0",
        f'      NAME "{vocal_path.name}"',
        *source_block(vocal_path.resolve(), plan["trim_length"], 6),
        "    >",
        "  >",
    ]

    # Track 2: Vocal Echo (Stereo Delay Bus)
    lines += [
        "  <TRACK",
        '    NAME "Vocal Echo (Stereo Delay Bus)"',
        "    VOLPAN 1.000000 0 -1 -1 1",
        "    MUTESOLO 0 0 0",
        "    FX 1",
        "    MAINSEND 1 0",
        f"    AUXRECV 0 0 {echo_send_vol:.6f} 0 0 0 0 0 0 -1:U 0 -1 ''",
        "    <FXCHAIN",
        "      SHOW 0",
        "      LASTSEL 0",
        "      DOCKED 0",
        "      BYPASS 0 0 0",
        '      <VST "VST: ReaDelay (Cockos) - Slap & Stereo Echo" readelay.vst.dylib 0 "" 1919247468<5653547265646C72656164656C617900> ""',
        "      >",
        "      BYPASS 0 0 0",
        '      <VST "VST: ReaEQ (Cockos) - Warm Echo Filter" reaeq.vst.dylib 0 "" 1919247729<56535472656571726561657100000000> ""',
        "      >",
        "    >",
        "  >",
    ]

    # Track 3: Vocal Reverb (Lush Plate Bus)
    lines += [
        "  <TRACK",
        '    NAME "Vocal Reverb (Lush Plate Bus)"',
        "    VOLPAN 1.000000 0 -1 -1 1",
        "    MUTESOLO 0 0 0",
        "    FX 1",
        "    MAINSEND 1 0",
        f"    AUXRECV 0 0 {rev_send_vol:.6f} 0 0 0 0 0 0 -1:U 0 -1 ''",
        "    <FXCHAIN",
        "      SHOW 0",
        "      LASTSEL 0",
        "      DOCKED 0",
        "      BYPASS 0 0 0",
        '      <VST "VST: ReaVerbate (Cockos) - Lush Vocal Plate" reaverbate.vst.dylib 0 "" 1919252066<56535472657662726561766572626174> ""',
        "      >",
        "      BYPASS 0 0 0",
        '      <VST "VST: ReaEQ (Cockos) - Abbey Road Reverb Curve" reaeq.vst.dylib 0 "" 1919247729<56535472656571726561657100000000> ""',
        "      >",
        "    >",
        "  >",
    ]

    # Track 4: Backing Track (Instrumental)
    if back_path and back_path.is_file():
        lines += [
            "  <TRACK",
            '    NAME "Backing Track (Instrumental)"',
            f"    VOLPAN {back_vol:.6f} 0 -1 -1 1",
            "    MUTESOLO 0 0 0",
            "    FX 1",
            "    MAINSEND 1 0",
            "    <FXCHAIN",
            "      SHOW 0",
            "      LASTSEL 0",
            "      DOCKED 0",
            "      BYPASS 0 0 0",
            '      <VST "VST: ReaEQ (Cockos) - Vocal Pocket Carve" reaeq.vst.dylib 0 "" 1919247729<56535472656571726561657100000000> ""',
            "      >",
            "    >",
            "    <ITEM",
            "      POSITION 0.000000",
            f"      LENGTH {back_len:.6f}",
            "      LOOP 0",
            f'      NAME "{back_path.name}"',
            *source_block(back_path.resolve(), back_len, 6),
            "    >",
            "  >",
        ]

    # Track 5: Muted Lyrics Video (Cmd+Shift+V Teleprompter)
    if vid_path and vid_path.is_file():
        lines += [
            "  <TRACK",
            '    NAME "Lyrics Video (Press Cmd+Shift+V)"',
            "    VOLPAN 1.000000 0 -1 -1 1",
            "    MUTESOLO 1 0 0",
            "    MAINSEND 1 0",
            "    <ITEM",
            "      POSITION 0.000000",
            f"      LENGTH {longest:.6f}",
            "      LOOP 0",
            f'      NAME "{vid_path.name}"',
            *source_block(vid_path.resolve(), longest, 6),
            "    >",
            "  >",
        ]

    lines.append(">")
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return longest


def build_rpp(dst, tracks, sample_rate, render_to=None, arm_mic=False,
              monitoring=True, video_path=None, markers=None, tempo=120.0):
    """Write a Reaper project. The format is line-oriented text: tokens, then
    optional `<CHUNK ... >` blocks nested by two spaces."""
    longest = 0.0
    rec_dir = "recordings" if arm_mic else ""
    lines = [
        '<REAPER_PROJECT 0.1 "7.0" 0',
        "  RIPPLE 0",
        f'  RECORD_PATH "{rec_dir}" ""',
        f"  SAMPLERATE {sample_rate} 0 0",
        f"  TEMPO {float(tempo):g} 4 4",
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
    for idx, (sec, label) in enumerate(markers or [], 1):
        safe_label = label.replace('"', "'")
        lines.append(f'  MARKER {idx} {sec:.3f} "{safe_label}" 0')

    if arm_mic:
        mon_flag = 1 if monitoring else 0
        lines += [
            "  <TRACK",
            '    NAME "Lead Vocals (Mic)"',
            "    VOLPAN 1.000000 0 -1 -1 1",
            "    MUTESOLO 0 0 0",
            f"    REC 1 0 {mon_flag} 0 0 0 0 0",
            "  >",
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
        volume = db_to_linear_gain(gain)
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

    if video_path and Path(video_path).is_file():
        vpath = Path(video_path)
        vlen = longest or 180.0
        lines += [
            "  <TRACK",
            '    NAME "Lyrics Video (Press Cmd+Shift+V)"',
            "    VOLPAN 1.000000 0 -1 -1 1",
            "    MUTESOLO 1 0 0",
            "    <ITEM",
            "      POSITION 0.000000",
            f"      LENGTH {vlen:.6f}",
            "      LOOP 0",
            f'      NAME "{vpath.name}"',
            *source_block(vpath.resolve(), vlen, 6),
            "    >",
            "  >",
        ]

    lines.append(">")

    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return longest


def resolve_reaper_binary():
    """Return the path to the Reaper executable or None."""
    binary = shutil.which("reaper")
    if binary:
        return binary
    app = S.hostenv.find_app("reaper")
    return str(app) if app else None


def apply_reascript_live(lua_path, report_path=None):
    """Run a `.lua` ReaScript against a running (or freshly launched) Reaper via `-nonewinst`."""
    binary = resolve_reaper_binary()
    if not binary:
        S.die("reaper not found. " + S.hostenv.package_hint("reaper"))
    before = report_path.stat().st_mtime if report_path and report_path.exists() else 0
    subprocess.Popen([binary, "-nonewinst", str(lua_path)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if report_path:
        for _ in range(30):
            if report_path.exists() and report_path.stat().st_mtime > before and report_path.stat().st_size:
                return True
            time.sleep(0.3)
        return False
    return True


def render(project, dst):
    """Ask Reaper to render the project from the command line.

    -renderproject is the only genuinely headless thing Reaper does; it still
    launches the app, renders, and exits. There is no exit-code contract, so
    the output file is the only thing worth believing.
    """
    binary = resolve_reaper_binary()
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
    ap.add_argument("--track", "-t", action="append", default=[],
                    metavar="FILE[@START][:GAIN_DB]",
                    help="a stem, in track order; repeatable")
    ap.add_argument("--karaoke-backing",
                    help="instrumental backing track WAV/MP3 (auto gain-staged for vocal headroom)")
    ap.add_argument("--arm-mic", action="store_true",
                    help="add a record-armed 'Lead Vocals (Mic)' track at Track 1")
    ap.add_argument("--monitoring", default="on", choices=["on", "off"],
                    help="input monitoring state for --arm-mic (default: on)")
    ap.add_argument("--video",
                    help="karaoke/reference video file added on a muted track for Cmd+Shift+V")
    ap.add_argument("--markers",
                    help="markdown or text file with timestamped lyric/section markers")
    ap.add_argument("--tempo", type=float, default=120.0,
                    help="project tempo in BPM (default: 120)")
    ap.add_argument("--produce-vocal",
                    help="recorded vocal WAV to analyze, automate, and produce with native Reaper FX")
    ap.add_argument("--apply-live", action="store_true",
                    help="execute the generated .produce.lua in Reaper via -nonewinst and write fx-report.txt")
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
    if args.arm_mic:
        (dst.parent / "recordings").mkdir(parents=True, exist_ok=True)

    markers = parse_markers_file(args.markers) if args.markers else []

    if args.produce_vocal:
        vocal_file = Path(args.produce_vocal).expanduser()
        if not vocal_file.is_file():
            S.die(f"vocal take not found: {vocal_file}")
        if dst.exists():
            backup_dir = dst.parent / "Backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_rpp = backup_dir / f"{dst.stem}-raw-recording.rpp"
            if not backup_rpp.exists():
                shutil.copy2(dst, backup_rpp)
                S.report(backup_rpp, "raw session backup")

        backing_file = args.karaoke_backing
        if not backing_file and args.track:
            backing_file = str(parse_track(args.track[0])[0])

        print(f"🎛️  analyzing vocal take ({vocal_file.name}) …")
        plan = analyze_vocal_take(vocal_file, backing_file)
        plan_path = dst.with_name(f"{dst.stem}.produce.json")
        plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        S.report(plan_path, "vocal production plan")

        length = build_produced_rpp(
            dst, plan, backing_file, video_path=args.video, markers=markers,
            sample_rate=args.rate, tempo=args.tempo,
            render_to=Path(args.render) if args.render else None,
        )
        S.report(dst, f"produced 5-track .rpp ({length:.1f}s)")

        lua_path = dst.with_name(f"{dst.stem}.produce.lua")
        report_path = dst.with_name(f"{dst.stem}.fx-report.txt")
        lua_path.write_text(build_produce_lua(dst, report_path, plan, tempo=args.tempo),
                            encoding="utf-8")
        S.report(lua_path, "ReaScript live FX configurator")

        if args.apply_live:
            if apply_reascript_live(lua_path, report_path=report_path) and report_path.exists():
                S.report(report_path, "verified live Reaper FX state")
            else:
                print(f"⚠ timed out waiting for live Reaper FX report → {report_path}", file=sys.stderr)
        if args.render:
            target = Path(args.render)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not render(dst, target):
                S.die(f"Reaper did not write {target}. Open {dst} and check the "
                      f"render settings (File -> Render).")
            S.report(target)
        if args.open and not args.apply_live:
            opener = "open" if S.hostenv.IS_MAC else "xdg-open"
            subprocess.run([opener, str(dst)], check=False)
        return 0

    tracks = [parse_track(spec) for spec in args.track]
    if args.karaoke_backing:
        bpath = Path(args.karaoke_backing).expanduser()
        if not bpath.is_file():
            S.die(f"karaoke backing file not found: {bpath}")
        b_lufs = S.measure(bpath)
        bg_gain = round(max(-8.0, min(0.0, -14.0 - b_lufs["i"])), 1)
        tracks.append((bpath, 0.0, bg_gain))

    if not tracks and not args.arm_mic:
        ap.error("provide at least one --track (-t), --karaoke-backing, or --produce-vocal")

    if args.duck:
        tracks = [(path, start, gain if index == 0 else gain + args.duck_gain)
                  for index, (path, start, gain) in enumerate(tracks)]

    print(f"◈ {len(tracks)} stem(s) → {dst.name}")
    if any(p.suffix.lower() in (".mid", ".midi") for p, _, _ in tracks):
        print(f"  {S.YELLOW}!{S.RESET} {MIDI_CAVEAT}")
    length = build_rpp(
        dst, tracks, args.rate,
        render_to=Path(args.render) if args.render else None,
        arm_mic=args.arm_mic, monitoring=(args.monitoring == "on"),
        video_path=args.video, markers=markers, tempo=args.tempo,
    )
    if args.arm_mic:
        print(f"  {S.DIM}{'Lead Vocals (Mic)':<28} [ARMED, monitoring {args.monitoring.upper()}]{S.RESET}")
    for path, start, gain in tracks:
        print(f"  {S.DIM}{path.name:<28} @{start:>7.2f}s  "
              f"{gain:+.1f}dB{S.RESET}")
    if args.video:
        print(f"  {S.DIM}{Path(args.video).name:<28} [MUTED video teleprompter, Cmd+Shift+V]{S.RESET}")
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
