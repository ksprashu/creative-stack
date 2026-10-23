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

"""Wire GUI creative apps into headless, agent-callable entry points.

macOS buries each app's CLI inside its .app bundle; Linux scatters them across
/usr/bin, /opt and Flatpak exports. Either way this links them into
~/.local/bin under a stable name, so skill scripts call `blender` /
`inkscape` / `gimp-console` and never hardcode a path.

Also: fetches a General MIDI SoundFont for fluidsynth, writes the DaVinci
Resolve scripting environment, and prints how to finish OBS setup.

    python3 scripts/install_bridges.py           # link + fetch
    python3 scripts/install_bridges.py --check    # report only, change nothing
"""

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hostenv  # noqa: E402

REPO_ROOT = hostenv.REPO_ROOT
BIN_DIR = hostenv.bin_dir()
ENV_FILE = REPO_ROOT / "creative-stack.env"

GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"


def ok(msg):
    print(f"    {GREEN}✓{RESET} {msg}")


def warn(msg):
    print(f"    {YELLOW}!{RESET} {msg}")


def info(msg):
    print(f"    {DIM}{msg}{RESET}")


# The stable names skills call. The candidate paths behind each one differ per
# platform and live in hostenv.APP_BRIDGES, which doctor.py reads too.
BRIDGE_NAMES = ["blender", "inkscape", "gimp-console", "gimp-app"]

RESOLVE_API, RESOLVE_LIB, RESOLVE_IS_STUDIO = hostenv.resolve_paths()

# Small, permissively licensed (MIT) General MIDI bank. ~140MB uncompressed is
# too big to vendor, so fetch on demand and cache in-repo (gitignored).
SOUNDFONT_URL = (
    "https://github.com/musescore/MuseScore/raw/2.1/share/sound/FluidR3Mono_GM.sf3"
)
SOUNDFONT_PATH = REPO_ROOT / "assets/soundfonts/FluidR3Mono_GM.sf3"


# A macOS app bundle finds its own plug-ins relative to the path it was started
# from. Through a symlink in ~/.local/bin, GIMP 3.2 finds none, prints "No batch
# interpreters are available" and then waits forever for a quit that never
# comes. Bundle binaries therefore get an exec wrapper, which keeps the path.
WRAPPER_MARK = "# creative-stack bridge"


def wrapper_text(target):
    return f'#!/bin/sh\n{WRAPPER_MARK}\nexec "{target}" "$@"\n'


def is_ours(path):
    return (path.is_file() and not path.is_symlink()
            and WRAPPER_MARK.encode() in path.read_bytes()[:200])


def link_bridges(check_only):
    print("\n▸ Headless app bridges")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    linked = 0

    for name in BRIDGE_NAMES:
        target = hostenv.find_app(name)
        if target is None:
            warn(f"{name}: no app found — skipping")
            continue

        link = BIN_DIR / name
        wrap = ".app/Contents/MacOS/" in str(target)
        if wrap and is_ours(link) and link.read_text() == wrapper_text(target):
            ok(f"{name} -> {target} (wrapper)")
            linked += 1
            continue
        if target == link or target.resolve() == link:
            ok(f"{name} already on PATH at {target}")
            linked += 1
            continue
        if not wrap and link.is_symlink() and link.resolve() == target.resolve():
            ok(f"{name} -> {target}")
            linked += 1
            continue

        if check_only:
            warn(f"{name}: would link -> {target}")
            continue

        # Replace a stale link, but never clobber a real file we did not create.
        if link.is_symlink() or is_ours(link):
            link.unlink()
        elif link.exists():
            warn(f"{name}: {link} exists and is not a symlink — leaving it alone")
            continue

        if wrap:
            link.write_text(wrapper_text(target))
            link.chmod(0o755)
        else:
            link.symlink_to(target)
        ok(f"{name} -> {target}" + (" (wrapper)" if wrap else ""))
        linked += 1

    if str(BIN_DIR) not in os.environ.get("PATH", ""):
        warn(f"{BIN_DIR} is not on PATH. Add to {hostenv.shell_rc()}:")
        info(f'export PATH="{BIN_DIR}:$PATH"')

    return linked


def resolve_env(check_only):
    """DaVinci Resolve's Python API is found via three env vars, not a package."""
    print("\n▸ DaVinci Resolve scripting environment")
    if not RESOLVE_API.exists():
        warn("Resolve scripting API not found — skipping")
        return {}

    env = {
        "RESOLVE_SCRIPT_API": str(RESOLVE_API),
        "RESOLVE_SCRIPT_LIB": str(RESOLVE_LIB),
        "PYTHONPATH": f"{RESOLVE_API / 'Modules'}",
    }
    for k, v in env.items():
        exists = Path(v.split(":")[0]).exists()
        (ok if exists else warn)(f"{k}={v}")

    # Free edition only allows scripting from Resolve's own console. Detect and
    # say so plainly, rather than letting /vidkit fail later with a null handle.
    # Blackmagic ships Linux as Studio-only, so an install there always scripts.
    if RESOLVE_IS_STUDIO:
        ok("Studio detected — external scripting available")
    else:
        warn("Free edition — external scripting is NOT available.")
        info("/vidkit will emit FCP7 XML for manual import instead. This is expected.")
    return env


def write_env_file(env, check_only):
    """Emit a sourceable env file. Secrets are NOT written here — they stay in
    Keychain and are read at call time."""
    print("\n▸ Environment file")
    if check_only:
        warn(f"would write {ENV_FILE}")
        return

    store, read = hostenv.secret_set_hint("google-api-key", "GOOGLE_API_KEY")
    lines = [
        "# Generated by scripts/install_bridges.py. Contains no secrets, but it"
        " is gitignored:",
        "# every value is an absolute path resolved for this machine, so"
        " committing it would",
        "# hand the next machine a set of directories that do not exist there.",
        f"# Paths only, resolved for {hostenv.pretty_name()}.",
        f"# Never add API keys here. This machine's secret store is"
        f" {hostenv.secret_backend()}:",
        f"#   {store}",
        f"#   {read}",
        "",
        f'export CREATIVE_STACK_ROOT="{REPO_ROOT}"',
        f'export PATH="{BIN_DIR}:$PATH"',
    ]
    if not hostenv.IS_MAC:
        lines.append('export PATH="$HOME/.cargo/bin:$HOME/go/bin:$PATH"')
    for k, v in env.items():
        lines.append(f'export {k}="{v}"')
    # Pin whatever hostenv.soundfont() resolves to today, so a later distro
    # package upgrade cannot silently swap the bank out from under a mix that
    # has already been rendered once.
    bank = hostenv.soundfont()
    if bank:
        lines.append(f'export CREATIVE_STACK_SOUNDFONT="{bank}"')

    ENV_FILE.write_text("\n".join(lines) + "\n")
    ok(f"wrote {ENV_FILE}")
    info(f"source it from {hostenv.shell_rc()}:  source {ENV_FILE}")


def _download(url, dst):
    """Fetch a URL to a path, preferring curl over urllib.

    urllib validates TLS against certifi's bundle, which a python.org or
    pyenv-built interpreter ships without installing — so every download fails
    with CERTIFICATE_VERIFY_FAILED on an otherwise healthy machine. curl uses
    the OS trust store, follows redirects (this URL is one), and is present on
    macOS, Arch and Debian alike. urllib stays as the fallback for the rare box
    without it.
    """
    curl = shutil.which("curl")
    if curl:
        subprocess.run([curl, "-fsSL", "--retry", "3", "-o", str(dst), url],
                       check=True)
        return
    urllib.request.urlretrieve(url, dst)  # noqa: S310


def fetch_soundfont(check_only):
    print("\n▸ SoundFont for fluidsynth")
    # hostenv already knows every place a bank can live — the repo copy this
    # function downloads, and the distro packages (`soundfont-fluid` on Arch,
    # `fluid-soundfont-gm` on Debian) that ship the same FluidR3. If any of
    # them answers, skip 140MB of network.
    found = hostenv.soundfont()
    if found:
        size = found.stat().st_size // 1_000_000
        where = "downloaded" if found == SOUNDFONT_PATH else "system"
        ok(f"using {where} SoundFont {found} ({size}MB)")
        return
    if check_only:
        warn("would download FluidR3Mono_GM.sf3")
        return

    SOUNDFONT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        info(f"downloading {SOUNDFONT_URL} ...")
        _download(SOUNDFONT_URL, SOUNDFONT_PATH)
        ok(f"saved {SOUNDFONT_PATH} ({SOUNDFONT_PATH.stat().st_size // 1_000_000}MB)")
    except Exception as exc:  # noqa: BLE001 - network is best-effort
        SOUNDFONT_PATH.unlink(missing_ok=True)
        warn(f"download failed: {exc}")
        info("MIDI rendering in /sound will be unavailable until a .sf2/.sf3 exists at")
        info(str(SOUNDFONT_PATH))


def obs_notes():
    """obs-websocket cannot be enabled headlessly — it is a GUI toggle."""
    print("\n▸ OBS WebSocket")
    installed = (Path("/Applications/OBS.app").exists() if hostenv.IS_MAC
                 else bool(shutil.which("obs")))
    if not installed:
        warn("OBS not installed — skipping")
        return
    cfg = hostenv.obs_config_dir() / "plugin_config/obs-websocket"
    if (cfg / "config.json").exists():
        ok("obs-websocket already configured")
    else:
        warn("obs-websocket not yet enabled (this is a one-time GUI step)")
        info("OBS → Tools → WebSocket Server Settings → Enable, then store the password:")
        info("  " + hostenv.secret_set_hint("obs", "OBS_WEBSOCKET_PASSWORD")[0])


def blender_addon_note():
    print("\n▸ Blender MCP addon")
    info("Installed on demand when you enable the Blender MCP server.")
    info("  claude mcp enable blender   # then in Blender: Edit → Preferences → Add-ons")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    args = ap.parse_args()

    print("=" * 66)
    print(" 🔌 CREATIVE STACK — app bridges")
    print("=" * 66)

    link_bridges(args.check)
    env = resolve_env(args.check)
    fetch_soundfont(args.check)
    write_env_file(env, args.check)
    obs_notes()
    blender_addon_note()

    print("\n" + "=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
