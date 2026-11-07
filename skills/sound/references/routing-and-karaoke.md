# Audio Routing, Live Capture, Karaoke & Mastering Playbook

Reference instructions for agents and humans executing live loopback capture, Audio Hijack 4
automation, stem-separated karaoke production, and EBU R128 mastering via `/sound`.

---

## 1. Cross-Platform Audio Loopback & Passthrough Architecture

| OS | Virtual Passthrough / Loopback | Node-Graph / Live Mixing & Automation | CLI Inspection & Routing |
| --- | --- | --- | --- |
| **macOS** | **Rogue Amoeba Loopback 2** (`ARK.driver`), **BlackHole** (`blackhole-2ch`), **eqMac** (`eqMac.driver`) | **Audio Hijack 4** (`AH4ScriptContext` JS API, `.ahcommand`, `audiohijack://`, Shortcuts intents) | `python3 scripts/record.py --list-devices`, `SwitchAudioSource` (`switchaudio-osx`) |
| **Linux** | **PipeWire** (`pw-loopback`, `pw-link`) & **PulseAudio** (`module-null-sink` `.monitor` sources) | **PipeWire + WirePlumber** (`pw-link`, `qpwgraph`, `carla`) | `python3 scripts/record.py --list-devices`, `pactl list short sources`, `wpctl status` |
| **Windows** | **VB-CABLE**, **Virtual Audio Cable (VAC)**, **ASIO Link Pro**, **WASAPI Process Loopback** | **Voicemeeter Banana / Potato** (`VoicemeeterRemote64.dll` / `voicemeeter-api`) | `ffmpeg -f dshow -list_devices true -i dummy` |

---

## 2. macOS Loopback 2 + Audio Hijack 4 Workflows

### A. Zero-Feedback Live Karaoke / Podcast Setup (Hardware Interface + Loopback 2)

To record your microphone (e.g., `Volt 276`) and system/app audio (Spotify, Apple Music, browser,
or a DAW) simultaneously **without** feeding your own microphone back into the system loopback:

1. In **Loopback.app**, create a virtual device (e.g., `Karaoke Passthrough` or `Clean Mic`) whose
   **Sources** include only the target apps (Browser / Music app) and **not** the hardware mic.
2. Monitor through your hardware interface (`Volt 276` / `FiiO K11 R2R`) so vocal monitoring is
   zero-latency in hardware.
3. Run dual-stem capture with `record.py`:

```bash
# Inspect device indices and confirm [loopback/passthrough] detection
python3 skills/sound/scripts/record.py --list-devices

# Capture mic + loopback onto separate 24-bit 48kHz stems (.mic.wav and .loopback.wav),
# run clean.py (highpass, denoise, de-ess, gate, compress, R128 normalise) on the mic,
# and write a balanced stereo mix:
python3 skills/sound/scripts/record.py \
  --device "Volt 276" \
  --loopback "Clean Mic" \
  --loopback-db -6 \
  --duration 180 \
  --clean \
  -o out/session.wav
```

This produces:
* `out/session.capture.json` — reviewable, re-runnable capture specification (`--from-plan`)
* `out/session.mic.wav` — raw 24-bit 48 kHz mono microphone stem
* `out/session.mic-clean.wav` — repaired, de-noised, R128-normalised vocal stem
* `out/session.loopback.wav` — raw 24-bit 48 kHz stereo passthrough stem
* `out/session.wav` — combined stereo mix

### B. Driving Audio Hijack 4 Headlessly via JavaScript (`.ahcommand`)

Audio Hijack 4 embeds a JavaScript automation runtime (`AH4ScriptContext`). Ensure
`Audio Hijack → Settings → Advanced → Allow execution of external scripts` is ticked once, then
let `record.py` generate and dispatch `.ahcommand` scripts:

```bash
# Start a named Audio Hijack session and point its Recorder block at out/
python3 skills/sound/scripts/record.py \
  --hijack-session "Podcast Capture" \
  --hijack-action start \
  -o out/podcast.wav

# Timed capture: start the session, record for 60s, and stop automatically
python3 skills/sound/scripts/record.py \
  --hijack-session "Podcast Capture" \
  --duration 60 \
  -o out/podcast.wav

# Stop a running session
python3 skills/sound/scripts/record.py \
  --hijack-session "Podcast Capture" \
  --hijack-action stop \
  -o out/podcast.json
```

---

## 3. Karaoke Stem Separation, Key Transposition & Vocal Mixing (`karaoke.py`)

### Engine Selection (`--engine`)
* `auto` (default): Uses **Demucs v4** (`htdemucs --two-stems=vocals`) when installed in `.venv`
  (`uv pip install --python .venv/bin/python -r requirements-ml.txt`), and falls back to
  `center-cut` when offline or when `demucs` is not installed.
* `demucs`: Forces neural stem separation via Hybrid Transformer Demucs.
* `center-cut`: Zero-ML stereo phase cancellation (`c0 - 0.92*c1`) above 130 Hz paired with an
  intact `<130 Hz` stereo bass bypass so kick drums and basslines remain full.
* `none`: Uses pre-separated stems passed via `--instrumental` and `--vocals`.

### Full Karaoke Production & Performance Mastering Recipe

```bash
# Step 1: Separate song into instrumental + vocal stems, shift key down 2 semitones
#         (preserving formants via rubberband), transcribe isolated vocals for word-level
#         timings, and render a 1080p karaoke video with a -18 dB guide vocal:
python3 skills/sound/scripts/karaoke.py \
  -i raw/song.mp3 \
  --semitones -2 \
  --tempo 1.0 \
  --guide-db -18 \
  --reaper \
  -o out/song-karaoke.mp4

# Step 2 (Optional): Edit out/song-karaoke.srt if any lyric line needs wording tweaks,
#                    then re-render without re-running stem separation:
python3 skills/sound/scripts/karaoke.py \
  --from-plan out/song-karaoke.karaoke.json \
  -o out/song-karaoke.mp4

# Step 3: Record or supply your own vocal take (--sing) and master the performance
#         (applies 85 Hz rumble filter, 3:1 vocal compressor, studio plate reverb,
#         and two-pass EBU R128 normalisation to -14 LUFS / -1.0 dBTP):
python3 skills/sound/scripts/karaoke.py \
  --from-plan out/song-karaoke.karaoke.json \
  --sing out/session.mic-clean.wav \
  --reverb 0.24 \
  --guide-db off \
  --reaper \
  -o out/final-performance.mp4
```

---

## 4. Mastering Targets by Delivery Platform

When running `clean.py`, `mix.py`, or `karaoke.py`, pass `--target` and `--true-peak` (or `--lra`)
to match the destination platform:

| Platform / Deliverable | `--target` (LUFS) | `--true-peak` (dBTP) | `--lra` (LU) | Notes |
| --- | --- | --- | --- | --- |
| **YouTube / Spotify / Web Video** *(default)* | `-14.0` | `-1.0` | `11.0` | Matches `brand/tokens.json` defaults |
| **Apple Podcasts / Spoken Word** | `-16.0` | `-1.0` | `7.0` | Tighter loudness range keeps speech even in noisy environments |
| **Broadcast TV (EBU R128)** | `-23.0` | `-1.0` | `11.0` | European broadcast standard |
| **Club / High-Headroom Instrumental** | `-12.0` | `-0.5` | `9.0` | Use only for standalone music / karaoke playback |
