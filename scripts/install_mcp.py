#!/usr/bin/env python3
# Copyright 2026 Creative Stack Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Register GUI-app MCP servers and official skill marketplaces.

Every installed harness gets the same servers in whatever dialect it speaks.
Which harnesses those are, and which config file each reads, is data —
config/harnesses.json, the same table scripts/sync_skills.py works from.

Design decision — these servers are *opt-in*:

  Claude Code has no `mcp disable` subcommand, so opt-in is achieved with
  project scope. Servers declared in this repo's .mcp.json show as
  "⏸ Pending approval" and are never connected to until you approve them in a
  session. They also stay out of every unrelated project's tool context.

  The same reasoning generalises: a config marked `scope: user` in the harness
  table is *never written*. Codex and the Antigravity IDE have no project-scope
  MCP config, so wiring them up automatically would silently launch Blender and
  GIMP servers in every unrelated repo you open. Those two get a printed
  command instead, which is a decision you make rather than one made for you.

  To use one everywhere, promote it to user scope — the exact command is
  printed at the end of this script.

Security: every third-party server here runs arbitrary code with your
filesystem access. Sources are pinned to a reviewed commit, never `main`.
A real advisory exists in this ecosystem (command injection in a popular
ImageMagick MCP server, s1ko/mcp-imagemagick-rce), so pinning is not paranoia.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hostenv  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR = Path.home() / ".local/share/creative-stack/mcp"
HARNESSES = REPO_ROOT / "config/harnesses.json"

GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"


def ok(m):
    print(f"    {GREEN}✓{RESET} {m}")


def warn(m):
    print(f"    {YELLOW}!{RESET} {m}")


def info(m):
    print(f"    {DIM}{m}{RESET}")


# Pinned 2026-09-18. Re-pin deliberately after reviewing the diff.
SERVERS = {
    "blender": {
        "kind": "uvx",
        "package": "mcp-for-blender==2.0.0",
        "repo": "ahujasid/mcp-for-blender",
        "stars": "28.9k",
        "needs": "Blender open with the 'MCP for Blender' addon enabled",
    },
    "gimp": {
        "kind": "git",
        "repo": "maorcc/gimp-mcp",
        "sha": "09bfb2d3e5ca",
        "entry": "gimp_mcp_server.py",
        "stars": "219",
        "needs": "GIMP open with the gimp-mcp plug-in installed",
    },
    "resolve": {
        "kind": "git",
        "repo": "samuelgursky/davinci-resolve-mcp",
        "sha": "16e4a39f8754",
        "entry": "src/server.py",
        "stars": "2.9k",
        "needs": "DaVinci Resolve STUDIO running (free edition cannot be scripted)",
        "studio_only": True,
    },
    # First-party (same author as this stack). An exact version, never
    # @latest: npx would otherwise fetch whatever was published last on every
    # cold start. Nothing to open first, so it is useful in any repo — see the
    # promote command this script prints.
    "anot8": {
        "kind": "npx",
        "package": "@ksprashu/anot8@1.1.0",
        "repo": "ksprashu/anot8",
        "stars": "own",
        "needs": "Node 20+ (headless; no app to open)",
    },
}

# Official, first-party skills that already cover a large slice of this stack.
# Building our own versions of these would be redundant.
#
# The key is the *registered* marketplace name, which is whatever the upstream
# repo's marketplace.json declares — not the repo path and not an alias we
# choose. `anthropics/skills` registers itself as `anthropic-agent-skills`, and
# installing against any other name fails with "not found in marketplace".
MARKETPLACES = {"anthropic-agent-skills": "anthropics/skills"}

# These are plugin *bundles*, not individual skills. `pptx` is not installable;
# it ships inside `document-skills` along with docx, pdf and xlsx. Likewise
# algorithmic-art, canvas-design, brand-guidelines, theme-factory,
# web-artifacts-builder and slack-gif-creator all arrive with `example-skills`.
PLUGINS = [
    # pptx, docx, pdf, xlsx
    ("document-skills", "anthropic-agent-skills"),
    # algorithmic-art, brand-guidelines, canvas-design, doc-coauthoring,
    # frontend-design, internal-comms, mcp-builder, skill-creator,
    # slack-gif-creator, theme-factory, web-artifacts-builder, webapp-testing
    ("example-skills", "anthropic-agent-skills"),
]


def run(cmd, **kw):
    """Never raise. Every caller here is optional work on somebody else's CLI.

    OSError, not FileNotFoundError: a file can be on PATH with the execute bit
    set and still not be executable. `@anthropic-ai/claude-code` leaves a
    shebang-less shell stub at `bin/claude.exe` when its native binary did not
    download, and `execve` answers that with `[Errno 8] Exec format error`,
    which FileNotFoundError does not catch. That traceback took the entire
    bootstrap down at stage 5 of 7 over a plugin marketplace nobody asked for.
    """
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300, **kw)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        return 126, f"cannot run {cmd[0]}: {exc.strerror or exc}"


def resolve_is_studio():
    """Only Studio exposes the external scripting API the Resolve MCP needs."""
    return hostenv.resolve_paths()[2]


def portable(arg):
    """An argument with $HOME factored out, for a config that gets committed.

    .mcp.json is checked in so the same two servers are declared on every
    machine, and Claude Code expands ${HOME} when it reads it. Writing
    /Users/<name>/... or /home/<name>/... instead would make the repo work on
    exactly one box, which defeats the point of having it in git.

    Only harnesses whose table entry sets `portable: true` get this. The others
    receive a real absolute path and their config is gitignored, because a
    harness that does not expand ${HOME} would take the literal string as a
    directory name and fail with a path nobody can find on disk.
    """
    try:
        return "${HOME}/" + str(Path(arg).resolve().relative_to(Path.home()))
    except (ValueError, OSError):
        return str(arg)


def portablize(servers):
    home = str(Path.home())
    return {name: {**spec,
                   "args": [portable(a) if str(a).startswith(home) else a
                            for a in spec["args"]]}
            for name, spec in servers.items()}


def vendor_repo(name, spec):
    """Clone at a pinned SHA. Idempotent: re-checks out the pin if it drifted."""
    dest = VENDOR / name
    url = f"https://github.com/{spec['repo']}.git"
    sha = spec["sha"]

    if not dest.exists():
        VENDOR.mkdir(parents=True, exist_ok=True)
        rc, out = run(["git", "clone", "--quiet", url, str(dest)])
        if rc != 0:
            warn(f"{name}: clone failed — {out[:70]}")
            return None

    rc, out = run(["git", "-C", str(dest), "checkout", "--quiet", sha])
    if rc != 0:
        # Shallow or stale clone: fetch the specific object then retry.
        run(["git", "-C", str(dest), "fetch", "--quiet", "origin", sha])
        rc, out = run(["git", "-C", str(dest), "checkout", "--quiet", sha])
    if rc != 0:
        warn(f"{name}: could not pin to {sha} — {out[:70]}")
        return None

    entry = dest / spec["entry"]
    if not entry.exists():
        warn(f"{name}: entry point missing at {entry}")
        return None

    ok(f"{name} pinned at {sha} ({spec['repo']}, ⭐{spec['stars']})")
    return dest


def build_config():
    print("\n▸ Vendoring MCP servers (pinned commits)")
    servers = {}

    for name, spec in SERVERS.items():
        if spec.get("studio_only") and not resolve_is_studio():
            warn(f"{name}: skipped — Resolve Studio not detected")
            info("Free edition cannot be scripted externally. /vidkit emits FCPXML instead.")
            continue

        if spec["kind"] == "uvx":
            if not shutil.which("uvx"):
                warn(f"{name}: uvx not found — install uv first")
                continue
            servers[name] = {"command": "uvx", "args": [spec["package"]]}
            ok(f"{name} -> uvx {spec['package']} ({spec['repo']}, ⭐{spec['stars']})")

        elif spec["kind"] == "npx":
            if not shutil.which("npx"):
                warn(f"{name}: npx not found — install Node first")
                continue
            servers[name] = {"command": "npx", "args": ["-y", spec["package"], "mcp"]}
            ok(f"{name} -> npx {spec['package']} ({spec['repo']})")

        elif spec["kind"] == "git":
            dest = vendor_repo(name, spec)
            if dest is None:
                continue
            servers[name] = {
                "command": "uv",
                "args": ["run", "--directory", str(dest), spec["entry"]],
            }

    return servers


def load_harnesses():
    try:
        return json.loads(HARNESSES.read_text()).get("harnesses", {})
    except FileNotFoundError:
        sys.exit(f"missing {HARNESSES} — the harness table is required")
    except json.JSONDecodeError as exc:
        sys.exit(f"{HARNESSES} is not valid JSON: {exc}")


def installed(cfg):
    return any(Path(p).expanduser().exists() for p in cfg.get("detect", []))


def read_json(path):
    """Existing config, or {} — a malformed file is reported, not clobbered."""
    if not path.exists():
        return {}, True
    try:
        return json.loads(path.read_text()), True
    except json.JSONDecodeError:
        return {}, False


def render(fmt, servers):
    """One server map in one harness's dialect.

    The shapes are not interchangeable: `mcpServers` splits the command from
    its arguments, Codex's TOML is printed rather than written.
    """
    if fmt == "mcpServers":
        return {"mcpServers": {n: dict(s) for n, s in servers.items()}}
    raise ValueError(f"no writer for format {fmt!r}")


def merge(fmt, existing, servers):
    """Add our servers to whatever is already declared, keeping the rest."""
    block = render(fmt, servers)
    key = next(iter(block))
    merged = dict(existing)
    merged.setdefault(key, {}).update(block[key])
    return merged


def write_harness_configs(servers, check_only):
    print("\n▸ MCP config per harness")
    if not servers:
        return warn("no servers to declare")

    for key, cfg in load_harnesses().items():
        mcp = cfg.get("mcp")
        label = cfg.get("label", key)
        if not mcp:
            continue
        if not installed(cfg):
            info(f"{label}: not installed — skipped")
            continue

        if mcp["scope"] == "user":
            # Deliberately not written. See the module docstring.
            info(f"{label}: user-scope only ({mcp['path']}) — command printed below")
            continue

        path = REPO_ROOT / mcp["path"]
        if check_only:
            warn(f"would write {path} ({mcp['format']})")
            continue

        existing, clean = read_json(path)
        if not clean:
            warn(f"{path} was malformed — rewriting")
        payload = portablize(servers) if mcp.get("portable") else servers
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merge(mcp["format"], existing, payload),
                                   indent=2) + "\n")
        note = "committed" if mcp.get("portable") else "local, gitignored"
        ok(f"{label}: {path.relative_to(REPO_ROOT)} "
           f"({len(servers)} server(s), {note})")

    info("Project scope keeps these out of unrelated repos; Claude Code shows "
         "them as '⏸ Pending approval' until you approve them in a session.")


def install_marketplaces(check_only):
    print("\n▸ Plugin marketplaces")
    # Probed, not merely located. npm can leave a `claude` on PATH that is a
    # stub explaining the native binary is missing, and marketplaces are a
    # Claude-Code-only feature — there is nothing to fall back to, so the
    # honest move is to say why and move on.
    if not shutil.which("claude"):
        return warn("claude CLI not on PATH — skipping")
    rc, out = run(["claude", "--version"])
    if rc != 0:
        return warn(f"claude CLI will not run — skipping ({out[:70]})")

    for name, source in MARKETPLACES.items():
        if check_only:
            warn(f"would add marketplace {source}")
            continue
        rc, out = run(["claude", "plugin", "marketplace", "add", source])
        if rc == 0 or "already" in out.lower():
            ok(f"marketplace {source}")
        else:
            warn(f"marketplace {source}: {out[:70]}")

    for plugin, marketplace in PLUGINS:
        if check_only:
            warn(f"would install plugin {plugin}")
            continue
        rc, out = run(["claude", "plugin", "install", f"{plugin}@{marketplace}"])
        if rc == 0 or "already" in out.lower():
            ok(f"plugin {plugin}")
        else:
            warn(f"plugin {plugin}: {out[:70]}")


# How each harness takes a user-scope server on the command line. A harness
# absent from this map has no CLI for it and must be edited by hand.
PROMOTE = {
    "claude-code": "claude mcp add --scope user {name} -- {command} {argv}",
    "codex": "codex mcp add {name} -- {command} {argv}",
    "gemini-cli": "gemini mcp add {name} {command} {argv}",
}


def print_next_steps(servers):
    print("\n" + "=" * 66)
    print(" Opt-in servers registered at PROJECT scope")
    print("=" * 66)
    for name in servers:
        print(f"\n  {name}  — requires: {SERVERS[name]['needs']}")

    if servers:
        print("\n  To use one from any directory, promote it to user scope:")
        for key, cfg in load_harnesses().items():
            if key not in PROMOTE or not installed(cfg):
                continue
            print(f"\n    # {cfg.get('label', key)}")
            for name, spec in servers.items():
                print("    " + PROMOTE[key].format(
                    name=name, command=spec["command"],
                    argv=" ".join(spec["args"])))
        manual = [c for k, c in load_harnesses().items()
                  if k not in PROMOTE and installed(c)
                  and (c.get("mcp") or {}).get("scope") == "user"]
        for cfg in manual:
            print(f"\n    # {cfg['label']}: no CLI — hand-edit "
                  f"{cfg['mcp']['path']}")

    if "blender" in servers:
        print("\n  Blender also needs its addon installed once:")
        print("    uvx mcp-for-blender install-addon")
        print("    then Blender: Edit → Preferences → Add-ons → enable 'MCP for Blender'")
    print("\n" + "=" * 66)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    ap.add_argument("--skip-plugins", action="store_true",
                    help="only wire MCP servers, skip marketplaces")
    args = ap.parse_args()

    print("=" * 66)
    print(" 🔗 CREATIVE STACK — MCP servers & plugin marketplaces")
    print("=" * 66)

    servers = build_config()
    write_harness_configs(servers, args.check)
    if not args.skip_plugins:
        install_marketplaces(args.check)
    print_next_steps(servers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
