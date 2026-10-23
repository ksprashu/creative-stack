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

"""Generate voiceover narration from text with Gemini TTS or Chirp 3 HD.

    python3 voice.py -t "Welcome to the build pipeline tour." -o out/voice.wav
    python3 voice.py -i script.md -o out/voice.wav --voice Charon --style "calm, unhurried"
    python3 voice.py -i dialogue.txt -o out/chat.wav --voices Kore,Puck
    python3 voice.py -i script.md -o out/voice.wav --plan-only   # edit, then:
    python3 voice.py --from-plan out/voice.plan.json -o out/voice.wav
    python3 voice.py -i script.md -o out/voice.wav --engine chirp --lang en-GB

A script is split into paragraph-sized chunks, each chunk is one TTS call, and
the results are joined with a short silence. The chunk list is written to
`<output>.plan.json` before anything is synthesised, so a mispronounced word or
a bad split is a one-line edit and a `--from-plan` re-run of the same chunks.

A file whose every line reads `Name: words` with exactly two names becomes a
two-speaker dialogue — the API's multi-speaker mode tops out at two voices.

`--engine chirp` uses Cloud Text-to-Speech's Chirp 3 HD voices instead: the same
voice names, no style prompt, but steady across long reads, 40+ locales via
`--lang`, and no speaker limit (each turn is its own request). Vertex project
and ADC only — there is no AI Studio path.

Output is 24kHz mono WAV at whatever level the model produced. It is not
mastered: run it through `/sound clean.py` and `mix.py`, which own loudness.
"""

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genailib as G  # noqa: E402

# Best first. Vertex serves gemini-3.8-flash-tts from `global` even though
# `models.list()` does not name it — generating is the only honest probe. It
# needs google-genai >= 2.31 to express the request; older SDKs skip 3.8 and
# land on the 3.1 preview (us-central1) or the 2.5 models (global).
SEQUENCE = [
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-tts",
    "gemini-2.5-pro-tts",
]

# The prebuilt voices every Gemini TTS model shares, with Google's own one-word
# character for each. Not exhaustive — the 3.8 models add a regional library and
# designed voices, which `--voice voice_...` passes straight through.
VOICES = {
    "Zephyr": "bright", "Puck": "upbeat", "Charon": "informative",
    "Kore": "firm", "Fenrir": "excitable", "Leda": "youthful",
    "Orus": "firm", "Aoede": "breezy", "Callirrhoe": "easy-going",
    "Autonoe": "bright", "Enceladus": "breathy", "Iapetus": "clear",
    "Umbriel": "easy-going", "Algieba": "smooth", "Despina": "smooth",
    "Erinome": "clear", "Algenib": "gravelly", "Rasalgethi": "informative",
    "Laomedeia": "upbeat", "Achernar": "soft", "Alnilam": "firm",
    "Schedar": "even", "Gacrux": "mature", "Pulcherrima": "forward",
    "Achird": "friendly", "Zubenelgenubi": "casual", "Vindemiatrix": "gentle",
    "Sadachbia": "lively", "Sadaltager": "knowledgeable", "Sulafat": "warm",
}
DEFAULT_VOICE = "Charon"
SECOND_VOICE = "Kore"

# Well under the request limit (Vertex caps the text field at 4,000 bytes) and
# short enough that one bad take costs seconds, not the whole narration.
MAX_CHARS = 1500
RATE = 24000
TURN_GAP = 0.25  # Chirp dialogue: silence between turns inside one chunk
SPEAKER_LINE = re.compile(r"^\s*([A-Z][\w .'-]{0,30}?)\s*:\s+(\S.*)$")


# ------------------------------------------------------------------ script ---

def plain(text):
    """Markdown to the words a narrator would actually say."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}(#{1,6}|[-*+]|\d+\.)\s+", "", text, flags=re.M)
    return re.sub(r"(\*\*|__|`|(?<!\w)[*_](?=\S))", "", text)


def split_long(paragraph):
    """A paragraph over MAX_CHARS, broken at sentence ends."""
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
        if current and len(current) + len(sentence) + 1 > MAX_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    return pieces + ([current] if current else [])


def chunk_narration(text):
    """One chunk per paragraph — a paragraph break is where a narrator breathes."""
    chunks = []
    for paragraph in re.split(r"\n\s*\n", plain(text)):
        paragraph = " ".join(paragraph.split())
        if paragraph:
            chunks.extend({"text": piece} for piece in split_long(paragraph))
    return chunks


def parse_dialogue(text):
    """[(speaker, line)] if every line is `Name: words`, else None."""
    lines = [line for line in text.splitlines() if line.strip()]
    turns = [SPEAKER_LINE.match(line) for line in lines]
    if not lines or not all(turns):
        return None
    return [(m.group(1), m.group(2).strip()) for m in turns]


def chunk_dialogue(turns):
    """Consecutive turns packed into chunks, never splitting a turn."""
    chunks, current, size = [], [], 0
    for speaker, line in turns:
        if current and size + len(line) > MAX_CHARS:
            chunks.append({"turns": current})
            current, size = [], 0
        current.append({"speaker": speaker, "text": line})
        size += len(line)
    return chunks + ([{"turns": current}] if current else [])


def build_plan(text, args):
    turns = parse_dialogue(text)
    plan = {"engine": args.engine, "style": args.style or "", "gap": args.gap,
            "model": args.model}
    if args.engine == "chirp":
        plan["lang"] = args.lang
    if turns:
        speakers = list(dict.fromkeys(s for s, _ in turns))
        if len(speakers) > 2 and args.engine == "chirp":
            if not args.voices or len(args.voices.split(",")) < len(speakers):
                G.die(f"{len(speakers)} speakers need --voices with "
                      f"{len(speakers)} names")
        elif len(speakers) > 2:
            G.die(f"{len(speakers)} speakers ({', '.join(speakers)}) — Gemini "
                  "TTS voices at most two per request. Split the script.")
        voices = (args.voices.split(",") if args.voices
                  else [args.voice or DEFAULT_VOICE, SECOND_VOICE])
        if len(speakers) == 2 and len(voices) < 2:
            G.die("a two-speaker dialogue needs --voices A,B")
        plan["speakers"] = dict(zip(speakers, (v.strip() for v in voices)))
        plan["chunks"] = chunk_dialogue(turns)
    else:
        plan["voice"] = args.voice or DEFAULT_VOICE
        plan["chunks"] = chunk_narration(text)
    if not plan["chunks"]:
        G.die("nothing to say — the script is empty after stripping markdown")
    return plan


# --------------------------------------------------------------- synthesis ---

def version(model):
    match = re.match(r"gemini-(\d+(?:\.\d+)?)", model)
    return float(match.group(1)) if match else 0.0


def sdk_has_new_shape(types):
    """Gemini 3.8 TTS wants `voice_config.voice` and `speech_metadata` on parts.

    google-genai grew both around 2.30; an older SDK cannot even express the
    request, so the 3.8 models are dropped from the chain rather than sent a
    payload the client-side validation will reject.
    """
    return ("voice" in types.VoiceConfig.model_fields
            and "speech_metadata" in types.Part.model_fields)


def voice_config(types, name, new_shape):
    if new_shape:
        return types.VoiceConfig(voice=name)
    return types.VoiceConfig(
        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=name))


def request(types, chunk, plan, model):
    """(contents, speech_config) in the dialect this model family speaks.

    3.8 treats text as a verbatim transcript, so delivery notes travel in
    `speech_metadata.style` and each dialogue turn is its own part. Earlier
    models read the whole text as an instruction, so the style is a lead-in
    sentence and the dialogue is a `Name: line` block.
    """
    new_shape = version(model) >= 3.8
    style = plan.get("style", "")
    speakers = plan.get("speakers")

    if speakers:
        speech = types.SpeechConfig(
            multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                speaker_voice_configs=[
                    types.SpeakerVoiceConfig(
                        speaker=name, voice_config=voice_config(types, v, new_shape))
                    for name, v in speakers.items()]))
        if new_shape:
            parts = [types.Part(text=t["text"], speech_metadata=types.SpeechMetadata(
                speaker=t["speaker"], style=style or None)) for t in chunk["turns"]]
            return [types.Content(role="user", parts=parts)], speech
        names = " and ".join(speakers)
        lead = f"TTS the following conversation between {names}"
        lead += f", delivered {style}:" if style else ":"
        body = "\n".join(f"{t['speaker']}: {t['text']}" for t in chunk["turns"])
        return f"{lead}\n{body}", speech

    speech = types.SpeechConfig(
        voice_config=voice_config(types, plan["voice"], new_shape))
    if new_shape:
        meta = types.SpeechMetadata(style=style) if style else None
        part = types.Part(text=chunk["text"], speech_metadata=meta)
        return [types.Content(role="user", parts=[part])], speech
    return (f"Say in a {style} voice: {chunk['text']}" if style
            else chunk["text"]), speech


def to_pcm(data, mime):
    """(pcm_bytes, sample_rate) from whatever the model returned.

    3.8 answers a unary call with a complete WAV; everything earlier sends
    headerless 16-bit PCM and names the rate only in the MIME type. Sniff the
    RIFF magic rather than trusting the MIME string alone.
    """
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data)) as wav:
            if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
                G.die(f"unexpected TTS format: {wav.getnchannels()}ch "
                      f"{8 * wav.getsampwidth()}-bit")
            return wav.readframes(wav.getnframes()), wav.getframerate()
    match = re.search(r"rate=(\d+)", mime or "")
    return data, int(match.group(1)) if match else RATE


def synthesise(plan, args):
    """[(pcm, rate)] for every chunk, plus the model/backend that answered."""
    _, types = G.load_genai()
    sequence = SEQUENCE
    if not sdk_has_new_shape(types):
        sequence = [m for m in SEQUENCE if version(m) < 3.8]
        G.warn("google-genai is too old for the Gemini 3.8 TTS request shape — "
               "skipping 3.8. Upgrade: uv pip install -U google-genai")
    primary = args.model or plan.get("model")
    chain = G.chain_for(primary, sequence) if primary else sequence

    def call_for(chunk):
        def call(client, model):
            contents, speech = request(types, chunk, plan, model)
            response = client.models.generate_content(
                model=model, contents=contents,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"], speech_config=speech))
            for candidate in response.candidates or []:
                for part in (candidate.content.parts if candidate.content else []):
                    if part.inline_data and part.inline_data.data:
                        return to_pcm(part.inline_data.data, part.inline_data.mime_type)
            return None
        return call

    takes, used = [], None
    for index, chunk in enumerate(plan["chunks"]):
        # Once a model answers, keep it at the head: switching voice models
        # between paragraphs is audible even when the voice name is the same.
        if used:
            chain = G.chain_for(used[0], chain)
        take, model, backend = G.attempt(chain, call_for(chunk), args.backend,
                                         quiet=index > 0)
        used = (model, backend)
        takes.append(take)
        seconds = len(take[0]) / 2 / take[1]
        print(f"  {G.DIM}chunk {index + 1}/{len(plan['chunks'])}: "
              f"{seconds:.1f}s  ({backend}/{model}){G.RESET}")
    return takes, used


def write(takes, gap, out):
    rate = takes[0][1]
    if any(r != rate for _, r in takes):
        G.die("chunks came back at different sample rates — re-run with --model")
    silence = b"\x00\x00" * int(rate * gap)
    pcm = silence.join(p for p, _ in takes)

    wav_path = out if out.suffix.lower() == ".wav" else out.with_suffix(".tmp.wav")
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    if wav_path != out:
        ffmpeg = shutil.which("ffmpeg") or G.die(
            "ffmpeg not found. " + G.hostenv.package_hint("ffmpeg"))
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-y", "-i", str(wav_path), str(out)], check=True)
        wav_path.unlink()
    return len(pcm) / 2 / rate


def synthesise_chirp(plan):
    """[(pcm, rate)] per chunk from Cloud Text-to-Speech, Chirp 3 HD voices.

    Chirp takes no delivery prompt and has no multi-speaker mode, so a
    dialogue chunk is one request per turn joined with TURN_GAP of silence.
    """
    try:
        from google.cloud import texttospeech as tts
        from google.api_core.client_options import ClientOptions
    except ImportError:
        G.die("--engine chirp needs google-cloud-texttospeech:\n"
              "  uv pip install -r requirements.txt")
    project = G.vertex_project() or G.die(
        "--engine chirp bills a GCP project and none is configured. Run:\n"
        "  gcloud config set project <id>")
    if plan.get("style"):
        G.warn("Chirp 3 HD takes no style prompt — --style is ignored")
    lang = plan.get("lang", "en-US")
    client = tts.TextToSpeechClient(
        client_options=ClientOptions(quota_project_id=project))
    audio = tts.AudioConfig(audio_encoding=tts.AudioEncoding.LINEAR16,
                            sample_rate_hertz=RATE)

    def say(text, voice):
        name = voice if "Chirp" in voice else f"{lang}-Chirp3-HD-{voice}"
        response = client.synthesize_speech(
            input=tts.SynthesisInput(text=text),
            voice=tts.VoiceSelectionParams(language_code=lang, name=name),
            audio_config=audio)
        return to_pcm(response.audio_content, "")[0]

    takes = []
    for index, chunk in enumerate(plan["chunks"]):
        if "turns" in chunk:
            silence = b"\x00\x00" * int(RATE * TURN_GAP)
            pcm = silence.join(say(t["text"], plan["speakers"][t["speaker"]])
                               for t in chunk["turns"])
        else:
            pcm = say(chunk["text"], plan["voice"])
        takes.append((pcm, RATE))
        print(f"  {G.DIM}chunk {index + 1}/{len(plan['chunks'])}: "
              f"{len(pcm) / 2 / RATE:.1f}s  (texttospeech/chirp3-hd){G.RESET}")
    return takes, ("chirp3-hd", "texttospeech")


# -------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--text", "-t", help="the words to speak")
    src.add_argument("--input", "-i", help="a .txt or .md script")
    src.add_argument("--from-plan", help="render an existing (edited) .plan.json")
    ap.add_argument("--output", "-o", help=".wav (native) or anything ffmpeg writes")
    ap.add_argument("--voice", help=f"prebuilt voice (default {DEFAULT_VOICE}) "
                                    "or a designed voice_... id")
    ap.add_argument("--voices", help="two voices for a dialogue, e.g. Kore,Puck")
    ap.add_argument("--style", help='delivery, e.g. "warm, unhurried, smiling"')
    ap.add_argument("--gap", type=float, default=0.6,
                    help="seconds of silence between chunks (default 0.6)")
    ap.add_argument("--engine", default="gemini", choices=["gemini", "chirp"],
                    help="gemini (styleable, default) or chirp (Chirp 3 HD)")
    ap.add_argument("--lang", default="en-US",
                    help="Chirp locale, e.g. en-GB, hi-IN (default en-US)")
    ap.add_argument("--model", help="head the model chain with this model")
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "vertex", "aistudio"])
    ap.add_argument("--plan-only", action="store_true",
                    help="write <output>.plan.json and stop")
    ap.add_argument("--list-voices", action="store_true")
    args = ap.parse_args()

    if args.list_voices:
        for name, character in VOICES.items():
            print(f"  {name:<15} {character}")
        return 0
    if not args.output:
        ap.error("--output is required")
    out = G.prepare(args.output)
    plan_path = out.with_suffix(".plan.json")

    if args.from_plan:
        plan = json.loads(Path(args.from_plan).read_text())
    elif args.text or args.input:
        text = args.text or Path(args.input).expanduser().read_text()
        plan = build_plan(text, args)
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
        print(f"  {G.DIM}plan: {plan_path}  ({len(plan['chunks'])} chunk(s)){G.RESET}")
    else:
        ap.error("one of --text, --input or --from-plan is required")
    if args.plan_only:
        return 0

    who = (", ".join(f"{s}={v}" for s, v in plan["speakers"].items())
           if plan.get("speakers") else plan["voice"])
    print(f"◈ {len(plan['chunks'])} chunk(s) → {out.name}  {G.DIM}({who}){G.RESET}")
    if plan.get("engine") == "chirp":
        takes, (model, backend) = synthesise_chirp(plan)
    else:
        takes, (model, backend) = synthesise(plan, args)
    seconds = write(takes, plan.get("gap", args.gap), out)

    script = " ".join(c.get("text") or " ".join(t["text"] for t in c["turns"])
                      for c in plan["chunks"])
    G.provenance(out, f"[voice: {who}; style: {plan.get('style') or '-'}] "
                      f"{script[:400]}", model, backend)
    G.report(out, f"{seconds:.1f}s, {takes[0][1] // 1000}kHz mono")
    print(f"  {G.DIM}not mastered — run /sound clean.py, then mix.py "
          f"under any music{G.RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
