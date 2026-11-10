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

"""Hand the automated cut over to Kdenlive / MLT (or any FCP7/EDL NLE).

ffmpeg is right for the mechanical 90% — silence, captions, encoding — and
wrong for the last 10%, where you want to see the waveform and nudge a cut by
two frames. This turns segments.json into a multi-track MLT timeline Kdenlive
(and Shotcut) can open directly or `melt` can render headlessly, plus FCP7 XML
and EDL for external NLEs, with the source media still linked.

    python3 timeline.py -s out/cut.json -o out/edit.mlt                   # Kdenlive / MLT XML
    python3 timeline.py -s out/cut.json -o out/edit.mlt --render out.mp4  # + headless melt render
    python3 timeline.py -s out/cut.json -o out/edit.xml                   # FCP7 XML
    python3 timeline.py -s out/cut.json -o out/edit.edl                   # CMX 3600

Markers land on the timeline at every point where a long pause was removed —
in a recorded walkthrough those are almost always the topic changes, so they
are the right starting point for chapters.

Note on FCP7 XML: OpenTimelineIO writes **FCP7 XMEML**, not Apple's newer
FCPXML, so an XML output must be named `.xml` rather than `.fcpxml`.
"""

import argparse
import json
import math
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from vidlib import die, hms, need, read_segments, run
import hostenv  # noqa: E402

# AAF is deliberately absent: OpenTimelineIO's AAF adapter can only *write*
# clips that carry a SourceMobID it read from an existing AAF, so a timeline
# built from scratch fails with "Cannot find mob ID for clip".
OTIO_FORMATS = {"xml": "fcp_xml", "edl": "cmx_3600", "otio": "otio_json"}
MLT_FORMATS = {"mlt", "kdenlive"}
FORMATS = {**OTIO_FORMATS, "mlt": "mlt", "kdenlive": "mlt"}


def mlt_rate(fps):
    """Return (frame_rate_num, frame_rate_den) for MLT profiles."""
    fps = float(fps or 30.0)
    for num, den in ((24000, 1001), (30000, 1001), (60000, 1001)):
        if abs(fps - (num / den)) < 0.01:
            return num, den
    if abs(fps - round(fps)) < 1e-3:
        return max(1, int(round(fps))), 1
    scaled = max(1, int(round(fps * 1000)))
    divisor = math.gcd(scaled, 1000)
    return scaled // divisor, 1000 // divisor


def build_mlt_xml(doc, name, markers):
    """Build standard MLT XML (opens natively in Kdenlive/Shotcut; renders via melt)."""
    rate_num, rate_den = mlt_rate(doc.get("fps") or 30.0)
    fps = rate_num / rate_den
    width = int(doc.get("width") or 1920)
    height = int(doc.get("height") or 1080)
    dar_gcd = math.gcd(width, height) or 1

    duration_s = float(doc.get("duration") or 0.0)
    if duration_s <= 0.0 and doc.get("segments"):
        duration_s = max(float(s["end"]) for s in doc["segments"])
    source_frames = max(1, int(round(duration_s * fps)))

    root = ET.Element("mlt", {
        "LC_NUMERIC": "C",
        "version": "7.0.0",
        "title": name,
        "producer": "main_bin",
    })
    ET.SubElement(root, "profile", {
        "description": "automatic",
        "width": str(width),
        "height": str(height),
        "progressive": "1",
        "sample_aspect_num": "1",
        "sample_aspect_den": "1",
        "display_aspect_num": str(width // dar_gcd),
        "display_aspect_den": str(height // dar_gcd),
        "frame_rate_num": str(rate_num),
        "frame_rate_den": str(rate_den),
        "colorspace": "709",
    })

    producer = ET.SubElement(root, "producer", {
        "id": "producer0",
        "in": "0",
        "out": str(source_frames - 1),
    })
    res_prop = ET.SubElement(producer, "property", {"name": "resource"})
    res_prop.text = str(doc["source"])

    main_bin = ET.SubElement(root, "playlist", {"id": "main_bin"})
    retain = ET.SubElement(main_bin, "property", {"name": "xml_retain"})
    retain.text = "1"
    ET.SubElement(main_bin, "entry", {
        "producer": "producer0",
        "in": "0",
        "out": str(source_frames - 1),
    })

    playlist = ET.SubElement(root, "playlist", {"id": "playlist0"})
    timeline_frames = 0
    for seg in doc["segments"]:
        in_frame = max(0, int(round(float(seg["start"]) * fps)))
        clip_frames = max(1, int(round((float(seg["end"]) - float(seg["start"])) * fps)))
        out_frame = in_frame + clip_frames - 1
        ET.SubElement(playlist, "entry", {
            "producer": "producer0",
            "in": str(in_frame),
            "out": str(out_frame),
        })
        timeline_frames += clip_frames

    tractor = ET.SubElement(root, "tractor", {
        "id": "tractor0",
        "title": name,
        "in": "0",
        "out": str(max(0, timeline_frames - 1)),
    })
    proj = ET.SubElement(tractor, "property", {"name": "kdenlive:projectTractor"})
    proj.text = "1"
    if markers:
        guides = [
            {"pos": max(0, int(round(float(at) * fps))), "comment": label, "type": 0}
            for at, label in markers
        ]
        gprop = ET.SubElement(tractor, "property", {"name": "kdenlive:docproperties.guides"})
        gprop.text = json.dumps(guides)
    ET.SubElement(tractor, "track", {"producer": "playlist0"})

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def melt_render_cmd(melt_bin, mlt_path, render_path):
    """Build a deterministic H.264/AAC faststart melt command."""
    return [
        str(melt_bin),
        str(mlt_path),
        "-consumer",
        f"avformat:{render_path}",
        "vcodec=libx264",
        "crf=18",
        "preset=medium",
        "acodec=aac",
        "ab=192k",
        "movflags=+faststart",
    ]


def gap_markers(doc, gap_min):
    """A marker wherever autocut removed at least gap_min seconds."""
    segments = doc["segments"]
    marks, elapsed = [], 0.0
    for index, segment in enumerate(segments):
        if index:
            removed = segment["start"] - segments[index - 1]["end"]
            if removed >= gap_min:
                marks.append((elapsed, f"cut {removed:.1f}s"))
        elapsed += segment["end"] - segment["start"]
    return marks


def parse_chapters(path):
    """Read YouTube-style `0:00 Title` lines into (seconds, title)."""
    chapters = []
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        stamp, _, title = line.partition(" ")
        parts = stamp.split(":")
        if not all(p.isdigit() for p in parts) or not 2 <= len(parts) <= 3:
            die(f"{path}: expected 'MM:SS Title', got: {raw!r}")
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + int(part)
        chapters.append((float(seconds), title.strip() or f"Chapter {len(chapters) + 1}"))
    if not chapters:
        die(f"{path}: no chapter lines found")
    return chapters


def build(doc, name, markers):
    import opentimelineio as otio
    from opentimelineio.opentime import RationalTime, TimeRange

    fps = doc["fps"]
    source = Path(doc["source"])
    if not source.exists():
        print(f"⚠️  source media is missing at {source} — the timeline will "
              "import offline and need relinking.")

    def frames(seconds):
        return RationalTime(round(seconds * fps), fps)

    timeline = otio.schema.Timeline(name=name)
    timeline.global_start_time = RationalTime(0, fps)
    track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    timeline.tracks.append(track)

    available = TimeRange(start_time=RationalTime(0, fps),
                          duration=frames(doc["duration"]))

    for index, segment in enumerate(doc["segments"], 1):
        reference = otio.schema.ExternalReference(
            target_url=source.as_uri(), available_range=available)
        track.append(otio.schema.Clip(
            name=f"{source.stem}_{index:03d}",
            media_reference=reference,
            source_range=TimeRange(
                start_time=frames(segment["start"]),
                duration=frames(segment["end"] - segment["start"])),
        ))

    for at, label in markers:
        # Markers hang off the clip that is on screen when they fire, which is
        # what NLEs expect; a marker in empty space is silently dropped.
        clip = track.child_at_time(frames(at)) if track else None
        target = clip if clip is not None else track
        offset = frames(at)
        if clip is not None:
            start = track.range_of_child(clip).start_time
            offset = frames(at) - start + clip.source_range.start_time
        target.markers.append(otio.schema.Marker(
            name=label,
            marked_range=TimeRange(start_time=offset, duration=RationalTime(0, fps)),
            color=otio.schema.MarkerColor.CYAN,
        ))

    return timeline


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--segments", "-s", required=True, help="segments.json from autocut")
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--format", choices=sorted(FORMATS), default=None,
                    help="default: inferred from the output extension")
    ap.add_argument("--name", default=None, help="timeline name inside the NLE")
    ap.add_argument("--chapters", help="YouTube-style 'MM:SS Title' file to mark up")
    ap.add_argument("--markers", default="gaps", choices=["gaps", "none"],
                    help="auto-markers at removed pauses (default gaps)")
    ap.add_argument("--gap-min", type=float, default=2.0,
                    help="minimum removed pause that earns a marker")
    ap.add_argument("--render", default=None,
                    help="optional output video path to render headlessly via melt")
    args = ap.parse_args()

    doc = read_segments(args.segments)
    output = Path(args.output)
    fmt = args.format or output.suffix.lstrip(".").lower()
    if fmt == "fcpxml":
        die("OpenTimelineIO writes FCP7 XMEML, not Apple FCPXML. "
            f"Rename the output: -o {output.with_suffix('.xml')}")
    if fmt not in FORMATS:
        die(f"unknown format '{fmt}'. Use one of: {', '.join(sorted(FORMATS))}")

    markers = []
    if args.markers == "gaps":
        markers += gap_markers(doc, args.gap_min)
    if args.chapters:
        markers += parse_chapters(args.chapters)
    markers.sort()

    name = args.name or Path(doc["source"]).stem
    output.parent.mkdir(parents=True, exist_ok=True)

    if fmt in MLT_FORMATS:
        source = Path(doc["source"])
        if not source.exists():
            print(f"⚠️  source media is missing at {source} — the timeline will "
                  "open offline and need relinking.")
        output.write_text(build_mlt_xml(doc, name, markers), encoding="utf-8")
    else:
        try:
            import opentimelineio as otio
        except ImportError:
            die("OpenTimelineIO not installed. Run: uv pip install -r requirements.txt")
        timeline = build(doc, name, markers)
        try:
            otio.adapters.write_to_file(timeline, str(output), adapter_name=OTIO_FORMATS[fmt])
        except Exception as exc:                              # adapter-specific
            output.unlink(missing_ok=True)
            die(f"{OTIO_FORMATS[fmt]} adapter failed: {exc}")

    total = sum(s["end"] - s["start"] for s in doc["segments"])
    print(f"✓ {output}  ({len(doc['segments'])} clips, {hms(total)}, "
          f"{len(markers)} markers, {doc['fps']:g}fps)")
    if fmt in MLT_FORMATS:
        print(f"  Kdenlive: kdenlive {output}   |   Headless: melt {output}")
    else:
        print("  Import in your NLE as FCP7 XML / CMX 3600 EDL / OpenTimelineIO")

    if args.render:
        melt_bin = need("melt", hostenv.package_hint("mlt"))
        render_out = Path(args.render)
        render_out.parent.mkdir(parents=True, exist_ok=True)
        if fmt in MLT_FORMATS:
            run(melt_render_cmd(melt_bin, output, render_out))
        else:
            with tempfile.NamedTemporaryFile("w", suffix=".mlt", encoding="utf-8",
                                             delete=False) as tmp:
                tmp.write(build_mlt_xml(doc, name, markers))
                tmp_path = Path(tmp.name)
            try:
                run(melt_render_cmd(melt_bin, tmp_path, render_out))
            finally:
                tmp_path.unlink(missing_ok=True)
        print(f"✓ {render_out}  (rendered via melt)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
