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

"""Find secrets and personal data in a screenshot and write blurs for them.

/shot's first rule is "check for secrets before publishing". This makes the
check a command: OCR reads every word with its box, rules flag the ones that
look like credentials or personal data, and each hit becomes an anot8 `blur`
in the screenshot's spec — for review, then `anot8 render`.

    python3 scan.py -i out/raw.png                 # report, add blurs to out/raw.anot8.json
    python3 scan.py -i out/raw.png --ai            # plus Gemini: faces, avatars, names, pickers
    python3 scan.py -i out/final.png --check       # exit 1 if anything is still readable

What the rules catch (tesseract, local, no network):

  key      known formats — Google, AWS, GitHub, Slack, Stripe, OpenAI/Anthropic,
           OAuth bearer tokens, JWTs, private key headers
  label    the value after "password:", "token=", "api key:", "secret:" ...
  email    addresses
  project  GCP project ids: projects/<id>, --project <id>, generated ids
  card     13-19 digit numbers that pass the Luhn check
  entropy  long random-looking strings no other rule named

OCR misreads random strings (`AIza` comes back as `AlZza`) and gives them low
confidence, so no word is dropped for confidence and the key prefixes allow
the usual I/l/1 swaps. A rule that misses still leaves `--ai`, which
reads the image rather than the OCR text.

Matched text is never printed or written in full: reports show the first four
characters and the length, so the scan log is not itself a leak.
"""

import argparse
import io
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import hostenv  # noqa: E402

GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"

# Tesseract reads small UI text badly; upscaling to about this width first is
# the single biggest accuracy win. Coordinates are scaled back afterwards.
OCR_TARGET_WIDTH = 2400
# Blur a little past the OCR box, which hugs the glyphs: anti-aliased edges
# and descenders outside it can still give a character away.
PAD_FRACTION = 0.25

AI_QUERY = ("anything that should not be published from a screenshot: faces, "
            "profile photos and avatars, people's names, email addresses, phone "
            "numbers, account or organisation switchers, QR codes, and any "
            "password, API key, token or other secret")

# I, l, 1 and | are the swap tesseract makes most in random strings.
_I = "[Il1|]"
KEY_RULES = [
    ("google-api-key", rf"A{_I}?[Zz]{{1,2}}a[0-9A-Za-z_\-]{{20,}}"),
    ("google-oauth", r"ya29\.[0-9A-Za-z_\-]{20,}"),
    ("aws-access-key", rf"A[KS]{_I}A[0-9A-Z]{{16}}"),
    ("github-token", r"gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{40,}"),
    ("slack-token", r"xox[abprs]-[0-9A-Za-z\-]{10,}"),
    ("stripe-key", r"(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,}"),
    ("openai-anthropic-key", r"sk-(?:ant-|proj-)?[0-9A-Za-z_\-]{20,}"),
    ("jwt", r"eyJ[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{4,}"),
    ("private-key", r"BEGIN[A-Z ]*PRIVATE"),
]
LABELS = [
    # "Password: ...", "api key = ...", "Authorization: Bearer ..."
    re.compile(r"(?i)\b(?:pass(?:word|wd)?|pwd|secret|client[ _-]?secret|"
               r"(?:access |refresh |api )?token|api[ _-]?key|auth(?:orization)?|"
               r"bearer|session|cookie)\s*[:=]\s*"),
    # GITHUB_TOKEN=..., AWS_ACCESS_KEY_ID=..., DB_PASSWORD=...
    re.compile(r"\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|PWD|"
               r"CREDENTIALS?|SESSION|COOKIE)[A-Z0-9_]*\s*[:=]\s*"),
]
# Same-length swaps, so offsets into the text still line up with the words.
OCR_FIXES = str.maketrans({"—": "-", "–": "-", "‘": "'", "’": "'",
                           "“": '"', "”": '"'})
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PROJECT_ID = r"[a-z][a-z0-9\-]{4,28}[a-z0-9]"
PROJECT_RULES = [
    re.compile(rf"projects/({PROJECT_ID})"),
    re.compile(rf"--project[= ]({PROJECT_ID})"),
    re.compile(rf"(?i)\bproject(?: id)?:?\s+({PROJECT_ID})\b"),
    # Console-generated ids end in six digits: "my-app-412907".
    re.compile(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)*-\d{6})\b"),
]
CARD = re.compile(r"\b(?:\d[ \-]?){13,19}\b")


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- OCR ---

def parse_tsv(tsv, scale=1.0):
    """(page_size, lines) from tesseract TSV. A line is a list of words,
    each {"text", "box": [x, y, w, h]} in original-image pixels."""
    size, lines, order = None, {}, []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12:
            continue
        level = cols[0]
        x, y, w, h = (round(int(v) / scale) for v in cols[6:10])
        if level == "1":
            size = (w, h)
        if level != "5" or not cols[11].strip():
            continue
        key = tuple(cols[1:5])
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append({"text": cols[11].strip().translate(OCR_FIXES),
                           "box": [x, y, w, h]})
    return size, [lines[k] for k in order]


def ocr(path):
    """Run tesseract on a cleaned-up copy of the image."""
    binary = hostenv.which("tesseract") or die(
        "tesseract is not installed — run scripts/bootstrap.sh, or: "
        "brew install tesseract / apt install tesseract-ocr")
    data, scale, size = prepare(path)
    # psm 11: sparse text. A screenshot is islands of labels, not prose.
    out = subprocess.run([binary, "stdin", "stdout", "--psm", "11", "tsv"],
                         input=data, capture_output=True, check=False)
    if out.returncode != 0:
        die(f"tesseract failed: {out.stderr.decode(errors='replace').strip()[:300]}")
    page, lines = parse_tsv(out.stdout.decode(errors="replace"), scale)
    return size or page, lines


def prepare(path):
    """Grey, contrast-stretched, dark-on-light and upscaled: what tesseract
    reads best. Returns (PNG bytes, scale, original size). Without Pillow the
    original goes in unchanged and the size comes from tesseract."""
    try:
        from PIL import Image, ImageOps, ImageStat       # noqa: PLC0415
    except ImportError:
        return path.read_bytes(), 1.0, None
    img = ImageOps.autocontrast(Image.open(path).convert("L"))
    size = img.size
    if ImageStat.Stat(img).mean[0] < 128:               # dark mode
        img = ImageOps.invert(img)
    scale = max(1.0, OCR_TARGET_WIDTH / img.width)
    if scale > 1.0:
        img = img.resize((round(img.width * scale), round(img.height * scale)),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), scale, size


# -------------------------------------------------------------- rules ---

def entropy(text):
    """Shannon entropy in bits per character."""
    counts = {c: text.count(c) for c in set(text)}
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def luhn(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def looks_random(token):
    """Long, mixed-class and high-entropy: how an unknown key looks. Words,
    paths and hex hashes (commit SHAs) don't pass."""
    if len(token) < 24 or re.fullmatch(r"[0-9a-fA-F]+", token):
        return False
    classes = sum(bool(re.search(p, token)) for p in ("[a-z]", "[A-Z]", "[0-9]"))
    return classes >= 3 and entropy(token) >= 3.8


def line_text(words):
    """The line as one string, plus each word's (start, end) offset in it."""
    text, spans = "", []
    for word in words:
        if text:
            text += " "
        spans.append((len(text), len(text) + len(word["text"])))
        text += word["text"]
    return text, spans


def glue(text):
    """OCR splits a token at `_` and `-` ("KEY_ 1234"); drop those spaces.

    Returns the glued string and, for each of its characters plus the end,
    the matching offset in `text`, so a match maps back to the words."""
    out, where = [], []
    for i, ch in enumerate(text):
        if ch == " " and 0 < i < len(text) - 1 and (
                text[i - 1] in "_-" or text[i + 1] in "_-"):
            continue
        out.append(ch)
        where.append(i)
    where.append(len(text))
    return "".join(out), where


def find(words):
    """Every hit on one OCR line: (kind, start, end) offsets into its text."""
    text, _ = line_text(words)
    glued, where = glue(text)
    hits = []

    def add(kind, start, end):
        """Keep the widest span: a key inside a labelled value is one hit."""
        if end <= start or any(s <= start and end <= e for _, s, e in hits):
            return
        inner = [h for h in hits if start <= h[1] and h[2] <= end]
        for h in inner:
            hits.remove(h)
        hits.append((inner[0][0] if len(inner) == 1 else kind, start, end))

    def add_glued(kind, m):
        add(kind, where[m.start()], where[m.end() - 1] + 1)

    for kind, pattern in KEY_RULES:
        for m in re.finditer(pattern, glued):
            add_glued(kind, m)
    for rule in LABELS:
        for m in rule.finditer(text):
            add("label", m.end(), len(text))   # the value runs to the end of the line
    for m in EMAIL.finditer(text):
        add("email", m.start(), m.end())
    for rule in PROJECT_RULES:
        for m in rule.finditer(text):
            add("project", m.start(1), m.end(1))
    for m in CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and luhn(digits):
            add("card", m.start(), m.end())
    for m in re.finditer(r"[^\s:=]+=*", glued):    # split KEY=value; keep base64 padding
        if looks_random(m.group()):
            add_glued("entropy", m)
    return sorted(hits, key=lambda h: h[1])


def scan_lines(lines):
    """Hits across all lines, each with the union box of the words it covers."""
    found = []
    for words in lines:
        text, spans = line_text(words)
        for kind, start, end in find(words):
            covered = [clip(w["box"], s, e, start, end)
                       for w, (s, e) in zip(words, spans) if s < end and start < e]
            if covered:
                found.append({"kind": kind, "source": "ocr",
                              "preview": preview(text[start:end]),
                              "box": union(covered)})
    return found


def clip(box, s, e, start, end):
    """The part of a word's box that [start, end) covers, by character share.

    OCR reads `GITHUB_TOKEN=ghp_...` as one word; blurring only the value keeps
    the variable name readable. Glyph widths vary, so the cut errs one
    character wide on each side."""
    x, y, w, h = box
    n = max(1, e - s)
    lo, hi = max(0, start - s - 1), min(n, end - s + 1)
    if lo == 0 and hi == n:
        return box
    return [x + round(w * lo / n), y, round(w * (hi - lo) / n), h]


def preview(secret):
    """Enough to recognise a hit in a report, not enough to use it."""
    secret = secret.strip()
    return f"{secret[:4]}… ({len(secret)} chars)"


# --------------------------------------------------------------- boxes ---

def union(boxes):
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return [x0, y0, x1 - x0, y1 - y0]


def pad(box, size):
    """Grow by a fraction of the text height, clamped to the image."""
    x, y, w, h = box
    p = max(2, round(h * PAD_FRACTION))
    x0, y0 = max(0, x - p), max(0, y - p)
    x1, y1 = min(size[0], x + w + p), min(size[1], y + h + p)
    return [x0, y0, x1 - x0, y1 - y0]


def covers(outer, inner):
    """True when `outer` contains most (80%) of `inner`'s area."""
    ix = max(0, min(outer[0] + outer[2], inner[0] + inner[2]) - max(outer[0], inner[0]))
    iy = max(0, min(outer[1] + outer[3], inner[1] + inner[3]) - max(outer[1], inner[1]))
    area = inner[2] * inner[3]
    return area > 0 and ix * iy >= 0.8 * area


# ---------------------------------------------------------------- spec ---

def spec_box(ann, size):
    """An anot8 blur's [x, y, w, h] in pixels; percentages are resolved."""
    def px(value, full):
        if isinstance(value, str) and value.endswith("%"):
            return float(value[:-1]) * full / 100
        return float(value)
    return [px(ann["x"], size[0]), px(ann["y"], size[1]),
            px(ann["width"], size[0]), px(ann["height"], size[1])]


def merge_into_spec(spec, findings, size):
    """Add a blur per finding that no existing blur already covers.

    Blurs go first so later callouts draw over them, matching /shot's rule.
    Returns the number added."""
    anns = spec.setdefault("annotations", [])
    existing = [spec_box(a, size) for a in anns
                if a.get("type") == "blur" and all(k in a for k in ("x", "y", "width", "height"))]
    taken = {a.get("id") for a in anns}
    new = []
    for f in findings:
        box = pad(f["box"], size)
        if any(covers(e, box) for e in existing):
            continue
        n = 1
        while f"scan-{n}" in taken:
            n += 1
        taken.add(f"scan-{n}")
        existing.append(box)
        new.append({"type": "blur", "id": f"scan-{n}", "x": box[0], "y": box[1],
                    "width": box[2], "height": box[3], "badge": False,
                    "note": f"scan: {f['kind']}"})
    spec["annotations"] = new + anns
    return len(new)


def new_spec(image, spec_path, size):
    """A spec like `anot8 init` writes. The source is relative to the spec,
    which is how anot8 resolves it."""
    source = os.path.relpath(image.resolve(), spec_path.resolve().parent)
    return {"version": 1,
            "image": {"source": source, "width": size[0], "height": size[1]},
            "annotations": []}


# ----------------------------------------------------------------- AI ---

def ai_findings(path, model, backend):
    """Gemini's read of the image itself, via /raster's segment detector."""
    sys.path.insert(0, str(ROOT / "skills/raster/scripts"))
    import segment                                       # noqa: PLC0415
    from PIL import Image                                # noqa: PLC0415
    image = Image.open(path)
    items, route = segment.detect(image, AI_QUERY, model or segment.DEFAULT_MODEL,
                                  backend)
    found = []
    for item in items:
        x0, y0, x1, y1 = segment.box_px(item["box_2d"], image.size)
        found.append({"kind": f"ai: {item.get('label', 'match')}", "source": route,
                      "preview": item.get("label", ""), "box": [x0, y0, x1 - x0, y1 - y0]})
    return found


# ---------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, help="screenshot (PNG/JPEG)")
    ap.add_argument("--spec", "-s",
                    help="anot8 spec to add blurs to (default: <input>.anot8.json; "
                         "created if missing)")
    ap.add_argument("--json", help="also write the findings here")
    ap.add_argument("--check", action="store_true",
                    help="write nothing; exit 1 if anything is found (pre-publish gate)")
    ap.add_argument("--ai", action="store_true",
                    help="also ask Gemini for faces, avatars, names and pickers")
    ap.add_argument("--model", help="Gemini model for --ai")
    ap.add_argument("--backend", choices=["auto", "vertex", "aistudio"], default="auto")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.is_file():
        die(f"no such file: {src}")

    size, lines = ocr(src)
    if not size:
        die(f"tesseract returned no page size for {src}")
    findings = scan_lines(lines)
    if args.ai:
        findings += ai_findings(src, args.model, args.backend)

    for f in findings:
        x, y, w, h = f["box"]
        print(f"  {YELLOW}{f['kind']:<22}{RESET} {f['preview']:<28} "
              f"{DIM}at {x},{y} {w}×{h}{RESET}")
    if args.json:
        Path(args.json).write_text(json.dumps(findings, indent=2) + "\n")

    if args.check:
        if findings:
            print(f"❌ {len(findings)} possible secret(s) still readable in {src}")
            return 1
        print(f"{GREEN}✓{RESET} nothing readable flagged in {src}")
        return 0

    if not findings:
        print(f"{GREEN}✓{RESET} nothing flagged in {src} — still look before publishing")
        return 0
    spec_path = Path(args.spec) if args.spec else src.with_suffix(".anot8.json")
    spec = (json.loads(spec_path.read_text()) if spec_path.exists()
            else new_spec(src, spec_path, size))
    added = merge_into_spec(spec, findings, size)
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"{GREEN}✓{RESET} {added} blur(s) added to {spec_path} "
          f"({len(findings) - added} already covered)")
    print(f"  {DIM}review, then: anot8 render {spec_path} -o out/annotated.png{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
