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

"""Hand the automated cut over to a real NLE.

ffmpeg is right for the mechanical 90% — silence, captions, encoding — and
wrong for the last 10%, where you want to see the waveform and nudge a cut by
two frames. This turns segments.json into a timeline DaVinci Resolve, Premiere
or Final Cut can open, with the source media still linked.

    python3 timeline.py -s out/cut.json -o out/edit.xml     # FCP7 XML
    python3 timeline.py -s out/cut.json -o out/edit.edl     # CMX 3600

Markers land on the timeline at every point where a long pause was removed —
in a recorded walkthrough those are almost always the topic changes, so they
are the right starting point for chapters.

Note on the XML flavour: OpenTimelineIO writes **FCP7 XMEML**, not Apple's
newer FCPXML, so the file must be named `.xml`. Resolve and Premiere both read
it; naming it `.fcpxml` makes Resolve try the wrong parser and reject it.

Resolve: File ▸ Import ▸ Timeline ▸ Pre-Conformed EDL / AAF / XML. The free
edition imports all three; only *scripting* Resolve needs Studio.
"""

import argparse
import sys
from pathlib import Path

from vidlib import die, hms, read_segments

# AAF is deliberately absent: OpenTimelineIO's AAF adapter can only *write*
# clips that carry a SourceMobID it read from an existing AAF, so a timeline
# built from scratch fails with "Cannot find mob ID for clip".
FORMATS = {"xml": "fcp_xml", "edl": "cmx_3600", "otio": "otio_json"}


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
    args = ap.parse_args()

    try:
        import opentimelineio as otio
    except ImportError:
        die("OpenTimelineIO not installed. Run: uv pip install -r requirements.txt")

    doc = read_segments(args.segments)
    output = Path(args.output)
    fmt = args.format or output.suffix.lstrip(".").lower()
    if fmt == "fcpxml":
        die("OpenTimelineIO writes FCP7 XMEML, which Resolve only accepts from a "
            f"'.xml' file. Rename the output: -o {output.with_suffix('.xml')}")
    if fmt not in FORMATS:
        die(f"unknown format '{fmt}'. Use one of: {', '.join(sorted(FORMATS))}")

    markers = []
    if args.markers == "gaps":
        markers += gap_markers(doc, args.gap_min)
    if args.chapters:
        markers += parse_chapters(args.chapters)
    markers.sort()

    name = args.name or Path(doc["source"]).stem
    timeline = build(doc, name, markers)

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        otio.adapters.write_to_file(timeline, str(output), adapter_name=FORMATS[fmt])
    except Exception as exc:                                  # adapter-specific
        # Adapters can fail partway and leave a truncated file behind, which
        # then imports as an empty timeline instead of erroring.
        output.unlink(missing_ok=True)
        die(f"{FORMATS[fmt]} adapter failed: {exc}")

    total = sum(s["end"] - s["start"] for s in doc["segments"])
    print(f"✓ {output}  ({len(doc['segments'])} clips, {hms(total)}, "
          f"{len(markers)} markers, {doc['fps']:g}fps)")
    print("  Resolve: File ▸ Import ▸ Timeline ▸ Pre-Conformed EDL / AAF / XML")
    return 0


if __name__ == "__main__":
    sys.exit(main())
