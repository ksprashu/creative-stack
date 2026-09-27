# Creative Stack

An AI-native content production toolchain. One repo that installs the media tools, wires them
into headless entry points, and registers a set of agent skills — so an agent can actually
edit your video, build your deck and clean up your screenshots instead of telling you which GUI
button to press.

Runs on **macOS**, **Arch** (including Omarchy) and **Debian/Ubuntu**, from a clean machine, in
one command — and the skills install into **Claude Code, Codex, Gemini CLI, Antigravity,
opencode and Cursor**, whichever of them the machine has.

```bash
git clone https://github.com/<you>/creative-stack ~/code/github/creative-stack
cd ~/code/github/creative-stack
bash scripts/bootstrap.sh
```

---

## What you get

| Skill | What it does |
| --- | --- |
| `/shot` | Screenshots worth publishing: device frames, gradient backdrops, numbered callouts, automatic secret redaction, code and terminal images |
| `/vidkit` | Recording → published video: transcribe, cut silence, auto-zoom, burn captions, chapters, loudness, encode ladder, NLE handoff |
| `/deck` | Outline → brand-themed slides → HTML / PDF / editable PPTX / per-slide PNG |
| `/vector` | SVG art, logos, architecture diagrams, icon sets |
| `/raster` | Batch image editing, compositing, size ladders, background removal |
| `/render3d` | Product shots, animated logo stings, isometric diagram renders |
| `/sound` | Voice cleanup, −14 LUFS normalisation, ducking, MIDI beds, stingers |
| `/artgen` | Prompt → image / video / music via Gemini, Imagen, Veo and Lyria |

Underneath: 96 packages across 15 groups, headless bridges to Blender, Inkscape and GIMP, and
three opt-in MCP servers.

---

## Install

```bash
bash scripts/bootstrap.sh                  # everything
bash scripts/bootstrap.sh --headless       # no desktop apps — servers, CI, containers
bash scripts/bootstrap.sh --skip-heavy     # no LaTeX, Blender or Resolve (~10GB lighter)
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
| opencode | reads Claude Code's and the hub's | `opencode.json` (project, `enabled: false`) |
| Cursor | reads Claude Code's | `.cursor/mcp.json` (project) |

```bash
python3 scripts/sync_skills.py --list          # which of these exist here
python3 scripts/sync_skills.py --fix --prune   # link into all of them
```

Only installed harnesses are written to — `~/.agents/skills` is the one exception, created
unconditionally because Codex and opencode both read it and a freshly imaged box has no harness
config yet. Symlinks pointing outside this repo are never touched and the shared `skills.json`
registries are merged, so `agent-skill-forge` and creative-stack coexist in the same directories.

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

---

## Security

*   **API keys never touch the repo.** They live in the OS secret store; skill scripts read them
    from `os.environ` and fail fast if absent. `creative-stack.env` holds paths only, and
    `validate_skills.py` blocks a commit containing an `AIza…` / `sk-…` / `ghp_…` token or a
    hardcoded home directory.
*   **MCP servers are pinned to reviewed commit SHAs**, never `main`. These are third-party
    servers that execute code with your filesystem access — a published advisory documents command
    injection in a popular ImageMagick MCP server. All three are registered **disabled**; enable
    one per session with `claude mcp enable blender`.
*   **`/shot` redacts before it publishes.** Screenshots of a terminal or a cloud console are the
    most common way a token gets into a blog post.

---

## Layout

```
brand/            tokens.json — colours, type, logo. tokens.local.json overrides, gitignored.
packages/         manifest.json — the single source of truth for what gets installed
config/           harnesses.json — every agent runtime's skills dirs and MCP dialect
scripts/          bootstrap, hostenv, packages, doctor, bridge/MCP installers, skill sync
skills/           the eight skills, symlinked into every installed harness
templates/        screenshot frames, deck themes, the Blender studio rig
out/              everything generated. gitignored.
```

## License

Apache-2.0. See [LICENSE](LICENSE).
