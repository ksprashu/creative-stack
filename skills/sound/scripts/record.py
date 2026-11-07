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

"""Capture live audio from microphones, virtual loopback devices, or Audio Hijack.

    # inspect inputs, virtual loopback cables (Loopback, BlackHole, PipeWire) & bridges
    python3 record.py --list-devices

    # record 10s from a Loopback / BlackHole / PipeWire virtual device
    python3 record.py --device "Loopback Audio" --duration 10 -o out/system.wav

    # dual-stem capture: mic + system loopback into separate WAV stems + mixed output
    python3 record.py --device "MacBook Pro Microphone" --loopback "Loopback Audio" \\
        --duration 30 --clean -o out/podcast-take.wav

    # drive a named Rogue Amoeba Audio Hijack 4 session headlessly via JavaScript
    python3 record.py --hijack-session "Podcast Capture" --hijack-action start
    python3 record.py --hijack-session "Podcast Capture" --duration 15 -o out/hijack.json

Writes a reviewable `<stem>.capture.json` plan alongside the output so a capture
setup can be inspected or re-run with `--from-plan`.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import soundlib as S  # noqa: E402
import hostenv  # noqa: E402


def build_hijack_js(session_name, action="start", recorder_path=None):
    """Build Audio Hijack 4 JavaScript (`AH4ScriptContext`) to start/stop a session."""
    if action not in ("start", "stop", "status"):
        S.die(f"unknown Audio Hijack action '{action}' (choose start, stop, status)")
    escaped = json.dumps(session_name)
    lines = [
        f"var session = app.sessionWithName({escaped});",
        "if (!session) {",
        f"  console.error('Audio Hijack session not found: ' + {escaped});",
        "} else {",
    ]
    if recorder_path:
        folder = json.dumps(str(Path(recorder_path).resolve().parent))
        fname = json.dumps(Path(recorder_path).stem)
        lines += [
            "  var rec = session.blockWithName('Recorder');",
            "  if (rec) {",
            f"    rec.folderPath = {folder};",
            f"    rec.fileName = {fname};",
            "  }",
        ]
    if action == "start":
        lines.append("  if (!session.running) { session.start(); }")
    elif action == "stop":
        lines.append("  if (session.running) { session.stop(); }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def resolve_device_spec(query, devices):
    """Match a device index, exact name, or case-insensitive substring."""
    if query is None:
        return None
    text = str(query).strip()
    if text.isdigit():
        idx = int(text)
        for dev in devices:
            if dev.get("index") == idx:
                return dev
        return {"index": idx, "name": text, "backend": hostenv.audio_capture_backend(),
                "loopback": False}
    low = text.lower()
    for dev in devices:
        if dev["name"].lower() == low:
            return dev
    for dev in devices:
        if low in dev["name"].lower():
            return dev
    return {"index": None, "name": text, "backend": hostenv.audio_capture_backend(),
            "loopback": hostenv.is_loopback_device(text)}


def build_capture_plan(args, devices):
    """Assemble the reviewable `<stem>.capture.json` specification."""
    mic = resolve_device_spec(args.device, devices) if args.device else None
    loop = resolve_device_spec(args.loopback, devices) if args.loopback else None
    return {
        "platform": hostenv.pretty_name(),
        "backend": hostenv.audio_capture_backend(),
        "rate": args.rate,
        "channels": args.channels,
        "duration": args.duration,
        "clean_mic": bool(args.clean),
        "loopback_db": args.loopback_db,
        "device": mic,
        "loopback": loop,
        "hijack_session": args.hijack_session,
        "hijack_action": args.hijack_action,
    }


def print_device_inventory(as_json=False):
    """Print detected audio inputs, virtual loopback drivers, and app bridges."""
    devices = hostenv.audio_devices()
    routing = hostenv.audio_routing_status()
    if as_json:
        print(json.dumps({"devices": devices, "routing": routing}, indent=2))
        return 0

    print(f"◈ Audio Capture & Loopback Inventory  {S.DIM}({hostenv.pretty_name()}){S.RESET}")
    print(f"\n  {S.DIM}── Input & Passthrough Sources ({hostenv.audio_capture_backend()}) ──{S.RESET}")
    if not devices:
        print("    ! no capture devices reported")
    for dev in devices:
        tag = " [loopback/passthrough]" if dev.get("loopback") else ""
        dflt = " [default]" if dev.get("default") else ""
        meta_parts = [p for p in (dev.get("manufacturer"), dev.get("transport")) if p]
        if dev.get("channels") and dev.get("rate"):
            meta_parts.append(f"{dev['channels']}ch @ {dev['rate']}Hz")
        meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
        idx = f"[{dev['index']}]" if dev.get("index") is not None else "[-]"
        print(f"    {idx:<5} {dev['name']}{S.DIM}{meta}{dflt}{tag}{S.RESET}")

    print(f"\n  {S.DIM}── Routing Drivers & Bridges ──{S.RESET}")
    for key, label in (
        ("audio_hijack", "Audio Hijack 4"),
        ("loopback_app", "Loopback 2"),
        ("switchaudio", "SwitchAudioSource"),
        ("reaper", "REAPER DAW"),
        ("audacity", "Audacity"),
    ):
        val = routing.get(key)
        mark = f"{S.GREEN}✓{S.RESET}" if val else f"{S.DIM}·{S.RESET}"
        print(f"    {mark} {label:<20} {S.DIM}{val or 'not installed'}{S.RESET}")
    hal = routing.get("hal_drivers") or []
    if hal:
        print(f"    {S.GREEN}✓{S.RESET} {'HAL / Virtual Drivers':<20} "
              f"{S.DIM}{', '.join(hal)}{S.RESET}")
    return 0


def capture_streams(plan, dst):
    """Run ffmpeg against one or two live audio inputs and write 24-bit WAV stems."""
    mic = plan.get("device")
    loop = plan.get("loopback")
    if not mic and not loop:
        S.die("specify --device, --loopback, or --hijack-session (see --list-devices)")

    rate = int(plan.get("rate") or S.RATE)
    channels = int(plan.get("channels") or 2)
    duration = plan.get("duration")
    dur_args = ["-t", str(duration)] if duration else []

    if mic and loop:
        mic_wav = dst.with_name(f"{dst.stem}.mic.wav")
        loop_wav = dst.with_name(f"{dst.stem}.loopback.wav")
        mic_in = hostenv.audio_input_args(
            mic["index"] if mic.get("index") is not None else mic["name"])
        loop_in = hostenv.audio_input_args(
            loop["index"] if loop.get("index") is not None else loop["name"])
        print(f"🎙️  capturing mic ({mic['name']}) + loopback ({loop['name']}) …")
        cmd = [
            *mic_in, *loop_in,
            "-map", "0:a:0", *dur_args, "-ar", str(rate), "-ac", "1",
            "-c:a", "pcm_s24le", str(mic_wav),
            "-map", "1:a:0", *dur_args, "-ar", str(rate), "-ac", str(channels),
            "-c:a", "pcm_s24le", str(loop_wav),
        ]
        S.ffmpeg(cmd)
        if plan.get("clean_mic"):
            cleaned = dst.with_name(f"{dst.stem}.mic-clean.wav")
            clean_py = Path(__file__).resolve().parent / "clean.py"
            S.run([sys.executable, str(clean_py), "-i", str(mic_wav), "-o", str(cleaned)])
            mic_wav = cleaned
        loop_db = float(plan.get("loopback_db", -6.0))
        S.ffmpeg([
            "-i", str(mic_wav), "-i", str(loop_wav),
            "-filter_complex",
            f"[0:a]aformat=sample_rates={rate}:channel_layouts=stereo[m];"
            f"[1:a]aformat=sample_rates={rate}:channel_layouts=stereo,"
            f"volume={loop_db}dB[bg];"
            "[m][bg]amix=inputs=2:duration=longest:normalize=0[out]",
            "-map", "[out]", *S.encode_args(dst), str(dst),
        ])
        S.report(mic_wav, "mic stem")
        S.report(loop_wav, "loopback stem")
        S.report(dst, "combined mix")
        return

    chosen = mic or loop
    in_args = hostenv.audio_input_args(
        chosen["index"] if chosen.get("index") is not None else chosen["name"])
    raw_dst = dst.with_name(f"{dst.stem}.raw.wav") if plan.get("clean_mic") else dst
    print(f"🎙️  capturing from '{chosen['name']}' "
          f"({f'{duration}s' if duration else 'press Ctrl-C to stop'}) …")
    S.ffmpeg([*in_args, *dur_args, "-ar", str(rate), "-ac", str(channels),
              *S.encode_args(raw_dst), str(raw_dst)])
    if plan.get("clean_mic"):
        clean_py = Path(__file__).resolve().parent / "clean.py"
        S.run([sys.executable, str(clean_py), "-i", str(raw_dst), "-o", str(dst)])
    else:
        S.report(dst, "captured")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-devices", action="store_true",
                    help="list audio inputs, virtual loopback cables, and bridges")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON with --list-devices")
    ap.add_argument("--device", "-d",
                    help="input device name or index (microphone or loopback)")
    ap.add_argument("--loopback", "-l",
                    help="secondary loopback/passthrough device for dual-stem capture")
    ap.add_argument("--loopback-db", type=float, default=-6.0,
                    help="gain in dB applied to the loopback stem when mixing (default -6)")
    ap.add_argument("--duration", "-t", type=float,
                    help="capture duration in seconds (omit for manual stop)")
    ap.add_argument("--rate", type=int, default=S.RATE,
                    help=f"sample rate in Hz (default {S.RATE})")
    ap.add_argument("--channels", type=int, default=2,
                    help="channel count for capture (default 2)")
    ap.add_argument("--clean", action="store_true",
                    help="run clean.py (highpass, denoise, R128 normalise) on the mic track")
    ap.add_argument("--hijack-session",
                    help="Audio Hijack 4 session name to control via JavaScript")
    ap.add_argument("--hijack-action", default="start",
                    choices=["start", "stop", "status"],
                    help="action for --hijack-session (default: start)")
    ap.add_argument("--hijack-js",
                    help="custom Audio Hijack 4 JavaScript snippet or .ahcommand file")
    ap.add_argument("--from-plan",
                    help="re-run capture from an edited .capture.json file")
    ap.add_argument("--plan-only", action="store_true",
                    help="write the .capture.json plan and exit without recording")
    ap.add_argument("--output", "-o",
                    help="output audio file (.wav, .flac, .mp3) or .json plan path")
    args = ap.parse_args()

    if args.list_devices:
        return print_device_inventory(as_json=args.json)

    if args.from_plan:
        plan_path = Path(args.from_plan)
        if not plan_path.exists():
            S.die(f"capture plan not found: {plan_path}")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        dst = Path(args.output) if args.output else plan_path.with_suffix(".wav")
    else:
        if not (args.device or args.loopback or args.hijack_session or args.hijack_js):
            ap.error("provide --list-devices, --device, --loopback, --hijack-session, "
                     "or --from-plan")
        devices = hostenv.audio_devices()
        plan = build_capture_plan(args, devices)
        dst = Path(args.output or "out/capture.wav")
        dst.parent.mkdir(parents=True, exist_ok=True)
        plan_path = (dst if dst.suffix == ".json"
                     else dst.with_name(f"{dst.stem}.capture.json"))
        plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        S.report(plan_path, "capture plan")
        if args.plan_only:
            return 0

    if args.hijack_js or plan.get("hijack_session"):
        session_name = plan.get("hijack_session")
        action = plan.get("hijack_action", "start")
        js = (Path(args.hijack_js).read_text(encoding="utf-8")
              if args.hijack_js and Path(args.hijack_js).exists()
              else args.hijack_js or build_hijack_js(
                  session_name, action,
                  None if dst.suffix == ".json" else dst))
        ah_file = dst.with_name(f"{dst.stem}.ahcommand")
        ah_file.write_text(js, encoding="utf-8")
        S.report(ah_file, f"Audio Hijack script ({action})")
        ok, detail = hostenv.run_audio_hijack_script(ah_file)
        if not ok:
            S.die(detail)
        if plan.get("duration") and action == "start":
            time.sleep(float(plan["duration"]))
            stop_js = build_hijack_js(session_name, "stop")
            stop_file = dst.with_name(f"{dst.stem}.stop.ahcommand")
            stop_file.write_text(stop_js, encoding="utf-8")
            hostenv.run_audio_hijack_script(stop_file)
        return 0

    dst.parent.mkdir(parents=True, exist_ok=True)
    capture_streams(plan, dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
