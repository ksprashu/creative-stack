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

"""Shared backend plumbing for the /artgen scripts.

Two backends reach the same models by different routes, and the whole point of
this module is that a skill script never has to care which one answered:

    vertex    gcloud application-default credentials, billed to a GCP project.
              Subject to org policy, which is exactly the problem — a policy
              can block `veo` on a project where `gemini-*-image` works fine.
    aistudio  a GOOGLE_API_KEY. No org policy, lower quota, no VPC-SC.

`attempt()` walks a model chain across whichever backends are configured and
returns the first answer, so an org-policy block degrades to the other backend
instead of failing the render.
"""

import functools
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("CREATIVE_STACK_ROOT",
                                Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import brand  # noqa: E402
import hostenv  # noqa: E402

GREEN, YELLOW, RED, DIM, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m")

# Vertex serves different model families from different places, and asking the
# wrong one returns a 404 that reads like "your project has no access" rather
# than "wrong region". Gemini 3 image models are `global`-only; Veo and Lyria
# are regional and are not in `global` at all. The TTS models split the same
# way: the 3.1 preview is listed only in us-central1, the 2.5 models in global.
# Vertex's Live model has its own name and is us-central1 only.
# Getting this wrong is the single most confusing failure in the whole skill,
# so it is a table, not a default. First prefix match wins — keep the specific
# entries above the bare "gemini".
_LOCATIONS = [
    ("veo", "us-central1"),
    ("lyria", "us-central1"),
    ("gemini-3.1-flash-tts", "us-central1"),
    ("gemini-live", "us-central1"),
    ("gemini", "global"),
]
DEFAULT_LOCATION = "global"


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"  {YELLOW}!{RESET} {msg}")


def load_genai():
    """The google-genai SDK, or an actionable install line."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        die("google-genai is not installed. Run:\n"
            f"  uv pip install --python {REPO_ROOT}/.venv/bin/python google-genai\n"
            f"and re-run this script with {REPO_ROOT}/.venv/bin/python")
    return genai, types


def vertex_location(model):
    for prefix, location in _LOCATIONS:
        if model.startswith(prefix):
            return location
    return DEFAULT_LOCATION


@functools.lru_cache(maxsize=1)
def vertex_project():
    """The GCP project for the Vertex path, or None.

    gcloud's active config is consulted last and deliberately: an explicit
    environment variable is a decision about *this* render, while the gcloud
    config is whatever the last unrelated `gcloud config set` left behind.
    Cached because reading it costs a `gcloud` subprocess and every model in
    every chain asks for it.
    """
    for var in ("CREATIVE_STACK_GCP_PROJECT", "GOOGLE_CLOUD_PROJECT",
                "ANTHROPIC_VERTEX_PROJECT_ID"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        out = subprocess.run(["gcloud", "config", "get-value", "project"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if value and value != "(unset)" else None


def api_key():
    """The AI Studio key, or None. Never printed, never written anywhere.

    Environment first so a shell can scope a key to one command. The keyring is
    the fallback because `creative-stack.env` deliberately does not carry
    secrets — see scripts/hostenv.py.
    """
    return (os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or hostenv.secret_get("google-api-key"))


class Backend:
    """One route to the models, with a lazily built per-location client."""

    def __init__(self, name):
        self.name = name
        self._clients = {}

    def client(self, model):
        genai, _ = load_genai()
        location = vertex_location(model) if self.name == "vertex" else "-"
        if location not in self._clients:
            if self.name == "vertex":
                self._clients[location] = genai.Client(
                    vertexai=True, project=vertex_project(), location=location)
            else:
                self._clients[location] = genai.Client(api_key=api_key())
        return self._clients[location]

    def __repr__(self):
        return f"<Backend {self.name}>"


def backends(prefer="auto"):
    """Ordered backends to try, best first.

    Vertex leads on `auto` because it is the billed, quota'd, auditable path
    and the one that will still work when a personal API key is rotated out.
    """
    have_vertex = vertex_project() is not None
    have_key = api_key() is not None

    if prefer == "vertex":
        if not have_vertex:
            die("--backend vertex, but no GCP project is configured. Set "
                "$GOOGLE_CLOUD_PROJECT or run: gcloud config set project <id>")
        chosen = ["vertex"]
    elif prefer == "aistudio":
        if not have_key:
            store, read = hostenv.secret_set_hint("google-api-key", "GOOGLE_API_KEY")
            die("--backend aistudio, but no API key is available. Store one:\n"
                f"  {store}\n  {read}")
        chosen = ["aistudio"]
    else:
        chosen = [n for n, ok in (("vertex", have_vertex),
                                  ("aistudio", have_key)) if ok]
    if not chosen:
        store, read = hostenv.secret_set_hint("google-api-key", "GOOGLE_API_KEY")
        die("no AI backend is configured. Either authenticate to GCP:\n"
            "  gcloud auth application-default login\n"
            "  gcloud config set project <id>\n"
            "or store an AI Studio key:\n"
            f"  {store}\n  {read}")
    return [Backend(name) for name in chosen]


# The server having a bad minute: worth an identical retry. Lyria in particular
# answers a perfectly ordinary prompt with `503 deadline_exceeded` about one
# time in three and succeeds on the same request seconds later.
_RETRY_CODES = {500, 503, 504}
_RETRY_STATUS = {"UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"}
# Anything else is a routing decision: 404 means the model is not exposed to
# this project, 403 means org policy said no, 429 is quota. None improve on a
# retry; all are answered by trying somewhere else.


def _classify(exc):
    """'retry' (same call again) or 'move-on' (next backend or model).

    The structured `code`/`status` that google-genai puts on its errors are
    checked before the message text, and this is not fastidiousness: a Vertex
    404 body ends with a documentation URL containing the digits 500, so
    substring-matching the stringified exception classifies a missing model as
    a transient server fault and retries it twice for nothing.
    """
    code = getattr(exc, "code", None)
    status = str(getattr(exc, "status", "") or "").upper()
    if isinstance(code, int) or status:
        return "retry" if (code in _RETRY_CODES
                           or status in _RETRY_STATUS) else "move-on"
    text = str(exc)
    if any(token in text for token in ("UNAVAILABLE", "INTERNAL",
                                       "deadline_exceeded")):
        return "retry"
    return "move-on"


def attempt(chain, call, prefer="auto", retries=2, quiet=False):
    """Run `call(client, model)` over the model chain until something answers.

    Returns (result, model, backend_name). Models are the outer loop: a render
    is better off on the requested model via a different backend than on a
    weaker model via the preferred one.
    """
    routes = backends(prefer)
    if not quiet:
        print(f"  {DIM}chain: {' → '.join(chain)}  "
              f"via {', '.join(b.name for b in routes)}{RESET}")

    last = "nothing was attempted"
    for model in chain:
        for backend in routes:
            for retry in range(retries + 1):
                try:
                    result = call(backend.client(model), model)
                except Exception as exc:  # noqa: BLE001 - every SDK error is a route decision
                    kind = _classify(exc)
                    last = f"{backend.name}/{model}: {str(exc).splitlines()[0][:160]}"
                    if kind == "retry" and retry < retries:
                        warn(f"{backend.name}/{model} is busy — retrying "
                             f"({retry + 1}/{retries})")
                        time.sleep(3 * (retry + 1))
                        continue
                    warn(last)
                    break
                if result is not None:
                    return result, model, backend.name
                last = f"{backend.name}/{model} returned no media"
                warn(last)
                break
    die(f"every model in the chain failed. Last: {last}")


def chain_for(primary, sequence):
    """`primary` first, then the rest of `sequence` with no duplicate.

    Mirrors the fallback-chain rule in the existing /image-gen skill so the two
    behave the same way when a model is rate-limited mid-series.
    """
    return [primary] + [m for m in sequence if m != primary]


# ------------------------------------------------- vertex raw predict ---
#
# Lyria has no method on the google-genai client — `generate_music` does not
# exist, and the SDK's only music surface is the AI Studio realtime websocket,
# which streams PCM into a live session rather than returning a file. The
# Vertex `:predict` endpoint does return a finished WAV, so /artgen calls it
# directly. This is the one place in the skill that speaks raw REST.


def access_token():
    """An OAuth token for ADC, without shelling out if we can help it."""
    try:
        import google.auth
        from google.auth.transport.requests import Request
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(Request())
        return creds.token
    except Exception:  # noqa: BLE001 - fall through to the CLI
        pass
    try:
        out = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def vertex_predict(model, body, retries=3, timeout=600):
    """POST to a Vertex publisher model's :predict endpoint, with retries."""
    import json
    import urllib.error
    import urllib.request

    project = vertex_project()
    if not project:
        die("this model is Vertex-only and no GCP project is configured. Run:\n"
            "  gcloud auth application-default login\n"
            "  gcloud config set project <id>")
    token = access_token()
    if not token:
        die("could not obtain Google credentials. Run:\n"
            "  gcloud auth application-default login")

    location = vertex_location(model)
    url = (f"https://{location}-aiplatform.googleapis.com/v1/projects/"
           f"{project}/locations/{location}/publishers/google/models/"
           f"{model}:predict")

    last = ""
    for retry in range(retries + 1):
        request = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            last = f"HTTP {exc.code}: {detail}"
            transient = exc.code in (429, 500, 503)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = str(exc)[:200]
            transient = True
        if transient and retry < retries:
            warn(f"{model} is busy — retrying ({retry + 1}/{retries})")
            time.sleep(4 * (retry + 1))
            continue
        break
    die(f"{model} failed: {last}")


# ------------------------------------------------------------------- brand ---

def tokens(profile=None):
    try:
        return brand.load(profile)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        die(str(exc))


def style_directive(tokens_, scheme="light"):
    """Brand guidance appended to a prompt, as prose the model can follow.

    Hex codes are given verbatim because the image models honour them far more
    reliably than colour names, but the palette is described as a hierarchy
    rather than a list — "accent on surface" produces a composition, while five
    loose hex values produce a swatch chart.
    """
    colour = tokens_.get("color", {})
    palette = colour.get(scheme, colour.get("light", {}))
    marks = colour.get("brand", {})
    fonts = tokens_.get("font", {})

    bits = []
    if palette:
        bits.append(
            f"Background {palette.get('bg', '#FFFFFF')}, surfaces "
            f"{palette.get('surface', '#F8F9FA')}, primary accent "
            f"{palette.get('accent', '#1A73E8')}, text "
            f"{palette.get('text', '#202124')}.")
    if marks:
        bits.append("Secondary accents drawn only from " +
                    ", ".join(sorted(set(marks.values()))) + ".")
    if fonts.get("display"):
        bits.append(f"Any lettering set in {fonts['display'].split(',')[0]}, "
                    "geometric sans, generous tracking.")
    bits.append("Flat vector illustration, no photographic texture, no drop "
                "shadows, no gradients other than a single subtle one, "
                "generous negative space.")
    return " ".join(bits)


def apply_brand(prompt, profile, scheme="light", style=None):
    """Prompt plus brand direction. `profile` of None leaves the prompt alone."""
    if not profile:
        return prompt if not style else f"{prompt} Style: {style}."
    directive = style_directive(tokens(profile), scheme)
    extra = f" Style: {style}." if style else ""
    return f"{prompt}{extra} Brand direction: {directive}"


# ------------------------------------------------------------------ output ---

def prepare(path):
    """Make the parent directory and hand back the Path."""
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def report(path, note=""):
    size = Path(path).stat().st_size
    unit = f"{size / 1_048_576:.1f}MB" if size >= 1_048_576 else f"{size // 1024}KB"
    print(f"  {GREEN}✓{RESET} {path} {DIM}({unit}{', ' + note if note else ''}){RESET}")


def provenance(path, prompt, model, backend):
    """Write a sidecar recording how the file was made.

    AI-generated assets end up in blog posts and slide decks months later, by
    which time nobody remembers which model drew them or what it was asked. A
    two-line sidecar costs nothing and settles the question — including when a
    publisher asks for a disclosure.
    """
    sidecar = Path(str(path) + ".txt")
    sidecar.write_text(
        f"generated-by: {backend}/{model}\n"
        f"generated-at: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
        f"prompt: {prompt}\n")
    return sidecar
