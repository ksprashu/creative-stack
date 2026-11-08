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

## 4. Reaper Native Karaoke Teleprompter, Vocal Production & Live ReaScript Mastering

When recording and producing inside **REAPER** (rather than rendering an offline FFmpeg mix),
`karaoke.py` and `session.py` automate the entire studio workflow using 100% native Reaper FX:

### Stage 1: Pull YouTube Karaoke Track, OCR Lyrics & Open Record-Armed Session

```bash
python3 skills/sound/scripts/karaoke.py \
  --youtube "https://www.youtube.com/watch?v=0d3LzwjjTog" \
  --ocr-lyrics --reaper --arm-mic --monitoring on --bpm 98 --open \
  -o out/over-each-other/over-each-other.rpp
```

What this builds:
* `over-each-other.instrumental.wav` — 48 kHz / 24-bit uncompressed PCM backing track
* `over-each-other-video.mp4` — 1080p karaoke lyric video
* `over-each-other.lyrics.md` — OCR-extracted timestamped lyric cues (`ffmpeg` + `tesseract`)
* `over-each-other.rpp` — 3-track Reaper project:
  1. **Track 1 (`Lead Vocals (Mic)`)**: Record-armed (`REC 1 0 1 ...`), input monitoring ON, `RECORD_PATH "recordings" ""`
  2. **Track 2 (`Karaoke Backing Track`)**: Auto gain-staged (`-14 LUFS - measured_LUFS`, clamped to `[-8, 0] dB`) so hot `-8.8 LUFS` YouTube tracks leave headroom for vocals
  3. **Track 3 (`Lyrics Video (Press Cmd+Shift+V)`)**: Muted (`MUTESOLO 1 0 0`) video item so pressing `Cmd+Shift+V` (`Ctrl+Shift+V` on Linux/Windows) opens Reaper's floating Video Window as a sample-accurate lyric teleprompter without double audio
  4. **Timeline Markers (`MARKER`)**: Section and lyric prompts along the top ruler

### Stage 2: Analyze Recorded Vocal Take & Produce Natively in Reaper (`--produce-vocal`)

After the singer records their vocal take into `recordings/` and saves the project:

```bash
python3 skills/sound/scripts/session.py \
  -o out/over-each-other/over-each-other.rpp \
  --karaoke-backing out/over-each-other/over-each-other.instrumental.wav \
  --produce-vocal "out/over-each-other/recordings/01-Lead Vocals (Mic).wav" \
  --video out/over-each-other/over-each-other-video.mp4 \
  --markers out/over-each-other/over-each-other.lyrics.md \
  --tempo 98 --apply-live
```

What `--produce-vocal` does:
1. **Backs up raw recording session** to `Backups/<stem>-raw-recording.rpp`.
2. **Measures vocal telemetry**: EBU R128 loudness (`I`, `TP`, `LRA`), active singing start/end bounds (for non-destructive `POSITION`/`SOFFS`/`LENGTH` trim + fade-in/out), and 2-second RMS phrasing windows to compute a **Pre-FX Volume Automation Envelope (`<VOLENV>`)** that lifts quiet low-register verses (`+3` to `+4.8 dB`) and tames loud chorus/outro peaks (`-2.8 dB`) before hitting the compressors.
3. **Writes `<stem>.produce.json`** (reviewable intermediate plan) and the **5-track produced `.rpp`**:
   * **Track 1 (`Lead Vocals (Mic)`)**: Pre-FX `<VOLENV>` + `VST: ReaGate` + `VST: ReaEQ` (`95 Hz` HPF, `+1.2 dB @ 180 Hz` warmth, `-4.0 dB @ 330 Hz` mud cut, `+4.2 dB @ 3.4 kHz` presence, `+3.2 dB @ 10.5 kHz` air) + `VST: ReaComp` Peak Catcher (`4:1`) + `VST: ReaComp` Smooth Leveler (`2.5:1`) + `JS: Liteon/deesser` (`6.2 kHz`).
   * **Track 2 (`Vocal Echo (Stereo Delay Bus)`)**: Post-fader `AUXRECV` send (`-12.5 dB`) + `VST: ReaDelay` (`100%` wet, tempo-synced `2.00` eighth notes = 1/4-note echo via `param 4 = 2/256 = 0.0078125`, `-9 dB` feedback, `220 Hz - 3.8 kHz` bandpass) + `VST: ReaEQ` warm echo roll-off.
   * **Track 3 (`Vocal Reverb (Lush Plate Bus)`)**: Post-fader `AUXRECV` send (`-10.5 dB`) + `VST: ReaVerbate` (`100%` wet, room size `79`, `35 ms` pre-delay) + `VST: ReaEQ` Abbey Road curve (`220 Hz` HPF, `-2.5 dB @ 350 Hz` mud cut, `-6.0 dB @ 7.5 kHz` high shelf).
   * **Track 4 (`Backing Track (Instrumental)`)**: Gain-staged instrumental + `VST: ReaEQ` carving a subtle `-2.2 dB` vocal pocket at `3.2 kHz` and `-1.0 dB` at `200 Hz`.
   * **Track 5 (`Lyrics Video`)**: Muted video track kept in sync.
   * **Master Bus (`<MASTERFXLIST>`)**: Mastering `VST: ReaEQ` + `VST: ReaComp` Bus Glue + `JS: loser/masterLimiter` (`-1.0 dB` ceiling) + `JS: analysis/loudness_meter` (`-14 LUFS`).
4. **Generates `<stem>.produce.lua` and runs `REAPER -nonewinst` (`--apply-live`)**:
   * Calls `reaper.Main_openProject("noprompt:<path>.rpp")` so a running Reaper instance reloads cleanly without blocking on a modal dialog.
   * Converts all EQ dB gains to linear amplitude (`10 ^ (dB / 20)`) before calling `reaper.TrackFX_SetEQParam(tr, fx, bandtype, bandidx, 1, val, false)`.
   * Writes `<stem>.fx-report.txt` reading back every formatted parameter value from Reaper's engine to prove the live state.

### Stage 3: Lossless Screen-Recording Remux & Loudness Normalisation for LinkedIn / YouTube

```bash
python3 skills/vidkit/scripts/publish.py \
  -i ~/Movies/over-each-other-finale.mov \
  -o ~/Movies/over-each-other-finale.mp4 \
  --copy-video
```

---

## 5. Mastering Targets by Delivery Platform

When running `clean.py`, `mix.py`, or `karaoke.py`, pass `--target` and `--true-peak` (or `--lra`)
to match the destination platform:

| Platform / Deliverable | `--target` (LUFS) | `--true-peak` (dBTP) | `--lra` (LU) | Notes |
| --- | --- | --- | --- | --- |
| **YouTube / Spotify / Web Video** *(default)* | `-14.0` | `-1.0` | `11.0` | Matches `brand/tokens.json` defaults |
| **Apple Podcasts / Spoken Word** | `-16.0` | `-1.0` | `7.0` | Tighter loudness range keeps speech even in noisy environments |
| **Broadcast TV (EBU R128)** | `-23.0` | `-1.0` | `11.0` | European broadcast standard |
| **Club / High-Headroom Instrumental** | `-12.0` | `-0.5` | `9.0` | Use only for standalone music / karaoke playback |
