# Creative Stack

An AI-native content production toolchain. One repo that installs the media tools, wires them
into headless entry points, and registers a set of agent skills — so an agent can actually
edit your video, build your deck and clean up your screenshots instead of telling you which GUI
button to press.

Runs on **macOS**, **Arch** (including Omarchy) and **Debian/Ubuntu**, from a clean machine, in
one command — and the skills install into **Claude Code, Codex, Gemini CLI, Antigravity
and Cursor**, whichever of them the machine has.

```bash
git clone https://github.com/ksprashu/creative-stack.git ~/code/github/creative-stack
cd ~/code/github/creative-stack
bash scripts/bootstrap.sh
```

📖 **See also:**
* **[Audio Routing, Karaoke & Mastering Playbook (`skills/sound/references/routing-and-karaoke.md`)](skills/sound/references/routing-and-karaoke.md)** — Loopback 2, Audio Hijack 4, BlackHole, PipeWire, stem separation, and Reaper vocal production.

---

## What you get

| Skill | What it does |
| --- | --- |
| `/shot` | Screenshots worth publishing: device frames, gradient backdrops, numbered callouts, automatic secret redaction, code and terminal images |
| `/vidkit` | Recording → published video: transcribe, Gemini-proposed cuts and chapters, cut silence, secret blurring, auto-zoom, karaoke captions, motion-graphics titles and lower thirds, loudness, encode ladder, NLE handoff |
| `/deck` | Outline → brand-themed slides → HTML / PDF / editable PPTX / per-slide PNG |
| `/vector` | SVG art, logos, architecture diagrams, icon sets |
| `/raster` | Batch image editing, compositing, size ladders, BiRefNet background removal, select-by-description masks and redaction for images and video |
| `/render3d` | Product shots, animated logo stings, isometric diagram renders |
| `/sound` | Live Loopback / Audio Hijack / PipeWire capture, voice cleanup, −14 LUFS normalisation, ducking, MIDI beds, stingers, stem-separated karaoke with key shift, and native Reaper FX vocal production |
| `/artgen` | Prompt → image / video / music / voiceover via Gemini Image, Gemini Omni, Veo, Lyria, Gemini TTS and Chirp 3 HD |
| `anot8` | Screenshot callouts, spotlights and blur redaction from a JSON spec, with pixel bounds back for a visual check. Vendored from [ksprashu/anot8](https://github.com/ksprashu/anot8) at a pinned commit; `/shot` uses it |

Underneath: 100 packages across 15 groups, headless bridges to Blender, Inkscape, GIMP, Reaper and
Audacity, and four opt-in MCP servers.

📖 **See [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) for the complete reference, per-skill CLI flags, intermediate file contracts, and multi-skill production recipes.**

---

## Install

```bash
bash scripts/bootstrap.sh                  # everything
bash scripts/bootstrap.sh --headless       # no desktop apps — servers, CI, containers
bash scripts/bootstrap.sh --skip-heavy     # no LaTeX, Blender or Resolve (~10GB lighter)
bash scripts/bootstrap.sh --with-ml        # + torch/transformers for SAM 2 refinement and video tracking (~1GB)
bash scripts/bootstrap.sh --yes            # never prompt (cloud-init, provisioning)
bash scripts/bootstrap.sh --doctor         # verify only, change nothing
```

Seven stages, each idempotent: packages → python venv → node CLIs → app bridges → MCP servers →
skills → verification. Re-running is safe; every stage checks what is already there first.

**Fully unattended on Linux needs passwordless sudo.** Without it the script asks once at the
start and keeps the timestamp warm, so a long install still finishes without babysitting. With
`--yes` it refuses to start rather than stalling an hour in:

```
<you> ALL=(ALL) NOPASSWD: ALL     # /etc/sudoers.d/creative-stack
```

### Platform notes

|  | macOS | Arch / Omarchy | Debian / Ubuntu |
| --- | --- | --- | --- |
| Packages | Homebrew (installed if absent) | pacman + AUR via `paru-bin` | apt + NodeSource 22.x |
| Secrets | Keychain | libsecret (`secret-tool`) | libsecret (`secret-tool`) |
| ffmpeg | keg-only `ffmpeg-full` — the stock formula has no libass or freetype | distro build, already complete | distro build, already complete |
| Rust | Homebrew | `rust` | rustup — apt's 1.75 is too old to build `oxipng` and friends |
| DaVinci Resolve | free edition **cannot** be scripted externally | Studio-only on Linux, so scripting always works | same |

Where there is no secret store at all, keys fall back to a `0600` file at
`~/.config/creative-stack/secrets.env`. They are never written into the repo.

### After bootstrap

```bash
source creative-stack.env          # add to ~/.zshrc or ~/.bashrc
$EDITOR brand/tokens.json          # your colours, fonts, logo
make demo                          # end-to-end smoke test
```

---

## How it stays portable

Three files carry the whole portability story, so the skills never have to care:

**`packages/manifest.json`** — one entry per tool, with the name each package manager uses and
what binaries it should produce:

```json
{"id": "bat", "provides": ["bat", "batcat"], "brew": "bat",
 "arch": "bat", "debian": "bat", "optional": true,
 "note": "Debian installs the binary as batcat, hence two provides"}
```

Verification checks `provides`, not the package database — a tool installed by any means counts.
When a name is missing from the repos the resolver falls through to the AUR, then to cargo, go,
uv, npm or a source build, and reports which route it took. A stale package name costs a warning,
never the bootstrap. `Brewfile` is **generated** from this file (`packages.py --emit brewfile`)
so the two cannot drift.

**`scripts/hostenv.py`** — every platform-dependent path and capability behind one import:
which ffmpeg to call, where Chrome lives, which secret store exists, where fonts and the
Playwright cache go, whether there is a display at all.

```bash
python3 scripts/hostenv.py     # what this machine looks like to the stack
python3 scripts/packages.py --plan     # what bootstrap would install, and how
```

> `hostenv` is deliberately not called `platform.py` — skill scripts put `scripts/` on
> `sys.path`, and a module by that name would shadow the stdlib for everything imported after it.

**`config/harnesses.json`** — every agent runtime that consumes the skills: where it keeps them,
how to tell it is installed, and which of the three incompatible MCP config dialects it speaks.

| Harness | Skills directory | MCP config |
| --- | --- | --- |
| Claude Code | `~/.claude/skills` | `.mcp.json` (project, pending approval) |
| OpenAI Codex | `~/.agents/skills` | `~/.codex/config.toml` — command printed, not written |
| Gemini CLI | `~/.gemini/skills` | `.gemini/settings.json` (project) |
| Antigravity IDE | `~/.gemini/config/skills` | `~/.gemini/config/mcp_config.json` — printed |
| Antigravity CLI | `~/.gemini/antigravity-cli/skills` | — |
| Universal Agent Hub | `~/.agents/skills` | — |
| Cursor | reads Claude Code's | `.cursor/mcp.json` (project) |

```bash
python3 scripts/sync_skills.py --list          # which of these exist here
python3 scripts/sync_skills.py --fix --prune   # link into all of them
```

Only installed harnesses are written to — `~/.agents/skills` is the one exception, created
unconditionally because Codex reads it and a freshly imaged box has no harness
config yet. Symlinks pointing outside this repo are never touched and the shared `skills.json`
registries are merged, so `agent-skill-forge` and creative-stack coexist in the same directories.

A skill whose home is another repo (anot8) is listed in `EXTERNAL` in `sync_skills.py` with a
commit SHA. `--fix` sparse-clones it into `~/.local/share/creative-stack/skills/` and links it
like the rest; moving the pin is a one-line diff you read first.

MCP servers are only ever written at **project** scope. Codex and the Antigravity IDE have no
project scope, so they get a printed `mcp add` command instead of an edit — otherwise enabling
Blender here would start a Blender server in every unrelated repo you open.

---

## Verify

```bash
python3 scripts/doctor.py            # full report: tools, bridges, credentials, MCP, skills
python3 scripts/doctor.py --quick    # skip the network probes
python3 scripts/packages.py --check  # just: did every package produce its binaries
```

`doctor.py` probes each AI model separately and reports per-model reachability, so an org policy
blocking Veo shows up as one amber line rather than a skill failing at render time.

The claim that this works on a clean Linux box is tested rather than asserted:

```bash
make verify        # bootstrap a fresh Arch and a fresh Ubuntu in containers
```

It runs as a non-root user with passwordless sudo — the shape of an Omarchy or Ubuntu desktop —
and leaves the logs in `out/verify/`. CI runs the same script weekly on amd64, which covers the
failures nobody here controls: a renamed apt package, a dropped AUR package, a new compiler that
rejects a vendored crate.

---

## What it deliberately does not use

Popular in "AI media stack" lists, and left out on purpose. Each was weighed against the three
rules this repo runs on: deterministic local tools for precise edits, Google models and compute
for generation, free software everywhere else.

| Left out | Why | What does the job instead |
| --- | --- | --- |
| Sharp | A Node image layer beside a Python stack; its engine is already here | Pillow, libvips (`vips`), ImageMagick |
| MoviePy | Decodes every frame into numpy — slow, and duplicates what ffmpeg already does | ffmpeg + the OTIO timeline in `/vidkit` |
| Remotion | Needs a paid company licence above three people | `/vidkit motion.py`: HTML/CSS templates rendered frame-by-frame by Playwright, encoded by ffmpeg |
| fal.ai, Replicate | Paid third-party inference | Gemini Image / Veo / Lyria on Vertex; open weights via Vertex Model Garden or Cloud Run GPUs |
| Shotstack, Creatomate, JSON2Video | Paid cloud timelines | `/vidkit timeline.py` (OTIO JSON) rendered locally; Cloud Run Jobs if it has to be remote |
| Cloudinary | Paid URL transforms | `/raster ladder.py`, `cutout.py`, `segment.py` |
| Ultralytics YOLO | AGPL-3.0 | Gemini detection and segmentation, refined by SAM 2 (Apache-2.0) |
| Imagen | Shut down 17 Aug 2026 | `gemini-3.1-flash-image` and the rest of the Gemini Image chain |
| ComfyUI | Optional, not installed. Slow on Apple Silicon, and the Gemini APIs cover most of it | Worth adding only for LoRA style-locking, ControlNet or exact masked inpainting |
| A wide MCP surface | Third-party servers run code with your filesystem access | Skills that call CLIs; the few MCP servers are pinned and disabled by default |

---

## Security

*   **API keys never touch the repo.** They live in the OS secret store; skill scripts read them
    from `os.environ` and fail fast if absent. `creative-stack.env` holds paths only, and
    `validate_skills.py` blocks a commit containing an `AIza…` / `sk-…` / `ghp_…` token or a
    hardcoded home directory.
*   **MCP servers are pinned to reviewed commit SHAs or exact package versions**, never `main`
    or `@latest`. These servers execute code with your filesystem access — a published advisory
    documents command injection in a popular ImageMagick MCP server. All are registered at
    project scope and stay **pending** until approved; enable one per session with
    `claude mcp enable blender`.
*   **`/shot` redacts before it publishes.** Screenshots of a terminal or a cloud console are the
    most common way a token gets into a blog post. `scan.py` finds keys, labelled values,
    emails and project ids by OCR and writes the blurs; `scan.py --check` gates the final image.
    `/vidkit vidscan.py` does the same for recordings, blurring each hit for as long as it shows.

---

## Layout

```
brand/            tokens.json — colours, type, logo. tokens.local.json overrides, gitignored.
packages/         manifest.json — the single source of truth for what gets installed
config/           harnesses.json — every agent runtime's skills dirs and MCP dialect
scripts/          bootstrap, hostenv, packages, doctor, bridge/MCP installers, skill sync
skills/           the eight skills, symlinked into every installed harness (plus anot8, vendored)
templates/        screenshot frames, deck themes, motion-graphics templates, the Blender studio rig
tests/            stdlib-only unit tests for pure functions across skills and scripts
out/              everything generated. gitignored.
```

## License

Apache-2.0. See [LICENSE](LICENSE).
