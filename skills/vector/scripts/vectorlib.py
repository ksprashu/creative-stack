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

"""Shared plumbing for the /vector scripts.

Small on purpose: tool lookup, subprocess handling with a readable failure, and
the one piece of SVG knowledge every script here needs — that svgo will happily
delete the attribute the file depends on.
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import hostenv  # noqa: E402

GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"

VIEWBOX_RE = re.compile(r'\bviewBox\s*=\s*"([^"]+)"')
SVG_OPEN_RE = re.compile(r"<svg\b", re.I)


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def need(binary, package_id=None):
    """Resolve a binary or explain, in this machine's terms, how to get it."""
    found = hostenv.which(binary)
    if not found:
        die(f"{binary} not found. {hostenv.package_hint(package_id or binary)}")
    return found


def run(cmd, what, capture=False):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        die(f"{what} failed:\n    " + "\n    ".join(detail[-6:]))
    return result.stdout if capture else None


def magick(verb=None):
    cmd = hostenv.magick_cmd(verb)
    if not cmd:
        die("ImageMagick not found. "
            f"{hostenv.package_hint('imagemagick')}")
    return cmd


def svgo_optimise(src, dst, precision=3, required=False):
    """svgo `src` into `dst`, keeping the viewBox whatever svgo's version wants.

    `removeViewBox` sits in svgo 3's default preset and was taken out of
    svgo 4's, and the two disagree about the CLI flag that would turn it off.
    Rather than branch on a version string, run the default preset and put the
    attribute back if it went missing: an SVG without a viewBox cannot scale,
    which turns every icon in a `srcset` or a sprite into its intrinsic size or
    into nothing at all.

    Returns a "12KB → 8KB" string, or None when svgo is not installed and the
    caller said it was optional.
    """
    tool = hostenv.which("svgo")
    if not tool:
        if required:
            die(f"svgo not found. {hostenv.package_hint('svgo')}")
        return None

    original = src.read_text(errors="replace")
    before = len(original.encode())
    run([tool, "--quiet", "--precision", str(precision),
         str(src), "-o", str(dst)], "svgo")

    text = dst.read_text(errors="replace")
    if not VIEWBOX_RE.search(text):
        viewbox = VIEWBOX_RE.search(original)
        if viewbox:
            text = SVG_OPEN_RE.sub(f'<svg viewBox="{viewbox.group(1)}"',
                                   text, count=1)
            dst.write_text(text)

    after = dst.stat().st_size
    return f"{before // 1024 or 1}KB → {after // 1024 or 1}KB"


def svg_sources(inputs, recursive):
    """Resolve files, globs and directories to a sorted list of SVGs."""
    found = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            found += sorted(path.rglob("*.svg") if recursive
                            else path.glob("*.svg"))
        elif any(ch in item for ch in "*?["):
            found += sorted(Path().glob(item))
        elif path.is_file():
            found.append(path)
        else:
            die(f"no such file or directory: {item}")
    if not found:
        die("no SVG files matched the input")
    return found
