---
name: vidkit
description: Turn a raw screen recording into a published video — transcribe, cut silence, auto-zoom, brand-styled captions, chapters, loudness, encode ladder, and an NLE handoff. Trigger via /vidkit.
disable-model-invocation: true
---

# Vidkit: Recording → Published Video

Everything mechanical between hitting stop on the recorder and uploading the file.

---

## 🎯 Goal

A 40-minute unedited screen capture becomes a tight, captioned, correctly-loud video with
chapters and a poster frame — without opening an NLE. When the last 10% does need hand editing,
hand Resolve a timeline instead of a flat file, with the source media still linked.

Every stage writes a reviewable artefact (`segments.json`, `zoom.yaml`, `.srt`) before it writes
pixels, so a bad automatic decision is a two-line edit rather than a re-run.

---

## 📋 Step-by-Step Workflow

Run in this order. **Zoom before captions** — burned-in captions are the brightest moving thing
in the frame, and the motion detector will happily push in on them.

1. **Transcribe** (local, nothing uploaded). Feed it your jargon or it will invent some:
   ```bash
   python3 scripts/transcribe.py -i raw.mov -o out/talk \
     --model small.en --vocab "Cloud Run, GKE, Vertex AI, gcloud, Pub/Sub, Terraform"
   ```

2. **Cut the dead air.** Analyse first, look at the number, then render:
   ```bash
   python3 scripts/autocut.py -i raw.mov -o out/cut.json          # dry run
   python3 scripts/autocut.py -i raw.mov -o out/cut.json \
     --render out/cut.mp4 --srt out/talk.srt --srt-out out/cut.srt
   ```
   If it removed too much, the knob is `--threshold` (lower dB = less aggressive), then
   `--padding`.

   **Optionally, let Gemini propose the edit first.** Silence detection cannot see a false
   start, a retake or two minutes watching a build spinner; Gemini watching a small proxy
   (640px, 4fps — this step *does* upload) can. It writes `analysis.json` with chapters,
   highlights, cuts with a reason each, b-roll ideas carrying ready `/artgen` prompts, and a
   title and description. Review it — delete any cut you disagree with — then hand it on:
   ```bash
   python3 scripts/analyze.py -i raw.mov -o out/analysis.json --transcript out/talk.json \
     --notes "audience: platform engineers; keep the IAM detour"
   python3 scripts/autocut.py -i raw.mov -o out/cut.json --analysis out/analysis.json \
     --render out/cut.mp4 --srt out/talk.srt --srt-out out/cut.srt
   ```
   autocut removes the analysis cuts *on top of* the silences, and writes
   `out/cut.chapters.txt` — the chapters moved onto the cut timeline — for steps 5 and 6.
   Over 20MB of proxy on Vertex needs `--gcs-uri gs://…`; AI Studio uploads it itself.

   **Then redact.** A terminal or console recording shows tokens, project ids and emails for
   seconds at a time. `vidscan.py` OCRs a frame a second with `/shot`'s scan rules and blurs
   each hit for as long as it is on screen (padded by a sample either side). Review the
   tracks, delete false positives, then render — before zoom, so the blur rides the zoom:
   ```bash
   python3 scripts/vidscan.py -i out/cut.mp4 --json out/scan.json            # review
   python3 scripts/vidscan.py -i out/cut.mp4 --from-json out/scan.json --render out/redacted.mp4
   ```
   Carry on from `out/redacted.mp4`. Fast scrolling? Raise `--fps`.

3. **Push in on the action** — optional, but it is what makes a 4K capture legible at blog width:
   ```bash
   python3 scripts/zoom.py -i out/redacted.mp4 --auto --spec-out out/zoom.yaml   # review
   python3 scripts/zoom.py -i out/redacted.mp4 --spec out/zoom.yaml -o out/zoomed.mp4
   ```

4. **Caption**, styled from `brand/tokens.json`:
   ```bash
   python3 scripts/captions.py -i out/zoomed.mp4 -s out/cut.srt -o out/captioned.mp4 --style pill
   python3 scripts/captions.py -i out/zoomed.mp4 -s out/cut.srt -o out/yt.mp4 --soft
   ```
   For shorts and social cuts, animate them word by word — still one libass pass, seconds
   not minutes. `highlight` lights the current word; `karaoke` sweeps the accent through the
   line. `--words` takes whisper's full JSON (`whisper-cli … -ojf`) for exact timing; without
   it, or once autocut has moved the timeline, each cue is shared across its words by length:
   ```bash
   python3 scripts/captions.py -i out/zoomed.mp4 -s out/cut.srt -o out/short.mp4 --style highlight
   ```

   **Motion graphics** — title cards, lower thirds, end cards — are HTML/CSS templates in
   `templates/motion/`, rendered frame-exact by headless Chromium and encoded by ffmpeg.
   Every render writes `<name>.motion.json` first; edit it and re-render with `--spec`:
   ```bash
   python3 scripts/motion.py -t title -o out/title.mp4 \
     -p kicker="Cloud Run" -p title="Deploy in 90 seconds" -p subtitle="No Dockerfile"
   python3 scripts/motion.py -t lower-third --overlay-on out/captioned.mp4 --at 4 \
     -o out/with-lt.mp4 -p name="Ada Lovelace" -p role="Developer Advocate"
   python3 scripts/motion.py -t end-card -o out/end.mp4 \
     -p title="Thanks for watching" -p url="cloud.google.com/run"
   python3 scripts/motion.py -t lower-third --alpha -o out/lt.mov -p name="Ada"  # for Resolve
   ```
   `.mov --alpha` is ProRes 4444 for an NLE, `.webm --alpha` is VP9 with alpha for the web.
   A new template is any HTML file there that links `motion.css` and `motion.js`.

5. **Publish** — check nothing readable is left, then loudness, chapters, ladder, poster:
   ```bash
   python3 scripts/vidscan.py -i out/captioned.mp4 --check --fps 2
   python3 scripts/publish.py -i out/captioned.mp4 -o out/publish --chapters chapters.txt
   ```

6. **Or hand it to Resolve** instead of steps 3-5:
   ```bash
   python3 scripts/timeline.py -s out/cut.json -o out/edit.xml --chapters chapters.txt
   ```

---

## 💡 Concrete Example

A 22-minute Cloud Run walkthrough, start to upload:

```bash
# 1. transcript (once; reuse it for the blog post too)
python3 scripts/transcribe.py -i ~/Movies/cloudrun.mov -o out/talk \
  --model small.en --vocab "Cloud Run, Artifact Registry, gcloud, Cloud Build, IAM"

# 2. 22:14 -> 15:02
python3 scripts/autocut.py -i ~/Movies/cloudrun.mov -o out/cut.json \
  --render out/cut.mp4 --srt out/talk.srt --srt-out out/cut.srt

# 3. follow the cursor through the console
python3 scripts/vidscan.py -i out/cut.mp4 --json out/scan.json         # review the tracks
python3 scripts/vidscan.py -i out/cut.mp4 --from-json out/scan.json --render out/redacted.mp4
python3 scripts/zoom.py -i out/redacted.mp4 --auto -o out/zoomed.mp4 --zoom 1.5

# 4. captions that match the slides and the blog
python3 scripts/captions.py -i out/zoomed.mp4 -s out/cut.srt -o out/captioned.mp4 --style pill

# 5. nothing readable left? then chapters, from the markers autocut found at the long pauses
cat > out/chapters.txt <<'EOF'
0:00 What we're building
1:12 Deploying from source
6:40 Wiring up Artifact Registry
11:05 Rollbacks and traffic splitting
EOF

python3 scripts/vidscan.py -i out/captioned.mp4 --check --fps 2
python3 scripts/publish.py -i out/captioned.mp4 -o out/publish \
  --name cloudrun-walkthrough --chapters out/chapters.txt --thumb-at 74
```

`out/publish/` then holds `-1080p.mp4`, `-720p.mp4`, `-480p.mp4`, a 1280px poster frame,
`-chapters.txt` to paste into the YouTube description, and `manifest.json`.

Poster frames go through `/shot` for a proper thumbnail:

```bash
python3 ../shot/scripts/frame.py -i out/publish/cloudrun-walkthrough-poster.png \
  -o out/thumb.png --chrome browser --backdrop hero --caption "Deploy in 90 seconds"
```

---

## 🚫 Hard Constraints

*   **NEVER** run `captions.py` before `zoom.py`. Burned-in text is motion; the detector will
    zoom onto the subtitles and crop the demo out of frame.
*   **NEVER** call plain `ffmpeg` from these scripts — always `vidlib.need("ffmpeg")`. On macOS
    Homebrew's `ffmpeg` formula ships without libass and freetype, so `ass`, `subtitles` and
    `drawtext` are missing, and `need()` resolves the keg-only `ffmpeg-full` instead. On Linux
    the distro build already has them and `need()` returns the plain binary. Either way,
    `CREATIVE_STACK_FFMPEG` is the only supported override.
*   **NEVER** name a `timeline.py` output `.fcpxml`. OpenTimelineIO writes FCP7 XMEML; Resolve
    picks its parser from the extension and rejects the mismatch. Use `.xml`.
*   **NEVER** publish a terminal or console recording without `vidscan.py --check` on the
    final file. It only sees what OCR can read, so also watch the parts it cannot — a QR
    code, a face, an account picker.
*   **NEVER** publish without the loudness pass. A video that lands at −20 LUFS next to one at
    −14 reads as an audio fault, not a stylistic choice.
*   **NEVER** pass `analysis.chapters.txt` to `timeline.py` or `publish.py` after a cut. Gemini
    timestamps the *source*; once autocut has shortened it every chapter lands late, by however
    much was cut before it. Use the `cut.chapters.txt` autocut writes alongside `cut.json`.
*   **NEVER** run `autocut.py --analysis` without reading the cuts first. The model is told to
    be conservative, but a "retake" it removes may be the only take of that step — each cut
    carries a `reason` precisely so a human can veto it in seconds.
*   **NEVER** play a motion template in real time to capture it. Screenshots taken against a
    running clock drop and double frames whenever a frame takes longer than 1/fps to paint;
    `motion.py` pauses every animation and seeks it per frame. A template that animates from
    script must expose `window.seek(t)` instead of using `requestAnimationFrame` timing.
*   **NEVER** animate a `transform` on a wrapper that is not itself `position:absolute;
    inset:0`. A transform makes the element the containing block for its absolutely
    positioned children, so a zero-height wrapper drags them off-screen — the lower third
    rendered as an empty, perfectly valid ProRes file until its `.exit` wrapper was fixed.
    Extract a frame and look at it before trusting a render.
*   **NEVER** re-transcribe to fix a wrong word — edit the `.srt` and re-run from step 2.
    The transcript is a source file, not an intermediate.
*   **NEVER** trust an auto-cut on a recording with long deliberate pauses (a demo waiting on a
    build). Check `segments.json` first, and raise `--min-silence` past the pause length.
