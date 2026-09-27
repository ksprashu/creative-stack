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

"""Symlink creative-stack skills into every installed agent harness.

Follows the agent-skill-forge convention: skills live once in this repo and are
symlinked out, so editing a SKILL.md takes effect immediately with no re-sync.

Which harnesses exist, and where each keeps its skills, is data —
config/harnesses.json. Nothing in this file knows the name of a harness.

    python3 scripts/sync_skills.py                # read-only audit (default)
    python3 scripts/sync_skills.py --fix          # create/repoint links
    python3 scripts/sync_skills.py --fix --prune  # also remove stale links
    python3 scripts/sync_skills.py --fix --all    # include absent harnesses
    python3 scripts/sync_skills.py --uninstall --fix
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
HARNESSES = REPO_ROOT / "config/harnesses.json"

SKILLS = ["shot", "vidkit", "deck", "vector", "raster", "render3d", "sound", "artgen"]

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def ok(m):
    print(f"  {GREEN}✓{RESET} {m}")


def warn(m):
    print(f"  {YELLOW}!{RESET} {m}")


def bad(m):
    print(f"  {RED}✗{RESET} {m}")


def expand(raw):
    return Path(raw).expanduser()


def load_harnesses():
    """Read the harness table. A malformed table is fatal, not a fallback.

    Silently degrading to a hardcoded list here would be the worst outcome: the
    sync would appear to succeed while quietly missing whichever harness the
    user just added to the JSON.
    """
    try:
        data = json.loads(HARNESSES.read_text())
    except FileNotFoundError:
        sys.exit(f"{RED}missing {HARNESSES}{RESET} — the harness table is required")
    except json.JSONDecodeError as exc:
        sys.exit(f"{RED}{HARNESSES} is not valid JSON:{RESET} {exc}")
    return data.get("harnesses", {})


def installed(cfg):
    """A harness counts as present when any of its detect paths exists.

    `always` overrides detection for the vendor-neutral hub: on a freshly
    imaged machine nothing is detected, and a bootstrap that linked zero skills
    because no agent had been launched yet would not be much of a bootstrap.
    """
    return cfg.get("always", False) or any(
        expand(p).exists() for p in cfg.get("detect", []))


def link_state(src, link):
    """Classify a link target without following into a broken path."""
    if link.is_symlink():
        try:
            return "ok" if link.resolve(strict=True) == src.resolve() else "wrong"
        except OSError:
            return "broken"
    if link.exists():
        return "occupied"
    return "missing"


def plan(harnesses, only, force):
    """Resolve the harness table into a deduplicated list of directories.

    Several harnesses share a directory on purpose — Codex and the Universal
    Agent Hub both read ~/.agents/skills — so a directory is visited once and
    credited to every harness that wanted it. Without this, the audit prints
    the same eight links twice and --prune races itself.

    Returns ([(dir, [labels])], [skipped notes]).
    """
    order, owners, notes = [], {}, []

    for key, cfg in harnesses.items():
        label = cfg.get("label", key)
        if only and key not in only:
            continue
        if not installed(cfg) and not force:
            notes.append(f"{label} — not installed (--all to link anyway)")
            continue
        dirs = cfg.get("skills_dirs", [])
        if not dirs:
            via = ", ".join(cfg.get("covered_by", [])) or "nothing"
            notes.append(f"{label} — reads {via}, no directory of its own")
            continue
        for raw in dirs:
            path = expand(raw)
            if path not in owners:
                owners[path] = []
                order.append(path)
            owners[path].append(label)

    return [(p, owners[p]) for p in order], notes


def sync_dir(target, labels, skills, fix, prune, uninstall):
    print(f"{DIM}── {target}  ({', '.join(labels)}){RESET}")
    changed = problems = 0

    if not target.exists():
        if uninstall:
            print(f"  {DIM}·{RESET} does not exist")
            return 0, 0
        if not fix:
            warn("does not exist (use --fix to create)")
            return 0, 0
        target.mkdir(parents=True, exist_ok=True)

    for skill in skills:
        src = SKILLS_DIR / skill
        link = target / skill
        state = link_state(src, link)

        if uninstall:
            if state in ("ok", "wrong", "broken") and link.is_symlink():
                if fix:
                    link.unlink()
                    ok(f"removed {skill}")
                    changed += 1
                else:
                    warn(f"would remove {skill}")
            continue

        if state == "ok":
            ok(skill)
        elif state == "occupied":
            # A real directory we did not create. Never destroy user data.
            bad(f"{skill} — real directory exists, not touching it")
            problems += 1
        elif fix:
            if link.is_symlink():
                link.unlink()
            link.symlink_to(src, target_is_directory=True)
            ok(f"{skill} ({state} → linked)")
            changed += 1
        else:
            warn(f"{skill} — {state} (use --fix)")
            problems += 1

    if prune and not uninstall:
        for entry in sorted(target.iterdir()):
            if not entry.is_symlink() or entry.name in SKILLS:
                continue
            try:
                entry.resolve(strict=True)
            except OSError:
                if fix:
                    entry.unlink()
                    ok(f"pruned broken link {entry.name}")
                    changed += 1
                else:
                    warn(f"broken link {entry.name} (use --fix)")
    return changed, problems


def sync_registries(harnesses, only, force, fix, uninstall):
    """Add this repo's skills/ to each skills.json source registry.

    Merged, never rewritten. agent-skill-forge writes these same two files with
    its own entries, and clobbering them would silently unregister ~40 skills
    from Antigravity every time this script ran.
    """
    files = []
    for key, cfg in harnesses.items():
        if only and key not in only:
            continue
        if not installed(cfg) and not force:
            continue
        for raw in cfg.get("registry", []):
            path = expand(raw)
            if path not in files:
                files.append(path)
    if not files:
        return 0, 0

    print(f"{DIM}── skill source registries{RESET}")
    mine = str(SKILLS_DIR)
    changed = problems = 0

    for path in files:
        data, entries = {}, []
        if path.exists():
            try:
                data = json.loads(path.read_text())
                entries = data.get("entries", [])
            except json.JSONDecodeError:
                bad(f"{path} is not valid JSON — leaving it alone")
                problems += 1
                continue

        present = any(str(expand(e.get("path", ""))) == mine for e in entries)

        if uninstall:
            if not present:
                print(f"  {DIM}·{RESET} {path} — no entry of ours")
                continue
            if not fix:
                warn(f"would drop our entry from {path}")
                continue
            data["entries"] = [e for e in entries
                               if str(expand(e.get("path", ""))) != mine]
            path.write_text(json.dumps(data, indent=2) + "\n")
            ok(f"dropped our entry from {path}")
            changed += 1
            continue

        if present:
            ok(f"{path}")
        elif fix:
            path.parent.mkdir(parents=True, exist_ok=True)
            data["entries"] = entries + [{"path": mine}]
            path.write_text(json.dumps(data, indent=2) + "\n")
            ok(f"{path} (entry added, {len(entries)} existing kept)")
            changed += 1
        else:
            warn(f"{path} — missing our entry (use --fix)")
            problems += 1
    print()
    return changed, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fix", action="store_true", help="apply changes")
    ap.add_argument("--prune", action="store_true", help="remove broken symlinks")
    ap.add_argument("--uninstall", action="store_true", help="remove our links")
    ap.add_argument("--all", action="store_true",
                    help="link into harnesses that are not installed yet")
    ap.add_argument("--harness", action="append", metavar="KEY",
                    help="limit to one harness from config/harnesses.json "
                         "(repeatable)")
    ap.add_argument("--list", action="store_true",
                    help="show the harness table and exit")
    args = ap.parse_args()

    harnesses = load_harnesses()
    if args.list:
        for key, cfg in harnesses.items():
            mark = "●" if installed(cfg) else "○"
            dirs = ", ".join(cfg.get("skills_dirs", [])) or "(none)"
            print(f" {mark} {key:<16} {cfg.get('label', key):<22} {dirs}")
        return 0

    unknown = set(args.harness or []) - set(harnesses)
    if unknown:
        sys.exit(f"unknown harness: {', '.join(sorted(unknown))}")

    print("=" * 66)
    print(" 🔗 CREATIVE STACK — skill sync")
    print("=" * 66)
    print(f" Source: {SKILLS_DIR}\n")

    available = [s for s in SKILLS if (SKILLS_DIR / s / "SKILL.md").exists()]
    pending = [s for s in SKILLS if s not in available]
    if pending:
        print(f"{DIM}Not yet authored (skipped): {', '.join(pending)}{RESET}\n")

    targets, notes = plan(harnesses, set(args.harness or []), args.all)
    for note in notes:
        print(f"{DIM}·  {note}{RESET}")
    if notes:
        print()

    changed = problems = 0
    for target, labels in targets:
        c, p = sync_dir(target, labels, available,
                        args.fix, args.prune, args.uninstall)
        changed, problems = changed + c, problems + p
        print()

    c, p = sync_registries(harnesses, set(args.harness or []), args.all,
                           args.fix, args.uninstall)
    changed, problems = changed + c, problems + p

    verb = "applied" if args.fix else "pending"
    print("=" * 66)
    print(f" {len(targets)} directory(ies), {changed} change(s) {verb}, "
          f"{problems} problem(s)")
    if problems and not args.fix:
        print(" Re-run with --fix to resolve.")
    return 1 if (problems and args.fix) else 0


if __name__ == "__main__":
    sys.exit(main())
