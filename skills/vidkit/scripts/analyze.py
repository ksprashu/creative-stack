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

"""Have Gemini watch the recording and propose the edit.

Silence detection knows where nobody is talking; it does not know that the
first ninety seconds were a false start or that 6:40 is where the topic
changes. This sends a small proxy of the recording to Gemini and writes back
analysis.json — chapters, highlights, segments to cut (fumbles, retakes, dead
waiting), b-roll suggestions with ready-to-use /artgen prompts, and a title and
description. Nothing downstream runs automatically: review and edit the JSON,
then feed it on.

    python3 analyze.py -i raw.mov -o out/analysis.json
    python3 analyze.py -i raw.mov -o out/analysis.json --transcript out/talk.json

    # then: apply its cuts on top of silence removal. autocut also moves the
    # chapters onto the cut timeline, as out/cut.chapters.txt
    python3 autocut.py -i raw.mov -o out/cut.json --analysis out/analysis.json
    python3 timeline.py -s out/cut.json -o out/edit.xml \\
        --chapters out/cut.chapters.txt

analysis.chapters.txt is in source time — right for the uncut recording, wrong
for anything autocut has shortened.

Transport: the proxy (640px, 4fps, mono 32kbps) is sent inline when it is under
20MB, which covers well over an hour of screen recording. Larger proxies go
through the Files API on AI Studio; Vertex has no Files API, so there pass
--gcs-uri with a copy of the recording in Cloud Storage.
"""

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from vidlib import (REPO_ROOT, die, ffmpeg, hms, parse_srt, probe,
                    write_chapters)

sys.path.insert(0, str(REPO_ROOT / "skills/artgen/scripts"))
import genailib as G  # noqa: E402

DEFAULT_MODEL = "gemini-3.8-flash"
SEQUENCE = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash"]
INLINE_LIMIT = 20 * 1024 * 1024
ANALYSIS_VERSION = 1

_SPAN = {
    "type": "object",
    "properties": {
        "start": {"type": "number", "description": "seconds from the start"},
        "end": {"type": "number", "description": "seconds from the start"},
        "reason": {"type": "string"},
    },
    "required": ["start", "end", "reason"],
}

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "chapters": {"type": "array", "items": {
            "type": "object",
            "properties": {"start": {"type": "number"},
                           "title": {"type": "string"}},
            "required": ["start", "title"]}},
        "highlights": {"type": "array", "items": _SPAN},
        "cuts": {"type": "array", "items": _SPAN},
        "broll": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "at": {"type": "number"},
                "duration": {"type": "number"},
                "kind": {"type": "string", "enum": ["video", "image"]},
                "prompt": {"type": "string"},
            },
            "required": ["at", "duration", "kind", "prompt"]}},
    },
    "required": ["title", "description", "chapters", "highlights", "cuts", "broll"],
}

PROMPT = """You are the editor for a technical screen recording that is \
{duration:.1f} seconds long. Watch and listen to all of it, then propose the edit.

All times are seconds from the start of THIS file, as numbers (e.g. 83.5).

- chapters: topic changes a viewer would want to jump to. The first starts at 0. \
Short recordings may have just one. Titles are 2-6 words, no numbering.
- highlights: the strongest moments, worth using in a teaser or short.
- cuts: material to remove — false starts, retakes (keep the LAST good take), \
fumbles, "um, let me just…" detours, long waits on a spinner or build with \
nothing to look at. Do NOT list ordinary short pauses; silence removal handles \
those. Be conservative: when unsure, keep it. Give the reason for each.
- broll: places where a cutaway would help (abstract concepts, transitions, the \
opening). duration 3-8 seconds. kind "video" for motion, "image" for a still. \
prompt is a self-contained generation prompt: subject, style, camera, lighting, \
no on-screen text.
- title: under 70 characters, specific, no clickbait.
- description: 2-3 sentences for a YouTube description.
{extra}"""


def make_proxy(source, out):
    """A small re-encode that keeps what Gemini needs: legible UI and speech."""
    ffmpeg(["-i", str(source), "-vf", "scale=640:-2,fps=4",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1", "-b:a", "32k",
            "-movflags", "+faststart", "-y", str(out)])
    return out


def transcript_text(path):
    """Timestamped lines from a transcribe.py .json or .srt, for grounding."""
    path = Path(path)
    if path.suffix == ".srt":
        cues = parse_srt(path)
    else:
        doc = json.loads(path.read_text())
        # whisper.cpp -oj: {"transcription": [{"offsets": {"from": ms, "to": ms},
        # "text": ...}]}
        cues = [{"start": c["offsets"]["from"] / 1000,
                 "end": c["offsets"]["to"] / 1000, "text": c["text"]}
                for c in doc.get("transcription", [])]
    return "\n".join(f"[{c['start']:.1f}-{c['end']:.1f}] {c['text'].strip()}"
                     for c in cues if c["text"].strip())


def video_part(types, client, proxy, gcs_uri):
    if gcs_uri:
        return types.Part.from_uri(file_uri=gcs_uri, mime_type="video/mp4")
    size = proxy.stat().st_size
    if size <= INLINE_LIMIT:
        return types.Part.from_bytes(data=proxy.read_bytes(), mime_type="video/mp4")
    if client.vertexai:
        die(f"the proxy is {size // 2**20}MB, over the {INLINE_LIMIT // 2**20}MB "
            "inline limit, and Vertex has no Files API. Upload the recording and "
            "pass it in:\n  gcloud storage cp RECORDING gs://BUCKET/rec.mp4\n"
            "  analyze.py ... --gcs-uri gs://BUCKET/rec.mp4")
    uploaded = client.files.upload(file=str(proxy))
    while uploaded.state and uploaded.state.name == "PROCESSING":
        time.sleep(3)
        uploaded = client.files.get(name=uploaded.name)
    if uploaded.state and uploaded.state.name == "FAILED":
        die(f"Files API could not process {proxy.name}")
    return uploaded


def clean(doc, duration):
    """Clamp to the recording, drop inverted spans, sort. The model is good at
    this and not perfect at it, and a cut past the end breaks autocut."""
    def span(items):
        out = []
        for item in items:
            start = max(0.0, min(float(item["start"]), duration))
            end = max(0.0, min(float(item["end"]), duration))
            if end - start >= 0.2:
                out.append({**item, "start": round(start, 3), "end": round(end, 3)})
        return sorted(out, key=lambda s: s["start"])

    chapters = sorted(
        ({"start": round(max(0.0, min(float(c["start"]), duration)), 3),
          "title": c["title"].strip()} for c in doc.get("chapters", [])),
        key=lambda c: c["start"])
    if chapters:
        chapters[0]["start"] = 0.0          # YouTube rejects a list not at 0:00
    broll = sorted(
        ({**b, "at": round(max(0.0, min(float(b["at"]), duration)), 3),
          "duration": round(float(b["duration"]), 3)} for b in doc.get("broll", [])),
        key=lambda b: b["at"])
    return {
        "title": doc.get("title", "").strip(),
        "description": doc.get("description", "").strip(),
        "chapters": chapters,
        "highlights": span(doc.get("highlights", [])),
        "cuts": span(doc.get("cuts", [])),
        "broll": broll,
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, help="the recording")
    ap.add_argument("--output", "-o", required=True, help="analysis JSON path")
    ap.add_argument("--transcript", help="transcribe.py .json or .srt, for "
                    "sharper timestamps and fewer misheard terms")
    ap.add_argument("--notes", help="anything the editor should know "
                    "(audience, what must stay, target length)")
    ap.add_argument("--gcs-uri", help="gs:// copy of the recording, for files "
                    "too large to send inline on Vertex")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"heads the chain (default {DEFAULT_MODEL})")
    ap.add_argument("--backend", choices=["auto", "vertex", "aistudio"],
                    default="auto")
    ap.add_argument("--chapters-out", help="YouTube-style chapters file "
                    "(default: <output>.chapters.txt)")
    args = ap.parse_args()

    source = Path(args.input)
    if not source.exists():
        die(f"input not found: {source}")
    meta = probe(source)
    if not meta["has_video"]:
        die(f"{source} has no video stream")

    extra = ""
    if args.transcript:
        extra += ("\nTranscript with timestamps (trust these times over your "
                  "own estimates):\n" + transcript_text(args.transcript) + "\n")
    if args.notes:
        extra += f"\nNotes from the author: {args.notes}\n"
    prompt = PROMPT.format(duration=meta["duration"], extra=extra)

    _, types = G.load_genai()
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=SCHEMA,
        temperature=0.2,
        # No tools are passed; leaving AFC on only buys an SDK warning per call.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True))

    with tempfile.TemporaryDirectory(prefix="cs-analyze-") as tmp:
        proxy = None
        if not args.gcs_uri:
            print(f"🎞  proxy of {source.name}  ({hms(meta['duration'])}) …")
            proxy = make_proxy(source, Path(tmp) / "proxy.mp4")
            print(f"  {G.DIM}{proxy.stat().st_size // 1024}KB{G.RESET}")

        def call(client, model):
            part = video_part(types, client, proxy, args.gcs_uri)
            response = client.models.generate_content(
                model=model, contents=[part, prompt], config=config)
            return response.text or None

        print("🧠 watching …")
        started = time.time()
        text, model, backend = G.attempt(
            G.chain_for(args.model, SEQUENCE), call, prefer=args.backend)

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        die(f"{backend}/{model} returned malformed JSON ({exc}):\n{text[:400]}")

    analysis = {
        "version": ANALYSIS_VERSION,
        "tool": "analyze",
        "source": str(source.resolve()),
        "duration": meta["duration"],
        "model": model,
        "backend": backend,
        **clean(raw, meta["duration"]),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(analysis, indent=2) + "\n")
    chapters_out = Path(args.chapters_out or output.with_suffix(".chapters.txt"))
    write_chapters(chapters_out, analysis["chapters"])

    cut = sum(c["end"] - c["start"] for c in analysis["cuts"])
    print(f"✓ {output}  ({backend}/{model}, {time.time() - started:.0f}s)")
    print(f"  “{analysis['title']}”")
    print(f"  {len(analysis['chapters'])} chapters, "
          f"{len(analysis['highlights'])} highlights, "
          f"{len(analysis['cuts'])} cuts ({hms(cut)}), "
          f"{len(analysis['broll'])} b-roll ideas")
    print(f"✓ {chapters_out}")
    print(f"  Next: review the JSON, then →  python3 autocut.py -i {source} "
          f"-o out/cut.json --analysis {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
