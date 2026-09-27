---
name: sound
description: Voice cleanup, EBU R128 loudness, music beds and logo stings generated from scratch, ducked mixes, and a Reaper session when a human ear is needed. Trigger via /sound.
disable-model-invocation: true
---

# Sound: Voice, Music and the Mix

The audio half of a video, and the whole of a podcast. Repair the voice, generate a bed that
carries no licence, duck one under the other, and land on the loudness target the platform
actually wants.

---

## 🎯 Goal

A recording made on whatever microphone was to hand becomes a track that sounds deliberate, at
−14 LUFS with a −1 dBTP ceiling, every time and without a DAW. Music that goes under it is
generated here, so there is no attribution to chase and no takedown to answer.

Loudness is measured, not eyeballed: every script ends by re-measuring what it wrote and telling
you whether it hit the target.

---

## 📋 Step-by-Step Workflow

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
   ```

   Each stem lands on its own track at the right offset and gain. `--render` drives Reaper's
   headless renderer; `--open` just opens the session.

---

## 💡 Concrete Example

Scoring a ten-minute tutorial: repair the narration, build a bed the right length, open with a
sting, and end up with one file per delivery target.

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
