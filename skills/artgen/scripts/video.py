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

"""Generate a short video clip from a prompt, or animate a still, with Gemini Omni or Veo.

    python3 video.py -p "slow dolly across a developer desk at dawn" -o out/b-roll.mp4
    python3 video.py -p "the logo assembles from its parts" -i logo.png -o out/sting.mp4
    python3 video.py -p "abstract data flowing" -o out/loop.mp4 --ar 9:16 --fast

This is b-roll and title-card material, not the edit. Clips are a handful of
seconds long; `/vidkit` is what assembles them into something with a
narrative, and `/sound` is what scores them.

Generation is a long-running operation — a minute or two per clip, sometimes
four. The poll loop prints elapsed time so a stall is visibly a stall.
"""

import argparse
import base64
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genailib as G  # noqa: E402

# Quality first. `--fast` reorders this rather than replacing it, so a fast
# render that is refused still falls through to a slower model that works.
# Gemini Omni Flash leads: since its GA on 27 Aug 2026 the Gemini docs name it
# the default video model, ahead of Veo. It goes by two names because the
# backends disagree — AI Studio serves the GA `gemini-omni-1.1-flash`, while
# Vertex's Interactions API rejects that name with a 400 and serves only
# `-preview` (at `global`). Listing both costs one fast refusal per run.
#
# Veo stays as the fallback and as the pick for what Omni lacks: an exact
# first frame, scene extension, last-frame control. `--veo` skips Omni.
# Veo 2.0 is gone from the chain: no audio, visibly older, and shut down on
# the Gemini API in June 2026. The 3.0 pair stays only because Vertex still
# serves it; AI Studio retired it on the same date and will 404 through.
OMNI_SEQUENCE = ["gemini-omni-1.1-flash", "gemini-omni-1.1-flash-preview"]
VEO_SEQUENCE = [
    "veo-3.1-generate-001",
    "veo-3.1-fast-generate-001",
    "veo-3.1-lite-generate-001",
    "veo-3.0-generate-001",
    "veo-3.0-fast-generate-001",
]
MODEL_SEQUENCE = OMNI_SEQUENCE + VEO_SEQUENCE
FAST_FIRST = ["veo-3.1-fast-generate-001", "veo-3.1-lite-generate-001",
              "veo-3.0-fast-generate-001"]

# Veo and Omni both accept only these two. Anything else is silently letterboxed, which is
# worse than being told no.
ASPECTS = ["16:9", "9:16"]
POLL_SECONDS = 10


def _mime_ok(path):
    suffix = Path(path).suffix.lower()
    return {".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(suffix)


def omni_call(args, prompt, client, model):
    """One Omni clip via the Interactions API — synchronous, no polling.

    Omni has no duration, seed, audio or negative-prompt fields, so they go
    into the prompt or into post-processing. And it treats an image as a
    reference it may reinterpret, not as a guaranteed first frame; when that
    matters, `--veo` is the honest choice.
    """
    text = (f"{prompt}\n\nLength: {args.duration} seconds."
            + (f" Avoid: {args.negative}." if args.negative else "")
            + (" Use the attached image as the opening frame."
               if args.image else ""))
    parts = [{"type": "text", "text": text}]
    if args.image:
        parts.insert(0, {"type": "image",
                         "data": base64.b64encode(Path(args.image).read_bytes()).decode(),
                         "mime_type": _mime_ok(args.image)})
    interaction = client.interactions.create(
        model=model, input=parts, timeout=args.timeout,
        response_format={"type": "video", "aspect_ratio": args.ar,
                         "resolution": args.resolution})
    video = getattr(interaction, "output_video", None)
    if video is None or not video.data:
        return None
    data = video.data
    return base64.b64decode(data) if isinstance(data, str) else data


def strip_audio(path):
    """Omni always scores its clips; --no-audio has to be done afterwards."""
    sys.path.insert(0, str(G.REPO_ROOT / "skills/vidkit/scripts"))
    import vidlib  # noqa: PLC0415
    silent = path.with_name(f".{path.stem}-silent{path.suffix}")
    subprocess.run([vidlib.need("ffmpeg"), "-v", "error", "-y", "-i", str(path),
                    "-c:v", "copy", "-an", str(silent)], check=True)
    silent.replace(path)


def generate(args, prompt):
    """One clip: Omni in a single call, or a Veo operation polled to completion."""
    _, types = G.load_genai()

    source_image = None
    if args.image:
        mime = _mime_ok(args.image)
        if not mime:
            G.die(f"--image must be PNG, JPEG or WebP, got {args.image}")
        source_image = types.Image(image_bytes=Path(args.image).read_bytes(),
                                   mime_type=mime)

    config = types.GenerateVideosConfig(
        number_of_videos=1,
        aspect_ratio=args.ar,
        resolution=args.resolution,
        duration_seconds=args.duration,
        generate_audio=not args.no_audio,
        **({"negative_prompt": args.negative} if args.negative else {}),
        **({"seed": args.seed} if args.seed is not None else {}))

    def call(client, model):
        if model.startswith("gemini-omni"):
            return omni_call(args, prompt, client, model)
        # `source=` rather than prompt=/image=: the flat arguments are
        # deprecated in google-genai and will be removed, and the SDK warns
        # about it on every single call.
        operation = client.models.generate_videos(
            model=model,
            source=types.GenerateVideosSource(prompt=prompt,
                                              image=source_image),
            config=config)
        started = time.time()
        # `done` is None while running, not False — `not op.done` is correct
        # and `op.done is False` is not.
        while not operation.done:
            if time.time() - started > args.timeout:
                G.warn(f"{model} still running after {args.timeout}s — giving up")
                return None
            time.sleep(POLL_SECONDS)
            operation = client.operations.get(operation)
            print(f"    {G.DIM}{time.time() - started:.0f}s…{G.RESET}",
                  end="\r", flush=True)
        print(" " * 24, end="\r")

        response = operation.response
        if response is None:
            G.warn(f"{model}: {getattr(operation, 'error', 'no response')}")
            return None
        clips = getattr(response, "generated_videos", None) or []
        if not clips:
            # Veo reports its own safety filtering here rather than raising,
            # and the count is the only clue that a clean-looking empty result
            # was a refusal and not a glitch.
            filtered = getattr(response, "rai_media_filtered_count", 0)
            reasons = getattr(response, "rai_media_filtered_reasons", None)
            if filtered:
                G.warn(f"{model} filtered {filtered} clip(s): "
                       f"{reasons or 'no reason given'}")
            return None
        return clips[0].video.video_bytes

    return G.attempt(models(args), call, prefer=args.backend, retries=0)


def models(args):
    sequence = VEO_SEQUENCE if args.veo else MODEL_SEQUENCE
    if args.model:
        return G.chain_for(args.model, sequence)
    if args.fast:
        # Omni is already the quick one — a Flash model, one call, no queue —
        # so fast means "Omni, then the fast Veo variants".
        head = ([] if args.veo else OMNI_SEQUENCE) + FAST_FIRST
        return head + [m for m in sequence if m not in head]
    return list(sequence)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt", "-p", required=True)
    ap.add_argument("--output", "-o", required=True, help="MP4 to write")
    ap.add_argument("--image", "-i",
                    help="a still to animate — the exact first frame on Veo, "
                         "a strong reference on Omni")
    ap.add_argument("--ar", default="16:9", choices=ASPECTS)
    ap.add_argument("--resolution", default="1080p",
                    choices=["720p", "1080p"])
    ap.add_argument("--duration", type=int, default=8,
                    help="seconds of clip (Omni 3-10, Veo 3 4-8)")
    ap.add_argument("--count", "-n", type=int, default=1,
                    help="how many takes; each is a separate long-running job")
    ap.add_argument("--negative", help="what must not appear")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--no-audio", action="store_true",
                    help="silent clip — use when /sound will score it anyway")
    ap.add_argument("--fast", action="store_true",
                    help="prefer the fast Veo variants (quicker, cheaper, "
                         "less detailed)")
    ap.add_argument("--veo", action="store_true",
                    help="Veo only — for an exact first frame, or when Omni "
                         "is not enabled on the project")
    ap.add_argument("--model", help="pin the primary model")
    ap.add_argument("--timeout", type=int, default=600,
                    help="seconds to wait for one clip before moving on")
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "vertex", "aistudio"])
    ap.add_argument("--brand", help="brand profile, e.g. google")
    ap.add_argument("--scheme", default="light", choices=["light", "dark"])
    ap.add_argument("--style", help="extra style note folded into the prompt")
    args = ap.parse_args()

    if args.image and not Path(args.image).is_file():
        G.die(f"no such image: {args.image}")
    # Fail on a missing credential before announcing a job that will take
    # minutes, not after the first poll comes back empty.
    G.backends(args.backend)

    prompt = G.apply_brand(args.prompt, args.brand, args.scheme, args.style)
    out = G.prepare(args.output)
    if out.suffix.lower() != ".mp4":
        out = out.with_suffix(".mp4")

    print(f"◈ {args.count} clip(s) → {out.name}  "
          f"{G.DIM}({args.ar}, {args.resolution}, {args.duration}s"
          f"{', from ' + Path(args.image).name if args.image else ''})"
          f"{G.RESET}")

    for index in range(args.count):
        target = (out if args.count == 1
                  else out.with_name(f"{out.stem}-{index + 1}{out.suffix}"))
        data, model, backend = generate(args, prompt)
        target.write_bytes(data)
        if args.no_audio and model.startswith("gemini-omni"):
            strip_audio(target)
        G.provenance(target, prompt, model, backend)
        G.report(target, f"{backend}/{model}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
