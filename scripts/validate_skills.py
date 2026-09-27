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

"""Lint gate for creative-stack skills.

Encodes the agent-skill-forge conventions as enforceable rules so a malformed
SKILL.md fails bootstrap instead of silently not loading.

    python3 scripts/validate_skills.py
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"

REQUIRED_HEADINGS = ["## 🎯 Goal", "## 📋 Step-by-Step Workflow",
                     "## 💡 Concrete Example", "## 🚫 Hard Constraints"]

# Blocks personal data and absolute machine paths from committed skill bodies.
FORBIDDEN = [
    (re.compile(r"/Users/[a-z0-9._-]+/", re.I), "absolute home path (use ~ or $CREATIVE_STACK_ROOT)"),
    (re.compile(r"AIza[0-9A-Za-z_-]{20,}"), "Google API key"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "API secret"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "GitHub token"),
]

GREEN, YELLOW, RED, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[0m"


def parse_frontmatter(text):
    if not text.startswith("---"):
        return None, "missing YAML frontmatter"
    end = text.find("\n---", 3)
    if end == -1:
        return None, "unterminated frontmatter"
    fm = {}
    for line in text[3:end].strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, None


def validate(path):
    """Returns (errors, warnings) for one SKILL.md."""
    errors, warnings = [], []
    text = path.read_text(encoding="utf-8")
    name = path.parent.name

    fm, err = parse_frontmatter(text)
    if err:
        return [err], []

    if fm.get("name") != name:
        errors.append(f"frontmatter name '{fm.get('name')}' != directory '{name}'")

    desc = fm.get("description", "")
    if not desc:
        errors.append("missing description")
    elif not desc.endswith(f"Trigger via /{name}."):
        warnings.append(f"description should end with 'Trigger via /{name}.'")

    if fm.get("disable-model-invocation") != "true":
        warnings.append("slash-command skills should set disable-model-invocation: true")

    for heading in REQUIRED_HEADINGS:
        if heading not in text:
            warnings.append(f"missing section {heading}")

    for pattern, label in FORBIDDEN:
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            errors.append(f"line {line}: {label}")

    return errors, warnings


def main():
    print("=" * 66)
    print(" 🔍 CREATIVE STACK — skill validation")
    print("=" * 66)

    skills = sorted(p for p in SKILLS_DIR.glob("*/SKILL.md"))
    if not skills:
        print(f"\n{YELLOW}No SKILL.md files authored yet — nothing to validate.{RESET}")
        return 0

    total_errors = 0
    for path in skills:
        errors, warnings = validate(path)
        total_errors += len(errors)
        if not errors and not warnings:
            print(f"  {GREEN}✓{RESET} {path.parent.name}")
        else:
            mark = f"{RED}✗{RESET}" if errors else f"{YELLOW}!{RESET}"
            print(f"  {mark} {path.parent.name}")
            for e in errors:
                print(f"      {RED}error:{RESET} {e}")
            for w in warnings:
                print(f"      {YELLOW}warn:{RESET}  {w}")

    print("\n" + "=" * 66)
    print(f" {len(skills)} skill(s) checked, {total_errors} error(s)")
    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main())
