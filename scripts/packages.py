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

"""Resolve and install packages/manifest.json for whatever machine this is.

One manifest, three package managers. The resolver is deliberately forgiving:
distro package names move between repos, so a name that does not exist is not a
failure — it falls through repo -> AUR -> language ecosystem, and whatever
happens is reported. A stale name costs you a warning, never the bootstrap.

    python3 scripts/packages.py --plan                 # what would happen
    python3 scripts/packages.py --install               # do it
    python3 scripts/packages.py --install --profile headless --skip-heavy
    python3 scripts/packages.py --check                 # verify `provides`
    python3 scripts/packages.py --emit brewfile         # regenerate Brewfile
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hostenv  # noqa: E402

REPO_ROOT = hostenv.REPO_ROOT
MANIFEST = REPO_ROOT / "packages/manifest.json"
BREWFILE = REPO_ROOT / "Brewfile"

GREEN, YELLOW, RED, DIM, BOLD, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m")


def ok(msg):
    print(f"    {GREEN}✓{RESET} {msg}")


def warn(msg):
    print(f"    {YELLOW}!{RESET} {msg}")


def fail(msg):
    print(f"    {RED}✗{RESET} {msg}")


def info(msg):
    print(f"    {DIM}{msg}{RESET}")


def load():
    with MANIFEST.open() as handle:
        return json.load(handle)


def packages(data, profile="full", skip_heavy=False):
    """Flatten the manifest to the packages that apply to this machine."""
    out = []
    for group in data["groups"]:
        for pkg in group["packages"]:
            if pkg.get("macos_only") and not hostenv.IS_MAC:
                continue
            if pkg.get("linux_only") and hostenv.IS_MAC:
                continue
            if profile == "headless" and pkg.get("gui"):
                continue
            if skip_heavy and pkg.get("heavy"):
                continue
            out.append(dict(pkg, group=group["name"]))
    return out


def native_names(pkg):
    """The package-manager names for this platform, as a list."""
    key = {"macos": "brew", "arch": "arch", "debian": "debian"}.get(hostenv.FAMILY)
    value = pkg.get(key)
    return value.split() if value else []


def satisfied(pkg):
    """Is this package already usable? `provides` is the contract, not the
    package database — a tool installed by any means at all counts."""
    if hostenv.IS_MAC and pkg.get("mac_app"):
        # Cask apps live in /Applications, not on PATH. bootstrap --adopt takes
        # ownership of one installed by hand, so presence is what matters.
        if any((Path(d) / pkg["mac_app"]).exists()
               for d in ("/Applications", Path.home() / "Applications")):
            return True
    if hostenv.find_app(pkg["id"]):
        return True
    if pkg.get("check"):
        # Escape hatch for "installed but not good enough". Presence on PATH is
        # usually the whole question; for toolchains it is not — Ubuntu 24.04's
        # apt cargo is 1.75 and every cargo fallback here needs 1.82+.
        return subprocess.run(["sh", "-c", pkg["check"]],
                              capture_output=True).returncode == 0
    provides = pkg.get("provides") or []
    if provides:
        return any(hostenv.which(binary) for binary in provides)
    if pkg.get("font"):
        return font_installed(pkg["font"])
    # Nothing observable (a library like portaudio): let the pm decide.
    return None


def font_installed(name):
    if not hostenv.which("fc-list"):
        return None
    out = subprocess.run(["fc-list", "-q", f"{name}"], capture_output=True)
    return out.returncode == 0


# ------------------------------------------------------- repo availability ---

def repo_has(name):
    """Does the native package manager know this name? Guessed names are the
    norm across distros, so we ask before we install rather than after."""
    try:
        if hostenv.FAMILY == "arch":
            return subprocess.run(["pacman", "-Si", name], capture_output=True,
                                  timeout=20).returncode == 0
        if hostenv.FAMILY == "debian":
            out = subprocess.run(["apt-cache", "show", name], capture_output=True,
                                 timeout=20)
            return out.returncode == 0 and bool(out.stdout.strip())
        if hostenv.FAMILY == "macos":
            # `brew info` costs ~1s per name; across the manifest that is a
            # minute of nothing. Homebrew names are stable and were verified,
            # so let `brew install` be the test instead.
            return True
    except (OSError, subprocess.SubprocessError):
        return True       # cannot tell: let the install attempt be the test
    return True


def aur_has(name):
    helper = hostenv.aur_helper()
    if not helper:
        return False
    try:
        return subprocess.run([helper, "-Si", name], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------- planning ---

def route(pkg):
    """Decide how a single package gets installed on this machine.

    Returns (method, payload, reason). method is one of:
      native | cask | aur | npm | uv | cargo | go | source | font-release |
      manual | skip
    """
    names = native_names(pkg)
    if not names and not pkg.get("aur") and not pkg.get("fallback"):
        # The manifest offers no route here and never claimed one (xvfb on
        # macOS, ffmpeg-full on Linux). Not a gap — just not applicable.
        return "n/a", [], f"not applicable on {hostenv.FAMILY}"
    if names and all(repo_has(n) for n in names):
        return ("cask" if pkg.get("cask") and hostenv.IS_MAC else "native",
                names, f"{hostenv.PACKAGE_MANAGER}: {' '.join(names)}")

    if names:
        missing = [n for n in names if not repo_has(n)]
        if len(names) > 1 and pkg.get("best_effort"):
            # A bag of -dev headers is not all-or-nothing: distros rename them
            # (libfreetype6-dev -> libfreetype-dev) and a stale name should not
            # cost us the other nine. Install what exists, say what was dropped.
            present = [n for n in names if n not in missing]
            if present:
                return "native", present, (
                    f"{hostenv.PACKAGE_MANAGER}: {len(present)}/{len(names)}"
                    f" (skipped {', '.join(missing)})")
        reason = f"not in repos: {', '.join(missing)}"
    else:
        reason = f"no {hostenv.PACKAGE_MANAGER} package"

    if hostenv.FAMILY == "arch" and pkg.get("aur"):
        if aur_has(pkg["aur"]):
            return "aur", [pkg["aur"]], f"AUR: {pkg['aur']}"
        if not hostenv.aur_helper():
            reason = "AUR package but no paru/yay installed"

    fallback = pkg.get("fallback")
    if fallback:
        return fallback["kind"], [fallback["name"]], f"{reason} -> {fallback['kind']}"

    return "skip", [], reason


def plan(pkgs):
    rows = []
    for pkg in pkgs:
        state = satisfied(pkg)
        if state is True:
            rows.append((pkg, "have", [], "already installed"))
            continue
        method, payload, reason = route(pkg)
        rows.append((pkg, method, payload, reason))
    return rows


# -------------------------------------------------------------- installers ---

def run(cmd, timeout=3600):
    print(f"    {DIM}$ {' '.join(cmd)}{RESET}")
    try:
        return subprocess.run(cmd, timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"{cmd[0]}: {exc}")
        return False


def install_native(names):
    if not names:
        return True
    if hostenv.FAMILY == "arch":
        return run(hostenv.sudo_prefix() + ["pacman", "-S", "--needed",
                                            "--noconfirm", *names])
    if hostenv.FAMILY == "debian":
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        cmd = hostenv.sudo_prefix() + ["apt-get", "install", "-y",
                                       "--no-install-recommends", *names]
        print(f"    {DIM}$ {' '.join(cmd)}{RESET}")
        return subprocess.run(cmd, env=env, timeout=3600).returncode == 0
    if hostenv.FAMILY == "macos":
        return run(["brew", "install", *names])
    return False


def install_cask(names):
    # --adopt takes ownership of an app installed outside brew instead of
    # erroring or clobbering it. Most of these were already on the machine.
    return run(["brew", "install", "--cask", "--adopt", *names])


def install_aur(names):
    helper = hostenv.aur_helper()
    if not helper:
        return False
    return run([helper, "-S", "--needed", "--noconfirm", *names])


def install_npm(names):
    return run(["npm", "install", "--global", "--silent", *names])


def install_uv(names):
    if hostenv.which("uv"):
        return all(run(["uv", "tool", "install", "--quiet", n]) for n in names)
    if hostenv.which("pipx"):
        return all(run(["pipx", "install", n]) for n in names)
    return False


def install_cargo(names):
    if not hostenv.which("cargo"):
        warn("cargo not found — install rustup to get these")
        return False
    return all(run(["cargo", "install", "--locked", n]) for n in names)


def install_go(names):
    if not hostenv.which("go"):
        warn("go not found — install go to get these")
        return False
    env = dict(os.environ, GOBIN=str(hostenv.bin_dir()))
    hostenv.bin_dir().mkdir(parents=True, exist_ok=True)
    good = True
    for name in names:
        cmd = ["go", "install", name]
        print(f"    {DIM}$ GOBIN={env['GOBIN']} {' '.join(cmd)}{RESET}")
        good &= subprocess.run(cmd, env=env, timeout=1800).returncode == 0
    return good


def install_source(names):
    """Source builds live in scripts/install/<name>.sh so they stay reviewable."""
    good = True
    for name in names:
        script = REPO_ROOT / f"scripts/install/{name}.sh"
        if not script.exists():
            warn(f"no source installer at {script}")
            good = False
            continue
        good &= run(["bash", str(script)])
    return good


def install_font_release(names):
    """Fetch a font family straight from its GitHub release into the user font
    dir. Used for families no distro packages (Google Sans Code)."""
    import urllib.request
    import zipfile
    import io

    good = True
    for repo in names:
        url = f"https://github.com/{repo}/archive/refs/heads/main.zip"
        try:
            info(f"downloading {url}")
            with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
                blob = response.read()
            target = hostenv.user_font_dir()
            target.mkdir(parents=True, exist_ok=True)
            count = 0
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                for entry in archive.namelist():
                    if entry.lower().endswith((".ttf", ".otf")):
                        (target / Path(entry).name).write_bytes(archive.read(entry))
                        count += 1
            ok(f"{repo}: {count} font files -> {target}")
        except Exception as exc:  # noqa: BLE001 - fonts are best-effort
            warn(f"{repo}: {exc}")
            good = False
    if hostenv.which("fc-cache"):
        subprocess.run(["fc-cache", "-f"], capture_output=True, timeout=300)
    return good


INSTALLERS = {
    "native": install_native, "cask": install_cask, "aur": install_aur,
    "npm": install_npm, "uv": install_uv, "cargo": install_cargo,
    "go": install_go, "source": install_source,
    "font-release": install_font_release,
}


def refresh_indexes():
    """One index refresh up front beats sixty stale-cache failures."""
    if hostenv.FAMILY == "debian":
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        subprocess.run(hostenv.sudo_prefix() + ["apt-get", "update", "-qq"],
                       env=env, timeout=600)
    elif hostenv.FAMILY == "arch":
        subprocess.run(hostenv.sudo_prefix() + ["pacman", "-Sy", "--noconfirm"],
                       timeout=600)
    elif hostenv.FAMILY == "macos":
        subprocess.run(["brew", "update"], timeout=1800)


def do_install(rows):
    """Install in method batches — one pacman/apt call beats sixty."""
    batches = {}
    manual, skipped = [], []
    for pkg, method, payload, reason in rows:
        if method == "have":
            continue
        if method == "n/a":
            continue
        if method == "manual":
            manual.append((pkg, payload, reason))
        elif method == "skip":
            skipped.append((pkg, reason))
        else:
            batches.setdefault(method, []).append((pkg, payload))

    results = {}
    # Order matters: `native` brings the toolchains the others are installed with.
    for method in ("native", "cask", "aur", "npm", "uv", "cargo", "go",
                   "source", "font-release"):
        items = batches.get(method)
        if not items:
            continue
        names = [n for _, payload in items for n in payload]
        print(f"\n  {BOLD}{method}{RESET} ({len(items)}): {', '.join(names[:8])}"
              + (" …" if len(names) > 8 else ""))

        if method in ("native", "cask", "aur", "npm"):
            # Batched. If the batch fails, retry one at a time so a single bad
            # name cannot take the other fifty-nine down with it.
            if INSTALLERS[method](names):
                for pkg, _ in items:
                    results[pkg["id"]] = True
            else:
                warn("batch failed — retrying individually")
                for pkg, payload in items:
                    results[pkg["id"]] = INSTALLERS[method](payload)
        else:
            for pkg, payload in items:
                results[pkg["id"]] = INSTALLERS[method](payload)

    return results, manual, skipped


# ------------------------------------------------------------------- check ---

def do_check(pkgs):
    missing, degraded = [], []
    for pkg in pkgs:
        state = satisfied(pkg)
        if state is True or state is None:
            continue
        (degraded if pkg.get("optional") else missing).append(pkg)
    return missing, degraded


# --------------------------------------------------------- brewfile output ---

BREW_HEADER = """# Copyright 2026 Creative Stack Contributors
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

# ==============================================================================
# Creative Stack — Homebrew manifest
#
#   GENERATED from packages/manifest.json. Do not edit.
#   Regenerate:  python3 scripts/packages.py --emit brewfile
#
# This file is the macOS *projection* of the cross-platform manifest, kept in
# the repo so `brew bundle` works on its own. Arch and Debian are resolved from
# the same manifest at install time by scripts/packages.py.
# ==============================================================================

tap "charmbracelet/tap"
"""


def emit_brewfile(data):
    lines = [BREW_HEADER]
    for group in data["groups"]:
        formulae, casks = [], []
        for pkg in group["packages"]:
            name = pkg.get("brew")
            if not name:
                continue
            note = pkg.get("note", "")
            entry = (pkg, name, note)
            (casks if pkg.get("cask") else formulae).append(entry)
        if not formulae and not casks:
            continue

        title = f" {group['name']} "
        lines.append(f"\n# {title:-^76}")
        if group.get("note"):
            for chunk in _wrap(group["note"], 74):
                lines.append(f"# {chunk}")
        for pkg, name, note in formulae + casks:
            if note:
                for chunk in _wrap(note, 74):
                    lines.append(f"# {chunk}")
            keyword = "cask" if pkg.get("cask") else "brew"
            lines.append(f'{keyword} "{name}"')
    return "\n".join(lines) + "\n"


def _wrap(text, width):
    import textwrap
    return textwrap.wrap(text, width) or [""]


# -------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="show the resolution table")
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--check", action="store_true", help="verify provides")
    mode.add_argument("--emit", choices=["brewfile"])
    ap.add_argument("--profile", choices=["full", "headless"], default="full",
                    help="headless drops desktop apps")
    ap.add_argument("--skip-heavy", action="store_true",
                    help="skip multi-GB packages (LaTeX, Blender, Resolve)")
    ap.add_argument("--group", action="append", help="limit to a group (repeatable)")
    ap.add_argument("--no-refresh", action="store_true",
                    help="do not refresh the package index first")
    args = ap.parse_args()

    data = load()

    if args.emit == "brewfile":
        BREWFILE.write_text(emit_brewfile(data))
        print(f"✓ {BREWFILE}")
        return 0

    if hostenv.FAMILY == "unknown":
        fail("Unsupported platform: no brew, pacman or apt found.")
        return 1

    pkgs = packages(data, args.profile, args.skip_heavy)
    if args.group:
        pkgs = [p for p in pkgs if p["group"] in args.group]

    print(f"{BOLD}{hostenv.pretty_name()}{RESET}  ·  {hostenv.PACKAGE_MANAGER}"
          + (f" + {hostenv.aur_helper()}" if hostenv.aur_helper() else "")
          + f"  ·  profile={args.profile}  ·  {len(pkgs)} packages")

    if args.check:
        missing, degraded = do_check(pkgs)
        for pkg in degraded:
            warn(f"{pkg['id']}: not installed (optional) — "
                 f"{pkg.get('note', 'feature unavailable')}")
        for pkg in missing:
            fail(f"{pkg['id']}: missing — expected one of "
                 f"{', '.join(pkg['provides'])}")
        if not missing:
            ok(f"all required packages present "
               f"({len(degraded)} optional missing)")
        return 1 if missing else 0

    rows = plan(pkgs)

    if args.plan:
        width = max(len(p["id"]) for p, *_ in rows) + 2
        colour = {"have": GREEN, "skip": RED, "n/a": DIM, "manual": YELLOW}
        for pkg, method, _payload, reason in rows:
            tint = colour.get(method, YELLOW)
            if method == "skip" and pkg.get("optional"):
                tint = DIM      # an optional gap is not a red flag

            print(f"  {pkg['id']:<{width}} {tint}{method:<13}{RESET} {DIM}{reason}{RESET}")
        return 0

    if not args.no_refresh:
        print(f"\n{BOLD}Refreshing package index{RESET}")
        refresh_indexes()

    results, manual, skipped = do_install(rows)

    print(f"\n{BOLD}Summary{RESET}")
    good = sum(1 for v in results.values() if v)
    bad = [k for k, v in results.items() if not v]
    have = sum(1 for _, m, *_ in rows if m == "have")
    ok(f"{have} already present, {good} installed")
    for pkg_id in bad:
        fail(f"{pkg_id}: install failed")
    for pkg, payload, reason in manual:
        warn(f"{pkg['id']}: manual install — {payload[0]}")
    for pkg, reason in skipped:
        (warn if pkg.get("optional") else fail)(f"{pkg['id']}: no install route ({reason})")

    hard = [k for k in bad
            if not next(p for p in pkgs if p["id"] == k).get("optional")]
    hard += [p["id"] for p, _ in skipped if not p.get("optional")]
    if hard:
        print(f"\n{RED}Required packages unresolved: {', '.join(hard)}{RESET}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
