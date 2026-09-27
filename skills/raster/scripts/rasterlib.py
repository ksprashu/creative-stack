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

"""Shared helpers for the /raster scripts.

Two things every raster script needs and neither should re-solve: reaching
ImageMagick across the 6/7 split, and knowing which of vips or magick is the
right tool for a given job.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("CREATIVE_STACK_ROOT",
                                Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import hostenv  # noqa: E402

# Formats we will write. Anything else is a typo, not an intention.
RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".tif", ".tiff", ".heic"}


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def magick(verb=None):
    """ImageMagick as an argv prefix, or exit with an install hint.

    IM7 is `magick convert …`; IM6 (still current on Ubuntu 24.04) has separate
    `convert` / `identify` / `composite` binaries and no `magick` at all.
    """
    cmd = hostenv.magick_cmd(verb)
    if not cmd:
        die(f"ImageMagick not found. {install_hint('imagemagick')}")
    return cmd


def have(binary):
    return hostenv.which(binary) is not None


def install_hint(package):
    return {
        "macos": f"brew install {package}",
        "arch": f"sudo pacman -S {package}",
        "debian": f"sudo apt install {package}",
    }.get(hostenv.FAMILY, f"install {package}")


def run(cmd, what):
    """Run a converter and turn its failure into one readable line.

    ImageMagick's stderr is verbose and mostly about its own internals; the
    caller wants to know which file broke and why, not the coder name.
    """
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        die(f"{what} failed: {detail[-1] if detail else 'no output'}")
    return proc


def probe(path):
    """(width, height, format) for an image, via vips if present else magick."""
    if have("vipsheader"):
        out = subprocess.run(["vipsheader", "-a", str(path)],
                             capture_output=True, text=True)
        if out.returncode == 0:
            fields = {}
            for line in out.stdout.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    fields[key.strip()] = value.strip()
            try:
                return (int(fields["width"]), int(fields["height"]),
                        fields.get("vips-loader", "").replace("load", ""))
            except (KeyError, ValueError):
                pass
    out = subprocess.run(magick("identify") + ["-format", "%w %h %m", str(path)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        die(f"cannot read {path}")
    width, height, fmt = out.stdout.split()[:3]
    return int(width), int(height), fmt.lower()


def inputs(paths, recursive=False):
    """Expand files, globs and directories into a sorted list of images."""
    found = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            walk = path.rglob("*") if recursive else path.glob("*")
            found += [p for p in walk if p.suffix.lower() in RASTER_SUFFIXES]
        elif any(ch in raw for ch in "*?["):
            found += [p for p in Path().glob(raw)
                      if p.suffix.lower() in RASTER_SUFFIXES]
        elif path.exists():
            found.append(path)
        else:
            die(f"no such file: {raw}")
    if not found:
        die("no images matched")
    return sorted(set(found))


def human(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f}{unit}" if unit == "B" else f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}GB"
