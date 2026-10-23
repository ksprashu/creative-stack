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

"""Transcribe a recording locally with whisper.cpp.

Runs on-device — no audio leaves the machine, which matters when the recording
shows an unreleased console or a customer's project ID.

    python3 transcribe.py -i raw.mov -o out/talk
        -> out/talk.srt  out/talk.vtt  out/talk.json  out/talk.txt

Technical vocabulary is the usual failure mode ("Cloud Run" -> "cloud rune").
Feed the jargon in as an initial prompt:

    python3 transcribe.py -i raw.mov -o out/talk \\
        --vocab "Cloud Run, GKE, Vertex AI, gcloud, Terraform, Pub/Sub"
"""

import argparse
import json
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# vidlib first: importing it is what puts scripts/ on sys.path, so hostenv
# above it fails whenever this runs standalone rather than from a harness.
from vidlib import (CACHE, clamp_cues, die, ffmpeg, hms, need, parse_srt, probe,
                    run, write_srt, write_vtt)
import hostenv  # noqa: E402

# whisper.cpp GGML weights. Bigger is slower but markedly better on accented
# speech and technical terms; small.en is the sweet spot for published captions.
MODEL_REPO = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
MODELS = {
    "tiny.en": 78,
    "base.en": 148,
    "small.en": 488,
    "medium.en": 1530,
    "large-v3-turbo": 1620,
}


def fetch_model(name):
    """Download a GGML model into the cache once; reuse it forever after."""
    if name not in MODELS:
        die(f"unknown model '{name}'. Choose from: {', '.join(MODELS)}")

    models_dir = CACHE / "whisper"
    models_dir.mkdir(parents=True, exist_ok=True)
    target = models_dir / f"ggml-{name}.bin"
    if target.exists() and target.stat().st_size > 1_000_000:
        return target

    url = f"{MODEL_REPO}/ggml-{name}.bin"
    print(f"⬇  fetching whisper model {name} (~{MODELS[name]}MB) → {target}")
    partial = target.with_suffix(".bin.part")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, \
                partial.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except (urllib.error.URLError, TimeoutError) as exc:
        partial.unlink(missing_ok=True)
        die(f"could not download {url}: {exc}")
    # Rename only on success so an interrupted download never looks complete.
    partial.rename(target)
    return target


def extract_audio(source, wav_path):
    """whisper.cpp only accepts 16 kHz mono PCM."""
    ffmpeg(["-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-y", str(wav_path)])


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, help="video or audio file")
    ap.add_argument("--output", "-o", required=True,
                    help="output prefix, without extension")
    ap.add_argument("--model", default="small.en", choices=sorted(MODELS))
    ap.add_argument("--language", "-l", default="en",
                    help="spoken language, or 'auto'")
    ap.add_argument("--vocab", default="",
                    help="comma-separated jargon to prime the decoder with")
    ap.add_argument("--max-len", type=int, default=42,
                    help="max characters per caption line (0 = whisper default)")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--vad", action="store_true",
                    help="voice-activity gate; drops hallucinated text in silence")
    args = ap.parse_args()

    source = Path(args.input)
    if not source.exists():
        die(f"input not found: {source}")

    whisper = need("whisper-cli", hostenv.whisper_hint())
    model = fetch_model(args.model)
    meta = probe(source)
    if not meta["has_audio"]:
        die(f"{source} has no audio track — nothing to transcribe.")

    out_prefix = Path(args.output)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="vidkit-asr-") as tmp:
        wav = Path(tmp) / "audio.wav"
        print(f"🎧 extracting audio  ({hms(meta['duration'])})")
        extract_audio(source, wav)

        cmd = [whisper, "-m", str(model), "-f", str(wav),
               "-of", str(out_prefix), "-l", args.language,
               "-t", str(args.threads),
               # -ojf, not -oj: the full JSON adds per-token timings, which
               # captions.py --style karaoke|highlight needs to land each word
               # exactly. It is a superset, so every -oj reader still works.
               "-osrt", "-ovtt", "-otxt", "-ojf",
               "-sow",          # split captions on word boundaries, not tokens
               "-np"]           # suppress the ggml backend banner
        if args.max_len:
            cmd += ["-ml", str(args.max_len)]
        if args.vocab:
            cmd += ["--prompt", args.vocab]
        if args.vad:
            cmd += ["--vad"]

        print(f"📝 transcribing with {args.model} …")
        run(cmd, capture=True)

    srt = out_prefix.with_suffix(".srt")
    if not srt.exists():
        die("whisper produced no .srt — check the audio track.")

    cues = clamp_cues(parse_srt(srt), meta["duration"])
    if not cues:
        die("every cue was empty or out of range — try a larger --model.")
    write_srt(srt, cues)
    write_vtt(out_prefix.with_suffix(".vtt"), cues)

    words = len(out_prefix.with_suffix(".txt").read_text().split()) \
        if out_prefix.with_suffix(".txt").exists() else 0

    print(f"✓ {srt}  ({len(cues)} cues, {words} words)")
    for suffix in (".vtt", ".txt", ".json"):
        if out_prefix.with_suffix(suffix).exists():
            print(f"  {out_prefix.with_suffix(suffix)}")
    print(f"  Next: cut the dead air →  python3 autocut.py -i {source} "
          f"-o {out_prefix.parent}/cut.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
