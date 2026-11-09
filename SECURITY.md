# Security policy

Creative Stack is a personal project, maintained on a best-effort basis. It is not an official
Google product and Google doesn't support it (see the README's [Disclaimer](README.md#disclaimer)).

## Reporting a vulnerability

Please report it privately through GitHub:
**[Report a vulnerability](https://github.com/ksprashu/creative-stack/security/advisories/new)**.
Don't open a public issue. I'll aim to reply within a week. There is no bug bounty.

Only the latest `main` gets fixes.

## What is in scope

- Scripts in `scripts/` and `skills/*/scripts/` doing something the docs don't say. Examples:
  command or filter-graph injection, writing outside the output path, uploading data a command
  doesn't disclose, or leaking a credential.
- Secret redaction (`/shot scan.py`) reporting an image as clean when a secret it is documented
  to catch is still readable.
- The CI workflows in `.github/workflows/`.

Third-party tools the bootstrap installs (ffmpeg, Blender, npm and PyPI packages and so on) are
out of scope. Report those upstream.

## Known trade-offs

- **Bootstrap uses vendor installers.** `scripts/bootstrap.sh` follows the official install
  instructions for Homebrew, NodeSource, uv and rustup, which pipe an HTTPS download into a shell.
  Read `scripts/bootstrap.sh` before running it on a machine you care about.
- **Version floors, not lockfiles.** Python dependencies are `>=` floors, and the floors are
  raised past known advisories. Run `pip-audit` or `osv-scanner` against your own environment.
- **Model downloads.** whisper.cpp and SAM 2 weights come from Hugging Face over HTTPS.
- **Gemini calls upload data.** The README's [Security](README.md#security) section lists exactly
  which commands send what.
