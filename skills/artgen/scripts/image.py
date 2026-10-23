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

"""Generate a brand-consistent image from a prompt, and check that it worked.

    python3 image.py -p "isometric diagram of a build pipeline" -o out/hero.png
    python3 image.py -p "the same robot, now waving" -r out/hero.png -o out/wave.png
    python3 image.py -p "section header" -o out/h1.png --ar 16:9 --brand google

The model chain and the generate-verify-refine loop are lifted from the
existing /image-gen skill so both behave identically; what is added here is the
Vertex path and brand tokens.

Verification is not decoration. Image models silently drop requested text,
misspell it, or render four fingers, and a batch of eight section headers is
exactly where nobody looks closely. A vision model checking its own output
catches most of it for the price of one extra call.
"""

import argparse
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genailib as G  # noqa: E402

# Same order as the /image-gen skill: the versatile workhorse, then the cheap
# one, then the premium one. There is no Imagen tail any more: the 4.0 family
# shut down on 17 Aug 2026 (Gemini API deprecations page names
# gemini-3.1-flash-image as the replacement) and Vertex no longer lists it.
# gemini-2.5-flash-image went on 2 Oct 2026 for the same reason.
MODEL_SEQUENCE = [
    "gemini-3.1-flash-image",
    "gemini-3.1-flash-lite-image",
    "gemini-3-pro-image",
]
DEFAULT_VERIFIER = "gemini-3.8-flash"

ASPECTS = ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "5:4", "4:5"]


def load_pil():
    try:
        from PIL import Image
    except ImportError:
        G.die("Pillow is not installed. Run:\n"
              f"  uv pip install --python {G.REPO_ROOT}/.venv/bin/python Pillow")
    return Image


def chain(primary, has_references):
    """The model order for this call.

    flash-lite is removed whenever reference images are attached: it is not
    tuned for multi-reference grounding and will cheerfully ignore the
    references rather than fail, which is the worst of both outcomes.
    """
    models = G.chain_for(primary, MODEL_SEQUENCE)
    if has_references:
        models = [m for m in models if "flash-lite" not in m]
    return models


def _image_bytes(response):
    """The first inline image in a generate_content response, or None."""
    candidates = getattr(response, "candidates", None) or []
    parts = list(getattr(response, "parts", None) or [])
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        if content and content.parts:
            parts += list(content.parts)
    for part in parts:
        blob = getattr(part, "inline_data", None)
        if blob and blob.data:
            return blob.data
    return None


def generate(prompt, references, aspect, size, prefer, primary, seed):
    """One image, as raw bytes, plus the model and backend that made it."""
    _, types = G.load_genai()
    Image = load_pil()

    refs = []
    for path in references:
        try:
            refs.append(Image.open(path))
        except OSError as exc:
            G.warn(f"skipping reference {path}: {exc}")

    def call(client, model):
        # aspect_ratio as a config field, not as "Aspect Ratio 16:9." glued to
        # the end of the prompt: the field is honoured exactly, whereas the
        # prompt text is a suggestion the model weighs against everything else
        # you asked for and frequently loses.
        config = types.GenerateContentConfig(
            image_config=types.ImageConfig(aspect_ratio=aspect,
                                           image_size=size),
            **({"seed": seed} if seed is not None else {}))
        response = client.models.generate_content(
            model=model, contents=[prompt, *refs], config=config)
        return _image_bytes(response)

    return G.attempt(chain(primary, bool(refs)), call, prefer=prefer)


def verify(prompt, data, model, prefer):
    """(passed, reason) from a vision model looking at what we just made."""
    _, types = G.load_genai()
    question = (
        f'Analyse this image against the request: "{prompt}"\n\n'
        "1. Subject: does it show what was asked for?\n"
        "2. Text: if the request quoted specific words, are they present, "
        "correctly spelled and legible?\n"
        "3. Quality: any warped hands, duplicated limbs, garbled glyphs or "
        "obvious artefacts?\n\n"
        "Answer with exactly 'YES' or 'NO: <one sentence>'.")

    def call(client, name):
        response = client.models.generate_content(
            model=name,
            contents=[question,
                      types.Part.from_bytes(data=data, mime_type="image/png")])
        return (response.text or "").strip() or None

    verdict, _, _ = G.attempt([model], call, prefer=prefer, quiet=True)
    return verdict.startswith("YES"), verdict


def refine(prompt, reason, model, prefer):
    """Rewrite the prompt to answer the verifier's objection."""
    def call(client, name):
        response = client.models.generate_content(
            model=name,
            contents=("You are a prompt engineer for a text-to-image model.\n"
                      f'Prompt: "{prompt}"\n'
                      f'It failed review: "{reason}"\n\n'
                      "Rewrite it to fix that specifically. Keep the subject "
                      "and style; be more explicit about layout and about any "
                      "text that must appear. Reply with the new prompt only, "
                      "no quotes and no commentary."))
        return (response.text or "").strip() or None

    try:
        better, _, _ = G.attempt([model], call, prefer=prefer, quiet=True)
    except SystemExit:
        return prompt
    return better


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt", "-p", required=True)
    ap.add_argument("--output", "-o", required=True,
                    help="PNG to write; numbered when --count > 1")
    ap.add_argument("--reference", "-r", action="append", default=[],
                    help="an image to stay consistent with; repeatable")
    ap.add_argument("--ar", default="16:9", choices=ASPECTS)
    ap.add_argument("--size", default="2K", choices=["1K", "2K", "4K"])
    ap.add_argument("--count", "-n", type=int, default=1,
                    help="how many variations to generate")
    ap.add_argument("--seed", type=int,
                    help="fixed seed, for a reproducible variation")
    ap.add_argument("--model", default=MODEL_SEQUENCE[0],
                    help=f"primary model (default {MODEL_SEQUENCE[0]})")
    ap.add_argument("--verifier-model", default=DEFAULT_VERIFIER)
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the review pass (one call instead of two)")
    ap.add_argument("--max-attempts", type=int, default=3,
                    help="generate/verify/refine rounds before giving up")
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "vertex", "aistudio"])
    ap.add_argument("--brand", help="brand profile, e.g. google")
    ap.add_argument("--scheme", default="light", choices=["light", "dark"])
    ap.add_argument("--style", help="extra style note folded into the prompt")
    args = ap.parse_args()

    for path in args.reference:
        if not Path(path).is_file():
            G.die(f"no such reference image: {path}")
    # Resolve the backend up front. Otherwise a missing credential is only
    # discovered inside the first attempt, after the header claims work has
    # started and after --count has already been echoed back.
    G.backends(args.backend)

    primary = args.model
    base = G.apply_brand(args.prompt, args.brand, args.scheme, args.style)
    out = G.prepare(args.output)
    attempts = 1 if args.no_verify else max(1, args.max_attempts)

    print(f"◈ {args.count} image(s) → {out.name}  "
          f"{G.DIM}({args.ar}, {args.size}"
          f"{', brand ' + args.brand if args.brand else ''}){G.RESET}")

    written = []
    for index in range(args.count):
        target = (out if args.count == 1
                  else out.with_name(f"{out.stem}-{index + 1}{out.suffix}"))
        prompt = base
        for attempt in range(1, attempts + 1):
            data, model, backend = generate(prompt, args.reference, args.ar,
                                            args.size, args.backend, primary,
                                            args.seed)
            if args.no_verify:
                break
            passed, reason = verify(args.prompt, data, args.verifier_model,
                                    args.backend)
            if passed:
                break
            print(f"  {G.YELLOW}!{G.RESET} attempt {attempt}/{attempts}: {reason}")
            if attempt < attempts:
                prompt = refine(prompt, reason, args.verifier_model, args.backend)
                print(f"    {G.DIM}refined: {prompt[:110]}…{G.RESET}")
            else:
                # Keep it anyway. A flawed image the user can look at and judge
                # is worth more than an empty directory and an exit code.
                print(f"  {G.YELLOW}!{G.RESET} keeping the last attempt unverified")

        target.write_bytes(data)
        G.provenance(target, prompt, model, backend)
        G.report(target, f"{backend}/{model}")
        written.append(target)

    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
