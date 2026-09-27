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

"""Push in on the part of the frame that is actually moving.

A full-screen 4K recording shown at blog width makes 12pt terminal text
illegible. This finds where the action is — the cursor, the line being typed,
the panel that just expanded — and rides a slow zoom onto it.

Detection is motion-based, not cursor-tracking: it samples the video, diffs
consecutive frames, and takes the centroid of what changed. On a screen
recording that is the cursor and the caret, which is exactly what you want.

    # analyse and write a reviewable spec, no encode
    python3 zoom.py -i cut.mp4 --auto --spec-out out/zoom.yaml

    # analyse, review, then render
    python3 zoom.py -i cut.mp4 --spec out/zoom.yaml -o out/zoomed.mp4

    # one shot
    python3 zoom.py -i cut.mp4 --auto -o out/zoomed.mp4 --zoom 1.6

The spec is plain YAML, so hand-correcting one bad window is a two-line edit
rather than a re-analysis:

    keyframes:
      - {at: 0.0,  zoom: 1.0, focus: [0.50, 0.50]}
      - {at: 6.0,  zoom: 1.5, focus: [0.31, 0.62]}
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from vidlib import die, ffmpeg, filter_script_args, hms, need, probe

# Analysis resolution. Small on purpose: we want the centroid of motion, not
# detail, and 160x90 keeps a 10-minute 4K recording under a second of numpy.
GRID_W, GRID_H = 160, 90


def sample_luma(source, sample_fps):
    """Decode the video as a stack of tiny grayscale frames."""
    import numpy as np

    proc = subprocess.Popen(
        [need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
         "-i", str(source),
         "-vf", f"fps={sample_fps},scale={GRID_W}:{GRID_H}:flags=bilinear,format=gray",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    raw, err = proc.communicate()
    if proc.returncode != 0:
        die("frame sampling failed:\n  "
            + "\n  ".join(err.decode(errors="replace").strip().splitlines()[-8:]))

    frame_bytes = GRID_W * GRID_H
    count = len(raw) // frame_bytes
    if count < 2:
        die("fewer than two sampled frames — is the clip shorter than 1/sample-fps?")
    return (np.frombuffer(raw[:count * frame_bytes], dtype=np.uint8)
            .reshape(count, GRID_H, GRID_W).astype(np.int16))


def detect(source, duration, sample_fps, window, max_zoom, coverage_max):
    """Return keyframes [{at, zoom, focus}] from frame-to-frame motion."""
    import numpy as np

    frames = sample_luma(source, sample_fps)
    diffs = np.abs(np.diff(frames, axis=0))          # (n-1, H, W)

    # A fixed dB-style threshold misfires on both dark terminals and bright
    # docs pages, so key off each clip's own noise floor.
    noise = float(np.percentile(diffs, 90)) or 1.0
    active = diffs > max(6.0, noise)

    per_window = max(1, int(round(window * sample_fps)))
    keyframes = []
    for start in range(0, len(active), per_window):
        block = active[start:start + per_window]
        at = start / sample_fps
        if not block.size:
            continue

        heat = block.sum(axis=0).astype(float)
        total = heat.sum()
        # Fraction of the frame that moved at all. Lots of coverage means a
        # scroll or a slide transition — zooming there just hides content.
        coverage = float((heat > 0).mean())

        if total == 0 or coverage > coverage_max:
            keyframes.append({"at": round(at, 2), "zoom": 1.0, "focus": [0.5, 0.5]})
            continue

        ys, xs = np.nonzero(heat)
        weights = heat[ys, xs]
        fx = float((xs * weights).sum() / weights.sum()) / (GRID_W - 1)
        fy = float((ys * weights).sum() / weights.sum()) / (GRID_H - 1)

        # Tighter motion earns more zoom, up to the cap.
        tightness = 1.0 - min(1.0, coverage / coverage_max)
        zoom = 1.0 + (max_zoom - 1.0) * tightness
        keyframes.append({"at": round(at, 2), "zoom": round(zoom, 3),
                          "focus": [round(fx, 4), round(fy, 4)]})

    keyframes = smooth(keyframes)
    if not keyframes:
        return [{"at": 0.0, "zoom": 1.0, "focus": [0.5, 0.5]}]

    # Open and close on the full frame. Cutting to an already-zoomed shot reads
    # as a mistake; easing in over the first window reads as intent.
    keyframes[0] = {"at": 0.0, "zoom": 1.0, "focus": keyframes[0]["focus"]}
    keyframes.append({"at": round(duration, 2), "zoom": 1.0, "focus": [0.5, 0.5]})
    return keyframes


def smooth(keyframes, alpha=0.45):
    """Exponentially smooth zoom and focus so the camera glides, not jitters."""
    out = []
    for frame in keyframes:
        if not out:
            out.append(dict(frame))
            continue
        prev = out[-1]
        out.append({
            "at": frame["at"],
            "zoom": round(prev["zoom"] + alpha * (frame["zoom"] - prev["zoom"]), 3),
            "focus": [round(p + alpha * (c - p), 4)
                      for p, c in zip(prev["focus"], frame["focus"])],
        })
    return out


def load_spec(path):
    import yaml
    doc = yaml.safe_load(Path(path).read_text()) or {}
    keyframes = doc.get("keyframes")
    if not keyframes:
        die(f"{path}: no 'keyframes:' list")
    for frame in keyframes:
        if "at" not in frame:
            die(f"{path}: every keyframe needs an 'at' time")
        frame.setdefault("zoom", 1.0)
        frame.setdefault("focus", [0.5, 0.5])
    return sorted(keyframes, key=lambda f: float(f["at"]))


def save_spec(path, keyframes, source, meta):
    lines = [
        "# Auto-detected zoom track. Times are seconds; focus is a fraction of",
        "# frame width/height, so the spec survives a resolution change.",
        f"# source: {Path(source).name}  {meta['width']}x{meta['height']} "
        f"@ {meta['fps']:g}fps",
        "keyframes:",
    ]
    for frame in keyframes:
        lines.append(f"  - {{at: {float(frame['at']):.2f}, "
                     f"zoom: {float(frame['zoom']):.3f}, "
                     f"focus: [{frame['focus'][0]:.4f}, {frame['focus'][1]:.4f}]}}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines) + "\n")


def piecewise(keyframes, value_of, time_var="in_time"):
    """Build a smoothstep-interpolated ffmpeg expression over the keyframes.

    Emitted as nested if(lt(t, t_next), <ramp>, <rest>) so the graph stays a
    single expression — zoompan has no notion of keyframes of its own.
    """
    if len(keyframes) == 1:
        return f"{value_of(keyframes[0]):.6f}"

    def build(index):
        if index >= len(keyframes) - 1:
            return f"{value_of(keyframes[-1]):.6f}"
        t0, t1 = float(keyframes[index]["at"]), float(keyframes[index + 1]["at"])
        v0, v1 = value_of(keyframes[index]), value_of(keyframes[index + 1])
        if t1 <= t0:
            return build(index + 1)
        # u = normalised progress through this span, smoothstep-eased.
        u = f"clip(({time_var}-{t0:.4f})/{t1 - t0:.4f},0,1)"
        ramp = f"({v0:.6f}+({v1 - v0:.6f})*({u}*{u}*(3-2*{u})))"
        return f"if(lt({time_var},{t1:.4f}),{ramp},{build(index + 1)})"

    return build(0)


def render(source, keyframes, meta, output, fps=None):
    fps = fps or meta["fps"]
    width = meta["width"] - (meta["width"] % 2)
    height = meta["height"] - (meta["height"] % 2)

    z = piecewise(keyframes, lambda k: max(1.0, float(k["zoom"])))
    fx = piecewise(keyframes, lambda k: float(k["focus"][0]))
    fy = piecewise(keyframes, lambda k: float(k["focus"][1]))

    # zoompan crops a window of (iw/zoom, ih/zoom) at (x, y) in *input*
    # coordinates, then scales it to s. Centre that window on the focus point
    # and clamp it inside the frame.
    x_expr = f"max(0,min(iw-iw/zoom,({fx})*iw-iw/zoom/2))"
    y_expr = f"max(0,min(ih-ih/zoom,({fy})*ih-ih/zoom/2))"

    graph = (f"[0:v]zoompan=z='{z}':x='{x_expr}':y='{y_expr}'"
             f":d=1:s={width}x{height}:fps={fps:g}[outv]")

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write(graph)
        graph_path = handle.name

    try:
        args = ["-i", str(source), *filter_script_args(graph_path),
                "-map", "[outv]"]
        args += ["-map", "0:a", "-c:a", "copy"] if meta["has_audio"] else ["-an"]
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                 "-y", str(output)]
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        ffmpeg(args)
    finally:
        Path(graph_path).unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--output", "-o", help="rendered video (omit with --spec-out)")
    ap.add_argument("--spec", help="read an existing zoom spec instead of detecting")
    ap.add_argument("--spec-out", help="write the detected spec here")
    ap.add_argument("--auto", action="store_true", help="detect motion automatically")
    ap.add_argument("--zoom", type=float, default=1.5,
                    help="maximum push-in (1.0 = none, default 1.5)")
    ap.add_argument("--window", type=float, default=3.0,
                    help="seconds per keyframe (default 3)")
    ap.add_argument("--sample-fps", type=float, default=4.0,
                    help="analysis frame rate (default 4)")
    ap.add_argument("--coverage-max", type=float, default=0.35,
                    help="above this fraction of moving frame, stay wide")
    args = ap.parse_args()

    source = Path(args.input)
    if not source.exists():
        die(f"input not found: {source}")
    if not args.output and not args.spec_out:
        die("nothing to do — pass --output, --spec-out, or both")
    if not args.auto and not args.spec:
        die("pass --auto to detect a zoom track, or --spec to use an existing one")
    if args.zoom < 1.0:
        die("--zoom must be >= 1.0; zoompan cannot pull back past the frame")

    meta = probe(source)
    if not meta["has_video"]:
        die(f"{source} has no video track.")

    if args.spec:
        keyframes = load_spec(args.spec)
        print(f"📐 {args.spec}  ({len(keyframes)} keyframes)")
    else:
        print(f"🔎 analysing motion in {source.name}  ({hms(meta['duration'])}) …")
        keyframes = detect(source, meta["duration"], args.sample_fps,
                           args.window, args.zoom, args.coverage_max)
        pushed = sum(1 for k in keyframes if k["zoom"] > 1.05)
        print(f"   {len(keyframes)} keyframes, {pushed} push in "
              f"(peak {max(k['zoom'] for k in keyframes):.2f}x)")

    if args.spec_out:
        save_spec(args.spec_out, keyframes, source, meta)
        print(f"✓ {args.spec_out}")

    if args.output:
        print(f"🎬 rendering zoom track …")
        render(source, keyframes, meta, args.output)
        print(f"✓ {args.output}  ({Path(args.output).stat().st_size // 1024}KB)")
    else:
        print(f"  Next: review the spec, then →  python3 zoom.py -i {source} "
              f"--spec {args.spec_out} -o out/zoomed.mp4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
