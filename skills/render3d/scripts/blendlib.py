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

"""Host side of /render3d: find Blender, hand it a job, report what happened.

Blender's Python runs in Blender's own interpreter, so none of this repo's
modules are importable there. The split is deliberate:

    blendlib.py      runs here. Resolves brand tokens, validates arguments,
                     builds a JSON job and launches Blender.
    _inblender.py    runs there. Reads the JSON from the environment, uses
                     `bpy`, imports nothing from this repo.

Everything crossing that boundary is plain JSON, which also means a job can be
dumped, inspected and re-run without this wrapper.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("CREATIVE_STACK_ROOT",
                                Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import hostenv  # noqa: E402

GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"

WORKER = Path(__file__).resolve().parent / "_inblender.py"
STUDIO_BLEND = REPO_ROOT / "templates/blender/studio.blend"

# What Blender can import without an add-on being enabled first.
MODEL_SUFFIXES = {".blend", ".obj", ".gltf", ".glb", ".fbx", ".stl", ".ply",
                  ".usd", ".usdc", ".usda", ".usdz", ".dae", ".abc", ".svg"}

# Blender prints a wall of version banner and progress lines; these are the
# ones worth showing when something goes wrong.
NOISE_RE = re.compile(r"^(Blender \d|Read prefs|Fra:|Saved:|Info: |\s*$)")


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def blender_bin():
    found = hostenv.find_app("blender") or hostenv.which("blender")
    if not found:
        die("Blender not found. Run scripts/install_bridges.py, or install it: "
            + {"macos": "brew install --cask blender",
               "arch": "sudo pacman -S blender",
               "debian": "sudo apt install blender"}.get(
                   hostenv.FAMILY, "see blender.org"))
    return str(found)


def blender_version():
    out = subprocess.run([blender_bin(), "--version"], capture_output=True,
                         text=True).stdout
    match = re.search(r"Blender (\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def palette(tokens, mode="light"):
    """Brand colours for the render, as hex — _inblender converts to linear."""
    colours = tokens.get("color", {})
    scheme = colours.get(mode, {})
    accents = colours.get("brand", {})
    return {
        "bg": scheme.get("bg", "#FFFFFF"),
        "surface": scheme.get("surface", "#F8F9FA"),
        "accent": scheme.get("accent", "#1A73E8"),
        "text": scheme.get("text", "#202124"),
        "blue": accents.get("blue", "#4285F4"),
        "red": accents.get("red", "#EA4335"),
        "yellow": accents.get("yellow", "#FBBC04"),
        "green": accents.get("green", "#34A853"),
    }


def gradient_stops(tokens, name):
    """The hex stops of a named brand gradient, or None.

    Brand tokens store gradients as the CSS string the deck and frame templates
    use verbatim, so the same definition of "hero" drives the 3D world
    background as drives an HTML backdrop.
    """
    css = (tokens.get("gradient") or {}).get(name)
    if not css:
        return None
    stops = re.findall(r"#(?:[0-9a-fA-F]{3}){1,2}\b", css)
    return stops or None


def resolve_model(spec):
    path = Path(spec)
    if not path.is_file():
        die(f"no such model: {spec}")
    if path.suffix.lower() not in MODEL_SUFFIXES:
        die(f"Blender will not import {path.suffix} out of the box. Known: "
            f"{', '.join(sorted(MODEL_SUFFIXES))}")
    return path.resolve()


def run_job(job, label, verbose=False, background_blend=None):
    """Launch Blender on `job` and stream the interesting half of its output.

    The job goes through an environment variable rather than argv: Blender
    forwards everything after `--` to the script unmangled in theory, but a
    3KB JSON blob with quotes in it is exactly the sort of thing a shell,
    a wrapper script or Blender's own arg parser mangles in practice.
    """
    env = dict(os.environ, CREATIVE_STACK_JOB=json.dumps(job))
    cmd = [blender_bin(), "--background"]
    if background_blend:
        cmd.append(str(background_blend))
    cmd += ["--factory-startup", "--python-exit-code", "1",
            "--python", str(WORKER)]

    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")

    if verbose:
        print(output)
    if result.returncode != 0:
        lines = [ln for ln in output.splitlines() if not NOISE_RE.match(ln)]
        die(f"{label} failed in Blender:\n    " + "\n    ".join(lines[-12:]))

    for line in output.splitlines():
        if line.startswith("CS:"):
            print("  " + line[3:].strip())
    return output


def human(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size / 1:.0f}{unit}"
        size /= 1024
    return f"{size:.0f}GB"


def report(path, note=""):
    if not Path(path).exists():
        die(f"Blender reported success but wrote no {path}")
    size = Path(path).stat().st_size
    suffix = f"  {DIM}{note}{RESET}" if note else ""
    print(f"  {GREEN}✓{RESET} {path}  {DIM}{size // 1024 or 1}KB{RESET}{suffix}")


VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".gif"}


def backdrop_png(dst, width, height, colour, stops=None):
    """Render the brand background to a PNG for the frames to sit on.

    Blender always renders onto transparent film here, because a shadow
    catcher only produces a shadow when there is nothing behind it. That
    leaves the backdrop to be put in afterwards — and doing it with
    ImageMagick rather than Blender's compositor is both simpler and more
    portable: the compositor was rebuilt between Blender 4 and 5, which
    dropped `scene.node_tree` and the Composite node outright.
    """
    base = hostenv.magick_cmd()
    if not base:
        die(f"ImageMagick not found. {hostenv.package_hint('imagemagick')}")
    if stops and len(stops) >= 2:
        source = f"gradient:{stops[0]}-{stops[-1]}"
    else:
        source = f"xc:{colour}"
    # The PNG24: prefix is not optional. ImageMagick notices a flat white (or
    # any neutral) canvas has no colour in it and writes a Grayscale PNG, and
    # both `-composite` and ffmpeg's `overlay` take their pixel format from
    # the first input — so the backdrop silently desaturates the render that
    # lands on top of it. `-type TrueColor` does not override the encoder's
    # decision here; the explicit coder does.
    subprocess.run(base + ["-size", f"{width}x{height}", source,
                           f"PNG24:{dst}"],
                   check=True, capture_output=True)
    return dst


def flatten(render, backdrop, dst):
    """Composite one transparent render over the backdrop."""
    base = hostenv.magick_cmd()
    result = subprocess.run(
        base + [str(backdrop), str(render), "-composite",
                "-alpha", "remove", "-alpha", "off", str(dst)],
        capture_output=True, text=True)
    if result.returncode != 0:
        die("compositing the backdrop failed:\n    "
              + (result.stderr or "").strip())


def mux(frames_dir, stem, dst, fps, backdrop=None):
    """PNG sequence into a video, with the encoder settings /vidkit uses.

    `backdrop` is laid under every frame in the same pass — 90 frames is 90
    ImageMagick processes otherwise, and ffmpeg is already reading them all.
    """
    ffmpeg = hostenv.ffmpeg_bin()
    if not ffmpeg:
        die(f"ffmpeg not found — the frames are in {frames_dir}. "
              f"{hostenv.ffmpeg_install_hint()}")

    pattern = str(frames_dir / f"{stem}%04d.png")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    if backdrop:
        cmd += ["-loop", "1", "-framerate", str(fps), "-i", str(backdrop)]
    cmd += ["-framerate", str(fps), "-i", pattern]

    chain, tail = [], f"[{1 if backdrop else 0}:v]"
    if backdrop:
        # shortest=1 so the looping still image stops when the frames run out.
        chain.append("[0:v][1:v]overlay=shortest=1[flat]")
        tail = "[flat]"

    if dst.suffix.lower() == ".gif":
        # One shared palette for the whole clip: the default per-frame palette
        # makes a turntable shimmer as the quantiser changes its mind.
        chain.append(f"{tail}split[a][b];[a]palettegen=stats_mode=diff[p];"
                     f"[b][p]paletteuse=dither=bayer:bayer_scale=3[v]")
        cmd += ["-loop", "0"]
    elif dst.suffix.lower() == ".webm":
        chain.append(f"{tail}null[v]")
        cmd += ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0",
                # VP9 is the only target here that can carry the alpha through,
                # so a transparent turntable stays transparent.
                "-pix_fmt", "yuv420p" if backdrop else "yuva420p"]
    else:
        # A turntable is usually going into a slide or a page at an arbitrary
        # size, and h264 refuses odd dimensions.
        chain.append(f"{tail}pad=ceil(iw/2)*2:ceil(ih/2)*2[v]")
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "slow",
                "-pix_fmt", "yuv420p"]

    cmd += ["-filter_complex", ";".join(chain), "-map", "[v]", str(dst)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        die("ffmpeg mux failed:\n    "
              + "\n    ".join((result.stderr or "").strip().splitlines()[-6:]))


def ensure_studio(quiet=False):
    """Build templates/blender/studio.blend on first use.

    The rig is generated rather than committed: a .blend is an opaque binary
    that cannot be reviewed in a diff, and regenerating it takes a second.
    """
    if STUDIO_BLEND.exists():
        return STUDIO_BLEND
    if not quiet:
        print(f"  {DIM}building the studio rig (first run){RESET}")
    STUDIO_BLEND.parent.mkdir(parents=True, exist_ok=True)
    run_job({"action": "build_studio", "output": str(STUDIO_BLEND)},
            "studio build")
    if not STUDIO_BLEND.exists():
        die("the studio rig did not get written — run scripts/studio.py to see why")
    return STUDIO_BLEND
