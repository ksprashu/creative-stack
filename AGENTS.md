# AGENTS.md

Invariants for anyone — human or model — changing this repo.

## What this is

A portable content-production toolchain plus eight agent skills. It targets macOS, Arch and
Debian/Ubuntu from a clean machine, and Claude Code, Codex, Gemini CLI, Antigravity, opencode
and Cursor from a clean profile. Portability is a feature, not a nice-to-have: a change that
only works on the machine — or in the harness — you tested it on is a bug.

## The three chokepoints

Everything platform-dependent goes through exactly one of these. If you find yourself writing
`if sys.platform == "darwin"` in a skill script, you are in the wrong file.

*   **`packages/manifest.json`** — what gets installed. One entry per tool with `brew`, `arch`,
    `debian` names, a `provides` list of the binaries it must produce, and an optional
    `fallback` route (`aur` / `npm` / `uv` / `cargo` / `go` / `source` / `font-release`).
    `Brewfile` is generated from this; never hand-edit it.
*   **`scripts/hostenv.py`** — how to reach things. Paths, binaries, the secret backend, whether
    a display exists. Add a function here rather than branching at the call site.
*   **`config/harnesses.json`** — who consumes it. Each agent runtime's skills directories,
    skill-source registry files, and MCP config path and dialect, plus the paths that prove it
    is installed. Supporting a new harness is an edit here and nowhere else; no script may
    name `~/.claude/skills` or any other harness path directly.

Named `hostenv`, not `platform`, because skill scripts put `scripts/` on `sys.path` and a
`platform.py` there would shadow the stdlib for every module imported afterwards.

## Hard rules

*   **No secrets in the repo, ever.** Keys live in the OS secret store (Keychain / libsecret /
    `0600` file). Scripts read them from `os.environ` and fail fast. `creative-stack.env` is
    paths only. `validate_skills.py` is the gate — do not weaken it.
*   **MCP servers are pinned to reviewed commit SHAs.** Never `main`, never a floating tag.
    They execute arbitrary code with filesystem access. All are registered disabled.
*   **Never write a user-scope MCP config.** A harness with no project scope (Codex, the
    Antigravity IDE) gets a printed command, not an edit. Writing one would start Blender and
    GIMP servers in every unrelated repo the user opens.
*   **`sync_skills.py` must never delete a non-symlink directory,** and never a symlink that
    points outside this repo. Those directories are shared — `agent-skill-forge` and anything
    else the user installs link into the same `~/.claude/skills`. A name we do not recognise is
    evidence of another owner, not of staleness. The same applies to the `skills.json`
    registries: merge, never rewrite.
*   **Only write where a harness is installed.** The one exception is `~/.agents/skills`, the
    vendor-neutral directory Codex and opencode both document, which is created unconditionally
    so a freshly imaged box has the skills waiting before any agent CLI has ever run.
*   **Verify by `provides`, not by the package database.** A tool installed by any route counts.
    Use `hostenv.which()` rather than `shutil.which()` — it also looks in the directories
    bootstrap installs into, so a freshly-installed tool is not reported missing.
*   **Every stage is idempotent.** `bootstrap.sh` gets re-run constantly; detect existing state
    before doing anything.
*   **A missing package is a warning, not a failure.** Distro names move. The resolver falls
    through and reports the route it took; `doctor.py` is where problems get surfaced.

## Skill conventions

Copied from `agent-skill-forge` so the muscle memory carries over.

*   `SKILL.md` frontmatter is exactly three fields: `name`, `description` (ending in
    `Trigger via /x.`), `disable-model-invocation: true`.
*   Headings are `## 🎯 Goal`, `## 📋 Step-by-Step Workflow`, `## 💡 Concrete Example`,
    `## 🚫 Hard Constraints` — in that order.
*   Hard constraints are written as `**NEVER** …` and each one explains *why*. They exist
    because something already went wrong; keep the reason.
*   Every script carries the Apache-2.0 header and is runnable standalone with `--help`.
*   Scripts write a reviewable intermediate (`.json`, `.yaml`, `.srt`, `.md`) before they write
    pixels, so a bad automatic decision is an edit rather than a re-run.

## Platform gotchas already paid for

Do not re-discover these.

*   Homebrew's `ffmpeg` has no libass, freetype or fontconfig — `ass`, `subtitles` and `drawtext`
    are missing. macOS uses keg-only `ffmpeg-full`; Linux distro builds are already complete.
    Always `vidlib.need("ffmpeg")`.
*   ffmpeg 9 removed `-filter_complex_script`; 7.1+ wants `-/filter_complex FILE`.
*   Ubuntu 24.04 ships ImageMagick **6** — the binary is `convert`, not `magick`. Use
    `hostenv.magick_cmd()`.
*   Ubuntu 24.04's apt `cargo` is 1.75, too old to build `oxipng`, `gifski`, `typst` or
    `silicon`. Debian gets rustup instead; the manifest gates on the *version*, not presence.
*   ggml's `GGML_NATIVE=ON` emits an `-mcpu=native+nodotprod+…` string that GCC 13 on aarch64
    rejects, so `install/whisper-cpp.sh` retries with `-DGGML_NATIVE=OFF`.
*   Debian renames binaries (`bat` → `batcat`). That is what multiple `provides` entries are for.
*   DaVinci Resolve free **cannot** be scripted from an external process; on Linux Blackmagic
    ships Studio only, so scripting always works there. `/vidkit` emits FCP7 XML (`.xml`, never
    `.fcpxml` — OTIO writes XMEML and Resolve picks its parser from the extension).
*   Global npm needs a writable prefix; Linux defaults to root-owned `/usr`.
*   pacman 7 needs `DisableSandbox` under qemu emulation (containers, CI).
*   The official `archlinux` image is amd64-only. Arch Linux ARM reports `ID=archarm`, which is
    why family detection matches on `ID_LIKE` and falls back to `command -v pacman`.

## Harness gotchas already paid for

*   Codex's documented user-level skills path is `~/.agents/skills`, **not** `~/.codex/skills`
    (that holds `config.toml`). opencode and Cursor have no directory of their own at all —
    both read `~/.claude/skills`, so linking into their config dirs as well would register
    every skill two or three times in one picker.
*   The three MCP config dialects are not interchangeable: `mcpServers` (Claude Code, Gemini
    CLI, Cursor) splits command from args; opencode's `mcp` block wants one argv list and is
    the only non-Claude format with an `enabled` flag; Codex uses TOML `[mcp_servers.x]`.
*   Only `.mcp.json` is documented to expand `${HOME}`. Every other generated MCP config gets a
    real absolute path and is gitignored — `install_mcp.py` regenerates it per machine.
*   `claude mcp list` answers for one harness and only when that harness is installed. The
    doctor reads the config files instead, or a Codex-only box reports the MCP layer as broken.

## Testing a change

```bash
python3 scripts/hostenv.py                 # does this machine resolve correctly
python3 scripts/packages.py --plan         # does every package have a route
python3 scripts/doctor.py --quick          # nothing regressed
python3 scripts/validate_skills.py         # skills still lint
python3 scripts/sync_skills.py --list      # which harnesses are detected here
bash    scripts/verify_linux.sh            # clean Arch + Ubuntu, in containers
make demo                                  # six artefacts land in out/
```

For a cross-platform change, run it in a container before claiming it works:

```bash
docker run --rm -v "$PWD:/repo:ro" ubuntu:24.04 \
  bash -c 'apt-get update -qq && apt-get install -y -qq python3 curl ca-certificates \
           && cp -r /repo /work && cd /work && python3 scripts/packages.py --plan'
docker run --rm --platform linux/amd64 -v "$PWD:/repo:ro" archlinux:latest \
  bash -c 'echo DisableSandbox >> /etc/pacman.conf && pacman -Sy --noconfirm python \
           && cp -r /repo /work && cd /work && python3 scripts/packages.py --plan'
```
