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

"""Everything that differs between macOS, Arch and Debian, in one place.

Named `hostenv` and not `platform` on purpose: skill scripts put `scripts/` on
sys.path, and a module called platform.py there would shadow the stdlib for
every library imported afterwards.

Skill scripts must never test `sys.platform` themselves. They ask this module
for a *thing* — the ffmpeg that can burn subtitles, a Chrome that can render a
slide, the place a secret lives — and get an answer that is already correct for
the machine they happen to be on.

    python3 scripts/hostenv.py             # what am I running on?
    python3 scripts/hostenv.py --json
"""

import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(os.environ.get("CREATIVE_STACK_ROOT",
                                Path(__file__).resolve().parent.parent))

# --------------------------------------------------------------- detection ---

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


@lru_cache(maxsize=1)
def os_release():
    """Parse /etc/os-release into a dict. Empty on macOS."""
    data = {}
    path = Path("/etc/os-release")
    if not path.exists():
        return data
    for line in path.read_text(errors="replace").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


@lru_cache(maxsize=1)
def family():
    """The packaging family this machine belongs to.

    Returns "macos", "arch" or "debian". Derivatives are folded into their
    parent via ID_LIKE, which is how Omarchy, EndeavourOS, Manjaro, Pop!_OS,
    Linux Mint and friends get supported without naming any of them.
    """
    if IS_MAC:
        return "macos"
    rel = os_release()
    ids = [rel.get("ID", "")] + rel.get("ID_LIKE", "").split()
    for name in ids:
        if name in ("arch", "archlinux"):
            return "arch"
        if name in ("debian", "ubuntu"):
            return "debian"
    # No os-release match: fall back to whichever package manager exists, so a
    # stripped container still resolves rather than dying on detection.
    if shutil.which("pacman"):
        return "arch"
    if shutil.which("apt-get"):
        return "debian"
    return "unknown"


FAMILY = family()

PACKAGE_MANAGER = {"macos": "brew", "arch": "pacman", "debian": "apt"}.get(FAMILY)


def pretty_name():
    if IS_MAC:
        ver = _platform.mac_ver()[0] or "unknown"
        return f"macOS {ver} ({_platform.machine()})"
    rel = os_release()
    name = rel.get("PRETTY_NAME") or rel.get("NAME") or "Linux"
    return f"{name} ({_platform.machine()})"


@lru_cache(maxsize=1)
def aur_helper():
    """Arch keeps a lot of creative tooling in the AUR. paru/yay are the two
    helpers that can run non-interactively; without one we fall back to pip,
    npm, cargo or a release tarball instead of asking the user to build.

    The helper has to actually *run*, not merely be on PATH. `paru-bin` ships a
    prebuilt binary linked against a specific libalpm soname, so the moment
    pacman moves from 7.0 to 7.1 every invocation dies with
    `libalpm.so.15: cannot open shared object file` — exit 127, empty output.
    A caller asking "is this package in the AUR?" reads that as "no", and the
    whole AUR route disappears behind three separate "not in repos" warnings
    that point at the packages rather than at the broken helper.
    """
    if FAMILY != "arch":
        return None
    for name in ("paru", "yay"):
        if not shutil.which(name):
            continue
        try:
            probe = subprocess.run([name, "--version"], capture_output=True,
                                   timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return name
    return None


def has_display():
    """Is there a display server we could actually open a GUI app against?

    This gates *runtime* GUI use (OBS remote control, a live Blender session),
    not installation — a headless box should still install Blender so
    `blender --background` works.
    """
    if os.environ.get("CREATIVE_STACK_HEADLESS") == "1":
        return False
    if IS_MAC:
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def sudo_prefix():
    """Non-interactive sudo, or nothing when already root."""
    if os.geteuid() == 0:
        return []
    if not shutil.which("sudo"):
        return []
    return ["sudo", "-n"] if os.environ.get("CREATIVE_STACK_SUDO_NONINTERACTIVE") else ["sudo"]


# ------------------------------------------------------------------- paths ---

def bin_dir():
    """Where we link headless entry points. On PATH by default on Linux."""
    return Path(os.environ.get("CREATIVE_STACK_BIN", Path.home() / ".local/bin"))


def cache_dir():
    base = (Path.home() / "Library/Caches" if IS_MAC
            else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")))
    return base / "creative-stack"


def config_dir():
    base = (Path.home() / "Library/Application Support" if IS_MAC
            else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")))
    return base / "creative-stack"


def user_font_dir():
    return Path.home() / ("Library/Fonts" if IS_MAC else ".local/share/fonts")


def shell_rc():
    """The rc file to suggest sourcing creative-stack.env from."""
    shell = Path(os.environ.get("SHELL", "/bin/bash")).name
    return Path.home() / {"zsh": ".zshrc", "fish": ".config/fish/config.fish"}.get(
        shell, ".bashrc")


def playwright_cache():
    if IS_MAC:
        return Path.home() / "Library/Caches/ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-playwright"


# -------------------------------------------------------------- soundfonts ---

# fluidsynth needs a General MIDI bank and there is no standard location for
# one. install_bridges.py downloads FluidR3 into the repo if the distro has not
# already packaged it, so both routes have to be searched — in that order,
# because the repo copy is the one bootstrap can guarantee the version of.
_SOUNDFONT_REPO = ["assets/soundfonts/FluidR3Mono_GM.sf3",
                   "assets/soundfonts/FluidR3_GM.sf2"]
_SOUNDFONT_SYSTEM = {
    "macos": ["/opt/homebrew/share/soundfonts/default.sf2",
              "/opt/homebrew/share/fluid-synth/sf2/VintageDreamsWaves-v2.sf2"],
    "arch": ["/usr/share/soundfonts/FluidR3_GM.sf2",       # soundfont-fluid
             "/usr/share/soundfonts/default.sf2"],
    "debian": ["/usr/share/sounds/sf2/FluidR3_GM.sf2",     # fluid-soundfont-gm
               "/usr/share/sounds/sf3/FluidR3_GM.sf3",
               "/usr/share/soundfonts/FluidR3_GM.sf2"],
}

# fluid-synth's own Homebrew formula ships VintageDreamsWaves, a 400KB novelty
# bank. It is GM-mapped, so it resolves and renders and every check passes —
# and the bed comes back sounding like a 1995 sound card. A real GM bank is
# 13MB compressed (sf3) at the very smallest, so size is the only reliable way
# to tell a usable bank from a placeholder.
_SOUNDFONT_MIN_BYTES = 4_000_000


def soundfont():
    """Path to a General MIDI bank for fluidsynth, or None.

    `$CREATIVE_STACK_SOUNDFONT` wins so a user can point at their own bank —
    FluidR3 is serviceable and free, not good — and is taken on trust at any
    size, because an explicit override is a decision, not a fallback.
    """
    override = os.environ.get("CREATIVE_STACK_SOUNDFONT")
    if override and Path(override).exists():
        return Path(override)
    candidates = [REPO_ROOT / rel for rel in _SOUNDFONT_REPO]
    candidates += [Path(p) for p in _SOUNDFONT_SYSTEM.get(FAMILY, [])]
    return next((p for p in candidates
                 if p.exists() and p.stat().st_size >= _SOUNDFONT_MIN_BYTES),
                None)


def soundfont_hint():
    return ("No SoundFont found. Run scripts/install_bridges.py to fetch one, "
            "or point $CREATIVE_STACK_SOUNDFONT at a .sf2/.sf3 you already have.")


# ------------------------------------------------------------------ ffmpeg ---

# Homebrew's `ffmpeg` formula dropped libass, freetype and fontconfig, so `ass`,
# `subtitles` and `drawtext` are missing from it — the keg-only `ffmpeg-full`
# carries them. Every Linux distro ships one ffmpeg that has the lot, so there
# is nothing to work around there.
_FFMPEG_FULL_DIRS = [
    Path("/opt/homebrew/opt/ffmpeg-full/bin"),
    Path("/usr/local/opt/ffmpeg-full/bin"),     # Intel Homebrew prefix
]


def ffmpeg_bin(tool="ffmpeg"):
    """Resolve ffmpeg/ffprobe to a build that can render text and subtitles."""
    override = os.environ.get(f"CREATIVE_STACK_{tool.upper()}")
    if override:
        return override
    if IS_MAC:
        for directory in _FFMPEG_FULL_DIRS:
            candidate = directory / tool
            if candidate.exists():
                return str(candidate)
    return shutil.which(tool)


def ffmpeg_install_hint():
    return {"macos": "brew install ffmpeg-full",
            "arch": "sudo pacman -S ffmpeg",
            "debian": "sudo apt install ffmpeg"}.get(FAMILY, "install ffmpeg")


# ------------------------------------------------------------------- which ---

def install_dirs():
    """Directories this repo installs binaries into, in addition to PATH.

    packages.py drops cargo/go/uv/source builds into these, and a shell that
    has not been restarted since bootstrap will not have them on PATH yet. A
    verification pass that says "missing" about a tool it just installed is
    worse than useless, so look here too.
    """
    home = Path.home()
    candidates = [bin_dir(), home / ".cargo/bin", home / "go/bin",
                  home / ".local/bin"]
    if FAMILY == "arch":
        # Arch parks perl-packaged binaries outside the default PATH and adds
        # them from /etc/profile, so exiftool is invisible to anything not
        # started from a login shell — including this process.
        candidates += [Path("/usr/bin/vendor_perl"), Path("/usr/bin/site_perl"),
                       Path("/usr/bin/core_perl")]
    # bin_dir() usually *is* ~/.local/bin; keep order, drop the repeat.
    return list(dict.fromkeys(candidates))


def which(binary):
    """shutil.which, plus the directories bootstrap installs into."""
    found = shutil.which(binary)
    if found:
        return found
    for directory in install_dirs():
        candidate = directory / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# ------------------------------------------------------------- imagemagick ---

def magick_cmd(verb=None):
    """ImageMagick invocation as a list, across the 6/7 split.

    ImageMagick 7 unified everything behind `magick convert …`; version 6 has
    separate `convert`, `identify`, `composite` binaries and no `magick` at all.
    Ubuntu 24.04 is still on 6, Homebrew and Arch are on 7 — so the same skill
    script has to be able to say "resize this" on both.

        magick_cmd()           -> ["magick"]  or  ["convert"]
        magick_cmd("identify") -> ["magick", "identify"]  or  ["identify"]
    """
    if shutil.which("magick"):
        return ["magick"] + ([verb] if verb else [])
    legacy = verb or "convert"
    return [legacy] if shutil.which(legacy) else []


# ------------------------------------------------------------------ chrome ---

_CHROME_CANDIDATES = {
    "macos": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ],
    "linux": [
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/usr/bin/microsoft-edge", "/snap/bin/chromium",
        "/var/lib/flatpak/exports/bin/org.chromium.Chromium",
    ],
}

# Playwright names its bundled Chromium differently per OS.
_PLAYWRIGHT_GLOBS = {
    "macos": "chromium*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    "linux": "chromium*/chrome-linux*/chrome",
}


def chrome_path():
    """A Chrome/Chromium marp and Playwright can drive, or None."""
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]

    for path in _CHROME_CANDIDATES["macos" if IS_MAC else "linux"]:
        if Path(path).exists():
            return path
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found

    # Playwright's bundled build is the one every bootstrapped machine has.
    pattern = _PLAYWRIGHT_GLOBS["macos" if IS_MAC else "linux"]
    builds = sorted(playwright_cache().glob(pattern))
    return str(builds[-1]) if builds else None


def chrome_install_hint():
    return {"macos": "brew install --cask google-chrome",
            "arch": "sudo pacman -S chromium",
            "debian": "sudo apt install chromium-browser"}.get(
                FAMILY, "install Chromium")


# ------------------------------------------------------------- app bridges ---

# Stable name -> candidate paths per family, most-preferred first. On Linux
# these are normally already on PATH; the entries exist so a Flatpak- or
# tarball-installed app is still found.
APP_BRIDGES = {
    "blender": {
        "macos": ["/Applications/Blender.app/Contents/MacOS/Blender"],
        "linux": ["/usr/bin/blender", "/usr/local/bin/blender",
                  "/var/lib/flatpak/exports/bin/org.blender.Blender",
                  "/opt/blender/blender"],
    },
    "inkscape": {
        "macos": ["/Applications/Inkscape.app/Contents/MacOS/inkscape"],
        "linux": ["/usr/bin/inkscape", "/usr/local/bin/inkscape",
                  "/var/lib/flatpak/exports/bin/org.inkscape.Inkscape"],
    },
    "gimp-console": {
        "macos": ["/Applications/GIMP.app/Contents/MacOS/gimp-console-3.2",
                  "/Applications/GIMP.app/Contents/MacOS/gimp-console-3",
                  "/Applications/GIMP.app/Contents/MacOS/gimp-console"],
        "linux": ["/usr/bin/gimp-console-3.2", "/usr/bin/gimp-console-3.0",
                  "/usr/bin/gimp-console-2.10", "/usr/bin/gimp-console"],
    },
    "gimp-app": {
        "macos": ["/Applications/GIMP.app/Contents/MacOS/gimp"],
        "linux": ["/usr/bin/gimp", "/usr/bin/gimp-3.2", "/usr/bin/gimp-2.10"],
    },
    # marp shells out to soffice for --pptx-editable. The cask does not put it
    # on PATH on macOS, so the bundle has to be searched by hand.
    "soffice": {
        "macos": ["/Applications/LibreOffice.app/Contents/MacOS/soffice"],
        "linux": ["/usr/bin/soffice", "/usr/local/bin/soffice",
                  "/usr/lib/libreoffice/program/soffice",
                  "/var/lib/flatpak/exports/bin/org.libreoffice.LibreOffice"],
    },
    "reaper": {
        "macos": ["/Applications/REAPER.app/Contents/MacOS/REAPER"],
        "linux": ["/usr/bin/reaper", "/usr/local/bin/reaper",
                  "/opt/REAPER/reaper"],
    },
    "audacity": {
        "macos": ["/Applications/Audacity 4.app/Contents/MacOS/Audacity",
                  "/Applications/Audacity.app/Contents/MacOS/Audacity"],
        "linux": ["/usr/bin/audacity", "/usr/local/bin/audacity",
                  "/var/lib/flatpak/exports/bin/org.audacityteam.Audacity"],
    },
    "audio-hijack": {
        "macos": ["/Applications/Audio Hijack.app/Contents/MacOS/Audio Hijack"],
        "linux": [],
    },
    "loopback": {
        "macos": ["/Applications/Loopback.app/Contents/MacOS/Loopback"],
        "linux": [],
    },
}


def app_candidates(name):
    entry = APP_BRIDGES.get(name, {})
    paths = list(entry.get("macos" if IS_MAC else "linux", []))
    # A PATH hit wins over nothing, and covers distro layouts we did not list.
    found = shutil.which(name)
    if found and found not in paths:
        paths.append(found)
    return paths


def find_app(name):
    return next((Path(p) for p in app_candidates(name) if Path(p).exists()), None)


# ---------------------------------------------------------- davinci resolve ---

def resolve_paths():
    """(scripting_api_dir, fusionscript_lib, is_studio).

    Resolve on Linux installs under /opt/resolve with an entirely different
    layout to the macOS bundle, and the Studio licence lives elsewhere again.
    """
    if IS_MAC:
        api = Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve"
                   "/Developer/Scripting")
        lib = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents"
                   "/Libraries/Fusion/fusionscript.so")
        studio = (Path("/Applications/DaVinci Resolve/DaVinci Resolve Studio.app").exists()
                  or Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve"
                          "/Resolve.lic").exists())
        return api, lib, studio

    api = Path("/opt/resolve/Developer/Scripting")
    lib = Path("/opt/resolve/libs/Fusion/fusionscript.so")
    # The Linux build ships Studio only (Blackmagic does not distribute the free
    # edition for Linux), so a present install implies scripting works.
    studio = api.exists()
    return api, lib, studio


def obs_config_dir():
    if IS_MAC:
        return Path.home() / "Library/Application Support/obs-studio"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "obs-studio"


# ------------------------------------------------------------ audio routing ---

LOOPBACK_KEYWORDS = (
    "loopback", "blackhole", "ark", "eqmac", "soundflower",
    "monitor", "pipewire", "vb-cable", "voicemeeter", "virtual",
)


def is_loopback_device(name):
    """True when an audio source name looks like a virtual loopback/passthrough."""
    low = (name or "").lower()
    return low.endswith(".monitor") or any(k in low for k in LOOPBACK_KEYWORDS)


def parse_avfoundation_audio(output):
    """Extract [{index, name, backend, loopback}] from ffmpeg avfoundation output."""
    devices = []
    in_audio = False
    for line in (output or "").splitlines():
        if "AVFoundation audio devices:" in line:
            in_audio = True
            continue
        if in_audio and "AVFoundation" in line and "devices:" in line:
            break
        if not in_audio:
            continue
        m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if m:
            idx, name = int(m.group(1)), m.group(2).strip()
            devices.append({
                "index": idx,
                "name": name,
                "backend": "avfoundation",
                "loopback": is_loopback_device(name),
            })
    return devices


def parse_system_profiler_audio(output):
    """Extract CoreAudio input devices from `system_profiler SPAudioDataType -json`.

    Loopback 2 virtual devices can have any user-chosen name (e.g. 'Clean Mic'),
    so checking `coreaudio_device_transport == 'coreaudio_device_type_virtual'`
    and the manufacturer ('Rogue Amoeba') catches virtual passthrough devices
    that a name-only substring match would miss.
    """
    try:
        doc = json.loads(output or "{}")
    except json.JSONDecodeError:
        return []
    devices = []
    for group in doc.get("SPAudioDataType", []):
        for item in group.get("_items", []):
            inputs = int(item.get("coreaudio_device_input") or 0)
            if inputs <= 0:
                continue
            name = (item.get("_name") or "").strip()
            mfg = (item.get("coreaudio_device_manufacturer") or "").strip()
            transport = (item.get("coreaudio_device_transport") or "").strip()
            virtual = (
                transport == "coreaudio_device_type_virtual"
                or any(v in mfg.lower() for v in ("rogue amoeba", "existential audio", "bitgapp"))
                or is_loopback_device(name)
            )
            devices.append({
                "index": None,
                "name": name,
                "manufacturer": mfg,
                "transport": transport.replace("coreaudio_device_type_", ""),
                "channels": inputs,
                "rate": int(item.get("coreaudio_device_srate") or 48000),
                "default": item.get("coreaudio_default_audio_input_device") == "spaudio_yes",
                "backend": "avfoundation",
                "loopback": virtual,
            })
    return devices


def parse_pactl_sources(output):
    """Extract [{index, name, backend, loopback}] from `pactl list short sources`."""
    devices = []
    for line in (output or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 2 and parts[0].isdigit():
            idx, name = int(parts[0]), parts[1].strip()
            devices.append({
                "index": idx,
                "name": name,
                "backend": "pulse",
                "loopback": is_loopback_device(name),
            })
    return devices


def audio_capture_backend():
    if IS_MAC:
        return "avfoundation"
    if shutil.which("pactl"):
        return "pulse"
    return "alsa"


def audio_devices():
    """List available audio capture & loopback devices on this host."""
    if IS_MAC:
        avf = []
        ff = ffmpeg_bin("ffmpeg") or "ffmpeg"
        try:
            proc = subprocess.run(
                [ff, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                capture_output=True, text=True, timeout=15,
            )
            avf = parse_avfoundation_audio(proc.stdout + "\n" + proc.stderr)
        except (OSError, subprocess.SubprocessError):
            pass
        sp = []
        try:
            proc = subprocess.run(
                ["system_profiler", "SPAudioDataType", "-json"],
                capture_output=True, text=True, timeout=15,
            )
            sp = parse_system_profiler_audio(proc.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
        if sp:
            idx_by_name = {d["name"].lower(): d["index"] for d in avf}
            for i, d in enumerate(sp):
                d["index"] = idx_by_name.get(d["name"].lower(), i)
            return sp
        return avf
    if shutil.which("pactl"):
        try:
            proc = subprocess.run(
                ["pactl", "list", "short", "sources"],
                capture_output=True, text=True, timeout=10,
            )
            return parse_pactl_sources(proc.stdout)
        except (OSError, subprocess.SubprocessError):
            return []
    return []


def audio_input_args(device):
    """FFmpeg input arguments (`[-f, <backend>, -i, <spec>]`) for a capture device."""
    backend = audio_capture_backend()
    if backend == "avfoundation":
        spec = str(device)
        return ["-f", "avfoundation", "-i", spec if spec.startswith(":") else f":{spec}"]
    return ["-f", backend, "-i", str(device)]


def audio_routing_status():
    """Installed virtual audio drivers, loopback tools, and DAW bridges."""
    hal_drivers = []
    if IS_MAC:
        hal_dir = Path("/Library/Audio/Plug-Ins/HAL")
        if hal_dir.is_dir():
            hal_drivers = sorted(p.name for p in hal_dir.glob("*.driver"))
    else:
        for daemon in ("pipewire", "pulseaudio", "jackd"):
            if shutil.which(daemon):
                hal_drivers.append(daemon)
    return {
        "audio_hijack": str(find_app("audio-hijack") or "") or None,
        "loopback_app": str(find_app("loopback") or "") or None,
        "reaper": str(find_app("reaper") or "") or None,
        "audacity": str(find_app("audacity") or "") or None,
        "switchaudio": which("SwitchAudioSource"),
        "hal_drivers": hal_drivers,
    }


def run_audio_hijack_script(script_path):
    """Execute an `.ahcommand` JavaScript file via Audio Hijack 4 on macOS."""
    if not IS_MAC:
        return False, "Audio Hijack is macOS-only; use PipeWire/Pulse loopback (`--device`) on Linux."
    app = find_app("audio-hijack")
    if not app:
        return False, "Audio Hijack.app is not installed in /Applications."
    try:
        proc = subprocess.run(
            ["open", "-g", "-a", "/Applications/Audio Hijack.app", str(script_path)],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout).strip() or "open -a Audio Hijack failed"
        return True, f"dispatched {Path(script_path).name} to Audio Hijack.app"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def demucs_cmd():
    """Invocation list for Demucs stem separation, or None when not installed."""
    venv_demucs = REPO_ROOT / ".venv/bin/demucs"
    if venv_demucs.is_file() and os.access(venv_demucs, os.X_OK):
        return [str(venv_demucs)]
    found = which("demucs")
    if found:
        return [found]
    return None


# ----------------------------------------------------------------- secrets ---
#
# Secrets never go in creative-stack.env or a dotfile. macOS has Keychain;
# Linux has libsecret via `secret-tool`. A headless box with no keyring daemon
# falls back to a 0600 file under the config dir, which is still outside the
# repo and still never committed.

SECRET_SERVICE = "creative-stack"
_SECRET_FILE = config_dir() / "secrets.env"


def secret_backend():
    if IS_MAC:
        return "keychain"
    if shutil.which("secret-tool"):
        return "secret-tool"
    return "file"


def secret_get(name):
    """Read a secret, or None. Never raises, never prints the value."""
    backend = secret_backend()
    try:
        if backend == "keychain":
            out = subprocess.run(
                ["security", "find-generic-password", "-a", os.environ.get("USER", ""),
                 "-s", f"{SECRET_SERVICE}-{name}", "-w"],
                capture_output=True, text=True, timeout=10)
            return out.stdout.strip() or None if out.returncode == 0 else None
        if backend == "secret-tool":
            out = subprocess.run(
                ["secret-tool", "lookup", "service", SECRET_SERVICE, "account", name],
                capture_output=True, text=True, timeout=10)
            return out.stdout.strip() or None if out.returncode == 0 else None
        if _SECRET_FILE.exists():
            for line in _SECRET_FILE.read_text().splitlines():
                key, _, value = line.partition("=")
                if key.strip() == name:
                    return value.strip().strip('"')
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def secret_set_hint(name, env_var):
    """The exact command to store a secret on *this* machine."""
    backend = secret_backend()
    if backend == "keychain":
        return (f'security add-generic-password -a "$USER" '
                f'-s {SECRET_SERVICE}-{name} -w',
                f'export {env_var}=$(security find-generic-password '
                f'-a "$USER" -s {SECRET_SERVICE}-{name} -w)')
    if backend == "secret-tool":
        return (f'secret-tool store --label="creative-stack {name}" '
                f'service {SECRET_SERVICE} account {name}',
                f'export {env_var}=$(secret-tool lookup '
                f'service {SECRET_SERVICE} account {name})')
    return (f'install -m 600 /dev/null {_SECRET_FILE} && '
            f'echo "{name}=<value>" >> {_SECRET_FILE}',
            f'export {env_var}=$(grep ^{name}= {_SECRET_FILE} | cut -d= -f2-)')


# ------------------------------------------------------------------- fonts ---

# Last-resort font paths per family, used only when fontconfig cannot answer.
# A Linux container with no fonts at all is a real situation, and PIL's built-in
# bitmap font is too small to label a screenshot with.
_FONT_FALLBACKS = {
    "macos": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "/System/Library/Fonts/SFNS.ttf",
              "/Library/Fonts/Roboto-Bold.ttf"],
    "linux": ["/usr/share/fonts/truetype/roboto/Roboto-Bold.ttf",
              "/usr/share/fonts/TTF/Roboto-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "/usr/share/fonts/noto/NotoSans-Bold.ttf"],
}


def font_file(family="Roboto", bold=True, strict=False):
    """Absolute path to a usable TTF/OTF, or None.

    fontconfig is the right answer on both platforms — it knows about user
    fonts, Homebrew casks and distro packages alike. The hardcoded list only
    catches machines where fontconfig is missing.

    fc-match never fails: ask for a font that is not installed and it hands
    back its best guess, which is how "Roboto Mono" quietly becomes Andale
    Mono. Pass strict=True when a near-miss is worse than nothing — a caller
    that can fall back to a renderer's own bundled font wants to know.
    """
    query = f"{family}:style={'Bold' if bold else 'Regular'}"
    if shutil.which("fc-match"):
        try:
            out = subprocess.run(["fc-match", "-f", "%{family}\t%{file}", query],
                                 capture_output=True, text=True, timeout=10)
            got, _, path = out.stdout.strip().partition("\t")
            if path and Path(path).exists():
                if not strict:
                    return path
                # fc-match reports families as a comma-separated list.
                wanted = family.replace(" ", "").lower()
                if any(name.replace(" ", "").lower() == wanted
                       for name in got.split(",")):
                    return path
                return None
        except (OSError, subprocess.SubprocessError):
            pass
    if strict:
        return None
    for path in _FONT_FALLBACKS["macos" if IS_MAC else "linux"]:
        if Path(path).exists():
            return path
    for directory in (user_font_dir(),):
        matches = sorted(directory.glob("*.ttf")) + sorted(directory.glob("*.otf"))
        if matches:
            return str(matches[0])
    return None


def whisper_hint():
    return {"macos": "brew install whisper.cpp",
            "arch": "paru -S whisper.cpp",
            "debian": "bash scripts/install/whisper-cpp.sh"}.get(
                FAMILY, "build whisper.cpp from source")


def package_hint(pkg_id):
    """The command that installs `pkg_id` on *this* box.

    Skills tell you what is missing often enough that hardcoding three package
    managers into every error message is a maintenance tax. The manifest
    already knows the per-family name, so read it from there and fall back to
    the bootstrap script when the package needs a non-native route.
    """
    generic = "run scripts/bootstrap.sh"
    try:
        manifest = json.loads(
            (REPO_ROOT / "packages/manifest.json").read_text())
    except (OSError, ValueError):
        return generic

    everything = [pkg for group in manifest.get("groups", [])
                  for pkg in group.get("packages", [])]
    for pkg in everything:
        if pkg.get("id") != pkg_id:
            continue
        name = pkg.get(FAMILY if FAMILY != "macos" else "brew")
        if not name:
            return generic
        # A cask is a different subcommand, and `brew install <cask-name>`
        # fails outright for the GUI apps — reaper, libreoffice, mactex-no-gui
        # — rather than falling through to the cask tap.
        brew = "brew install --cask" if pkg.get("cask") else "brew install"
        prefix = {"macos": brew,
                  "arch": f"{'sudo ' if os.geteuid() else ''}pacman -S",
                  "debian": f"{'sudo ' if os.geteuid() else ''}apt install",
                  }.get(FAMILY)
        return f"{prefix} {name}" if prefix else generic
    return generic


def font_install_hint(family):
    return {"macos": f"brew install --cask font-{family.lower().replace(' ', '-')}",
            "arch": "sudo pacman -S ttf-roboto noto-fonts",
            "debian": "sudo apt install fonts-roboto fonts-noto"}.get(
                FAMILY, "install a TTF font")


# -------------------------------------------------------------------- misc ---

def describe():
    api, _, studio = resolve_paths()
    return {
        "platform": pretty_name(),
        "family": FAMILY,
        "package_manager": PACKAGE_MANAGER,
        "aur_helper": aur_helper(),
        "has_display": has_display(),
        "secret_backend": secret_backend(),
        "ffmpeg": ffmpeg_bin(),
        "chrome": chrome_path(),
        "bin_dir": str(bin_dir()),
        "resolve": {"installed": api.exists(), "studio": studio},
        "apps": {n: str(find_app(n) or "") for n in APP_BRIDGES},
    }


# ------------------------------------------------------------ path repair ---

def augment_path():
    """Put our install directories on PATH for this process and its children.

    Importing a module should not normally mutate the environment, and this one
    does it deliberately. bootstrap installs into ~/.cargo/bin, ~/go/bin and
    ~/.local/bin, but a python process started before those existed has a stale
    PATH — so `cargo install oxipng` fails with ENOENT one line after rustup
    reported success, and a verification pass calls a tool missing when it is
    sitting right there. Child processes inherit os.environ, so fixing it once
    here fixes it for every subprocess and every `sh -c` check too.

    Returns the directories that were added.
    """
    current = os.environ.get("PATH", "").split(os.pathsep)
    added = [str(d) for d in install_dirs()
             if d.is_dir() and str(d) not in current]
    if added:
        os.environ["PATH"] = os.pathsep.join(added + current)
    return added


_PATH_ADDED = augment_path()


def main():
    if "--json" in sys.argv:
        print(json.dumps(describe(), indent=2))
        return 0
    info = describe()
    print(f"platform         {info['platform']}")
    print(f"family           {info['family']}  (pm: {info['package_manager']}"
          + (f", aur: {info['aur_helper']}" if info["aur_helper"] else "") + ")")
    print(f"display          {'yes' if info['has_display'] else 'no (headless)'}")
    print(f"secrets          {info['secret_backend']}")
    print(f"ffmpeg           {info['ffmpeg'] or 'NOT FOUND'}")
    print(f"chrome           {info['chrome'] or 'NOT FOUND'}")
    if _PATH_ADDED:
        print(f"path added       {', '.join(_PATH_ADDED)}")
    for name, path in info["apps"].items():
        print(f"  {name:<14} {path or '—'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
