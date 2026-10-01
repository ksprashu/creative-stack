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
*   **Global npm needs a writable prefix, set *before* the package stage.** Linux defaults to
    root-owned `/usr`, so every npm tool dies with `EACCES: mkdir '/usr/lib/node_modules/…'`.
    Fixing it in bootstrap's Node stage — which runs *after* packages — left both a clean Arch
    and a clean Ubuntu reporting `Required packages unresolved: svgo, mermaid-cli, marp-cli` on
    a machine that ended up healthy. `packages.ensure_npm_prefix()` owns it, and tests
    writability rather than comparing to `/usr` (nvm and Volta set their own).
*   pacman 7 needs `DisableSandbox` under qemu emulation (containers, CI).
*   The official `archlinux` image is amd64-only. Arch Linux ARM reports `ID=archarm`, which is
    why family detection matches on `ID_LIKE` and falls back to `command -v pacman`.
*   **An AUR helper has to be probed, not just found on PATH.** `paru-bin` is a prebuilt binary
    pinned to one libalpm soname, so after pacman moves 7.0 → 7.1 every call exits 127 with
    `libalpm.so.15: cannot open shared object file`. Callers read that as "package not in the
    AUR", and the entire AUR route vanishes behind warnings that blame the packages. `hostenv
    .aur_helper()` runs `--version` before returning a name; bootstrap rebuilds `paru` from
    source when the binary is broken, removing the conflicting `paru-bin` first (makepkg
    `--noconfirm` answers **N** to "Remove paru-bin?" and then fails to install what it built).
*   **A route can succeed and still install nothing.** AUR metadata has no architecture field, so
    `paru -S pandoc-bin` resolves on aarch64 and then dies on the PKGBUILD's x86_64 check.
    `do_install` re-tests `provides` at the end and spends the declared `fallback` on anything
    still missing.
*   Arch Linux ARM has no `pandoc-cli` (Haskell), `plantuml` or `handbrake-cli`. pandoc is
    hard-required, so it falls back to upstream's static release tarball; the other two are
    optional and stay warnings.
*   **`pacman -Syu`, never `pacman -Sy`.** Arch has no stable release to pin against, so a db
    refreshed without the matching upgrade is the documented partial-upgrade state: the new
    index describes packages built against libraries this box has not taken yet.
*   **A long bootstrap outlives its package index.** Arch mirrors keep exactly one version of
    each package and delete the rest when a new one lands, so an hour into the run pacman asks
    for the `qpdf-12.4.1` its snapshot remembers and all thirteen mirrors answer 404 for a
    package that is perfectly available as 12.4.2. It reads like a mirror outage and is not
    one. `install_native` refreshes the db and retries once before believing the failure.
*   **A killed pacman poisons everything after it.** `subprocess.run(timeout=…)` kills the child
    while it holds `/var/lib/pacman/db.lck`, and pacman declines to remove a lock it did not
    make — so every later call in the run dies on the lock file and one slow mirror becomes
    sixty "install failed" lines naming packages that were never the problem. Hence
    `PACMAN_TIMEOUT` of four hours (a headless Arch install is ~330 packages and over a
    gigabyte; Arch Linux ARM's mirrors need over two hours for the download alone) *and*
    `clear_stale_pacman_lock()` before every pacman-backed route. Ctrl-C out of `make install`
    leaves the identical state, so this is not container-only.
*   **On Arch, whoever installs first picks the provider for everyone.** `jack` is virtual with
    two providers, and `--noconfirm` silently takes provider 1 rather than prompting — so
    `gifski` pulling in ffmpeg, four groups before audio is even reached, installs `jack2` and
    the manifest's `pipewire-jack` then fails on the conflict. `early: true` on a manifest entry
    hoists it to the front of the install order (stable sort; everything else keeps manifest
    order). It is for races against a dependency resolver, not against another package.
*   **The cargo and uv routes compile C, so they need `packages.build_env()`.** Arch Linux ARM
    builds its `python` with distcc, so sysconfig hands every setuptools extension
    `CXX=/usr/lib/distcc/bin/g++`, a path that does not exist on the installed machine — manim
    dies on `[Errno 2]`. And GCC 14 made `-Wincompatible-pointer-types` an error, which breaks
    crates vendoring old C (silicon's `onig_sys`). `build_env()` pins real CC/CXX and relaxes
    both warnings, the same way distros do — in `CFLAGS` only, since both name C constructs and
    g++ answers every translation unit with "valid for C/ObjC but not for C++".
*   **Debian splits out every header, and they all belong in `source-build-deps`.**
    `python3-dev` or anything building a C extension from an sdist stops at
    `fatal error: Python.h`; aarch64 has no wheels for two of manim's dependencies, so a
    headless Ubuntu also needs `libx11-dev`, `libgl1-mesa-dev`, `libegl1-mesa-dev` (moderngl's
    `glcontext` compiles an `x11.cpp`) and `libpango1.0-dev`, `libcairo2-dev` (manimpango's
    configure). Each was a separate serial failure — add the set, not the next one in the
    chain. **Do not hang them off the `python` entry:** its `provides` is `python3`, which is
    satisfied on any machine that can run this script, so it resolves to `have` and its package
    list is never installed. An entry whose `provides` is already true cannot carry anything.
*   **Retry a failed `cargo install --locked` without the lock.** A lockfile is a snapshot of
    what compiled on the author's machine, and an abandoned crate's snapshot rots: silicon
    0.5.2 pins a `time` from before the `Box<_>` inference fix and dies in `error[E0282]` in a
    dependency nobody chose. Locked stays the default; the retry is for pins that aged out.
*   Debian's `fonts-roboto` is a 2017 snapshot of the proportional family — it installs cleanly
    and leaves no Roboto Mono, so the package reports success and `fc-list` disagrees. That
    entry has `debian: null` and a `font-release` fallback on `googlefonts/RobotoMono`.
*   A crate is not always on crates.io under the name you want: `cargo install agg` pulls an
    unrelated library and fails with "it has no binaries". `install_cargo` treats a URL-shaped
    name as `--git`.
*   **Line-buffer stdout in anything that shells out.** Python block-buffers to a pipe, so under
    `bootstrap.sh > log` the `$ cargo install …` echo lines flush at exit while the subprocess
    output goes straight through. The log then shows every build detached from the command that
    caused it, and reads exactly like a run where each command printed nothing and failed —
    which is how an hour went into a non-existent "npm is silently failing" bug.
*   **npm 12 does not run install scripts, and does not fail when it skips one.** It prints
    `added 2 packages`, exits 0, and leaves a package that cannot run —
    `@anthropic-ai/claude-code`'s postinstall is what hardlinks the 240MB platform-native binary
    over a stub whose only content is an error message explaining that the postinstall did not
    run. Arch ships npm 12 and Ubuntu 24.04 does not, which is why this looked distro-specific
    and is not. `manifest.npm_allow_scripts` is the allow-list and `install_npm` passes it as
    `--allow-scripts=…` on the command line, never `npm config set` (which would edit the
    user's `~/.npmrc` and keep granting those packages script execution forever). Name
    transitive dependencies too — npm matches the package running the script, not the one you
    asked for. `install_npm` warns when npm blocks something the list does not cover.
*   **Never `npm --silent`.** It sets `loglevel=silent`, so a failed install appears in the log
    as a command that printed nothing and exited non-zero, which is unreadable and unfixable.
    `--no-fund --no-audit --loglevel=error` is as quiet as it gets while still explaining itself.
*   **Catch `OSError`, not `FileNotFoundError`, around anything you exec.** A file can be on
    PATH with the execute bit set and still not run: `@anthropic-ai/claude-code` wraps a 240MB
    platform-native optional dependency, and when that download fails the postinstall leaves a
    shebang-less shell stub at `bin/claude.exe`. `execve` answers with `[Errno 8] Exec format
    error`, which `FileNotFoundError` does not catch — one traceback out of `install_mcp.py`
    ended a bootstrap at stage 5 of 7 over a plugin marketplace nobody asked for. That package
    carries a `check` for the same reason: `which` cannot tell a stub from a CLI.
*   **Stages 4–6 of bootstrap warn, they do not exit.** They are wiring; `doctor.py` runs last
    so that it runs at all, and a machine that is 90% there with a report beats one that
    aborted halfway with none. `validate_skills.py` is the exception — it is also the secret
    scan, and syncing past it would copy whatever it caught into six harness directories.
*   **An installer's exit code is a claim; `provides` is the evidence.** The install summary
    re-checks and lets reality overrule the exit code in both directions — npm reported seven
    failures for tools doctor then found healthy, which teaches people to ignore the summary.
*   `font-release` must read the *release assets*, not `archive/refs/heads/main.zip`.
    `googlefonts/googlesans-code` keeps only `.glyph` sources on `main`, so the branch archive
    downloads fine, yields zero `.ttf`, and used to report ✓ for a font nobody installed.
    Extracting nothing is now an error.
*   Fetch with `packages.fetch()` / curl, not bare `urllib`: a python.org or pyenv interpreter
    has no certifi bundle, so every HTTPS call raises `CERTIFICATE_VERIFY_FAILED` and a caller
    that reads an exception as "unavailable" silently draws the wrong conclusion.
*   `Path("")` is `Path(".")`, which exists. `hostenv.find_app()` returns `None` for an absent
    app — keep the `None`, or every uninstalled GUI app reports as present and then fails its
    smoke test with `Permission denied: ''`.
*   A tool check has to accept alternative spellings (`magick|convert`, `bat|batcat`,
    `grim|maim`), the same way the manifest's `provides` is a list. Demanding one exact name was
    the only hard failure on an otherwise healthy Ubuntu 24.04.

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
