---
name: artgen
description: Generate images, b-roll video, instrumental music and voiceover from prompts using Gemini Image, Gemini Omni, Veo, Lyria, Gemini TTS and Chirp 3 HD, over Vertex with an AI Studio fallback. Trigger via /artgen.
disable-model-invocation: true
---

# Artgen: Prompt to Picture, Clip, Score and Voice

The generative half of the stack. `/raster`, `/vector` and `/render3d` make things out of
files you already have; this makes them out of a sentence.

---

## 🎯 Goal

A hero image that matches the brand palette, a six-second b-roll clip for a title card, and a
three-minute bed under a tutorial — produced headlessly, reproducibly, and with a sidecar
recording which model drew what.

Two backends reach the same models. Vertex is tried first because it is the billed, auditable,
quota'd path; the AI Studio key is the fallback for when a GCP org policy blocks a model.
Neither is required to be present — whichever is configured is the one that gets used.

---

## 📋 Step-by-Step Workflow

1. **Images, with a review pass by default:**

   ```bash
   python3 scripts/image.py -p "isometric diagram of a build pipeline" -o out/hero.png \
     --ar 16:9 --brand google
   ```

   The image is generated, then shown to a vision model with the original prompt and asked
   whether it actually complied. A failure rewrites the prompt from the objection and tries
   again, up to `--max-attempts` (3). `--no-verify` halves the cost and the wall time when the
   picture is decorative and nobody will read text in it.

2. **Keep a series consistent by passing the first image back in:**

   ```bash
   python3 scripts/image.py -p "the same scene, zoomed to stage one" \
     -r out/hero.png -o out/step1.png --brand google
   ```

   `-r` is repeatable. Reference images silently drop `flash-lite` from the model chain — it is
   not tuned for multi-reference grounding and ignores references rather than refusing them.

3. **B-roll and title cards:**

   ```bash
   python3 scripts/video.py -p "slow dolly across a developer desk at dawn" \
     -o out/broll.mp4 --duration 6 --no-audio
   python3 scripts/video.py -p "the logo assembles from its parts" \
     -i out/logo.png -o out/sting.mp4 --veo
   ```

   Gemini Omni Flash goes first, then Veo 3.1. Omni is Google's default video model now: one
   synchronous call of thirty seconds to two minutes, 3–10s clips, sound included. Veo is a
   long-running job of one to four minutes, with elapsed seconds printed while it runs. Use
   `--veo` when the still has to be the **exact** first frame. Omni treats `--image` as a
   reference it may reinterpret. `--no-audio` when `/sound` is going to score it anyway; on
   Omni the track is stripped afterwards, because the model always scores its clips.

4. **Music, when a generated MIDI bed is not enough:**

   ```bash
   python3 scripts/music.py -p "warm lo-fi beat, mellow rhodes, soft vinyl crackle" \
     -o out/bed.wav --duration 180
   ```

   Lyria returns a fixed clip of about 33 seconds. A longer `--duration` generates as many
   clips as it needs and crossfades them; the prompt holds the style steady across generations.

5. **Voiceover, when there is a script but no recording:**

   ```bash
   python3 scripts/voice.py -i script.md -o out/voice.wav --voice Charon \
     --style "warm, unhurried"
   python3 scripts/voice.py -i dialogue.txt -o out/chat.wav --voices Kore,Puck
   ```

   The script is split at paragraphs and `out/voice.plan.json` is written before any audio.
   A mispronounced word or a bad split is an edit to that file and a
   `--from-plan out/voice.plan.json` re-run. A file where every line is `Name: words` with two
   names becomes a two-voice dialogue; `--narration` reads such a file as prose. `--list-voices` prints the prebuilt voices.

   `--engine chirp` swaps Gemini TTS for Cloud Text-to-Speech's Chirp 3 HD: the same voice
   names, `--lang` for 40+ locales, and dialogue with any number of speakers. It takes no
   `--style`, so reach for it when the read must stay level across a long script or the locale
   is not English. Gemini TTS remains the default because only it can act.

6. **Pick a model deliberately only when you have a reason.** The chain already falls through
   a 404, a 403 and a 429 to the next model and the other backend. `--model` heads the chain
   with something specific (Imagen is shut down — its replacement, `gemini-3.1-flash-image`,
   already heads the image chain); `--backend vertex|aistudio`
   pins the route.

7. **Hand off.** Everything here writes a plain file and a `.txt` sidecar. `/raster ladder.py`
   makes the responsive sizes, `/sound clean.py` and `mix.py` master the music and the voice, `/vidkit` cuts the clips
   together, `/deck` puts the images on slides.

---

## 💡 Concrete Example

A blog post needs a hero, three consistent step diagrams, an animated header and a bed for the
companion video.

```bash
# 1. the hero, brand-locked, reviewed
python3 scripts/image.py -o out/hero.png --ar 16:9 --brand google \
  -p "isometric flat illustration of a content pipeline: camera, waveform and slide deck \
      flowing left to right along a rail"

# 2. three step diagrams that look like they came from the same hand
for n in 1 2 3; do
  python3 scripts/image.py -o "out/step$n.png" --ar 4:3 --brand google -r out/hero.png \
    -p "the same illustration, zoomed to stage $n, with a numbered badge '$n'"
done

# 3. animate the hero into a six-second header loop
python3 scripts/video.py -i out/hero.png -o out/header.mp4 --duration 6 --no-audio --fast \
  -p "the elements drift and settle into place, gentle parallax, no camera shake"

# 4. three minutes of bed for the video cut
python3 scripts/music.py -o out/bed.wav --duration 180 \
  -p "warm lo-fi hip hop, mellow rhodes chords, brushed drums, soft vinyl crackle, 84 bpm"

# 5. narrate the companion video from its script
python3 scripts/voice.py -i post/script.md -o out/voice.wav --style "friendly, measured"

# 6. master it under the narration — /artgen does not touch loudness
python3 ../sound/scripts/mix.py -v out/voice.wav -m out/bed.wav -o out/episode.wav --duck 14
```

---

## 🚫 Hard Constraints

*   **NEVER** hardcode a Vertex location. Gemini image models are served from `global` and are
    absent from `us-central1`; Veo and Lyria are the other way round. Getting it wrong returns
    a 404 reading "your project does not have access", which sends you to the IAM console for a
    problem that is a region string. `genailib.vertex_location()` is the table.
*   **NEVER** call Omni by its GA name only. Vertex's Interactions API rejects
    `gemini-omni-1.1-flash` with a 400 and serves `gemini-omni-1.1-flash-preview`, while AI
    Studio serves the GA name. The chain lists both so either backend lands on a model that
    exists there.
*   **NEVER** classify an API error by substring-matching the stringified exception. A Vertex
    404 body ends in a documentation URL containing the digits `500`, so a naive match treats a
    missing model as a transient server fault and retries it twice for nothing. Read `.code`
    and `.status` off the exception — `genailib._classify()` does.
*   **NEVER** put the aspect ratio in the prompt text. `Aspect Ratio 16:9.` glued to the end is
    a suggestion the model weighs against everything else you asked for; `ImageConfig
    (aspect_ratio=...)` is honoured exactly.
*   **NEVER** retry Lyria by regenerating from scratch on a 503. It answers a perfectly ordinary
    prompt with `503 deadline_exceeded` roughly one time in three and succeeds on an identical
    retry seconds later — `genailib.vertex_predict()` already backs off and repeats.
*   **NEVER** concatenate two Lyria clips without a crossfade. They are separate generations, so
    the seam clicks and the phrase changes mid-bar; `acrossfade=d=2` hides both.
*   **NEVER** read an API key from a dotfile, a config file in the repo, or an argument. It comes
    from `$GOOGLE_API_KEY` or the OS keyring via `hostenv.secret_get`, and it is never printed,
    never logged, and never written to a sidecar.
*   **NEVER** ship a generated asset without its sidecar. Six months later nobody remembers which
    model drew the header, and a publisher asking for an AI-disclosure needs an answer.
*   **NEVER** normalise or master audio here. `music.py` writes 48kHz stereo at whatever level
    Lyria produced; `/sound mix.py` is what ducks it under a voice and lands it on −14 LUFS.
*   **NEVER** put delivery notes in a Gemini 3.8 TTS transcript. 3.8 reads `text` verbatim, so
    "Say warmly: …" gets spoken aloud; style travels in `speech_metadata.style`. Earlier models
    are the opposite and need the lead-in sentence — `voice.py request()` speaks both dialects,
    and drops 3.8 from the chain when the installed google-genai cannot express its request.
*   **NEVER** switch TTS models between chunks of one narration. The same voice name sounds
    different on each model, and the seam is audible; `voice.py` pins whichever model answered
    the first chunk to the head of the chain.
*   **NEVER** treat a `--count` of images as free. Each one is a generation plus a verification
    call; a loop over eight section headers with the default settings is up to 48 model calls.
*   **NEVER** use `/artgen` where `/vector` or `/render3d` would do. A diagram that has to be
    edited later should be SVG from `d2` or Inkscape, and a product shot that has to match a
    real object should be Blender. Generated pixels cannot be corrected, only regenerated.
