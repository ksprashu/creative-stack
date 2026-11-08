---
name: sound
description: Live capture via Loopback/Audio Hijack/PipeWire, voice cleanup, EBU R128 loudness, music beds and logo stings, ducked mixes, stem-separated karaoke with key shift, and Reaper sessions. Trigger via /sound.
disable-model-invocation: true
---

# Sound: Capture, Voice, Music, Karaoke and the Mix

The audio half of a video, and the whole of a podcast or karaoke session. Capture live microphones
and virtual loopback busses, repair the voice, generate a bed that carries no licence, duck one
under the other, separate stems and shift keys for karaoke, and land on the loudness target the
platform actually wants.

---

## 🎯 Goal

A recording made on whatever microphone or virtual loopback bus was to hand becomes a track that
sounds deliberate, at −14 LUFS with a −1 dBTP ceiling, every time and without a DAW. Music that
goes under it is generated here, so there is no attribution to chase and no takedown to answer;
songs fed into `karaoke.py` come back split into instrumental and vocal stems, transposed to the
singer's key, and rendered with word-sweeping `\kf` lyrics.

Loudness is measured, not eyeballed: every script ends by re-measuring what it wrote and telling
you whether it hit the target.

---

## 📋 Step-by-Step Workflow

0. **Capture live audio or drive Audio Hijack / Loopback when starting from a live source:**

   ```bash
   python3 scripts/record.py --list-devices
   python3 scripts/record.py --device "Volt 276" --loopback "Clean Mic" --duration 30 --clean -o out/take.wav
   python3 scripts/record.py --hijack-session "Podcast Capture" --hijack-action start
   ```

   `--list-devices` enumerates hardware inputs alongside virtual passthrough drivers (Rogue Amoeba
   Loopback `ARK.driver`, BlackHole, eqMac on macOS; PipeWire / PulseAudio `.monitor` sources on
   Linux). Passing both `--device` and `--loopback` records separate 24-bit 48 kHz `.mic.wav` and
   `.loopback.wav` stems before mixing them, and writes a reviewable `<stem>.capture.json` plan.

1. **Repair the voice first.** Everything downstream assumes this has happened:

   ```bash
   python3 scripts/clean.py -i raw.wav -o voice.wav
   python3 scripts/clean.py -i screencast.mov -o voice.wav --mono
   ```

   A video file is fine as input — the first audio stream is pulled out. The chain is rumble
   filter → denoise → de-ess → gate → compress → normalise, in that order.

2. **Denoise properly when the recording allows it.** `--denoise auto` is a blind spectral
   filter. If there is a second of room tone before you started talking, point at it instead and
   sox subtracts a real fingerprint of the room:

   ```bash
   python3 scripts/clean.py -i raw.wav -o voice.wav --denoise 0,1.5
   ```

   Aim the window at true silence, never at a breath — a breath in the profile removes the
   voice along with the noise.

3. **Generate the music.** Five styles, seeded so the same flags give the same bars:

   ```bash
   python3 scripts/bed.py -o bed.wav --style ambient --duration 90
   python3 scripts/bed.py -o sting.wav --style corporate --sting
   python3 scripts/bed.py -o bed.mid --style lofi --bars 16 --key F --seed 7
   ```

   | `--style` | Feel |
   | --- | --- |
   | `ambient` | pads, no pulse; sits under narration and disappears |
   | `corporate` | piano and strings; the default for a product walkthrough |
   | `upbeat` | drums and a pluck; intros, montages, time-lapses |
   | `lofi` | dorian 7ths and an upright bass; tutorials, long-form |
   | `tension` | minor second movement; a problem statement, a before-shot |

   `--sting` swaps the loop for a short resolving gesture at a brisker tempo — about five
   seconds, which is a logo animation. Writing `.mid` gives you the notes instead of the audio.

4. **Mix, with the bed ducking under the voice:**

   ```bash
   python3 scripts/mix.py -v voice.wav -m bed.wav -o episode.wav --lead 3 --tail 4
   python3 scripts/mix.py -v voice.wav -m bed.wav -s ding.wav@40 -o episode.m4a --duck 16
   ```

   `--duck` is an exact number of dB, not a suggestion. `--lead` and `--tail` give the bed room
   at either end; a shorter bed is looped to fit automatically.

5. **Check the number that was printed.** Every script re-measures its own output. If it says
   `!` rather than `✓`, the source was near-silent or already clipped, and no amount of gain
   will fix that.

6. **Hand it to Reaper only when the automatic mix is not enough:**

   ```bash
   python3 scripts/session.py -o show.rpp -t voice.wav -t "bed.wav@3:-16"
   python3 scripts/session.py -o show.rpp -t voice.wav -t bed.wav --duck --render final.wav
   python3 scripts/session.py -o out/song/song.rpp --karaoke-backing out/song/backing.wav \
     --produce-vocal "out/song/recordings/01-Lead Vocals (Mic).wav" \
     --video out/song/video.mp4 --markers out/song/lyrics.md --tempo 98 --apply-live
   ```

   Each stem lands on its own track at the right offset and gain. `--produce-vocal` analyzes the
   singer's recorded take (LUFS, true peak, LRA, 2-second RMS phrasing windows), builds a Pre-FX
   `<VOLENV>` automation envelope, wires a 5-track studio session with 100% native Reaper FX
   (`ReaGate`, `ReaEQ`, dual `ReaComp`, `JS: Liteon/deesser`, tempo-synced `ReaDelay` Echo bus,
   `ReaVerbate` Reverb bus, Backing Track vocal-pocket `ReaEQ`, and Master bus `ReaEQ` + `ReaComp`
   + `JS: loser/masterLimiter` + `JS: analysis/loudness_meter`), and `--apply-live` executes the
   companion `.produce.lua` ReaScript via `REAPER -nonewinst` and writes `fx-report.txt`.
   `--render` drives Reaper's headless renderer; `--open` just opens the session.

7. **Separate stems, shift key/tempo, and render synchronized karaoke (`karaoke.py`):**

   ```bash
   python3 scripts/karaoke.py -i song.mp3 -o out/karaoke.mp4
   python3 scripts/karaoke.py -i song.wav --semitones -2 --guide-db -18 -o out/karaoke.mp4
   python3 scripts/karaoke.py -i song.wav --sing out/take.mic.wav --reaper -o out/performance.mp4
   ```

   `--engine auto` uses Demucs v4 (`htdemucs`) when installed and falls back to bass-preserving
   stereo phase cancellation (`center-cut`, which keeps stereo bass below 130 Hz intact while
   cancelling center-panned vocals). `rubberband` shifts pitch (`--semitones`) and tempo
   (`--tempo`) with formant preservation; `whisper-cli` transcribes the isolated vocal stem for
   word timings; and `/vidkit captions.py --style karaoke` burns `\kf` word-sweeping lyrics into
   the 1080p output.

   See [`references/routing-and-karaoke.md`](references/routing-and-karaoke.md) for the complete
   Audio Hijack 4 / Loopback 2 / PipeWire routing recipes, dual-stem karaoke recording setup, and
   platform mastering target table.

---

## 💡 Concrete Example

### Example A — Scoring and Mastering a Ten-Minute Tutorial

Repair the narration, build a bed the right length, open with a sting, and end up with one file per
delivery target.

```bash
# 1. repair the narration, learning the room from the pause before the first word
python3 scripts/clean.py -i raw/narration.wav -o out/voice.wav --denoise 0,1.8 --mono

# 2. a bed as long as the narration (the voice is 9m52s, so ask for 600s and let it round up)
python3 scripts/bed.py -o out/bed.wav --style lofi --duration 600 --key F --seed 12

# 3. a sting for the title card
python3 scripts/bed.py -o out/sting.wav --style corporate --sting

# 4. the mix: 3s of bed alone up front, 5s to fade out, bed 14dB under the voice
python3 scripts/mix.py -v out/voice.wav -m out/bed.wav -o out/episode.wav \
  --lead 3 --tail 5 --duck 14

# 5. the podcast cut, quieter and more even, as the platforms prefer
python3 scripts/mix.py -v out/voice.wav -m out/bed.wav -o out/episode.mp3 \
  --lead 3 --tail 5 --duck 14 --target -16 --lra 7

# 6. if it needs a human pass, open the stems as a session instead
python3 scripts/session.py -o out/episode.rpp \
  -t out/voice.wav -t "out/bed.wav@0:-14" -t "out/sting.wav@0" --open
```

`/vidkit publish.py` mounts `out/episode.wav` back onto the video; `/render3d logo3d.py --spin`
is what `out/sting.wav` is scored to.

### Example B — Live Loopback Capture, Karaoke Key Shift & Vocal Mastering

Separate a song into stems, transpose it down 2 semitones to fit the singer's range, capture the
singer's microphone alongside a Loopback virtual bus, and master the final performance to −14 LUFS:

```bash
# 1. inspect hardware interfaces (e.g. Volt 276) and virtual loopback busses (Loopback / BlackHole / PipeWire)
python3 scripts/record.py --list-devices

# 2. split the song into instrumental + vocal stems, shift key -2 semitones via rubberband,
#    transcribe word timings from the isolated vocal stem, and build the 1080p karaoke practice track
python3 scripts/karaoke.py -i raw/song.mp3 --semitones -2 --guide-db -18 \
  --reaper -o out/karaoke-practice.mp4

# 3. capture the live vocal take on the mic while monitoring the backing track on the loopback bus
python3 scripts/record.py --device "Volt 276" --loopback "Clean Mic" \
  --duration 180 --clean -o out/live-take.wav

# 4. mix the cleaned vocal take (with studio vocal compression + plate reverb) over the transposed
#    instrumental, master to -14 LUFS / -1.0 dBTP, and emit both the 1080p video and Reaper .rpp session
python3 scripts/karaoke.py --from-plan out/karaoke-practice.karaoke.json \
  --sing out/live-take.mic-clean.wav --reverb 0.22 --guide-db off \
  --reaper -o out/karaoke-master.mp4
```

### Example C — YouTube Karaoke Teleprompter Session, Native Reaper FX Production & LinkedIn Export

Pull a YouTube karaoke track, extract on-screen lyric cues via OCR, open a record-armed Reaper
session with a `Cmd+Shift+V` lyric teleprompter, then produce & master the recorded vocal take
natively inside Reaper and export a loudness-normalised MP4:

```bash
# 1. pull the YouTube karaoke track (48kHz 24-bit WAV + 1080p video), extract OCR lyric markers,
#    and open a record-armed Reaper session with input monitoring ON and muted video teleprompter
python3 scripts/karaoke.py --youtube "https://www.youtube.com/watch?v=0d3LzwjjTog" \
  --ocr-lyrics --reaper --arm-mic --monitoring on --bpm 98 --open \
  -o out/over-each-other/over-each-other.rpp

# 2. after recording the vocal take in Reaper, analyze the take and apply native Reaper FX,
#    Pre-FX volume automation, stereo Echo/Reverb buses, and Master bus processing live
python3 scripts/session.py -o out/over-each-other/over-each-other.rpp \
  --karaoke-backing out/over-each-other/over-each-other.instrumental.wav \
  --produce-vocal "out/over-each-other/recordings/01-Lead Vocals (Mic).wav" \
  --video out/over-each-other/over-each-other-video.mp4 \
  --markers out/over-each-other/over-each-other.lyrics.md \
  --tempo 98 --apply-live

# 3. convert a screen-recorded .mov finale to a LinkedIn-ready .mp4 (-c:v copy + 2-pass -14 LUFS)
python3 ../vidkit/scripts/publish.py -i ~/Movies/over-each-other-finale.mov \
  -o ~/Movies/over-each-other-finale.mp4 --copy-video
```

---

## 🚫 Hard Constraints

*   **NEVER** normalise in a single pass. `loudnorm` on its own has to guess the gain as it goes
    and lands up to 2 LU off target, which is plainly audible next to a track that was done
    properly. Measure first, then feed `measured_I`/`measured_TP`/`measured_LRA`/`offset` into a
    second pass. `soundlib.measure()` and `soundlib.loudnorm_filter()` are that pair.
*   **NEVER** normalise before the last processing step. Every filter in the chain changes the
    loudness, so a measurement taken earlier describes a file that no longer exists.
*   **NEVER** use `sidechaincompress` to duck by a specified number of dB. Its gain reduction is
    a function of how far the key runs over an opaque *linear* threshold; measured across the
    parameter space on this stack it gave anywhere from 0 to 30dB of duck for settings that
    should have given a fixed amount, and thresholds above about 0.005 did nothing at all.
    `mix.py` detects the phrases and builds an explicit `volume` envelope, which delivers the
    requested depth to two decimal places.
*   **NEVER** let `loudnorm` choose the output sample rate. It resamples to 192kHz internally and
    writes 192kHz if nothing pins the rate; pass `-ar 48000` (that is `soundlib.RATE`) on every
    stage that follows it.
*   **NEVER** measure a mono file without `dual_mono=true`. R128 sums channels, so mono reads
    about 3 LU quieter than the same material in stereo — normalise without it and every
    voiceover in the project ends up 3dB hot against the music.
*   **NEVER** run `fluidsynth` without `-ni`. Headless, it otherwise opens an interactive shell
    on stdin and hangs until the job is killed.
*   **NEVER** end a generated MIDI file on its last musical event. fluidsynth stops rendering the
    instant the last event passes, so the release envelopes and the entire reverb tail are cut
    off mid-decay. `bed.py` appends two silent bars for exactly this reason.
*   **NEVER** pass `amix` without `normalize=0`. The default divides by the number of inputs, so
    adding a music bed silently drops the voice by 6dB.
*   **NEVER** round-trip between stages through a lossy codec. Intermediates are 24-bit WAV
    (`soundlib.intermediate()`); three generations of MP3 to reach one MP3 is how a clean
    voiceover picks up a swirl of artefacts.
*   **NEVER** commit a SoundFont. `assets/soundfonts/` is gitignored — the bank is 14MB of
    binary that `scripts/install_bridges.py` fetches, and `$CREATIVE_STACK_SOUNDFONT` overrides
    it for anyone with a better one.
*   **NEVER** cancel center-panned vocals across the full spectrum (`c0-c1` without a bass split).
    Kick drums and bass guitar are panned dead-center alongside lead vocals; cancelling below
    130 Hz wipes out the low end. `karaoke.py` splits at 130 Hz so stereo bass stays intact while
    phase cancellation acts only on the vocal band above 130 Hz.
*   **NEVER** pitch-shift a song or vocal without formant preservation (`rubberband --formant`).
    Scaling pitch without preserving formants shifts the vocal tract resonances and produces
    chipmunk or monster timbres even at ±2 semitones.
*   **NEVER** transcribe a full stereo mix for karaoke word timings when an isolated vocal stem is
    available. Accompaniment transients cause `whisper-cli -ojf` token offsets to drift; always
    separate stems first and transcribe `.vocals.wav`.
*   **NEVER** detect virtual loopback devices by matching the string `"Loopback"` alone. Rogue
    Amoeba Loopback lets users name virtual devices anything (e.g. `"Clean Mic"`); always query
    `hostenv.audio_devices()`, which inspects `coreaudio_device_transport == "coreaudio_device_type_virtual"`
    and the device manufacturer.
*   **NEVER** pass raw dB values to `reaper.TrackFX_SetEQParam(..., paramtype=1, val, false)`.
    When `isnorm=false`, `ReaEQ` expects **linear amplitude ratios (`10 ^ (dB / 20)`)** for band
    gain: passing `0.0` sets `-inf dB` and passing `-4.0` sets `+12.0 dB` (inverted phase). Always
    convert via `session.db_to_linear_gain(db)`.
*   **NEVER** guess `ReaDelay`'s normalized musical length (`param 4`). `ReaDelay` scales parameter 4
    linearly across `[0, 256]` eighth notes, so a 1/4-note echo (`2.0` eighth notes) is
    `2 / 256 = 0.0078125` (`session.reaper_delay_musical_norm(2.0)`), whereas `0.03125` is a full
    8-eighth-note bar.
*   **NEVER** leave a karaoke reference video unmuted in a Reaper recording session. The high-res
    24-bit WAV on Track 2 is the audio source; Track 3 (`Lyrics Video`) must have `MUTESOLO 1 0 0`
    so `Cmd+Shift+V` shows the synced lyric wipe without comb-filtering against the audio track.
*   **NEVER** overwrite a recorded Reaper `.rpp` session without backing up the raw recording
    project to `Backups/<stem>-raw-recording.rpp` first. `session.py --produce-vocal` writes the
    backup, emits `<stem>.produce.json` and `<stem>.produce.lua`, reloads via
    `reaper.Main_openProject("noprompt:...")`, and verifies every live parameter in `<stem>.fx-report.txt`.
