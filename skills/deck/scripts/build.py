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

"""Render a Marp deck to HTML, PDF, PPTX or PNG with the brand theme applied.

    python3 build.py -i deck.md -o out/deck.pdf
    python3 build.py -i deck.md -o out/deck.pptx --editable
    python3 build.py -i deck.md -o out/slides/      # one PNG per slide

The theme is regenerated from brand/tokens.json whenever the tokens change, so
there is no "rebuild the theme first" step to forget.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import theme  # noqa: E402

sys.path.insert(0, str(theme.REPO_ROOT / "scripts"))
import hostenv  # noqa: E402

FORMATS = {".html": ["--html"], ".pdf": ["--pdf"], ".pptx": ["--pptx"],
           ".png": ["--images", "png"], ".jpg": ["--images", "jpeg"]}


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, help="Marp markdown deck")
    ap.add_argument("--output", "-o", required=True,
                    help="output file; a trailing / means per-slide PNGs")
    ap.add_argument("--profile", default=None, help="brand profile")
    ap.add_argument("--editable", action="store_true",
                    help="PPTX with real text boxes instead of slide images")
    ap.add_argument("--notes", action="store_true",
                    help="include speaker notes (PDF only)")
    ap.add_argument("--watch", "-w", action="store_true", help="rebuild on save")
    args = ap.parse_args()

    marp = shutil.which("marp")
    if not marp:
        die("marp not found. Run: npm install -g @marp-team/marp-cli")

    source = Path(args.input)
    if not source.exists():
        die(f"deck not found: {source}")

    theme_dir, css = theme.ensure(args.profile)
    name = css.stem.replace("creative-", "")

    output = Path(args.output)
    is_dir = args.output.endswith("/") or (output.exists() and output.is_dir())
    suffix = ".png" if is_dir else output.suffix.lower()
    if suffix not in FORMATS:
        die(f"unsupported output '{suffix}'. Use one of: "
            f"{', '.join(sorted(FORMATS))}, or a directory for per-slide PNGs")

    cmd = [marp, str(source), "--theme-set", str(theme_dir),
           "--allow-local-files", *FORMATS[suffix]]
    soffice = None
    if suffix == ".pptx" and args.editable:
        # marp shells out to LibreOffice for editable PPTX and only says
        # "soffice binary could not be found" after it has already rendered
        # every slide — a minute of work thrown away for a missing package.
        soffice = hostenv.find_app("soffice")
        if not soffice:
            die("--editable needs LibreOffice: marp converts through its "
                f"soffice binary. {hostenv.package_hint('libreoffice')}. "
                "Without it, drop --editable for a PPTX of slide images.")
        cmd.append("--pptx-editable")
    if suffix == ".pdf" and args.notes:
        cmd.append("--pdf-notes")
    if args.watch:
        cmd.append("--watch")

    if is_dir:
        output.mkdir(parents=True, exist_ok=True)
        cmd += ["-o", str(output / f"{source.stem}.png")]
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["-o", str(output)]

    env = dict(os.environ)
    if soffice:
        # The macOS cask leaves soffice inside the .app bundle, off PATH, and
        # marp's converter only ever looks on PATH.
        env["PATH"] = f"{soffice.parent}{os.pathsep}{env.get('PATH', '')}"
    if suffix != ".html":
        chrome = hostenv.chrome_path()
        if not chrome:
            die("no Chrome/Chromium found — marp needs one for PDF, PPTX and "
                f"PNG export. {hostenv.chrome_install_hint()}, or set CHROME_PATH.")
        env["CHROME_PATH"] = chrome
        # marp runs Chrome as a child; on a headless Linux box without a
        # sandbox-capable kernel config it will not start otherwise.
        if not hostenv.IS_MAC:
            env.setdefault("CHROME_NO_SANDBOX", "true")

    print(f"🎞  building {source.name} → {suffix[1:].upper()}  (theme: {name})")
    proc = subprocess.run(cmd, env=env, capture_output=not args.watch, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-10:]
        die("marp failed:\n  " + "\n  ".join(detail))

    if is_dir:
        pngs = sorted(output.glob(f"{source.stem}*.png"))
        print(f"✓ {output}/  ({len(pngs)} slides)")
    else:
        print(f"✓ {output}  ({output.stat().st_size // 1024}KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
