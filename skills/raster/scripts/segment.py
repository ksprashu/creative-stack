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

"""Select objects by description, then mask, cut out or edit just those.

`cutout.py` answers "what is the subject?"; this answers "which thing did I
mean?" — "the mug on the left", "every face", "the API key in the terminal".

    python3 segment.py -i desk.png -q "the red mug" --cutout out/mug.png
    python3 segment.py -i desk.png -q "the laptop screen" --op blur -o out/redacted.png
    python3 segment.py -i shot.png -q "all faces" --op pixelate -o out/anon.png --refine sam2
    python3 segment.py -i desk.png --from-json out/desk.segments.json --mask out/mask.png
    python3 segment.py -i demo.mp4 -q "the email address" --op blur -o out/demo.mp4

Two stages, with a reviewable file between them:

  1. Gemini finds the objects and returns a box and polygon per match, written
     to `<output>.segments.json` before any pixel changes. A wrong match is
     an edit to that file (or a narrower query) and a `--from-json` re-run,
     not another model call.
  2. The polygons are rasterised into a mask and applied locally.

Gemini's polygons are good to a few pixels, which is plenty for blur, pixelate
and fill. For a cutout with a clean edge, `--refine sam2` hands each box to
SAM 2 (Apache-2.0, runs locally on MPS/CUDA/CPU) and uses its mask instead. It
needs torch and transformers: `uv pip install -r requirements-ml.txt`.

A video input is always SAM 2: Gemini finds the objects on one frame (`--at`,
seconds), SAM 2 tracks those boxes through every frame, and the edit is applied
per frame with the audio copied through untouched.
"""

import argparse
import base64
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rasterlib as R  # noqa: E402

sys.path.insert(0, str(R.REPO_ROOT / "skills/artgen/scripts"))
import genailib  # noqa: E402

GREEN, DIM, RESET = "\033[32m", "\033[2m", "\033[0m"

DEFAULT_MODEL = "gemini-3.8-flash"
# Detection quality falls off gently down this list; all are `global` on Vertex.
SEQUENCE = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash"]
SAM2_MODELS = {
    "tiny": "facebook/sam2.1-hiera-tiny",
    "small": "facebook/sam2.1-hiera-small",
    "base": "facebook/sam2.1-hiera-base-plus",
    "large": "facebook/sam2.1-hiera-large",
}
OPS = ("blur", "pixelate", "fill", "erase")
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
# SAM 2 works at 1024px internally; decoding larger only costs memory.
TRACK_MAX_SIDE = 1024
# A SAM 2 session holds every frame it is given as float32 at 1024², about
# 12MB each, so a minute of 30fps footage in one session wants 20GB+. Tracking
# runs in sessions of this many frames, each seeded from where the last ended.
TRACK_CHUNK = 120

# Gemini sees a downscaled copy. Coordinates come back normalised to 0-1000,
# so the resolution sent changes cost and latency, not where the mask lands.
DETECT_MAX_SIDE = 1536

PROMPT = """Find every object in the image matching this description:

  {query}

Return a JSON list. For each match give "label" (short and descriptive),
"box_2d" as [ymin, xmin, ymax, xmax] normalised to 0-1000, and "mask" as a
polygon outlining the object: a list of [x, y] points normalised to 0-1000,
traced closely around its visible edge. Return [] if nothing matches.
"""

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "label": {"type": "STRING"},
            "box_2d": {"type": "ARRAY", "items": {"type": "INTEGER"}},
            "mask": {"type": "ARRAY",
                     "items": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
        },
        "required": ["label", "box_2d"],
    },
}


# ------------------------------------------------------------ detect ---

def detect(image, query, model, backend):
    """Gemini's matches as a list of {label, box_2d, mask} dicts (0-1000)."""
    from PIL import Image                                # noqa: PLC0415
    _, types = genailib.load_genai()

    small = image.convert("RGB")
    small.thumbnail((DETECT_MAX_SIDE, DETECT_MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=92)
    part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=SCHEMA,
        temperature=0.2)

    def call(client, name):
        resp = client.models.generate_content(
            model=name, contents=[part, PROMPT.format(query=query)], config=config)
        return resp.text or None

    chain = genailib.chain_for(model, SEQUENCE)
    text, used, route = genailib.attempt(chain, call, prefer=backend)
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        R.die(f"{used} returned something that is not JSON: {text[:200]}")
    if isinstance(items, dict):                         # {"boxes": [...]} shape
        items = next((v for v in items.values() if isinstance(v, list)), [])
    return [i for i in items if _valid_box(i.get("box_2d"))], f"{route}/{used}"


def _valid_box(box):
    return (isinstance(box, list) and len(box) == 4
            and box[2] > box[0] and box[3] > box[1])


# -------------------------------------------------------------- masks ---

def box_px(box, size):
    """[ymin, xmin, ymax, xmax] in 0-1000 → (x0, y0, x1, y1) pixels."""
    w, h = size
    y0, x0, y1, x1 = box
    return (round(x0 / 1000 * w), round(y0 / 1000 * h),
            round(x1 / 1000 * w), round(y1 / 1000 * h))


def polygon_mask(item, size):
    """An 'L' mask from Gemini's answer, whichever shape it chose.

    Current models return a polygon; Gemini 2.x returned a base64 PNG cropped
    to the box. Both are handled, and an item with neither falls back to its
    box, so a sparse answer still masks something sensible.
    """
    from PIL import Image, ImageDraw                     # noqa: PLC0415

    w, h = size
    mask = Image.new("L", size, 0)
    raw = item.get("mask")
    if isinstance(raw, list) and len(raw) >= 3:
        points = [(x / 1000 * w, y / 1000 * h) for x, y, *_ in raw]
        ImageDraw.Draw(mask).polygon(points, fill=255)
    elif isinstance(raw, str) and raw:
        data = raw.split(",", 1)[-1]                     # strip a data: URL
        crop = Image.open(io.BytesIO(base64.b64decode(data))).convert("L")
        x0, y0, x1, y1 = box_px(item["box_2d"], size)
        if x1 > x0 and y1 > y0:
            crop = crop.resize((x1 - x0, y1 - y0)).point(lambda v: 255 if v > 127 else 0)
            mask.paste(crop, (x0, y0))
    else:
        ImageDraw.Draw(mask).rectangle(box_px(item["box_2d"], size), fill=255)
    return mask


def sam2_masks(image, items, size_name):
    """One SAM 2 mask per item, prompted by Gemini's box."""
    try:
        import torch                                     # noqa: PLC0415
        from transformers import Sam2Model, Sam2Processor  # noqa: PLC0415
    except ImportError:
        R.die("--refine sam2 needs torch and transformers. Run:\n"
              f"  uv pip install --python {R.REPO_ROOT}/.venv/bin/python "
              f"-r {R.REPO_ROOT}/requirements-ml.txt")
    from PIL import Image                                # noqa: PLC0415

    device, repo = _sam2_setup(size_name)
    processor = Sam2Processor.from_pretrained(repo)
    model = Sam2Model.from_pretrained(repo).to(device)

    rgb = image.convert("RGB")
    boxes = [[list(box_px(i["box_2d"], rgb.size)) for i in items]]
    inputs = processor(images=rgb, input_boxes=boxes, return_tensors="pt").to(device)
    with torch.no_grad():
        outputs = model(**inputs, multimask_output=False)
    masks = processor.post_process_masks(outputs.pred_masks.cpu(),
                                         inputs["original_sizes"])[0]
    # (objects, 1, H, W) booleans → one 'L' image per object.
    return [Image.fromarray((m[0].numpy() > 0).astype("uint8") * 255, "L")
            for m in masks]


def _sam2_setup(size_name):
    import torch                                         # noqa: PLC0415
    from transformers.utils import logging as hf_logging  # noqa: PLC0415

    # The sam2.1 checkpoints are published as `sam2_video`, and transformers
    # warns on every image load that it is instantiating `sam2` from them. The
    # image half is a strict subset, so the warning is noise; errors still show.
    hf_logging.set_verbosity_error()
    device = ("mps" if torch.backends.mps.is_available()
              else "cuda" if torch.cuda.is_available() else "cpu")
    repo = SAM2_MODELS[size_name]
    print(f"  {DIM}SAM 2 {repo} on {device} (first run downloads weights){RESET}")
    return device, repo


# A redaction painted inside the object's own outline leaves the outline, and
# the outline of a word is often enough to read it. Hiding ops therefore cover
# each match's rectangle unless --shape mask says otherwise.
HIDING_OPS = ("blur", "pixelate", "fill")


def boxy(args):
    return args.shape == "box" or (args.shape == "auto" and args.op in HIDING_OPS)


def union(masks, size):
    from PIL import Image, ImageChops                    # noqa: PLC0415
    out = Image.new("L", size, 0)
    for m in masks:
        out = ImageChops.lighter(out, m)
    return out


# ---------------------------------------------------------------- ops ---

def apply_op(image, mask, op, strength, colour, feather):
    from PIL import Image, ImageFilter                   # noqa: PLC0415

    base = image.convert("RGBA")
    if feather:
        mask = mask.filter(ImageFilter.GaussianBlur(feather))
    if op == "erase":
        alpha = base.getchannel("A")
        hole = Image.eval(mask, lambda v: 255 - v)
        from PIL import ImageChops                       # noqa: PLC0415
        base.putalpha(ImageChops.multiply(alpha, hole))
        return base
    if op == "blur":
        edited = base.filter(ImageFilter.GaussianBlur(strength))
    elif op == "pixelate":
        # A block small next to the text leaves each glyph's silhouette in
        # the pixels. Two blocks across the region's short side is the floor.
        block = max(2, int(strength))
        bbox = mask.getbbox()
        if bbox:
            block = max(block, min(bbox[2] - bbox[0], bbox[3] - bbox[1]) // 2)
        edited = base.resize((max(1, base.width // block),
                              max(1, base.height // block)), Image.NEAREST)
        edited = edited.resize(base.size, Image.NEAREST)
    else:                                                # fill
        edited = Image.new("RGBA", base.size, colour)
    return Image.composite(edited, base, mask)


def parse_colour(spec):
    value = spec.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
    except ValueError:
        R.die(f"--colour wants a hex colour like #202124, not {spec}")


# -------------------------------------------------------------- video ---

def video_meta(path):
    ffprobe = R.hostenv.ffmpeg_bin("ffprobe") or R.die(
        f"ffprobe not found. {R.hostenv.ffmpeg_install_hint()}")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate", "-of", "json", str(path)],
        capture_output=True, text=True)
    if out.returncode != 0:
        R.die(f"cannot read {path}: {out.stderr.strip()[:200]}")
    streams = json.loads(out.stdout).get("streams") or []
    if not streams:
        R.die(f"no video stream in {path}")
    stream = streams[0]
    # "0/0" when the container does not say; vidlib.probe falls back the same way
    num, _, den = stream.get("r_frame_rate", "0/0").partition("/")
    fps = float(num) / float(den) if float(den or 0) and float(num or 0) else 30.0
    return stream["width"], stream["height"], fps


def decode(path, size, scratch):
    """Every frame of `path` as an (N, H, W, 3) uint8 array at `size`.

    The frames go to a raw file under `scratch` and come back memory-mapped,
    so a long clip costs disk rather than RAM.
    """
    import numpy as np                                   # noqa: PLC0415
    ffmpeg = R.hostenv.ffmpeg_bin("ffmpeg") or R.die(
        f"ffmpeg not found. {R.hostenv.ffmpeg_install_hint()}")
    w, h = size
    raw = Path(scratch) / "frames.rgb"
    run = subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-i", str(path), "-vf", f"scale={w}:{h}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", str(raw)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if run.returncode != 0 or not raw.stat().st_size:
        R.die(f"decode failed: {run.stderr.decode()[-200:]}")
    return np.memmap(raw, np.uint8, "r").reshape(-1, h, w, 3)


def track(frames, items, at_frame, size_name, boxes):
    """Per-frame boolean masks (N, H, W) at the decode size, SAM 2 tracked.

    With `boxes`, each object is drawn as its own filled rectangle, so two
    faces in opposite corners hide two faces, not the frame between them.
    """
    try:
        import numpy as np                               # noqa: PLC0415
        import torch                                     # noqa: PLC0415
        from transformers import Sam2VideoModel, Sam2VideoProcessor  # noqa: PLC0415
    except ImportError:
        R.die("video selection needs torch and transformers. Run:\n"
              f"  uv pip install --python {R.REPO_ROOT}/.venv/bin/python "
              f"-r {R.REPO_ROOT}/requirements-ml.txt")
    device, repo = _sam2_setup(size_name)
    processor = Sam2VideoProcessor.from_pretrained(repo)
    model = Sam2VideoModel.from_pretrained(repo).to(device)

    n, h, w, _ = frames.shape
    masks = np.zeros((n, h, w), bool)

    def run(lo, hi, seed_at, seeds, reverse):
        """Track frames[lo:hi] from local frame `seed_at`.

        Returns each object's box where it was last seen, not only those
        visible on the final frame: a face that turns away across a session
        boundary would otherwise go unredacted for the rest of the clip.
        """
        session = processor.init_video_session(
            video=list(frames[lo:hi]), inference_device=device,
            video_storage_device="cpu", dtype=torch.float32)
        ids = sorted(seeds)
        processor.add_inputs_to_inference_session(
            inference_session=session, frame_idx=seed_at, obj_ids=ids,
            input_boxes=[[list(seeds[i]) for i in ids]])
        seen = dict(seeds)
        model(inference_session=session, frame_idx=seed_at)
        for out in model.propagate_in_video_iterator(
                session, start_frame_idx=seed_at, reverse=reverse):
            res = processor.post_process_masks(
                [out.pred_masks], original_sizes=[[h, w]], binarize=False)[0]
            per_object = (res > 0).squeeze(1).cpu().numpy()
            frame = masks[lo + out.frame_idx]
            for obj, m in zip(out.object_ids, per_object, strict=True):
                ys, xs = np.nonzero(m)
                if not len(xs):
                    continue
                seen[obj] = (int(xs.min()), int(ys.min()),
                             int(xs.max()) + 1, int(ys.max()) + 1)
                if boxes:
                    x0, y0, x1, y1 = seen[obj]
                    frame[y0:y1, x0:x1] = True
                else:
                    frame |= m
        return seen

    initial = {k + 1: box_px(it["box_2d"], (w, h)) for k, it in enumerate(items)}
    with torch.no_grad():
        # Forward from the detection frame. Consecutive sessions share a
        # frame, so each starts exactly where the previous one ended.
        lo, seeds = at_frame, initial
        while True:
            hi = min(n, lo + TRACK_CHUNK)
            seeds = run(lo, hi, 0, seeds, reverse=False)
            if hi == n:
                break
            lo = hi - 1
        # And back to the start.
        hi, seeds = at_frame + 1, initial
        while hi > 1:
            lo = max(0, hi - TRACK_CHUNK)
            seeds = run(lo, hi, hi - lo - 1, seeds, reverse=True)
            hi = lo + 1
    return masks


def process_video(args, src, record):
    """Detect on one frame, track with SAM 2, apply the op frame by frame."""
    import numpy as np                                   # noqa: PLC0415
    from PIL import Image, ImageFilter                   # noqa: PLC0415

    if args.op == "erase" or args.cutout:
        R.die("video output has no alpha channel here — use --op blur, "
              "pixelate or fill, or --mask for a matte")
    width, height, fps = video_meta(src)
    scale = min(1.0, TRACK_MAX_SIDE / max(width, height))
    small = (max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2))
    print(f"◈ decoding {src.name}  {DIM}{width}x{height} @ {fps:.2f}fps,"
          f" tracking at {small[0]}x{small[1]}{RESET}")
    scratch = tempfile.TemporaryDirectory(prefix="segment-")
    frames = decode(src, small, scratch.name)
    at_frame = min(len(frames) - 1, max(0, round(args.at * fps)))
    if record is not None:
        # The boxes describe one particular frame; tracking from any other
        # would start from where the object was, not where it is.
        at_frame = min(len(frames) - 1, int(record.get("frame", at_frame)))

    first_out = Path(args.output or args.mask or args.json)
    json_path = Path(args.json) if args.json else first_out.with_suffix(".segments.json")
    if record is not None:
        items = record["items"]
        print(f"◈ {len(items)} selection(s) from {args.from_json}, frame {at_frame}")
    else:
        print(f"◈ finding “{args.query}” on frame {at_frame}")
        items, via = detect(Image.fromarray(frames[at_frame]), args.query,
                            args.model, args.backend)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps({
            "source": str(src), "query": args.query, "model": via,
            "frame": at_frame, "items": items}, indent=2) + "\n")
        print(f"  {GREEN}✓{RESET} {json_path}  {DIM}{len(items)} match(es) via {via}{RESET}")
    if args.only:
        keep = {int(i) for i in args.only.split(",") if i.strip()}
        items = [it for n, it in enumerate(items) if n in keep]
    if not items:
        R.die("nothing matched — try a broader --query or a different --at")
    for n, it in enumerate(items):
        print(f"    {DIM}[{n}] {it.get('label', '?')}  box {it['box_2d']}{RESET}")
    if not (args.op or args.mask):
        return 0                                         # --json only: review first

    print(f"◈ tracking {len(items)} object(s) through {len(frames)} frames")
    masks = track(frames, items, at_frame, args.sam_size, boxes=boxy(args))
    del frames
    scratch.cleanup()

    ffmpeg = R.hostenv.ffmpeg_bin("ffmpeg") or R.die(
        f"ffmpeg not found. {R.hostenv.ffmpeg_install_hint()}")
    target = Path(args.output or args.mask)
    target.parent.mkdir(parents=True, exist_ok=True)
    reader = subprocess.Popen(
        [ffmpeg, "-v", "error", "-i", str(src), "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    writer = subprocess.Popen(
        [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-r", f"{fps}", "-i", "-",
         "-i", str(src), "-map", "0:v", "-map", "1:a?", "-c:a", "copy",
         # yuv420p needs even sides; pad rather than scale so no pixel moves
         "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(target)], stdin=subprocess.PIPE)
    colour = parse_colour(args.colour)
    frame_bytes = width * height * 3
    try:
        for i in range(len(masks)):
            buf = reader.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            frame = Image.frombuffer("RGB", (width, height), buf)
            mask = Image.fromarray(masks[i].astype(np.uint8) * 255, "L").resize(
                (width, height), Image.BILINEAR)
            if boxy(args):
                mask = mask.point(lambda v: 255 if v else 0)  # no soft box edge
            if args.grow > 0:
                mask = mask.filter(ImageFilter.MaxFilter(args.grow * 2 + 1))
            if args.op:
                frame = apply_op(frame, mask, args.op, args.strength, colour,
                                 args.feather).convert("RGB")
            else:
                frame = mask.convert("RGB")
            writer.stdin.write(frame.tobytes())
    except BrokenPipeError:
        pass                                             # writer died; its exit code says why
    finally:
        reader.stdout.close()
        reader.wait()
        try:
            writer.stdin.close()
        except BrokenPipeError:
            pass
        rc = writer.wait()
    if rc != 0:
        R.die("encode failed")
    print(f"  {GREEN}✓{RESET} {target}  {DIM}{R.human(target.stat().st_size)}{RESET}")
    return 0


# --------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, help="image to select from")
    ap.add_argument("--query", "-q", help="what to select, in plain words")
    ap.add_argument("--from-json", help="reuse a reviewed .segments.json; no model call")
    ap.add_argument("--output", "-o", help="edited image (with --op)")
    ap.add_argument("--op", choices=OPS, help="edit applied inside the selection")
    ap.add_argument("--mask", help="write the combined mask PNG here")
    ap.add_argument("--cutout", help="write the selection on transparency here (.png)")
    ap.add_argument("--json", help="where to write detections "
                                   "(default: next to the first output)")
    ap.add_argument("--only", help="comma-separated indexes from the JSON to keep, e.g. 0,2")
    ap.add_argument("--refine", choices=("none", "sam2"), default="none",
                    help="replace Gemini's polygons with SAM 2 masks (local)")
    ap.add_argument("--sam-size", choices=sorted(SAM2_MODELS), default="small")
    ap.add_argument("--strength", type=float, default=24,
                    help="blur radius or pixel block size (default 24)")
    ap.add_argument("--colour", default="#202124", help="fill colour for --op fill")
    ap.add_argument("--feather", type=float, default=0, help="soften the mask edge, px")
    ap.add_argument("--shape", choices=("auto", "box", "mask"), default="auto",
                    help="region an --op paints: auto = box for blur/pixelate/"
                         "fill (an outline can still be read), mask otherwise")
    ap.add_argument("--grow", type=int, default=0,
                    help="dilate the mask by N px — redaction wants a margin")
    ap.add_argument("--at", type=float, default=0,
                    help="video: second at which to find the objects (default 0)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--backend", choices=("auto", "vertex", "aistudio"), default="auto")
    args = ap.parse_args()

    if not (args.query or args.from_json):
        R.die("say what to select with --query, or reuse a file with --from-json")
    if args.op and not args.output:
        R.die(f"--op {args.op} needs --output")
    if not (args.op or args.mask or args.cutout or args.json):
        R.die("nothing to write — pass --op/-o, --mask, --cutout or --json")
    for path in (args.cutout, args.op == "erase" and args.output):
        if path and Path(path).suffix.lower() != ".png":
            R.die(f"{path}: transparency needs a .png")

    from PIL import Image, ImageFilter                   # noqa: PLC0415

    src = Path(args.input)
    if not src.exists():
        R.die(f"no such file: {src}")
    if src.suffix.lower() in VIDEO_SUFFIXES:
        if args.op and args.mask:
            R.die("for video, write either --op/-o or --mask, not both")
        record = (json.loads(Path(args.from_json).read_text())
                  if args.from_json else None)
        return process_video(args, src, record)
    image = Image.open(src)
    image.load()

    first_out = Path(args.output or args.cutout or args.mask or src)
    json_path = Path(args.json) if args.json else first_out.with_suffix(".segments.json")

    if args.from_json:
        record = json.loads(Path(args.from_json).read_text())
        items = record["items"]
        print(f"◈ {len(items)} selection(s) from {args.from_json}")
    else:
        print(f"◈ finding “{args.query}” in {src.name}")
        items, via = detect(image, args.query, args.model, args.backend)
        record = {"source": str(src), "query": args.query, "model": via,
                  "size": list(image.size),
                  "note": "box_2d is [ymin,xmin,ymax,xmax] and mask is [[x,y],...], "
                          "both 0-1000. Delete or edit items, then re-run with "
                          "--from-json to apply without another model call.",
                  "items": items}
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(record, indent=2) + "\n")
        print(f"  {GREEN}✓{RESET} {json_path}  {DIM}{len(items)} match(es) via {via}{RESET}")

    if args.only:
        keep = {int(i) for i in args.only.split(",") if i.strip()}
        items = [it for n, it in enumerate(items) if n in keep]
    for n, it in enumerate(items):
        print(f"    {DIM}[{n}] {it.get('label', '?')}  box {it['box_2d']}{RESET}")
    if not items:
        R.die("nothing matched — try a broader --query")

    def grown(m):
        return m.filter(ImageFilter.MaxFilter(args.grow * 2 + 1)) if args.grow > 0 else m

    if not (args.mask or args.cutout or args.op):
        return 0                                         # --json only: review first

    # The outline is only computed when an output needs it — SAM 2 is the slow
    # part, and a box redaction alone never does.
    mask = None
    if args.mask or args.cutout or not boxy(args):
        masks = (sam2_masks(image, items, args.sam_size) if args.refine == "sam2"
                 else [polygon_mask(it, image.size) for it in items])
        mask = grown(union(masks, image.size))
    op_mask = (grown(union([polygon_mask({"box_2d": it["box_2d"]}, image.size)
                            for it in items], image.size))
               if boxy(args) else mask)

    def save(img, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path)
        print(f"  {GREEN}✓{RESET} {path}  {DIM}{img.width}x{img.height}"
              f"  {R.human(path.stat().st_size)}{RESET}")

    if args.mask:
        save(mask, args.mask)
    if args.cutout:
        cut = image.convert("RGBA")
        cut.putalpha(mask.filter(ImageFilter.GaussianBlur(args.feather))
                     if args.feather else mask)
        bbox = mask.getbbox()
        save(cut.crop(bbox) if bbox else cut, args.cutout)
    if args.op:
        result = apply_op(image, op_mask, args.op, args.strength,
                          parse_colour(args.colour), args.feather)
        if Path(args.output).suffix.lower() in (".jpg", ".jpeg"):
            result = result.convert("RGB")
        save(result, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
