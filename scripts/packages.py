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

# Every install here is a subprocess writing to the same stdout we do. Python
# block-buffers when that stdout is a pipe — which it is under `bootstrap.sh >
# log` — so our "$ cargo install silicon" lines pile up in an 8KB buffer and
# flush at exit, while cargo's own output goes straight through. The log ends
# up with the whole build in one place and the commands that caused it in
# another, which reads as if every command ran silently and failed.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


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
    # Manifest order, so `--plan` reads as the grouped table it is written as.
    # `early` is not applied here: it is an install-ordering concern and only
    # bites on one code path, so do_install applies it where it matters rather
    # than reshuffling the list every other caller shares.
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

def run(cmd, timeout=3600, env=None):
    print(f"    {DIM}$ {' '.join(cmd)}{RESET}")
    try:
        return subprocess.run(cmd, timeout=timeout, env=env).returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"{cmd[0]}: {exc}")
        return False


def build_env():
    """Environment for the routes that compile C on the way in (cargo, uv).

    Two things go wrong on a real Linux box and neither is the package's fault:

    Arch Linux ARM builds its `python` with distcc, so sysconfig hands every
    setuptools C-extension build `CXX=/usr/lib/distcc/bin/g++` — a path that
    does not exist on the installed machine. manim's moderngl dependency dies
    with `[Errno 2] No such file or directory`. Pinning CC/CXX to the real
    compilers overrides the recorded value.

    GCC 14 promoted `-Wincompatible-pointer-types` and friends from warning to
    error, and crates that vendor decade-old C no longer build — silicon pulls
    in an onig_sys that fails on exactly this. Distros carry the same relaxation.
    """
    env = dict(os.environ)
    if hostenv.IS_MAC:
        return env
    for var, candidates in (("CC", ("gcc", "cc", "clang")),
                            ("CXX", ("g++", "c++", "clang++"))):
        current = env.get(var)
        if current and Path(current.split()[0]).exists():
            continue
        found = next((hostenv.which(c) for c in candidates if hostenv.which(c)), None)
        if found:
            env[var] = found
    # CFLAGS only. Both of these describe C constructs, so g++ answers every
    # single translation unit with "command-line option is valid for C/ObjC but
    # not for C++" — thousands of lines of noise around the one real error.
    relax = "-Wno-incompatible-pointer-types -Wno-implicit-function-declaration"
    env["CFLAGS"] = f"{env.get('CFLAGS', '')} {relax}".strip()
    return env


PACMAN_LOCK = Path("/var/lib/pacman/db.lck")

# An hour is not enough for the one long transaction. A headless Arch install is
# ~330 packages and over a gigabyte, and on Arch Linux ARM — whose mirrors run at
# a fraction of the speed of the x86 ones — that download alone takes over two
# hours. Timing out mid-download does not just lose the transaction: pacman is
# killed holding db.lck, and every later call in the run dies instantly on the
# lock, so one slow mirror turns into sixty "install failed" lines that have
# nothing to do with the packages named in them. The timeout is a backstop
# against a genuinely wedged process, not a schedule.
PACMAN_TIMEOUT = 4 * 3600

# Set by install_native the first time it answers a failure with a full -Syu.
# See the comment at that call for why the refresh is worth doing once and not
# worth doing twice.
_db_refreshed_after_failure = False


# Everything that takes db.lck. pacman itself is the common case here, but on a
# desktop — which Omarchy is — the lock is just as likely to belong to pamac's
# background update check or to a GUI front-end the user left open. Removing a
# live lock is not a failed install, it is two writers in one package database,
# so the list errs long: a name on it that is not running costs nothing.
PACMAN_LOCK_HOLDERS = ("pacman", "pacman-static", "pamac", "pamac-daemon",
                       "pamac-manager", "paru", "yay", "pikaur", "trizen",
                       "aura", "octopi", "checkupdates", "pacmatic")


def clear_stale_pacman_lock():
    """Remove a db.lck left behind by a package manager that was killed.

    pacman prints the advice and declines to act on it, which is right for an
    interactive user and wrong here: the rest of the bootstrap would fail on a
    lock file instead of on anything real. Interrupting `make install` with
    Ctrl-C produces exactly the same state.

    The lock is only removed once nothing is holding it. `fuser` answers that
    directly — pacman keeps the descriptor open for the life of the transaction
    — and is the authority when present; the process-name sweep is the fallback
    for a box without psmisc.
    """
    if hostenv.FAMILY != "arch" or not PACMAN_LOCK.exists():
        return
    try:
        if hostenv.which("fuser"):
            # 0 = someone has it open. Any other code (1 = nobody) falls through
            # to the name sweep rather than being trusted on its own, because
            # fuser cannot see into a PID namespace it is not in.
            if subprocess.run(["fuser", str(PACMAN_LOCK)],
                              capture_output=True, timeout=30).returncode == 0:
                return
        # One name per call rather than one alternation. `pgrep -x` anchors a
        # regex by comparing match extents, which does the right thing with
        # `(a|b)` on procps-ng and is not something to be relying on when the
        # cost of being wrong is two writers in one package database. Thirteen
        # process scans, only ever on the branch where the lock already exists.
        for holder in PACMAN_LOCK_HOLDERS:
            if subprocess.run(["pgrep", "-x", holder],
                              capture_output=True, timeout=30).returncode == 0:
                return  # a real package manager is running — the lock is live
    except (OSError, subprocess.SubprocessError):
        return          # cannot tell; never take a lock we have not ruled out
    warn(f"stale {PACMAN_LOCK} from an interrupted package manager — removing")
    subprocess.run(hostenv.sudo_prefix() + ["rm", "-f", str(PACMAN_LOCK)],
                   timeout=60)


def install_native(names):
    if not names:
        return True
    if hostenv.FAMILY == "arch":
        clear_stale_pacman_lock()
        cmd = hostenv.sudo_prefix() + ["pacman", "-S", "--needed",
                                       "--noconfirm", *names]
        if run(cmd, timeout=PACMAN_TIMEOUT):
            return True
        clear_stale_pacman_lock()
        # Arch mirrors keep one version of each package and delete the rest the
        # moment a new one lands. A full bootstrap runs for over an hour, so the
        # db snapshot taken at the start goes stale underneath us: pacman asks
        # for qpdf-12.4.1, the mirror now only has 12.4.2, and all thirteen
        # mirrors answer 404 for a package that is perfectly available. Refresh
        # and ask again — the second attempt requests the version that exists.
        #
        # Once per run, not once per failure. A failed batch is retried one
        # package at a time, so without the guard a single genuinely broken
        # name drags a full -Syu through every one of its fifty-nine innocent
        # neighbours: an hour of system upgrades that the first refresh already
        # settled. After the db is current a stale version is no longer a
        # plausible cause, so the second attempt would ask the same question
        # and get the same answer.
        global _db_refreshed_after_failure
        if _db_refreshed_after_failure:
            return False
        _db_refreshed_after_failure = True
        warn(f"{' '.join(names)}: install failed — refreshing db and retrying")
        refresh_indexes()
        return run(cmd, timeout=PACMAN_TIMEOUT)
    if hostenv.FAMILY == "debian":
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        cmd = hostenv.sudo_prefix() + ["apt-get", "install", "-y",
                                       "--no-install-recommends", *names]
        print(f"    {DIM}$ {' '.join(cmd)}{RESET}")
        return subprocess.run(cmd, env=env, timeout=3600).returncode == 0
    if hostenv.FAMILY == "macos":
        return run(["brew", "install", *names], env=BREW_ENV)
    return False


# Homebrew 7 asks "Would install … [y/N]" before every install. Under a TTY
# that stalls the run; without one it is a prompt nobody can answer.
BREW_ENV = dict(os.environ, HOMEBREW_NO_ASK="1")

# A cask marked `heavy` (MacTeX is ~6GB) installs alone and gets this long:
# inside the batch it once ran the whole batch past the one-hour timeout,
# which then threw away eight finished downloads with it.
HEAVY_TIMEOUT = 4 * 3600


def install_cask(names, timeout=3600):
    # --adopt takes ownership of an app installed outside brew instead of
    # erroring or clobbering it. Most of these were already on the machine.
    return run(["brew", "install", "--cask", "--adopt", *names],
               timeout=timeout, env=BREW_ENV)


def install_aur(names):
    helper = hostenv.aur_helper()
    if not helper:
        return False
    # paru and yay shell out to pacman for the dependency half, so a lock left
    # by an earlier casualty stops them just as dead.
    clear_stale_pacman_lock()
    return run([helper, "-S", "--needed", "--noconfirm", *names],
               timeout=PACMAN_TIMEOUT)


def ensure_npm_prefix():
    """Point npm's global prefix somewhere this user can actually write.

    On Linux the default is the root-owned `/usr`, so every `npm install
    --global` dies with `EACCES: mkdir '/usr/lib/node_modules/...'`. bootstrap
    used to fix this in its Node stage — which runs *after* packages, so the
    seven npm tools failed on a clean box and only appeared later, leaving a
    summary that blamed the packages. It belongs here, next to the command that
    needs it, because `packages.py --install` also gets run on its own.

    Writability, not a literal `/usr`: nvm, Volta and a Homebrew-on-Linux node
    all have their own prefixes and are already fine.
    """
    if hostenv.IS_MAC:
        return
    try:
        current = subprocess.run(["npm", "config", "get", "prefix"],
                                 capture_output=True, text=True,
                                 timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    if current and os.access(current, os.W_OK):
        return
    target = Path.home() / ".local"
    (target / "bin").mkdir(parents=True, exist_ok=True)
    if run(["npm", "config", "set", "prefix", str(target)]):
        warn(f"npm global prefix moved to {target} (was {current or 'unset'},"
             " not writable)")


def npm_major():
    try:
        out = subprocess.run(["npm", "--version"], capture_output=True,
                             text=True, timeout=60).stdout.strip()
        return int(out.split(".")[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0


def install_npm(names):
    ensure_npm_prefix()
    cmd = ["npm", "install", "--global", "--no-fund", "--no-audit",
           # Not --silent: that sets loglevel=silent, which swallows npm's
           # errors too, so a failed install shows up in the log as a command
           # that printed nothing and exited non-zero. --loglevel=error is as
           # quiet as it can get while still saying what went wrong. It still
           # lets `npm warn` through, which the allow-scripts audit below needs.
           "--loglevel=error"]
    allow = load().get("npm_allow_scripts", [])
    # A command-line flag, not `npm config set allow-scripts`: the persistent
    # form edits the user's ~/.npmrc and would keep granting these packages
    # script execution long after this install. Gated on the major version
    # because npm 11 and earlier have no such option and no such default.
    if allow and npm_major() >= 12:
        cmd.append("--allow-scripts=" + ",".join(allow))
    print(f"    {DIM}$ {' '.join(cmd + list(names))}{RESET}")
    try:
        # stderr captured, stdout not: `npm warn` goes to stderr and we have to
        # read it, but capturing both would hold npm's progress until the
        # command finished — the same buffered-log problem line buffering
        # exists to avoid.
        done = subprocess.run(cmd + list(names), timeout=3600,
                              stderr=subprocess.PIPE, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"npm: {exc}")
        return False
    output = (done.stderr or "").strip()
    if output:
        print(output)
    # npm reports a blocked script as a *warning* and exits 0, so without this
    # the only symptom is a tool that installs "successfully" and then does not
    # run. Surface the gap here, naming the fix, rather than leaving it to be
    # rediscovered from a stub binary's error message.
    if "not covered by allowScripts" in output:
        warn("npm blocked install scripts not in the manifest's"
             " npm_allow_scripts — add them there if a tool below is missing")
    # A global install is one transaction: one failing postinstall rolls back
    # every package in the call. do_install's batch-then-individually retry
    # is what keeps a bad package from costing the rest, so none here.
    return done.returncode == 0


def install_uv(names):
    env = build_env()
    if hostenv.which("uv"):
        return all(run(["uv", "tool", "install", "--quiet", n], env=env)
                   for n in names)
    if hostenv.which("pipx"):
        return all(run(["pipx", "install", n], env=env) for n in names)
    return False


def install_cargo(names):
    """A name that looks like a URL is installed with --git.

    Not everything is on crates.io under the name you expect: `cargo install
    agg` pulls an unrelated library crate and fails with "nothing to install",
    while the agg we want (asciinema's GIF renderer) only publishes from its
    repository.

    `--locked` first, then the same command without it. A lockfile is a snapshot
    of what compiled on the author's machine at release time, and an abandoned
    crate's snapshot rots: silicon 0.5.2 pins `time` 0.3.x from before the
    `Box<_>` inference fix, so `cargo install --locked silicon` fails with
    `error[E0282]` in a dependency nobody here chose. Unlocked, cargo picks the
    patched `time` and the build goes through. Locked remains the default
    because it is the reproducible one — the retry is for crates whose pins have
    aged out from under them, not a general loosening.
    """
    if not hostenv.which("cargo"):
        warn("cargo not found — install rustup to get these")
        return False
    env = build_env()
    good = True
    for name in names:
        head = (["cargo", "install", "--git", name]
                if name.startswith(("https://", "git@"))
                else ["cargo", "install", name])
        if run([*head[:2], "--locked", *head[2:]], env=env):
            continue
        warn(f"{name}: --locked build failed — retrying with fresh dependencies")
        good &= run(head, env=env)
    return good


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


def fetch(url, timeout=120):
    """Download a URL to bytes, preferring curl over urllib.

    urllib validates TLS against certifi, which a python.org or pyenv-built
    interpreter ships without installing — every fetch then dies with
    CERTIFICATE_VERIFY_FAILED on an otherwise healthy machine, and a caller
    that treats any exception as "not available" quietly draws the wrong
    conclusion. curl uses the OS trust store and exists on macOS, Arch and
    Debian alike. Same reasoning as install_bridges._download.
    """
    curl = hostenv.which("curl")
    if curl:
        done = subprocess.run([curl, "-fsSL", "--retry", "2", url],
                              capture_output=True, timeout=timeout)
        if done.returncode != 0:
            raise RuntimeError(done.stderr.decode(errors="replace").strip()
                               or f"curl exited {done.returncode}")
        return done.stdout
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return response.read()


def _font_zip_urls(repo):
    """Where a font family's *built* binaries live, best candidate first.

    The release assets, not the branch archive: googlefonts/googlesans-code
    keeps only .glyph sources on `main`, so downloading main.zip succeeds,
    finds zero .ttf files, and reports a cheerful ✓ for a font nobody
    installed. The branch archive stays as a fallback for the repos that do
    commit their builds and cut no releases.
    """
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        release = json.loads(fetch(api, timeout=60))
        urls = [a["browser_download_url"] for a in release.get("assets", [])
                if a["name"].lower().endswith(".zip")
                and "android" not in a["name"].lower()]
    except Exception:  # noqa: BLE001 - no releases, rate limited, offline
        urls = []
    for branch in ("main", "master"):
        urls.append(f"https://github.com/{repo}/archive/refs/heads/{branch}.zip")
    return urls


def install_font_release(names):
    """Fetch a font family straight from its GitHub release into the user font
    dir. Used for families no distro packages (Google Sans Code)."""
    import zipfile
    import io

    good = True
    for repo in names:
        try:
            target = hostenv.user_font_dir()
            target.mkdir(parents=True, exist_ok=True)
            count = 0
            # A candidate is only spent once it has yielded a font. Downloading
            # successfully is not the same as being the right archive: a repo
            # can publish a release zip of sources, or of webfonts only, and
            # breaking out of this loop on the download would then fail the
            # whole package with the branch archives — which do carry the
            # fonts — never tried. Extraction is the acceptance test, so it
            # belongs inside the loop over candidates.
            for candidate in _font_zip_urls(repo):
                try:
                    info(f"downloading {candidate}")
                    blob = fetch(candidate)
                except Exception as exc:  # noqa: BLE001 - try the next candidate
                    warn(f"{candidate}: {exc}")
                    continue
                with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                    for entry in archive.namelist():
                        if entry.lower().endswith((".ttf", ".otf")):
                            (target / Path(entry).name).write_bytes(
                                archive.read(entry))
                            count += 1
                if count:
                    break
                warn(f"{candidate}: contained no .ttf/.otf")
            if not count:
                raise RuntimeError("no archive yielded a font file")
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
    # Last, one at a time: nothing small waits behind a 6GB download.
    "heavy-cask": lambda names: install_cask(names, timeout=HEAVY_TIMEOUT),
}


def refresh_indexes():
    """One index refresh up front beats sixty stale-cache failures."""
    if hostenv.FAMILY == "debian":
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        subprocess.run(hostenv.sudo_prefix() + ["apt-get", "update", "-qq"],
                       env=env, timeout=600)
    elif hostenv.FAMILY == "arch":
        # -Syu, never -Sy. Arch has no stable release to pin against, so a db
        # refreshed without the matching upgrade leaves the machine in the
        # documented partial-upgrade state: the new index describes packages
        # built against libraries this box has not taken yet, and the next
        # install pulls a dependency that will not link. It is also slower in
        # practice, because every subsequent -S then has to resolve around the
        # skew. The upgrade is the price of touching the db at all.
        clear_stale_pacman_lock()
        subprocess.run(hostenv.sudo_prefix() + ["pacman", "-Syu", "--noconfirm"],
                       timeout=PACMAN_TIMEOUT)
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
        elif method == "cask" and pkg.get("heavy"):
            batches.setdefault("heavy-cask", []).append((pkg, payload))
        else:
            batches.setdefault(method, []).append((pkg, payload))

    results = {}
    # Order matters: `native` brings the toolchains the others are installed with.
    for method in ("native", "cask", "aur", "npm", "uv", "cargo", "go",
                   "source", "font-release", "heavy-cask"):
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
                # `early` first, and only here. Inside the batch above, naming
                # a package in the transaction settles any virtual it provides
                # whatever order the arguments are in. One at a time there is
                # no such protection: whoever installs first picks the provider
                # for everyone after, which is how gifski — pulling ffmpeg from
                # the image group — chose jack2 before the audio group had a
                # chance to ask for pipewire-jack. Stable, so everything that
                # has not asked to jump the queue keeps manifest order.
                for pkg, payload in sorted(items,
                                           key=lambda it: not it[0].get("early")):
                    results[pkg["id"]] = INSTALLERS[method](payload)
        else:
            for pkg, payload in items:
                results[pkg["id"]] = INSTALLERS[method](payload)

    # A route can report success and still leave nothing on PATH. `paru -S
    # pandoc-bin` resolves cleanly on aarch64 and then dies on the PKGBUILD's
    # x86_64-only arch check, and AUR metadata carries no architecture field we
    # could have consulted beforehand. `provides` is the only honest test, so
    # ask it again and spend the declared fallback on whatever is still missing
    # — the same rule doctor.py applies: a tool counts when its binary exists,
    # not when a package manager says the install went fine.
    rescued = []
    for pkg, method, payload, reason in rows:
        fallback = pkg.get("fallback")
        if not fallback or method in ("have", "n/a", fallback["kind"]):
            continue
        if satisfied(pkg) is not False:
            continue
        # `manual` is a destination, not an installer — there is no entry for it
        # in INSTALLERS and there should not be, because the payload is a
        # download page a human has to visit. Dispatching it raised KeyError and
        # took the whole install down before the summary that would have said
        # which packages were missing. Route it to the same report that a
        # first-choice `manual` goes to. Reaper reaches this path on a real Arch
        # desktop, where its AUR build needs a hand-placed archive.
        if fallback["kind"] not in INSTALLERS:
            if fallback["kind"] != "manual":
                warn(f"{pkg['id']}: unknown fallback kind {fallback['kind']!r}")
            manual.append((pkg, [fallback["name"]], f"{method} route failed"))
            results[pkg["id"]] = False
            continue
        warn(f"{pkg['id']}: {method} left it missing — falling back to"
             f" {fallback['kind']}")
        results[pkg["id"]] = INSTALLERS[fallback["kind"]]([fallback["name"]])
        # Ask `provides` again rather than trusting the fallback's own exit
        # code. "fallback rescued: marp-cli" was printed for a package that was
        # still missing afterwards, which is the exact failure this whole block
        # exists to catch.
        if satisfied(pkg) is not False:
            rescued.append(pkg["id"])
    if rescued:
        info(f"fallback rescued: {', '.join(rescued)}")

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
    mode.add_argument("--missing-npm", action="store_true",
                      help="print npm package names whose provides are absent")
    mode.add_argument("--fix-npm", action="store_true",
                      help="reinstall the npm packages --missing-npm lists")
    ap.add_argument("--profile", choices=["full", "headless"], default="full",
                    help="headless drops desktop apps")
    ap.add_argument("--skip-heavy", action="store_true",
                    help="skip multi-GB packages (LaTeX, Blender, FreeCAD)")
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

    if args.missing_npm or args.fix_npm:
        # Keeps the retry list in the manifest instead of spelling the package
        # names out a second time in shell, where they would drift.
        stragglers = []
        for pkg in pkgs:
            route = pkg.get("fallback") or {}
            if route.get("kind") == "npm" and satisfied(pkg) is False:
                stragglers.append(route["name"])
        if args.missing_npm:
            # Machine-readable and silent on success: bootstrap's Node stage
            # consumes this to decide whether to say anything at all.
            print("\n".join(stragglers))
            return 0
        # --fix-npm exists so that the retry goes through install_npm rather
        # than through a hand-written `npm install` in bootstrap.sh. The flags
        # are not cosmetic: without --allow-scripts the retry reinstalls npm
        # 12's no-postinstall stub, which is the very thing it is retrying.
        if not stragglers:
            return 0
        return 0 if install_npm(stragglers) else 1

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
    # An installer's exit code is a claim, not evidence. npm exits non-zero on
    # a first install that nevertheless leaves a working binary on PATH, and
    # reporting seven red ✗ for tools doctor then finds healthy teaches people
    # to ignore this summary. Where `provides` can answer, it overrules the exit
    # code in both directions; only packages with nothing observable (a bare
    # library) are still judged by what the package manager said.
    by_id = {p["id"]: p for p in pkgs}
    bad = []
    for pkg_id, claimed in results.items():
        state = satisfied(by_id[pkg_id])
        if state is False or (state is None and not claimed):
            bad.append(pkg_id)
    good = len(results) - len(bad)
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
