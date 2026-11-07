# Contributing to Creative Stack

Creative Stack is a portable content-production toolchain and eight agent skills targeting **macOS**,
**Arch Linux** (including Omarchy), and **Debian/Ubuntu**, across **Claude Code**, **OpenAI Codex**,
**Gemini CLI**, **Antigravity**, and **Cursor**.

Portability is a core invariant: a change that only works on the OS or agent harness it was
authored on is a bug.

---

## The Three Chokepoints

All platform- and harness-specific logic lives in exactly three files. Skill scripts under
`skills/*/scripts/` must never check `sys.platform` or hardcode harness directories (`~/.claude`,
`~/.gemini`, etc.):

1. **[`packages/manifest.json`](packages/manifest.json)** — What gets installed. Every tool has
   `brew`, `arch`, and `debian` package names, a `provides` list of binaries it must produce, and
   an optional `fallback` (`aur`, `npm`, `uv`, `cargo`, `go`, `source`, `font-release`).
   * **Never hand-edit [`Brewfile`](Brewfile)** — regenerate it with:
     ```bash
     python3 scripts/packages.py --emit brewfile
     ```
2. **[`scripts/hostenv.py`](scripts/hostenv.py)** — How to reach binaries, app bundles, fonts,
   audio/loopback devices, and OS secret stores. Add a helper function here rather than branching
   at the call site.
3. **[`config/harnesses.json`](config/harnesses.json)** — Every agent runtime's skills directories,
   registry files, detection paths, and MCP dialect.

---

## Skill & Script Conventions

* **`SKILL.md` frontmatter**: Exactly three fields (`name`, `description` ending in
  `Trigger via /<name>.`, and `disable-model-invocation: true`).
* **Required sections** (in order):
  1. `## 🎯 Goal`
  2. `## 📋 Step-by-Step Workflow`
  3. `## 💡 Concrete Example`
  4. `## 🚫 Hard Constraints` (each bullet begins with `**NEVER** …` and explains *why*).
* **Standalone CLI scripts**:
  * Every `.py` and `.sh` file starts with the Apache-2.0 license header.
  * Every skill script imports its sibling `*lib` module (`vidlib`, `soundlib`, `rasterlib`,
    `vectorlib`, `blendlib`, `genailib`) **before** `import hostenv`, so running the script
    standalone puts `scripts/` on `sys.path`.
  * Every script must run cleanly with `--help` and write a reviewable intermediate (`.json`,
    `.yaml`, `.srt`, `.md`, `.rpp`) before rendering pixels or audio.

---

## Pre-PR Verification Gate

Run the local verification suite before opening a pull request:

```bash
python3 scripts/validate_skills.py         # lint all SKILL.md files + scan for secrets/paths
python3 -m unittest discover -s tests -v   # run pure-function unit tests (stdlib only)
python3 scripts/packages.py --emit brewfile && git diff --exit-code Brewfile
python3 scripts/packages.py --plan         # verify every required package resolves a route
python3 scripts/doctor.py --quick          # verify local toolchain health
```

For changes touching `packages/manifest.json` or `scripts/bootstrap.sh`, verify against clean Linux
containers when Docker is available:

```bash
make verify
```

---

## License

By contributing to Creative Stack, you agree that your contributions will be licensed under the
[Apache License, Version 2.0](LICENSE).
