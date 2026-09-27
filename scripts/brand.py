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

"""Shared brand-token loader.

Every rendering skill imports this so a single edit to brand/tokens.json
restyles slides, screenshots, diagrams and video captions at once.

Resolution order (later wins):
    brand/tokens.json  ->  profile $extends chain  ->  brand/tokens.local.json

    python3 scripts/brand.py                    # dump the default profile
    python3 scripts/brand.py --profile personal # dump a specific profile
    python3 scripts/brand.py --css              # emit CSS custom properties
"""

import argparse
import copy
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("CREATIVE_STACK_ROOT",
                                Path(__file__).resolve().parent.parent))
TOKENS = REPO_ROOT / "brand/tokens.json"
TOKENS_LOCAL = REPO_ROOT / "brand/tokens.local.json"


def _deep_merge(base, override):
    """Recursively merge override into a copy of base. Scalars and lists are
    replaced wholesale; only dicts merge."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _strip_meta(obj):
    """Drop $comment / $todo / $note keys so they never leak into rendered CSS."""
    if isinstance(obj, dict):
        return {k: _strip_meta(v) for k, v in obj.items()
                if not (k.startswith("$") and k != "$extends")}
    if isinstance(obj, list):
        return [_strip_meta(v) for v in obj]
    return obj


def load(profile=None):
    """Return a fully resolved token dict for one profile."""
    if not TOKENS.exists():
        raise FileNotFoundError(f"brand tokens not found at {TOKENS}")

    doc = json.loads(TOKENS.read_text())
    if TOKENS_LOCAL.exists():
        doc = _deep_merge(doc, json.loads(TOKENS_LOCAL.read_text()))

    profiles = doc.get("profiles", {})
    name = profile or os.environ.get("CREATIVE_STACK_BRAND") or doc.get("default")
    if name not in profiles:
        raise KeyError(f"unknown brand profile '{name}'. "
                       f"Available: {', '.join(sorted(profiles))}")

    # Walk the $extends chain, base-first, guarding against cycles.
    chain, seen, cursor = [], set(), name
    while cursor:
        if cursor in seen:
            raise ValueError(f"circular $extends involving '{cursor}'")
        seen.add(cursor)
        chain.append(cursor)
        cursor = profiles[cursor].get("$extends")

    resolved = {}
    for link in reversed(chain):
        resolved = _deep_merge(resolved, profiles[link])
    resolved.pop("$extends", None)
    resolved["name"] = name
    return _strip_meta(resolved)


def _flatten(obj, prefix=""):
    for key, value in obj.items():
        token = f"{prefix}-{key}" if prefix else key
        if isinstance(value, dict):
            yield from _flatten(value, token)
        elif isinstance(value, list):
            yield token, " ".join(str(v) for v in value)
        else:
            yield token, str(value)


def to_css(tokens, selector=":root"):
    """Emit CSS custom properties, e.g. --color-light-bg: #FFFFFF;"""
    lines = [f"{selector} {{"]
    for key, value in _flatten(tokens):
        if key in ("name", "label"):
            continue
        lines.append(f"  --{key}: {value};")
    lines.append("}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", help="profile name (default: tokens.json 'default')")
    ap.add_argument("--css", action="store_true", help="emit CSS custom properties")
    ap.add_argument("--get", help="dotted path to a single value, e.g. color.dark.accent")
    args = ap.parse_args()

    try:
        tokens = load(args.profile)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    if args.get:
        cursor = tokens
        for part in args.get.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                print(f"❌ no such token: {args.get}", file=sys.stderr)
                return 1
            cursor = cursor[part]
        print(cursor if not isinstance(cursor, (dict, list))
              else json.dumps(cursor, indent=2))
    elif args.css:
        print(to_css(tokens))
    else:
        print(json.dumps(tokens, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
