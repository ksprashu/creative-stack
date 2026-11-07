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

"""Creative Stack health check.

Verifies the whole chain: CLI tools, headless app bridges, credentials, MCP
registration and skill symlinks. Exits non-zero only on HARD failures, so it is
safe to use as a CI/bootstrap gate.

    python3 scripts/doctor.py            # full report
    python3 scripts/doctor.py --quick    # skip the smoke tests and network probes
    python3 scripts/doctor.py --json     # machine-readable
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hostenv  # noqa: E402
from sync_skills import skill_sources  # noqa: E402

REPO_ROOT = hostenv.REPO_ROOT

# Tools whose absence breaks a core workflow. Everything else is a soft warning.
HARD_REQUIRED = {"ffmpeg", "ffprobe", "magick", "rsvg-convert", "pandoc"}

# An "a|b" entry is satisfied by either binary, and is how the manifest's
# `provides` lists look from this side. Distros disagree about names: Ubuntu
# 24.04 ships ImageMagick 6, which has `convert` and no `magick` at all; Debian
# renames bat to batcat; and a Linux box has `grim` (Wayland) or `maim` (X11)
# but never both. Demanding one exact spelling is how a perfectly healthy
# machine gets reported as broken — it was the only hard failure on Ubuntu.
TOOLS = {
    "raster": ["magick|convert", "vips", "exiftool", "gifski", "oxipng",
               "pngquant", "cwebp"],
    "vector": ["rsvg-convert", "resvg", "potrace", "svgo", "fontforge"],
    "diagram": ["dot", "d2", "plantuml", "mmdc"],
    "document": ["pandoc", "gs", "pdftoppm", "qpdf", "mutool", "typst", "tectonic"],
    "slides": ["marp", "presenterm"],
    "video": ["ffmpeg", "ffprobe", "HandBrakeCLI", "mkvmerge", "yt-dlp", "auto-editor"],
    "capture": ["vhs", "asciinema", "agg", "silicon", "termshot", "freeze", "anot8"],
    "audio": ["sox", "rubberband", "lame", "flac", "fluidsynth", "lilypond", "csound"],
    "speech": ["whisper-cli", "tesseract"],
    "animation": ["manim", "latex"],
    "util": ["jq", "gh", "bat", "chafa"],
}

if not hostenv.IS_MAC:
    TOOLS["util"] = ["jq", "gh", "bat|batcat", "chafa"]
    # No macOS equivalent — screencapture is built in there.
    TOOLS["capture"] = TOOLS["capture"] + ["grim|maim"]

# Filters /vidkit depends on. Their absence is the difference between "captions
# burn" and a cryptic "No such filter: 'ass'" halfway through a render. On macOS
# they only exist in the keg-only ffmpeg-full; hostenv resolves that.
REQUIRED_FILTERS = ["ass", "subtitles", "drawtext", "zoompan", "loudnorm",
                    "silencedetect"]

# Headless entry points for GUI apps. Resolved by install_bridges.py; the
# candidate paths per platform live in hostenv.APP_BRIDGES.
# Keep find_app's None rather than coercing it to "": Path("") is Path("."),
# which exists, so a box without Blender reported the app as present and then
# failed its smoke test with `Permission denied: ''`.
BRIDGES = {name: hostenv.find_app(name)
           for name in ("blender", "inkscape", "gimp-console")}

RESOLVE_API, RESOLVE_LIB, RESOLVE_IS_STUDIO = hostenv.resolve_paths()
RESOLVE_MODULES = RESOLVE_API / "Modules"


GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


class Report:
    """Collects results and tracks whether anything hard-failed."""

    def __init__(self):
        self.sections = {}
        self.hard_failures = []

    def add(self, section, name, status, detail=""):
        self.sections.setdefault(section, []).append(
            {"name": name, "status": status, "detail": detail}
        )
        if status == "fail":
            self.hard_failures.append(f"{section}/{name}: {detail}")

    def render(self):
        for section, rows in self.sections.items():
            print(f"\n{DIM}── {section} {'─' * max(0, 58 - len(section))}{RESET}")
            for r in rows:
                mark = {"ok": f"{GREEN}✓{RESET}", "warn": f"{YELLOW}!{RESET}",
                        "fail": f"{RED}✗{RESET}", "skip": f"{DIM}·{RESET}"}[r["status"]]
                detail = f"  {DIM}{r['detail']}{RESET}" if r["detail"] else ""
                print(f"  {mark} {r['name']:<22}{detail}")


def run(cmd, timeout=30, **kw):
    """Run a command, never raise. Returns (returncode, combined output)."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, **kw
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 1, str(exc)


def version_of(tool):
    """Best-effort one-line version string."""
    for flag in ("--version", "-version", "version", "-V"):
        rc, out = run([tool, flag], timeout=15)
        if rc == 0 and out:
            return out.splitlines()[0][:52]
    return ""


def check_platform(rep):
    """Lead with what machine we think this is — every other check depends on
    getting this right, so a wrong guess should be visible immediately."""
    rep.add("platform", "os", "ok", hostenv.pretty_name())
    pm = hostenv.PACKAGE_MANAGER
    rep.add("platform", "package manager", "ok" if pm else "fail",
            pm or "no brew/pacman/apt found")
    if hostenv.FAMILY == "arch":
        helper = hostenv.aur_helper()
        rep.add("platform", "AUR helper", "ok" if helper else "warn",
                helper or "none — AUR packages fall back to cargo/go/pip")
    rep.add("platform", "display", "ok" if hostenv.has_display() else "warn",
            "available" if hostenv.has_display()
            else "headless — GUI MCP servers unavailable, CLI path only")
    rep.add("platform", "secret store", "ok", hostenv.secret_backend())
    ffmpeg = hostenv.ffmpeg_bin()
    rep.add("platform", "ffmpeg", "ok" if ffmpeg else "fail",
            ffmpeg or hostenv.ffmpeg_install_hint())
    chrome = hostenv.chrome_path()
    rep.add("platform", "chrome", "ok" if chrome else "warn",
            chrome or hostenv.chrome_install_hint())


def check_tools(rep):
    for section, tools in TOOLS.items():
        for entry in tools:
            alts = entry.split("|")
            # hostenv.which, not shutil.which: it also searches the directories
            # bootstrap installs into, so a tool installed minutes ago by the
            # cargo/go/source route is not reported missing for want of a
            # re-login.
            found = next(filter(None, (hostenv.which(a) for a in alts)), None)
            label = alts[0]
            if found:
                detail = version_of(found)
                if Path(found).name != label:
                    detail = f"{Path(found).name}: {detail}".rstrip(": ")
                rep.add(f"tools:{section}", label, "ok", detail)
            else:
                status = "fail" if any(a in HARD_REQUIRED for a in alts) else "warn"
                missing = ("not installed" if len(alts) == 1
                           else f"not installed (tried {', '.join(alts)})")
                rep.add(f"tools:{section}", label, status, missing)


def check_bridges(rep, quick):
    for name, path in BRIDGES.items():
        if path is None:
            rep.add("bridges", name, "warn", "app not installed")
            continue
        linked = hostenv.which(name)
        rep.add("bridges", name, "ok" if linked else "warn",
                linked or f"{path} present but not linked into PATH")

    if quick:
        rep.add("bridges", "smoke tests", "skip", "--quick")
        return

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _smoke_blender(rep, td)
        _smoke_inkscape(rep, td)
        _smoke_gimp(rep, td)
        _smoke_ffmpeg(rep, td)
        _smoke_fluidsynth(rep, td)


def _produced(path):
    return path.exists() and path.stat().st_size > 0


def _smoke_blender(rep, td):
    exe = BRIDGES["blender"]
    if exe is None:
        return rep.add("smoke", "blender render", "skip", "Blender not installed")
    exe = str(exe)
    out = td / "blender.png"
    expr = (
        "import bpy;"
        "s=bpy.context.scene;"
        "s.render.resolution_x=64;s.render.resolution_y=64;"
        f"s.render.filepath=r'{out}';"
        "bpy.ops.render.render(write_still=True)"
    )
    rc, msg = run([exe, "--background", "--factory-startup",
                   "--python-expr", expr], timeout=180)
    if _produced(out):
        rep.add("smoke", "blender render", "ok", "64x64 PNG written")
    else:
        rep.add("smoke", "blender render", "warn", msg.splitlines()[-1][:60] if msg else "no output")


def _smoke_inkscape(rep, td):
    exe = BRIDGES["inkscape"]
    if exe is None:
        return rep.add("smoke", "inkscape export", "skip", "Inkscape not installed")
    exe = str(exe)
    svg = td / "in.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
        '<circle cx="32" cy="32" r="30" fill="#4285F4"/></svg>'
    )
    out = td / "ink.png"
    rc, msg = run([exe, str(svg), "--export-type=png",
                   f"--export-filename={out}"], timeout=120)
    rep.add("smoke", "inkscape export", "ok" if _produced(out) else "warn",
            "SVG→PNG ok" if _produced(out) else msg[:60])


def _smoke_gimp(rep, td):
    exe = BRIDGES["gimp-console"]
    if exe is None:
        return rep.add("smoke", "gimp batch", "skip", "GIMP not installed")
    exe = str(exe)
    # GIMP 3.x: Script-Fu is the stable batch surface across point releases.
    # 3.x moved the layer name before the size and dropped the drawable from
    # gimp-file-save; 3.2 also ignores (gimp-quit 0) and idles in the
    # background, so only --quit ends the process.
    out = td / "gimp.png"
    script = (
        '(let* ((img (car (gimp-image-new 64 64 RGB)))'
        '       (layer (car (gimp-layer-new img "l" 64 64 RGB-IMAGE 100 LAYER-MODE-NORMAL))))'
        '  (gimp-image-insert-layer img layer 0 0)'
        '  (gimp-context-set-background \'(66 133 244))'
        '  (gimp-drawable-fill layer FILL-BACKGROUND)'
        f'  (gimp-file-save RUN-NONINTERACTIVE img "{out}" -1))'
    )
    rc, msg = run([exe, "-idf", "--quit", "--batch-interpreter",
                   "plug-in-script-fu-eval", "-b", script], timeout=120)
    rep.add("smoke", "gimp batch", "ok" if _produced(out) else "warn",
            "Script-Fu render ok" if _produced(out) else msg[:60])


def _smoke_ffmpeg(rep, td):
    if not hostenv.which("ffmpeg"):
        return rep.add("smoke", "ffmpeg encode", "fail", "ffmpeg missing")
    out = td / "bars.mp4"
    rc, msg = run([hostenv.ffmpeg_bin("ffmpeg"), "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                   "-t", "1", "-pix_fmt", "yuv420p", str(out)], timeout=60)
    rep.add("smoke", "ffmpeg encode", "ok" if _produced(out) else "fail",
            "1s test clip" if _produced(out) else msg[-60:])
    _check_ffmpeg_filters(rep)


def _check_ffmpeg_filters(rep):
    """/vidkit needs libass and freetype. Distro ffmpeg has them; Homebrew's
    plain formula does not, which is what ffmpeg-full is for."""
    exe = hostenv.ffmpeg_bin("ffmpeg")
    if not exe:
        return
    rc, out = run([exe, "-hide_banner", "-filters"], timeout=30)
    missing = [f for f in REQUIRED_FILTERS
               if not re.search(rf"^\s*\S+\s+{f}\s", out, re.M)]
    label = Path(exe).parent.parent.name if "ffmpeg-full" in exe else "ffmpeg"
    if missing:
        rep.add("smoke", f"{label} filters", "fail",
                f"missing {', '.join(missing)} — {hostenv.ffmpeg_install_hint()}")
    else:
        rep.add("smoke", f"{label} filters", "ok",
                f"{len(REQUIRED_FILTERS)} required filters present")


def _smoke_fluidsynth(rep, td):
    if not hostenv.which("fluidsynth"):
        return rep.add("smoke", "fluidsynth render", "skip", "fluidsynth missing")
    sf = hostenv.soundfont()
    if not sf:
        return rep.add("smoke", "fluidsynth render", "warn",
                       "no SoundFont — run install_bridges.py")
    try:
        import mido
    except ImportError:
        return rep.add("smoke", "fluidsynth render", "skip", "mido not installed")
    mid = td / "t.mid"
    f = mido.MidiFile()
    tr = mido.MidiTrack()
    f.tracks.append(tr)
    for n in (60, 64, 67, 72):
        tr.append(mido.Message("note_on", note=n, velocity=80, time=0))
        tr.append(mido.Message("note_off", note=n, velocity=0, time=480))
    f.save(mid)
    out = td / "t.wav"
    rc, msg = run(["fluidsynth", "-ni", "-F", str(out), "-r", "44100", str(sf), str(mid)],
                  timeout=60)
    rep.add("smoke", "fluidsynth render", "ok" if _produced(out) else "warn",
            "MIDI→WAV ok" if _produced(out) else msg[-60:])


def check_credentials(rep, quick):
    """Probe both AI backends. Vertex org policy may block specific models."""
    project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID") or os.environ.get(
        "GOOGLE_CLOUD_PROJECT")
    rep.add("credentials", "vertex project", "ok" if project else "warn",
            project or "ANTHROPIC_VERTEX_PROJECT_ID unset")

    rc, _ = run(["gcloud", "auth", "application-default", "print-access-token"], timeout=30)
    rep.add("credentials", "gcloud ADC", "ok" if rc == 0 else "warn",
            "token obtained" if rc == 0
            else "run: gcloud auth application-default login")

    # Same resolution order as /artgen, keyring included — otherwise the doctor
    # reports a missing key on a machine where the skills work perfectly well.
    key = (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
           or hostenv.secret_get("google-api-key"))
    rep.add("credentials", "AI Studio key", "ok" if key else "warn",
            "set" if key
            else "GOOGLE_API_KEY unset — /artgen uses Vertex only, with no "
                 "fallback when org policy blocks a model")

    if quick:
        return rep.add("models", "probes", "skip", "--quick")
    _probe_models(rep, project, key)
    _probe_chirp(rep, project)


def _probe_chirp(rep, project):
    """Chirp 3 HD is Cloud Text-to-Speech, not Vertex: its own API to enable,
    its own client library. Listing voices is free and proves both."""
    py = REPO_ROOT / ".venv/bin/python"
    if not project or not py.exists():
        return rep.add("models", "chirp 3 hd", "skip", "no project or no .venv")
    probe = ("from google.cloud import texttospeech as t; "
             "from google.api_core.client_options import ClientOptions as O; "
             f"c = t.TextToSpeechClient(client_options=O(quota_project_id={project!r})); "
             "print(sum('Chirp3-HD' in v.name for v in "
             "c.list_voices(language_code='en-US').voices))")
    rc, out = run([str(py), "-c", probe], timeout=60)
    voices = out.strip().splitlines()[-1] if rc == 0 and out.strip() else ""
    rep.add("models", "texttospeech:chirp3-hd",
            "ok" if voices.isdigit() and int(voices) else "warn",
            f"speech — {voices} en-US voices" if voices.isdigit()
            else "unreachable — gcloud services enable texttospeech.googleapis.com, "
                 "uv pip install -r requirements.txt")


def _probe_models(rep, project, key):
    """Report per-model reachability so org-policy blocks are explicit, not a
    surprise mid-render."""
    try:
        from google import genai
    except ImportError:
        return rep.add("models", "probe", "skip", "google-genai not installed")

    targets = [
        ("gemini-3.1-flash-image", "image"),
        ("gemini-3.8-flash", "vision"),
        ("gemini-omni-1.1-flash-preview", "video"),
        ("veo-3.1-generate-001", "video"),
        ("lyria-002", "music"),
        ("gemini-3.1-flash-tts-preview", "speech"),
    ]

    # Vertex serves these families from different places, and a model is
    # invisible from the wrong one — listing gemini-3.1-flash-image in
    # us-central1 succeeds while generating with it there returns 404. Probe
    # each family where /artgen will actually call it.
    sys.path.insert(0, str(REPO_ROOT / "skills/artgen/scripts"))
    try:
        import genailib
        location_for = genailib.vertex_location
    except ImportError:
        def location_for(_model):
            return os.environ.get("CLOUD_ML_REGION", "us-central1")

    listings = {}

    def available_at(backend, location):
        """Model names reachable on one backend/location, cached, never raising."""
        if (backend, location) in listings:
            return listings[(backend, location)]
        try:
            client = (genai.Client(vertexai=True, project=project,
                                   location=location) if backend == "vertex"
                      else genai.Client(api_key=key))
            names = {m.name.split("/")[-1] for m in client.models.list()}
        except Exception as exc:  # noqa: BLE001 - report, never crash the doctor
            rep.add("models", f"{backend} @{location}", "warn", str(exc)[:60])
            names = set()
        listings[(backend, location)] = names
        return names

    backends = [("vertex", True)] if project else []
    if key:
        backends.append(("aistudio", True))
    if not backends:
        return rep.add("models", "probe", "skip", "no usable backend configured")

    for backend, _ in backends:
        for model, kind in targets:
            location = location_for(model) if backend == "vertex" else "-"
            hit = model in available_at(backend, location)
            where = f" @{location}" if backend == "vertex" else ""
            rep.add("models", f"{backend}:{model}", "ok" if hit else "warn",
                    f"{kind}{where}" if hit else f"not exposed{where}")


def check_ml(rep):
    """SAM 2 is an opt-in extra (bootstrap --with-ml), so absence is a note,
    never a failure. Asked of the venv's interpreter, not this one: doctor is
    often run with the system python3, which never has torch."""
    py = REPO_ROOT / ".venv/bin/python"
    if not py.exists():
        return rep.add("ml", "SAM 2", "skip", "no .venv yet")
    probe = ("import importlib.util as u, sys; "
             "sys.exit(0 if all(u.find_spec(m) for m in "
             "('torch', 'torchvision', 'transformers')) else 1)")
    try:
        have = subprocess.run([str(py), "-c", probe], timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        have = False
    rep.add("ml", "SAM 2", "ok" if have else "skip",
            "torch + transformers — segment.py --refine sam2, video tracking"
            if have else "not installed — bootstrap.sh --with-ml to enable "
                         "segment.py --refine sam2")


def check_resolve(rep):
    if not RESOLVE_API.exists() and not hostenv.find_app("resolve"):
        return rep.add("resolve", "install", "skip", "not installed")

    # The external scripting API only works on Studio — free allows scripting
    # from Resolve's own console only. Blackmagic ships Linux as Studio-only,
    # so a Linux install implies scripting works.
    is_studio = RESOLVE_IS_STUDIO
    rep.add("resolve", "edition", "ok",
            "Studio — external scripting available" if is_studio
            else "Free — external scripting NOT available; /vidkit emits FCP7 XML")
    rep.add("resolve", "scripting modules",
            "ok" if RESOLVE_MODULES.exists() else "warn",
            str(RESOLVE_MODULES) if RESOLVE_MODULES.exists() else "missing")


def harnesses():
    """The harness table, or {} if it is unreadable.

    Unlike sync_skills.py, the doctor must never abort on a bad table — its job
    is to report problems, and refusing to run is the least useful way to do
    that.
    """
    try:
        return json.loads((REPO_ROOT / "config/harnesses.json").read_text()
                          ).get("harnesses", {})
    except (OSError, json.JSONDecodeError) as exc:
        return {"_error": str(exc)}


def _present(cfg):
    return cfg.get("always", False) or any(
        Path(p).expanduser().exists() for p in cfg.get("detect", []))


def check_mcp(rep):
    """Read the configs on disk rather than asking a CLI.

    `claude mcp list` only answers for one harness and only when that harness
    is installed, which on a Codex-only Linux box reports the entire MCP layer
    as broken when it is fine.
    """
    seen = False
    for key, cfg in harnesses().items():
        mcp = cfg.get("mcp")
        if not mcp or not _present(cfg):
            continue
        label = cfg.get("label", key)
        if mcp["scope"] == "user":
            rep.add("mcp", label, "warn",
                    f"user-scope only — not written ({mcp['path']})")
            continue
        path = REPO_ROOT / mcp["path"]
        if not path.exists():
            rep.add("mcp", label, "warn",
                    f"{mcp['path']} missing — run install_mcp.py")
            continue
        text = path.read_text()
        names = [n for n in ("blender", "gimp", "resolve", "anot8") if f'"{n}"' in text]
        seen = True
        rep.add("mcp", label, "ok" if names else "warn",
                f"{mcp['path']}: {', '.join(names) or 'no servers declared'}")
    if not seen:
        rep.add("mcp", "harnesses", "warn", "no harness with a project-scope "
                                            "MCP config is installed")


def check_skills(rep):
    """A skill is healthy when every installed harness can see it."""
    table = harnesses()
    if "_error" in table:
        return rep.add("skills", "config/harnesses.json", "fail", table["_error"])

    targets = []
    for key, cfg in table.items():
        if not _present(cfg):
            continue
        for raw in cfg.get("skills_dirs", []):
            path = Path(raw).expanduser()
            if path not in targets:
                targets.append(path)

    if not targets:
        return rep.add("skills", "harnesses", "warn", "no harness detected")

    for skill, src in skill_sources().items():
        if not (src / "SKILL.md").exists():
            rep.add("skills", skill, "warn",
                    "not available yet — run sync_skills.py --fix")
            continue
        linked, wrong = [], []
        for target in targets:
            link = target / skill
            try:
                good = (link.is_symlink()
                        and link.resolve(strict=True) == src.resolve())
            except OSError:
                good = False          # dangling symlink
            (linked if good else wrong).append(target)
        if not wrong:
            rep.add("skills", skill, "ok", f"linked in {len(linked)} harness dir(s)")
        else:
            rep.add("skills", skill, "warn",
                    f"{len(linked)}/{len(targets)} linked — run sync_skills.py --fix")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true",
                    help="skip the headless smoke tests and the per-model network probes")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    # The model probes import google-genai, which lives in the venv. Run from
    # the system python3 (the README, bootstrap --doctor) they would all skip,
    # so hand over to the venv's interpreter when there is one.
    venv = REPO_ROOT / ".venv"
    if ((venv / "bin/python").exists() and Path(sys.prefix).resolve() != venv.resolve()
            and not os.environ.get("CREATIVE_STACK_DOCTOR_REEXEC")):
        os.environ["CREATIVE_STACK_DOCTOR_REEXEC"] = "1"
        os.execv(venv / "bin/python", [str(venv / "bin/python"), __file__, *sys.argv[1:]])

    rep = Report()
    check_platform(rep)
    check_tools(rep)
    check_bridges(rep, args.quick)
    check_credentials(rep, args.quick)
    check_ml(rep)
    check_resolve(rep)
    check_mcp(rep)
    check_skills(rep)

    if args.json:
        print(json.dumps(rep.sections, indent=2))
    else:
        print(f"\n{'=' * 66}\n 🩺 CREATIVE STACK DOCTOR\n{'=' * 66}")
        rep.render()
        counts = {}
        for rows in rep.sections.values():
            for r in rows:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
        print(f"\n{'=' * 66}")
        print(f" {GREEN}{counts.get('ok', 0)} ok{RESET}  "
              f"{YELLOW}{counts.get('warn', 0)} warn{RESET}  "
              f"{RED}{counts.get('fail', 0)} fail{RESET}  "
              f"{DIM}{counts.get('skip', 0)} skipped{RESET}")

    if rep.hard_failures:
        print(f"\n{RED}Hard failures:{RESET}")
        for f in rep.hard_failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
