# Changelog

All notable changes to **Creative Stack** are documented in this file.

## [0.1.0] - 2026-11-07

### Added
* **Eight Portable Agent Skills (`/shot`, `/vidkit`, `/deck`, `/vector`, `/raster`, `/render3d`, `/sound`, `/artgen`)**:
  * `/shot`: Deterministic browser/window capture (`capture.py`), parametric screenshot callouts, spotlighting, and redaction via Anot8 (`annotate.py`), and macOS/browser/terminal device framing (`frame.py`).
  * `/vidkit`: Local `whisper.cpp` word-timed transcription (`transcribe.py`), Gemini multimodal edit plans (`analyze.py`), silence cutting (`autocut.py`), cursor auto-zoom (`zoom.py`), `\kf` karaoke & highlight libass captions (`captions.py`), HTML/CSS Playwright motion graphics (`motion.py`), OpenTimelineIO FCP7 XML export (`timeline.py`), and multi-rung + single-file EBU R128 MP4 publishing (`publish.py`).
  * `/deck`: Markdown outline generation (`outline.py`), brand token Marp CSS themes (`theme.py`), and HTML / PDF / editable PPTX / PNG slide builds (`build.py`).
  * `/vector`: Diagram rendering across D2, Mermaid, Graphviz, and PlantUML (`diagram.py`), bitmap tracing (`trace.py`), SVG optimisation (`svg.py`), and icon sprite sheets (`sprite.py`).
  * `/raster`: Non-destructive ImageMagick 6/7 pipelines (`edit.py`), BiRefNet/ONNX background cutouts (`cutout.py`), Gemini + SAM 2 text-prompted segmentation and redaction for images and video (`segment.py`), and responsive WebP/AVIF/PNG ladders (`ladder.py`).
  * `/render3d`: Headless Blender Cycles/EEVEE studio rendering (`render.py`) and 3D SVG-extruded animated logo stings (`logo3d.py`).
  * `/sound`: Live hardware & virtual loopback capture (`record.py` for Loopback 2, Audio Hijack 4 `.ahcommand` JS automation, BlackHole, and PipeWire), voice cleanup & two-pass EBU R128 loudness normalisation (`clean.py`), procedural General MIDI music beds and stings (`bed.py`), phrase-envelope ducking (`mix.py`), stem-separated karaoke with `rubberband` formant-preserving key shift & YouTube OCR lyric teleprompter (`karaoke.py`), and native Reaper `.rpp` + ReaScript vocal production (`session.py`).
  * `/artgen`: Unified Google GenAI / Vertex AI generation across Gemini 3.1 Flash Image (`image.py`), Gemini Omni 1.1 Flash & Veo 3.1 (`video.py`), Lyria (`music.py`), and Gemini 3.8 TTS + Cloud TTS Chirp 3 HD (`voice.py`).
* **Cross-Platform Toolchain & Bootstrap (`packages/manifest.json`, `scripts/bootstrap.sh`, `scripts/hostenv.py`)**:
  * 100 packages across 15 groups supporting macOS (Homebrew), Arch Linux / Omarchy (`pacman` + AUR), and Debian / Ubuntu (`apt`), with automatic fallbacks to `cargo`, `go`, `uv`, `npm`, and source builds.
  * Multi-harness skill and project-scoped MCP synchronization (`scripts/sync_skills.py`, `scripts/install_mcp.py`, `config/harnesses.json`) across Claude Code, OpenAI Codex, Gemini CLI, Antigravity IDE, Antigravity CLI, Universal Agent Hub, and Cursor.
* **Documentation & Verification Suite**:
  * Comprehensive User Guide (`docs/USER_GUIDE.md`), Interactive Atelier HTML Showcase Report (`docs/report/index.html`), audio routing & karaoke playbook (`skills/sound/references/routing-and-karaoke.md`), stdlib unit tests (`tests/test_pure.py`), and containerized Arch + Ubuntu verification (`scripts/verify_linux.sh`).
