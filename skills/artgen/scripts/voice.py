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

"""Generate voiceover narration from text with Gemini TTS, Cloud TTS, Chirp 3 HD or Gemini Live.

    python3 voice.py -t "Welcome to the build pipeline tour." -o out/voice.wav
    python3 voice.py -i script.md -o out/voice.wav --voice Charon --style "calm, unhurried"
    python3 voice.py -i dialogue.txt -o out/chat.wav --voices Kore,Puck
    python3 voice.py -i script.md -o out/voice.wav --plan-only   # edit, then:
    python3 voice.py --from-plan out/voice.plan.json -o out/voice.wav
    python3 voice.py -i script.md -o out/voice.wav --engine cloud --lang en-IN
    python3 voice.py -i script.md -o out/voice.wav --engine chirp --lang en-GB
    python3 voice.py -i script.md -o out/voice.wav --engine live --voice Puck
    python3 voice.py --own-voice take.m4a -o out/voice.wav
    python3 voice.py -i dialogue.txt -o out/chat.wav --voices Kore --human Sam
    python3 voice.py -i script.md -o out/voice.wav --clone-from me.wav --consent consent.wav

A script is split into paragraph-sized chunks, each chunk is one TTS call, and
the results are joined with a short silence. The chunk list is written to
`<output>.plan.json` before anything is synthesised, so a mispronounced word or
a bad split is a one-line edit and a `--from-plan` re-run of the same chunks.

A file whose every line reads `Name: words`, with at least two names and no
digits in them, becomes a dialogue — two speakers at most on Gemini, whose
multi-speaker mode tops out there. One name throughout is narration with the
labels dropped. `--narration` reads the file as prose whatever it looks like.

`--engine cloud` sends the same prebuilt voices and style prompt to the
Gemini-TTS models Cloud Text-to-Speech serves (the 3.1 Flash preview, then
2.5 Pro and Flash), with `--lang` naming the locale. Vertex project and ADC.

`--engine chirp` uses Cloud Text-to-Speech's Chirp 3 HD voices instead: the same
voice names, no style prompt, but steady across long reads, 40+ locales via
`--lang`, and no speaker limit (each turn is its own request). Vertex project
and ADC only — there is no AI Studio path.

`--engine live` reads the script through a Gemini Live native-audio model:
the most conversational delivery of the three, but it is a dialogue model
being asked to read, so every take is checked against the model's own
transcript and retried once when it paraphrases.

Two routes put the user's own voice on the track instead. `--own-voice`
takes a recording they made (see `/sound record.py`) and converts it to the
same 24kHz mono WAV, so nothing downstream can tell the difference.
`--clone-from` with `--consent` replicates their voice through the AI Studio
voices API and reads the script with it; the consent clip must recite the
statement `--consent-statement` prints. The returned voice key is used in
memory and never written to disk unless `--store-voice` asks Google to keep a
reusable `voice_...` id instead.

`--human` mixes the user into a synthetic read: `--human Sam` makes every
line of the speaker Sam theirs, `--human 1,4` the first and fourth paragraphs
of a narration. Each such line gets a take file in `<output>.takes/`, listed
with its words in `SCRIPT.txt` there; record them, re-run with `--from-plan`,
and the takes are spliced in at the right places. Any chunk in the plan with
an `"audio"` path is read from that file instead of synthesised.

Output is 24kHz mono WAV at whatever level the model produced. It is not
mastered: run it through `/sound clean.py` and `mix.py`, which own loudness.
"""

import argparse
import asyncio
import base64
import difflib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
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

# Cloud Text-to-Speech serves its own Gemini-TTS list, picked per request by
# `model_name`. It answers 3.8 with a 400 "not supported", so its chain starts
# at the 3.1 preview, and every refusal falls through to the next model.
CLOUD_SEQUENCE = [
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-pro-tts",
    "gemini-2.5-flash-tts",
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
# Bytes, not characters: Devanagari is three bytes a character in UTF-8.
MAX_BYTES = 1500
RATE = 24000
TURN_GAP = 0.25  # Chirp and Live dialogue: silence between turns in one chunk

# Live models are dialogue models. 3.1 is the March 2026 preview; the 2.5
# native-audio preview is the fallback where 3.1 is not served yet. Both are
# AI Studio names: Vertex serves neither and has its own `gemini-live-…`.
LIVE_SEQUENCE = [
    "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-native-audio-preview-12-2025",
    "gemini-live-2.5-flash-native-audio",
]
# Word-level similarity between the script and what the Live model says it
# said. Below this the take is a paraphrase or a reply, not a reading.
LIVE_MIN_MATCH = 0.9

# Replicated voices are created against one model and only that model can
# speak them, so a replicated read has no fallback chain.
REPLICATION_MODEL = "gemini-3.8-flash-tts"
CLONE_SECONDS = (10.0, 30.0)
CLONED = "(replicated)"  # what the plan records in place of the voice key
CONSENT = ("I am the owner of this voice and I consent to Google using this "
           "voice to create a synthetic voice model.")
CONSENT_DOCS = ("https://ai.google.dev/gemini-api/docs/voice-replication"
                "#consent-phrases-by-language")
HUMAN = "(human)"  # a speaker the user records instead of a voice
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


def size(text):
    return len(text.encode())


def split_long(paragraph):
    """A paragraph over MAX_BYTES, broken at sentence ends."""
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?।])\s+", paragraph):
        if current and size(current) + size(sentence) + 1 > MAX_BYTES:
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
    """[(speaker, line)] if every line is `Name: words`, else None.

    A label with a digit in it is a list (`Step 1:`), not a person.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    turns = [SPEAKER_LINE.match(line) for line in lines]
    if not lines or not all(turns):
        return None
    if any(re.search(r"\d", m.group(1)) for m in turns):
        return None
    return [(m.group(1), m.group(2).strip()) for m in turns]


def chunk_dialogue(turns):
    """Consecutive turns packed into chunks, never splitting a turn."""
    chunks, current, used = [], [], 0
    for speaker, line in turns:
        if current and used + size(line) > MAX_BYTES:
            chunks.append({"turns": current})
            current, used = [], 0
        current.append({"speaker": speaker, "text": line})
        used += size(line)
    return chunks + ([{"turns": current}] if current else [])


def human_marks(value):
    """(speaker names, 1-based chunk numbers) from a `--human` list.

    Speaker names never contain digits (`parse_dialogue` reads `Step 1:` as a
    list), so a number can only mean a paragraph.
    """
    marks = [m.strip() for m in (value or "").split(",") if m.strip()]
    return ({m for m in marks if not m.isdigit()},
            {int(m) for m in marks if m.isdigit()})


def slug(name):
    return re.sub(r"[^\w-]+", "-", name).strip("-") or "line"


def build_plan(text, args, takes="voice.takes"):
    """The chunk list. `takes` is where human lines are recorded, relative
    to the plan file."""
    turns = None if args.narration else parse_dialogue(text)
    if turns and len({s for s, _ in turns}) == 1:
        # One voice throughout is narration; multi-speaker mode wants two.
        text, turns = "\n\n".join(line for _, line in turns), None
    plan = {"engine": args.engine, "style": args.style or "", "gap": args.gap,
            "model": args.model}
    if args.engine in ("chirp", "cloud"):
        plan["lang"] = args.lang or "en-US"
    names, numbers = human_marks(args.human)
    if args.clone_from:
        if turns:
            G.die("--clone-from reads narration in one voice; a dialogue needs "
                  "--narration or a second, prebuilt voice per speaker")
        plan["replicated"] = True
    if turns:
        if numbers:
            G.die("--human takes speaker names in a dialogue, not paragraph numbers")
        speakers = list(dict.fromkeys(s for s, _ in turns))
        unknown = names - set(speakers)
        if unknown:
            G.die(f"--human {', '.join(sorted(unknown))}: not a speaker in this "
                  f"script ({', '.join(speakers)})")
        # Only the synthetic speakers count against an engine's voice limit.
        synthetic = [s for s in speakers if s not in names]
        if len(synthetic) > 2 and args.engine == "chirp":
            if not args.voices or len(args.voices.split(",")) < len(synthetic):
                G.die(f"{len(synthetic)} speakers need --voices with "
                      f"{len(synthetic)} names")
        elif len(synthetic) > 2:
            G.die(f"{len(synthetic)} speakers ({', '.join(synthetic)}) — Gemini "
                  "TTS voices at most two per request. Split the script.")
        first = args.voice or DEFAULT_VOICE
        second = DEFAULT_VOICE if first == SECOND_VOICE else SECOND_VOICE
        voices = args.voices.split(",") if args.voices else [first, second]
        if len(synthetic) == 2 and len(voices) < 2:
            G.die("a two-speaker dialogue needs --voices A,B")
        assigned = dict(zip(synthetic, (v.strip() for v in voices)))
        plan["speakers"] = {s: assigned.get(s, HUMAN) for s in speakers}
        plan["chunks"] = chunk_dialogue(turns)
        line = 0
        for chunk in plan["chunks"]:
            for turn in chunk["turns"]:
                line += 1
                if turn["speaker"] in names:
                    turn["audio"] = f"{takes}/{line:02d}-{slug(turn['speaker'])}.wav"
    else:
        if names:
            G.die(f"--human {', '.join(sorted(names))}: this script is narration; "
                  "pass paragraph numbers, e.g. --human 1,4")
        plan["voice"] = CLONED if args.clone_from else (args.voice or DEFAULT_VOICE)
        plan["chunks"] = chunk_narration(text)
        beyond = {n for n in numbers if not 1 <= n <= len(plan["chunks"])}
        if beyond:
            G.die(f"--human {', '.join(map(str, sorted(beyond)))}: the script has "
                  f"{len(plan['chunks'])} paragraph(s)")
        for n in numbers:
            plan["chunks"][n - 1]["audio"] = f"{takes}/{n:02d}-narration.wav"
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


def tts_chain(primary, new_shape):
    """The fallback chain, `primary` first. The SDK filter runs last, so an
    asked-for 3.8 model is dropped too, not put back at the head."""
    chain = G.chain_for(primary, SEQUENCE) if primary else list(SEQUENCE)
    return chain if new_shape else [m for m in chain if version(m) < 3.8]


def custom_voice(plan):
    """True when the plan speaks in a voices-API voice (replicated or designed).

    Those live in AI Studio and only the 3.8 model they were made against can
    speak them, whether they came from --clone-from, --voice voice_... or an
    edited plan. A stock-voice fallback chain would just 400 on every model.
    """
    names = [plan.get("voice") or ""] + list((plan.get("speakers") or {}).values())
    return bool(plan.get("replicated")) or any(
        n.startswith(("voice_", "voicekey_")) for n in names)


def synthesise(plan, args):
    """[(pcm, rate)] for every chunk, plus the model/backend that answered."""
    _, types = G.load_genai()
    new_shape = sdk_has_new_shape(types)
    if not new_shape:
        G.warn("google-genai is too old for the Gemini 3.8 TTS request shape — "
               "skipping 3.8. Upgrade: uv pip install -U google-genai")
    chain = tts_chain(args.model or plan.get("model"), new_shape)
    pinned = custom_voice(plan)
    if pinned:
        if args.backend == "vertex":
            G.die("voice_... and replicated voices live in the AI Studio voices "
                  "API; drop --backend vertex")
        if not new_shape:
            G.die("a replicated voice needs the Gemini 3.8 request shape: "
                  "uv pip install -U google-genai")
        chain = [REPLICATION_MODEL]

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
        # Once a model answers, it is the only model: switching voice models
        # between paragraphs is audible even when the voice name is the same.
        # Retries and the other backend still apply; a different model does not.
        if used:
            chain = [used[0]]
        backend = "aistudio" if pinned else args.backend
        take, model, backend = G.attempt(chain, call_for(chunk), backend,
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
        subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-y", "-i", str(wav_path), str(out)], check=True)
        wav_path.unlink()
    return len(pcm) / 2 / rate


def cloud_tts(engine):
    """(texttospeech module, client, 24kHz LINEAR16 AudioConfig), billed to the
    configured project. Chirp and Cloud Gemini-TTS share it."""
    try:
        from google.cloud import texttospeech as tts
        from google.api_core.client_options import ClientOptions
    except ImportError:
        G.die(f"--engine {engine} needs google-cloud-texttospeech:\n"
              "  uv pip install -r requirements.txt")
    project = G.vertex_project() or G.die(
        f"--engine {engine} bills a GCP project and none is configured. Run:\n"
        "  gcloud config set project <id>")
    client = tts.TextToSpeechClient(
        client_options=ClientOptions(quota_project_id=project))
    audio = tts.AudioConfig(audio_encoding=tts.AudioEncoding.LINEAR16,
                            sample_rate_hertz=RATE)
    return tts, client, audio


def cloud_input(tts, chunk, plan, model):
    """(SynthesisInput, VoiceSelectionParams) for one Cloud Gemini-TTS request.

    Cloud TTS keeps the style out of the transcript in its own `prompt`
    field, and takes a dialogue as `Name: line` text with each name mapped
    to a prebuilt voice.
    """
    lang = plan.get("lang", "en-US")
    extra = {"prompt": plan["style"]} if plan.get("style") else {}
    if "turns" in chunk:
        text = "\n".join(f"{t['speaker']}: {t['text']}" for t in chunk["turns"])
        voice = tts.VoiceSelectionParams(
            language_code=lang, model_name=model,
            multi_speaker_voice_config=tts.MultiSpeakerVoiceConfig(
                speaker_voice_configs=[
                    tts.MultispeakerPrebuiltVoice(speaker_alias=s, speaker_id=v)
                    for s, v in plan["speakers"].items()]))
    else:
        text = chunk["text"]
        voice = tts.VoiceSelectionParams(language_code=lang, name=plan["voice"],
                                         model_name=model)
    return tts.SynthesisInput(text=text, **extra), voice


def synthesise_cloud(plan, args):
    """[(pcm, rate)] per chunk from Cloud Text-to-Speech's Gemini-TTS models.

    The first chunk walks the chain until a model answers; that model then
    reads every other chunk, for the same reason `synthesise` pins one.
    """
    from google.api_core import exceptions as gexc

    if custom_voice(plan):
        G.die("voice_... and replicated voices speak only through the AI Studio "
              "voices API; use --engine gemini")
    tts, client, audio = cloud_tts("cloud")
    primary = args.model or plan.get("model")
    chain = G.chain_for(primary, CLOUD_SEQUENCE) if primary else list(CLOUD_SEQUENCE)
    refused = (gexc.InvalidArgument, gexc.NotFound, gexc.PermissionDenied)
    takes = []
    for index, chunk in enumerate(plan["chunks"]):
        for model in chain:
            synthesis, voice = cloud_input(tts, chunk, plan, model)
            try:
                response = client.synthesize_speech(input=synthesis, voice=voice,
                                                    audio_config=audio)
                break
            except refused as exc:
                reason = str(exc).splitlines()[0][:160]
                if model == chain[-1]:
                    G.die(f"Cloud TTS refused {model}: {reason}")
                G.warn(f"{model}: {reason} — trying the next model")
        chain = [model]
        takes.append(to_pcm(response.audio_content, ""))
        print(f"  {G.DIM}chunk {index + 1}/{len(plan['chunks'])}: "
              f"{len(takes[-1][0]) / 2 / takes[-1][1]:.1f}s  "
              f"(texttospeech/{model}){G.RESET}")
    return takes, (model, "texttospeech")


def synthesise_chirp(plan):
    """[(pcm, rate)] per chunk from Cloud Text-to-Speech, Chirp 3 HD voices.

    Chirp takes no delivery prompt and has no multi-speaker mode, so a
    dialogue chunk is one request per turn joined with TURN_GAP of silence.
    """
    tts, client, audio = cloud_tts("chirp")
    if plan.get("style"):
        G.warn("Chirp 3 HD takes no style prompt — --style is ignored")
    lang = plan.get("lang", "en-US")

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


# -------------------------------------------------------------------- live ---

def words(text):
    return re.findall(r"[\w']+", text.lower())


def match(said, heard):
    """0..1 — how closely the Live transcript follows the script, word for word."""
    return difflib.SequenceMatcher(None, words(said), words(heard)).ratio()


def live_instruction(style):
    note = ("You are a voiceover artist. Read the user's text aloud exactly as "
            "written, word for word, and nothing else. Do not answer it, comment "
            "on it, greet anyone or add words of your own.")
    return note + (f" Delivery: {style}." if style else "")


async def live_take(client, types, model, text, voice, style):
    """(pcm, rate, transcript) for one reading over a Live session."""
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
        system_instruction=live_instruction(style),
        output_audio_transcription=types.AudioTranscriptionConfig())
    pcm, rate, heard = bytearray(), RATE, []
    async with client.aio.live.connect(model=model, config=config) as session:
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]),
            turn_complete=True)
        async for message in session.receive():
            content = message.server_content
            if not content:
                continue
            # One event can carry audio and transcript together on 3.1, so
            # every part of every event is read, not just the first.
            for part in (content.model_turn.parts if content.model_turn else None) or []:
                if part.inline_data and part.inline_data.data:
                    chunk, rate = to_pcm(part.inline_data.data, part.inline_data.mime_type)
                    pcm += chunk
            if content.output_transcription and content.output_transcription.text:
                heard.append(content.output_transcription.text)
            if content.turn_complete:
                break
    return bytes(pcm), rate, "".join(heard)


def best_read(take, text):
    """(pcm, rate) from up to two Live takes, or None if neither had audio.

    `take()` returns (pcm, rate, transcript). A take that matches the script
    ends it; otherwise the closer of the two is kept, and an empty retry never
    throws away a first take that did have audio.
    """
    best = None
    for _ in range(2):
        pcm, rate, heard = take()
        if not pcm:
            continue
        score = match(text, heard) if heard else 1.0
        if best is None or score > best[2]:
            best = (pcm, rate, score)
        if score >= LIVE_MIN_MATCH:
            break
    if best is None:
        return None
    if best[2] < LIVE_MIN_MATCH:
        G.warn(f"Live read drifted from the script ({best[2]:.0%} word "
               f"match): \"{text[:60]}…\" — listen, or use --engine gemini")
    return best[0], best[1]


def synthesise_live(plan, args):
    """[(pcm, rate)] per chunk from a Gemini Live model, one session per read.

    Like Chirp, a dialogue is one read per turn joined with TURN_GAP. Each read
    is checked against its transcript; a paraphrase gets one more take, and the
    closer of the two is kept with a warning.
    """
    _, types = G.load_genai()
    style = plan.get("style", "")

    def read_for(text, voice):
        def call(client, model):
            return best_read(lambda: asyncio.run(
                live_take(client, types, model, text, voice, style)), text)
        return call

    primary = args.model or plan.get("model")
    chain = G.chain_for(primary, LIVE_SEQUENCE) if primary else list(LIVE_SEQUENCE)
    takes, used = [], None
    for index, chunk in enumerate(plan["chunks"]):
        reads = ([(t["text"], plan["speakers"][t["speaker"]]) for t in chunk["turns"]]
                 if "turns" in chunk else [(chunk["text"], plan["voice"])])
        pieces = []
        for text, voice in reads:
            # Same rule as TTS: once a model answers, it reads everything.
            take, model, backend = G.attempt([used[0]] if used else chain,
                                             read_for(text, voice), args.backend,
                                             quiet=used is not None)
            used = (model, backend)
            pieces.append(take)
        rate = pieces[0][1]
        silence = b"\x00\x00" * int(rate * TURN_GAP)
        takes.append((silence.join(p for p, _ in pieces), rate))
        print(f"  {G.DIM}chunk {index + 1}/{len(plan['chunks'])}: "
              f"{len(takes[-1][0]) / 2 / rate:.1f}s  ({used[1]}/{used[0]}){G.RESET}")
    return takes, used


# ------------------------------------------------------------- own voice ---

def ffmpeg():
    return G.hostenv.ffmpeg_bin("ffmpeg") or G.die(
        "ffmpeg not found. " + G.hostenv.ffmpeg_install_hint())


def to_wav(src, dest, rate=RATE):
    """Any recording ffmpeg can read, as mono 16-bit WAV (24kHz by default)."""
    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", str(rate),
                    "-c:a", "pcm_s16le", str(dest)], check=True)
    return dest


def seconds_of(wav_path):
    with wave.open(str(wav_path)) as wav:
        return wav.getnframes() / wav.getframerate()


def use_own_voice(src, out):
    """The user's own recording, in the format a synthetic read would have."""
    src = Path(src).expanduser()
    if not src.is_file():
        G.die(f"no recording at {src}")
    # Convert into a scratch file first: `-o` may be the recording itself, and
    # ffmpeg truncates its output before it has read the input.
    with tempfile.TemporaryDirectory() as tmp:
        wav = to_wav(src, Path(tmp) / "own.wav")
        seconds = seconds_of(wav)
        if out.suffix.lower() == ".wav":
            shutil.move(str(wav), str(out))
        else:
            subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
                            "-y", "-i", str(wav), str(out)], check=True)
    # Not G.provenance: that sidecar says "generated-by", and this was not.
    Path(str(out) + ".txt").write_text(
        f"recorded-by: human (own voice, not synthesised)\n"
        f"source: {src.name}\n"
        f"converted-at: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
    return seconds


# ------------------------------------------------------------ human mix ---

def split_human(plan):
    """(plan of what is synthesised, layout of every chunk in speaking order).

    Each layout entry is the list of pieces one chunk is made of:
    ("tts", index into the synthetic plan's chunks) or
    ("human", take path, who, words). A chunk with an `audio` path is one
    human piece; a dialogue chunk is cut around its human turns, and each run
    of synthetic turns between them becomes a chunk of its own.
    """
    speakers = plan.get("speakers") or {}
    human = {s for s, v in speakers.items() if v == HUMAN}
    chunks, layout = [], []
    for number, chunk in enumerate(plan["chunks"], 1):
        if chunk.get("audio"):
            words_ = chunk.get("text") or " ".join(t["text"] for t in chunk["turns"])
            layout.append([("human", chunk["audio"], f"paragraph {number}", words_)])
            continue
        turns = chunk.get("turns")
        if not turns or not any(t["speaker"] in human for t in turns):
            layout.append([("tts", len(chunks))])
            chunks.append(chunk)
            continue
        pieces, run = [], []
        for turn in turns + [None]:
            if turn is None or turn["speaker"] in human:
                if run:
                    pieces.append(("tts", len(chunks)))
                    chunks.append({"turns": run})
                    run = []
                if turn is not None:
                    if not turn.get("audio"):
                        G.die(f"{turn['speaker']} is {HUMAN} in the plan but the "
                              f"turn \"{turn['text'][:40]}\" has no \"audio\" path")
                    pieces.append(("human", turn["audio"], turn["speaker"], turn["text"]))
            else:
                run.append(turn)
        layout.append(pieces)

    synth = {**plan, "chunks": chunks}
    if speakers:
        voices = {s: v for s, v in speakers.items() if v != HUMAN}
        if len(voices) >= 2:
            synth["speakers"] = voices
        else:
            # One synthetic speaker left is narration in their voice — and
            # Gemini's multi-speaker mode refuses a single voice.
            synth.pop("speakers")
            synth["voice"] = next(iter(voices.values()), DEFAULT_VOICE)
            synth["chunks"] = [{"text": " ".join(t["text"] for t in c["turns"])}
                               for c in chunks]
    return synth, layout


def take_path(plan_dir, ref):
    """The recorded take for `ref`, in whatever format it was saved, or None."""
    path = Path(plan_dir, ref).expanduser()
    if path.is_file():
        return path
    if not path.parent.is_dir():
        return None
    found = [p for p in sorted(path.parent.glob(path.stem + ".*"))
             if p.suffix.lower() != ".txt"]
    return found[0] if found else None


def human_pieces(layout):
    return [p for pieces in layout for p in pieces if p[0] == "human"]


def list_takes(layout, plan_dir, plan_file, out):
    """Write SCRIPT.txt beside the takes and print what is left to record.

    Returns how many takes are still missing.
    """
    pieces = human_pieces(layout)
    missing = [p for p in pieces if not take_path(plan_dir, p[1])]
    folders = {Path(plan_dir, p[1]).parent for p in pieces}
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        lines = [f"{Path(p[1]).name}\t{p[2]}: {p[3]}" for p in pieces
                 if Path(plan_dir, p[1]).parent == folder]
        (folder / "SCRIPT.txt").write_text("\n".join(lines) + "\n")
    if missing:
        where = ", ".join(str(f) for f in sorted(folders))
        print(f"◈ {len(missing)} of {len(pieces)} line(s) are yours to record → {where}")
        for _, ref, who, text in missing:
            print(f"  {Path(ref).name:<22} {who}: {text[:70]}")
        print(f"  {G.DIM}any format ffmpeg reads, same name (`/sound record.py` works); "
              f"then:\n  python3 voice.py --from-plan {plan_file} -o {out}{G.RESET}")
    return len(missing)


def read_take(path, rate):
    """A recorded take as PCM at the synthetic voices' sample rate."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = to_wav(path, Path(tmp) / "take.wav", rate)
        with wave.open(str(wav)) as handle:
            return handle.readframes(handle.getnframes())


def assemble(layout, takes, plan_dir):
    """[(pcm, rate)] per original chunk: synthetic takes and recordings
    interleaved in script order, joined with TURN_GAP within a chunk."""
    rate = takes[0][1] if takes else RATE
    silence = b"\x00\x00" * int(rate * TURN_GAP)
    chunks = []
    for pieces in layout:
        pcm = [takes[p[1]][0] if p[0] == "tts" else
               read_take(take_path(plan_dir, p[1]), rate) for p in pieces]
        chunks.append((silence.join(pcm), rate))
    return chunks


# ----------------------------------------------------------------- cloning ---

def clone_inputs(sample, consent, workdir):
    """(sample_b64, consent_b64) as 24kHz mono WAV, after checking the sample length."""
    paths = []
    for flag, src in (("--clone-from", sample), ("--consent", consent)):
        src = Path(src).expanduser()
        if not src.is_file():
            G.die(f"{flag}: no recording at {src}")
        paths.append(to_wav(src, Path(workdir) / f"{flag.strip('-')}.wav"))
    length = seconds_of(paths[0])
    low, high = CLONE_SECONDS
    if not low <= length <= high:
        G.die(f"--clone-from is {length:.1f}s; voice replication wants "
              f"{low:.0f}-{high:.0f}s of clean, natural speech")
    return [base64.b64encode(p.read_bytes()).decode() for p in paths]


def replicate(sample, consent, store):
    """A voice id (`voice_...`, stored) or key (`voicekey_...`, not) for the plan.

    The key is the voice: anyone holding it with an API key can speak in it.
    It lives in this process only, never in the plan, sidecar or terminal.
    """
    with tempfile.TemporaryDirectory() as tmp:
        sample_b64, consent_b64 = clone_inputs(sample, consent, tmp)
    client = G.backends("aistudio")[0].client(REPLICATION_MODEL)
    if not hasattr(client, "voices"):
        G.die("voice replication needs google-genai >= 2.25: "
              "uv pip install -U google-genai")
    voice = {"model": REPLICATION_MODEL, "type": "replicated",
             "replicated": {
                 "source_audio": {"mime_type": "audio/wav", "data": sample_b64},
                 "consent_audio": {"mime_type": "audio/wav", "data": consent_b64}}}
    if store:
        voice["display_name"] = "creative-stack narration"
    try:
        made = client.voices.create(store=store, voice=voice)
    except Exception as exc:  # noqa: BLE001 - every API refusal ends the same way
        G.die(f"voice replication refused: {str(exc).splitlines()[0][:200]}\n"
              f"  The consent clip must be the same speaker reciting: \"{CONSENT}\"")
    if store:
        print(f"  {G.DIM}stored voice: {made.id} — reuse with --voice {made.id}; "
              f"delete with client.voices.delete(id=...){G.RESET}")
        return made.id
    return made.key


# -------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--text", "-t", help="the words to speak")
    src.add_argument("--input", "-i", help="a .txt or .md script")
    src.add_argument("--from-plan", help="render an existing (edited) .plan.json")
    src.add_argument("--own-voice", metavar="RECORDING",
                     help="use your own recording as the narration (no synthesis)")
    ap.add_argument("--output", "-o", help=".wav (native) or anything ffmpeg writes")
    ap.add_argument("--voice", help=f"prebuilt voice (default {DEFAULT_VOICE}) "
                                    "or a designed voice_... id")
    ap.add_argument("--voices", help="two voices for a dialogue, e.g. Kore,Puck")
    ap.add_argument("--narration", action="store_true",
                    help="read `Name: words` lines as prose, not a dialogue")
    ap.add_argument("--style", help='delivery, e.g. "warm, unhurried, smiling"')
    ap.add_argument("--gap", type=float, default=0.6,
                    help="seconds of silence between chunks (default 0.6)")
    ap.add_argument("--engine", default="gemini",
                    choices=["gemini", "cloud", "chirp", "live"],
                    help="gemini (Gemini API TTS, styleable, default), cloud "
                         "(Gemini-TTS on Cloud Text-to-Speech, with --lang), chirp "
                         "(Chirp 3 HD) or live (Gemini Live native audio)")
    ap.add_argument("--human", metavar="WHO",
                    help="lines you record yourself: dialogue speakers (Sam) or "
                         "narration paragraph numbers (1,4), comma-separated")
    ap.add_argument("--clone-from", metavar="SAMPLE",
                    help="10-30s of your own speech; reads the script in your voice")
    ap.add_argument("--consent", metavar="RECORDING",
                    help="you reciting the --consent-statement (needed by --clone-from)")
    ap.add_argument("--store-voice", action="store_true",
                    help="have Google keep the replicated voice and print a "
                         "reusable voice_... id (default: use once, keep nothing)")
    ap.add_argument("--consent-statement", action="store_true",
                    help="print the sentence the --consent recording must say")
    ap.add_argument("--lang", default="en-US",
                    help="locale for --engine cloud and chirp, e.g. en-GB, "
                         "hi-IN (default en-US)")
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
    if args.consent_statement:
        print(f"  {CONSENT}\n  {G.DIM}other languages: {CONSENT_DOCS}{G.RESET}")
        return 0
    if not args.output:
        ap.error("--output is required")
    if args.clone_from and not args.consent:
        ap.error("--clone-from needs --consent: a recording of the same speaker "
                 "saying the --consent-statement")
    if args.clone_from and args.engine != "gemini":
        ap.error("--clone-from speaks through Gemini TTS; drop --engine")
    if args.clone_from and args.backend == "vertex":
        # Never reroute biometric audio past a pinned backend.
        ap.error("--clone-from uses the AI Studio voices API, which Vertex does not "
                 "serve; drop --backend vertex or record with --own-voice")
    out = G.prepare(args.output)
    plan_path = out.with_suffix(".plan.json")

    if args.own_voice:
        seconds = use_own_voice(args.own_voice, out)
        G.report(out, f"{seconds:.1f}s, your own recording")
        print(f"  {G.DIM}not mastered — run /sound clean.py, then mix.py "
              f"under any music{G.RESET}")
        return 0

    if args.from_plan:
        plan = json.loads(Path(args.from_plan).read_text())
        if args.clone_from:
            # A plan made for a stock voice, re-read in the user's own.
            if plan.get("speakers"):
                G.die("--clone-from reads narration in one voice; this plan is a dialogue")
            plan.update(engine="gemini", replicated=True, voice=CLONED)
    elif args.text or args.input:
        text = args.text or Path(args.input).expanduser().read_text()
        plan = build_plan(text, args, takes=f"{out.stem}.takes")
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
        print(f"  {G.DIM}plan: {plan_path}  ({len(plan['chunks'])} chunk(s)){G.RESET}")
    else:
        ap.error("one of --text, --input, --from-plan or --own-voice is required")
    plan_file = Path(args.from_plan) if args.from_plan else plan_path
    synth, layout = split_human(plan)
    missing = list_takes(layout, plan_file.parent, plan_file, args.output)
    if args.plan_only:
        return 0
    if missing:
        return 1
    if plan.get("replicated") and plan.get("voice") == CLONED and not args.clone_from:
        G.die("this plan was made for a replicated voice — pass --clone-from and "
              "--consent again, or set \"voice\" to a stored voice_... id")

    who = (", ".join(f"{s}={v}" for s, v in plan["speakers"].items())
           if plan.get("speakers") else plan["voice"])
    print(f"◈ {len(plan['chunks'])} chunk(s) → {out.name}  {G.DIM}({who}){G.RESET}")
    if not synth["chunks"]:
        takes, model, backend = [], None, None
    elif synth.get("replicated") and synth.get("voice") == CLONED:
        # The key goes into a copy that is never serialised; `who` above and
        # the sidecar below still name it CLONED.
        key = replicate(args.clone_from, args.consent, args.store_voice)
        takes, (model, backend) = synthesise({**synth, "voice": key}, args)
    elif synth.get("engine") == "chirp":
        takes, (model, backend) = synthesise_chirp(synth)
    elif synth.get("engine") == "cloud":
        takes, (model, backend) = synthesise_cloud(synth, args)
    elif synth.get("engine") == "live":
        takes, (model, backend) = synthesise_live(synth, args)
    else:
        takes, (model, backend) = synthesise(synth, args)
    recorded = human_pieces(layout)
    takes = assemble(layout, takes, plan_file.parent)
    seconds = write(takes, plan.get("gap", args.gap), out)

    script = " ".join(c.get("text") or " ".join(t["text"] for t in c["turns"])
                      for c in plan["chunks"])
    if model:
        G.provenance(out, f"[voice: {who}; style: {plan.get('style') or '-'}] "
                          f"{script[:400]}", model, backend)
    if recorded:
        # A mixed track says which parts are a person: an AI disclosure that
        # covers the whole file would be as wrong as one that is missing.
        whose = sorted({p[2] for p in recorded})
        line = (f"recorded-by: human ({len(recorded)} take(s): {', '.join(whose)}; "
                f"not synthesised)\n")
        sidecar = Path(str(out) + ".txt")
        sidecar.write_text((sidecar.read_text() if model else
                            f"converted-at: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
                           + line)
    G.report(out, f"{seconds:.1f}s, {takes[0][1] // 1000}kHz mono")
    print(f"  {G.DIM}not mastered — run /sound clean.py, then mix.py "
          f"under any music{G.RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
