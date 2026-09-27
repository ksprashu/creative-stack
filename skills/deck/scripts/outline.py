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

"""Turn a plain outline into a Marp deck.

Writing slides is mostly mechanical — front matter, slide breaks, which heading
becomes a divider, when eight bullets need to become two slides. Do that part
from an outline and spend the time on the words instead.

    python3 outline.py -i talk-outline.md -o out/talk.md --title "Deploying to Cloud Run"

Input is ordinary markdown:

    # Deploying to Cloud Run           <- title slide
    > Ship a container in 90 seconds   <- subtitle

    ## Why it matters                  <- no bullets: chapter divider
    ## Scales to zero                  <- has bullets: content slide
    - No idle cost
    - No YAML to write

    ```bash                            <- fenced code rides with its slide
    gcloud run deploy --source .
    ```

Slides with more than --max-bullets points are split rather than crammed; the
overflow slide keeps the heading and adds "(cont.)".
"""

import argparse
import re
import sys
from pathlib import Path

# The four brand accents, rotated across chapter dividers so a long deck has
# visual rhythm without anyone choosing a colour per slide.
ACCENTS = ["blue", "red", "yellow", "green"]


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def parse(text):
    """Split an outline into [{level, title, body: [lines]}]."""
    blocks, current, in_fence = [], None, False
    for line in text.splitlines():
        fence = line.lstrip().startswith("```")
        if fence:
            in_fence = not in_fence

        heading = None if in_fence or fence else re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            current = {"level": len(heading.group(1)),
                       "title": heading.group(2).strip(), "body": []}
            blocks.append(current)
        elif current is not None:
            current["body"].append(line)
        elif line.strip():
            # Text before the first heading becomes the title slide's subtitle.
            blocks.append({"level": 0, "title": "", "body": [line]})
            current = blocks[-1]
    return blocks


def trim(lines):
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def bullets_of(body):
    return [l for l in body if re.match(r"^\s*[-*+]\s+", l)]


def chunk(body, max_bullets):
    """Split a body at bullet boundaries, keeping fenced code with its slide."""
    bullets = bullets_of(body)
    if len(bullets) <= max_bullets:
        return [body]

    parts, current, count, in_fence = [], [], 0, False
    for line in body:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        is_bullet = not in_fence and re.match(r"^\s*[-*+]\s+", line)
        if is_bullet and count >= max_bullets:
            parts.append(trim(current))
            current, count = [], 0
        current.append(line)
        count += bool(is_bullet)
    if trim(list(current)):
        parts.append(trim(current))
    return parts


def render(blocks, title, subtitle, opts):
    profile_class = "dark" if opts["dark"] else ""
    front = [
        "---",
        "marp: true",
        f"theme: creative-{opts['profile']}",
        "paginate: true",
    ]
    if profile_class:
        front.append(f"class: {profile_class}")
    if opts["footer"]:
        front.append(f"footer: '{opts['footer']}'")
    front += ["---", ""]

    slides = []

    lead = blocks[0] if blocks and blocks[0]["level"] in (0, 1) else None
    heading = title or (lead["title"] if lead and lead["level"] == 1 else "Untitled")
    tagline = subtitle or ""
    if not tagline and lead:
        quoted = [l.lstrip("> ").strip() for l in lead["body"] if l.startswith(">")]
        tagline = quoted[0] if quoted else ""

    slides.append("<!-- _class: lead -->\n\n"
                  f"# {heading}\n" + (f"\n{tagline}\n" if tagline else ""))

    accent = 0
    for block in blocks:
        if block is lead or block["level"] == 0:
            continue
        body = trim(list(block["body"]))

        if block["level"] <= 2 and not body:
            slides.append(f"<!-- _class: section {ACCENTS[accent % len(ACCENTS)]} -->"
                          f"\n\n# {block['title']}\n")
            accent += 1
            continue

        for index, part in enumerate(chunk(body, opts["max_bullets"])):
            suffix = "" if index == 0 else " (cont.)"
            slide = f"## {block['title']}{suffix}\n\n" + "\n".join(part) + "\n"
            if opts["notes"]:
                slide += "\n<!--\nSpeaker notes: \n-->\n"
            slides.append(slide)

    if opts["outro"]:
        slides.append(f"<!-- _class: lead -->\n\n# {opts['outro']}\n")

    return "\n".join(front) + "\n\n---\n\n".join(slides)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, help="outline markdown")
    ap.add_argument("--output", "-o", required=True, help="Marp deck to write")
    ap.add_argument("--title", default=None, help="override the title slide")
    ap.add_argument("--subtitle", default=None)
    ap.add_argument("--profile", default="google", help="brand profile name")
    ap.add_argument("--footer", default=None, help="footer text on every slide")
    ap.add_argument("--outro", default=None,
                    help="closing slide text, e.g. 'Thanks — @handle'")
    ap.add_argument("--max-bullets", type=int, default=6,
                    help="split a slide past this many bullets (default 6)")
    ap.add_argument("--notes", action="store_true",
                    help="add a speaker-notes stub to every content slide")
    ap.add_argument("--dark", action="store_true", help="dark deck")
    args = ap.parse_args()

    source = Path(args.input)
    if not source.exists():
        die(f"outline not found: {source}")

    blocks = parse(source.read_text())
    if not blocks:
        die(f"{source} has no headings — nothing to turn into slides.")

    deck = render(blocks, args.title, args.subtitle, {
        "profile": args.profile, "footer": args.footer, "outro": args.outro,
        "max_bullets": args.max_bullets, "notes": args.notes, "dark": args.dark,
    })

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(deck)

    count = deck.count("\n---\n") + 1 - 1        # front matter closes with ---
    print(f"✓ {output}  ({count} slides)")
    print(f"  Next: render it →  python3 build.py -i {output} -o out/deck.pdf")
    return 0


if __name__ == "__main__":
    sys.exit(main())
