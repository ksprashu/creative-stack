# Security Policy

Creative Stack runs local media toolchains, headless desktop app bridges, and AI generation APIs.
Security boundaries are enforced by design and verified in CI on every change.

---

## Security Invariants

1. **Zero Secrets in the Repository**
   * API keys (`GOOGLE_API_KEY`, `GEMINI_API_KEY`, `OBS_WEBSOCKET_PASSWORD`) are stored exclusively
     in the OS secret store (**macOS Keychain**, **Linux `libsecret` / `secret-tool`**, or a `0600`
     file at `~/.config/creative-stack/secrets.env` on headless servers).
   * `creative-stack.env` contains resolved local paths only and is gitignored.
   * [`scripts/validate_skills.py`](scripts/validate_skills.py) runs as the first step of CI and
     blocks commits containing `AIza…`, `sk-…`, or `ghp_…` tokens or hardcoded user home paths.

2. **Pinned, Opt-In MCP Servers**
   * Third-party MCP servers (`blender`, `gimp`, `resolve`, `anot8`) execute with local filesystem
     access. All git-sourced MCP servers in [`scripts/install_mcp.py`](scripts/install_mcp.py) are
     pinned to reviewed commit SHAs (never floating branches or tags) and registered **disabled by
     default**.
   * [`scripts/install_mcp.py`](scripts/install_mcp.py) never writes user-scope MCP configuration
     files (`~/.codex/config.toml` or `~/.gemini/config/mcp_config.json`), preventing background
     servers from starting in unrelated repositories.

3. **Dependency Security Floors**
   * [`requirements-ml.txt`](requirements-ml.txt) enforces `torch>=2.6` (remediating
     `CVE-2025-32434` in `torch.load`) and `transformers>=5.0` (remediating
     `GHSA-59p9-h35m-wg4g` / `CVE-2026-29784`).
   * [`packages/manifest.json`](packages/manifest.json) scopes npm install script execution via an
     explicit `npm_allow_scripts` allowlist passed on the command line rather than mutating
     `~/.npmrc`.

4. **Automatic Screenshot Redaction**
   * `/shot` (`skills/shot/scripts/annotate.py` and `anot8`) supports automatic OCR/regex token
     redaction and irreversible pixelation/blurring before screenshots are framed or published.

---

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report vulnerabilities privately via
**[GitHub Security Advisories](https://github.com/ksprashu/creative-stack/security/advisories/new)**
on this repository. Include:
* The affected script, skill, or manifest entry
* Reproduction steps or proof-of-concept
* Platform and harness details (`python3 scripts/hostenv.py`)
