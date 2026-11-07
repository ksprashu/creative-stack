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

"""Build a karaoke backing track, word-timed ASS lyrics, vocal mix, and 1080p video.

    # split vocals/instrumental, transcribe word timings, and render a 1080p karaoke MP4
    python3 karaoke.py -i song.mp3 -o out/karaoke.mp4

    # shift key down 2 semitones with rubberband, keep a -18dB guide vocal
    python3 karaoke.py -i song.wav --semitones -2 --guide-db -18 -o out/karaoke.mp4

    # mix a recorded singer take (with studio vocal reverb) over the instrumental
    python3 karaoke.py -i song.wav --sing my-take.wav --reaper -o out/performance.mp4

    # re-render after hand-editing the intermediate .srt or .karaoke.json
    python3 karaoke.py --from-plan out/karaoke.karaoke.json -o out/karaoke.mp4

Separation engines (`--engine`):
  - `auto`       uses Demucs v4 (`htdemucs --two-stems=vocals`) when installed,
                 falling back to bass-preserving stereo `center-cut`
  - `demucs`     neural stem separation via Demucs (`htdemucs`)
  - `center-cut` zero-ML FFmpeg phase cancellation that preserves stereo bass (<130Hz)
  - `none`       treat input as already-separated (or pair with `--instrumental` / `--vocals`)
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import soundlib as S  # noqa: E402
import hostenv  # noqa: E402


def center_cut_instrumental_filter(bass_cutoff=130):
    """FFmpeg filter_complex that cancels center-panned vocals while keeping bass.

    Naive `c0-c1` vocal cancellation wipes out center-panned kick drums and bass.
    Splitting at `bass_cutoff` Hz preserves the low end in stereo while applying
    mid-channel phase cancellation only above `bass_cutoff` Hz.
    """
    fc = int(bass_cutoff)
    return (
        f"[0:a]asplit=2[low_in][high_in];"
        f"[low_in]lowpass=f={fc}[bass];"
        f"[high_in]highpass=f={fc},"
        f"pan=stereo|c0=c0-0.92*c1|c1=c1-0.92*c0[side];"
        f"[bass][side]amix=inputs=2:weights=1 1:normalize=0[out]"
    )


def center_cut_vocal_filter(low_hz=140, high_hz=8500):
    """Isolate center-panned lead vocal band for transcription and guide track."""
    return (
        f"highpass=f={int(low_hz)},lowpass=f={int(high_hz)},"
        f"pan=mono|c0=0.5*c0+0.5*c1"
    )


def vocal_reverb_chain(reverb_amount=0.22):
    """Subtle studio vocal plate reflection + compression for a singer take."""
    amt = max(0.0, min(0.6, float(reverb_amount)))
    if amt <= 0.0:
        return "highpass=f=85,acompressor=threshold=-18dB:ratio=3:attack=12:release=160:makeup=2"
    decay2 = round(amt * 0.68, 3)
    return (
        "highpass=f=85,"
        "acompressor=threshold=-18dB:ratio=3:attack=12:release=160:makeup=2,"
        f"aecho=0.82:0.88:42|68:{amt:.3f}|{decay2:.3f}"
    )


def resolve_engine(requested):
    """Pick the stem-separation engine (`demucs`, `center-cut`, or `none`)."""
    if requested in ("center-cut", "none"):
        return requested
    demucs_bin = hostenv.demucs_cmd()
    if requested == "demucs":
        if not demucs_bin:
            S.die("Demucs is not installed. Run: uv pip install --python .venv/bin/python demucs "
                  "or pass --engine center-cut")
        return "demucs"
    return "demucs" if demucs_bin else "center-cut"


def separate_stems(src, inst_dst, voc_dst, engine):
    """Write `inst_dst` (instrumental WAV) and `voc_dst` (vocals WAV)."""
    if engine == "none":
        S.ffmpeg(["-i", str(src), "-map", "a:0", "-ar", str(S.RATE), "-ac", "2",
                  "-c:a", "pcm_s24le", str(inst_dst)])
        S.ffmpeg(["-i", str(src), "-map", "a:0", "-af", center_cut_vocal_filter(),
                  "-ar", str(S.RATE), "-ac", "1", "-c:a", "pcm_s24le", str(voc_dst)])
        return

    if engine == "demucs":
        demucs_cmd = hostenv.demucs_cmd()
        with tempfile.TemporaryDirectory(prefix="cs-demucs-") as tmp:
            print("🎛️  separating vocals & instrumental with Demucs (htdemucs) …")
            S.run([*demucs_cmd, "--two-stems=vocals", "-n", "htdemucs",
                   "-o", tmp, str(src)])
            stem_dir = Path(tmp) / "htdemucs" / Path(src).stem
            no_voc = stem_dir / "no_vocals.wav"
            voc = stem_dir / "vocals.wav"
            if not no_voc.exists() or not voc.exists():
                S.die(f"Demucs did not produce stems in {stem_dir}")
            S.ffmpeg(["-i", str(no_voc), "-ar", str(S.RATE), "-ac", "2",
                      "-c:a", "pcm_s24le", str(inst_dst)])
            S.ffmpeg(["-i", str(voc), "-ar", str(S.RATE), "-ac", "1",
                      "-c:a", "pcm_s24le", str(voc_dst)])
        return

    print("🎛️  separating vocals & instrumental with bass-preserving center-cut …")
    S.ffmpeg([
        "-i", str(src),
        "-filter_complex", center_cut_instrumental_filter(),
        "-map", "[out]", "-ar", str(S.RATE), "-ac", "2",
        "-c:a", "pcm_s24le", str(inst_dst),
    ])
    S.ffmpeg([
        "-i", str(src), "-map", "a:0",
        "-af", center_cut_vocal_filter(),
        "-ar", str(S.RATE), "-ac", "1",
        "-c:a", "pcm_s24le", str(voc_dst),
    ])


def shift_key_and_tempo(wav_in, wav_out, semitones=0.0, tempo=1.0):
    """Pitch-shift by `semitones` and time-stretch by `tempo` using rubberband."""
    semi = float(semitones or 0.0)
    spd = float(tempo or 1.0)
    if abs(semi) < 1e-3 and abs(spd - 1.0) < 1e-3:
        if Path(wav_in).resolve() != Path(wav_out).resolve():
            shutil.copyfile(wav_in, wav_out)
        return
    rb = hostenv.which("rubberband")
    if rb:
        print(f"🎵 shifting key ({semi:+.1f} st) / tempo ({spd:.2f}x) with rubberband …")
        S.run([rb, "-q", "--formant", "-p", str(semi), "-t", str(1.0 / spd),
               str(wav_in), str(wav_out)])
        return
    if S.has_filter("rubberband"):
        pitch_ratio = 2.0 ** (semi / 12.0)
        S.ffmpeg(["-i", str(wav_in),
                  "-af", f"rubberband=pitch={pitch_ratio:.6f}:tempo={spd:.6f}:formant=preserved",
                  "-ar", str(S.RATE), "-c:a", "pcm_s24le", str(wav_out)])
        return
    S.die("rubberband is required for --semitones / --tempo. "
          "Install it via: python3 scripts/packages.py --install")


def ensure_karaoke_subtitles(vocals_wav, srt_path, words_path, ass_path, brand=None, accent=None):
    """Transcribe `vocals_wav` (if needed) and build the `\\kf` karaoke `.ass` file."""
    vidkit_dir = hostenv.REPO_ROOT / "skills/vidkit/scripts"
    transcribe_py = vidkit_dir / "transcribe.py"
    captions_py = vidkit_dir / "captions.py"

    if not srt_path.exists():
        prefix = srt_path.with_suffix("")
        print(f"📝 transcribing vocal stem for word timings → {srt_path.name} …")
        proc = subprocess.run(
            [sys.executable, str(transcribe_py), "-i", str(vocals_wav),
             "-o", str(prefix)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not srt_path.exists():
            # Fallback when a short instrumental/test clip has no spoken words:
            # emit a single instrumental cue so the pipeline remains deterministic.
            info = S.probe(vocals_wav)
            dur = max(1.0, info["duration"])
            srt_path.write_text(
                f"1\n00:00:00,000 --> 00:00:{min(59.0, dur):06.3f}".replace(".", ",")
                + "\n♪ Instrumental ♪\n",
                encoding="utf-8",
            )

    cmd = [sys.executable, str(captions_py), "-s", str(srt_path),
           "--style", "karaoke", "--ass-only", "-o", str(ass_path)]
    if words_path and Path(words_path).exists():
        cmd += ["--words", str(words_path)]
    if brand:
        cmd += ["--brand", brand]
    if accent:
        cmd += ["--accent", accent]
    S.run(cmd)


def mix_karaoke_audio(inst_wav, voc_wav, sing_wav, out_wav, guide_db, sing_reverb,
                      target_i, target_tp):
    """Combine instrumental, optional guide vocal, and optional singer take."""
    inputs = ["-i", str(inst_wav)]
    filters = [f"[0:a]aformat=sample_rates={S.RATE}:channel_layouts=stereo[inst]"]
    streams = ["[inst]"]
    idx = 1

    if guide_db is not None and voc_wav and Path(voc_wav).exists():
        inputs += ["-i", str(voc_wav)]
        filters.append(
            f"[{idx}:a]aformat=sample_rates={S.RATE}:channel_layouts=stereo,"
            f"volume={float(guide_db):.1f}dB[guide]"
        )
        streams.append("[guide]")
        idx += 1

    if sing_wav and Path(sing_wav).exists():
        inputs += ["-i", str(sing_wav)]
        filters_v = vocal_reverb_chain(sing_reverb)
        filters.append(
            f"[{idx}:a]{filters_v},"
            f"aformat=sample_rates={S.RATE}:channel_layouts=stereo[sing]"
        )
        streams.append("[sing]")
        idx += 1

    if len(streams) == 1:
        filters.append("[inst]anull[mixed]")
    else:
        filters.append(
            f"{''.join(streams)}amix=inputs={len(streams)}:duration=first:"
            f"dropout_transition=0:normalize=0[mixed]"
        )

    staging = Path(tempfile.mkdtemp(prefix="cs-karaoke-mix-"))
    try:
        pre = staging / "pre.wav"
        S.ffmpeg([*inputs, "-filter_complex", ";".join(filters),
                  "-map", "[mixed]", "-ar", str(S.RATE), "-c:a", "pcm_s24le",
                  str(pre)])
        stats = S.measure(pre)
        S.ffmpeg(["-i", str(pre),
                  "-af", S.loudnorm_filter(stats, target_i, target_tp),
                  *S.encode_args(out_wav), str(out_wav)])
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def render_karaoke_video(audio_wav, ass_path, out_mp4, src_media=None, brand=None):
    """Render a 1080p H.264 karaoke video with burned-in `\\kf` ASS lyrics."""
    tokens = S.tokens(brand)
    palette = tokens.get("color", {}).get("dark", {})
    bg_hex = (palette.get("bg") or "#0D1117").lstrip("#")
    accent_hex = (palette.get("accent") or "#1A73E8").lstrip("#")
    width, height = tokens.get("video", {}).get("resolution", [1920, 1080])
    info = S.probe(audio_wav)
    esc_ass = str(ass_path).replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")

    has_video = False
    if src_media and Path(src_media).exists():
        probe_out = S.run([
            hostenv.ffmpeg_bin("ffprobe"), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(src_media)
        ], capture=True).stdout.strip()
        has_video = bool(probe_out)

    print(f"🎬 rendering karaoke video ({width}x{height}) → {out_mp4.name} …")
    if has_video:
        S.ffmpeg([
            "-i", str(src_media), "-i", str(audio_wav),
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                   f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x{bg_hex},"
                   f"boxblur=20:2,eq=brightness=-0.22,ass='{esc_ass}'",
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k",
            "-shortest", "-movflags", "+faststart", str(out_mp4),
        ])
        return

    S.ffmpeg([
        "-f", "lavfi", "-i",
        f"color=c=0x{bg_hex}:s={width}x{height}:r=30:d={info['duration']:.3f}",
        "-i", str(audio_wav),
        "-filter_complex",
        f"[1:a]showwaves=s={width}x220:mode=cline:rate=30:colors=0x{accent_hex}@0.55[wave];"
        f"[0:v][wave]overlay=0:(H-h)/2-40,"
        f"ass='{esc_ass}'[v]",
        "-map", "[v]", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k",
        "-shortest", "-movflags", "+faststart", str(out_mp4),
    ])


def build_karaoke_plan(args, stem_base, dst):
    """Create the reviewable `<stem>.karaoke.json` plan dictionary."""
    engine = resolve_engine(args.engine)
    return {
        "input": str(args.input) if args.input else None,
        "output": str(dst),
        "engine": engine,
        "semitones": float(args.semitones),
        "tempo": float(args.tempo),
        "guide_db": None if args.guide_db == "off" else float(args.guide_db),
        "sing": str(args.sing) if args.sing else None,
        "sing_reverb": float(args.reverb),
        "brand": args.brand,
        "accent": args.accent,
        "reaper": bool(args.reaper),
        "artifacts": {
            "instrumental": str(args.instrumental or stem_base.with_name(f"{stem_base.name}.instrumental.wav")),
            "vocals": str(args.vocals or stem_base.with_name(f"{stem_base.name}.vocals.wav")),
            "srt": str(args.srt or stem_base.with_name(f"{stem_base.name}.srt")),
            "words": str(args.words or stem_base.with_name(f"{stem_base.name}.json")),
            "ass": str(stem_base.with_name(f"{stem_base.name}.ass")),
            "audio_mix": str(dst if dst.suffix.lower() in (".wav", ".flac", ".mp3", ".m4a", ".opus")
                             else stem_base.with_name(f"{stem_base.name}.mix.wav")),
        },
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", help="input song or video file")
    ap.add_argument("--output", "-o", required=True,
                    help="output .mp4 karaoke video or .wav/.mp3/.flac backing mix")
    ap.add_argument("--engine", default="auto",
                    choices=["auto", "demucs", "center-cut", "none"],
                    help="vocal separation engine (default: auto)")
    ap.add_argument("--instrumental", help="use an existing instrumental WAV/MP3")
    ap.add_argument("--vocals", help="use an existing isolated vocal WAV/MP3")
    ap.add_argument("--srt", help="existing .srt lyric file (skips whisper transcription)")
    ap.add_argument("--words", help="existing whisper -ojf word-timing JSON")
    ap.add_argument("--semitones", type=float, default=0.0,
                    help="shift key in semitones via rubberband, e.g. -2 or +1.5")
    ap.add_argument("--tempo", type=float, default=1.0,
                    help="tempo multiplier via rubberband, e.g. 0.95 (default 1.0)")
    ap.add_argument("--guide-db", default="off",
                    help="guide vocal level in dB (e.g. -18), or 'off' for pure instrumental")
    ap.add_argument("--sing", help="recorded singer vocal file to clean, reverb, and mix in")
    ap.add_argument("--reverb", type=float, default=0.22,
                    help="vocal plate reverb amount 0.0-0.6 for --sing (default 0.22)")
    ap.add_argument("--reaper", action="store_true",
                    help="also emit a multi-track Reaper .rpp session alongside the output")
    ap.add_argument("--brand", help="brand profile")
    ap.add_argument("--accent", help="karaoke sweep highlight colour, #RRGGBB")
    ap.add_argument("--target", type=float, help="target loudness in LUFS (default: brand -14)")
    ap.add_argument("--true-peak", type=float, dest="true_peak",
                    help="ceiling in dBTP (default: brand -1.0)")
    ap.add_argument("--from-plan", help="execute an existing .karaoke.json plan")
    ap.add_argument("--plan-only", action="store_true",
                    help="write the .karaoke.json plan and exit")
    args = ap.parse_args()

    dst = Path(args.output)
    dst.parent.mkdir(parents=True, exist_ok=True)
    stem_base = dst.with_suffix("")

    if args.from_plan:
        plan_path = Path(args.from_plan)
        if not plan_path.exists():
            S.die(f"karaoke plan not found: {plan_path}")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    else:
        if not args.input and not (args.instrumental and args.vocals):
            ap.error("provide --input (-i) or --from-plan")
        plan = build_karaoke_plan(args, stem_base, dst)
        plan_path = stem_base.with_name(f"{stem_base.name}.karaoke.json")
        plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        S.report(plan_path, "karaoke plan")
        if args.plan_only:
            return 0

    art = plan["artifacts"]
    inst_wav = Path(art["instrumental"])
    voc_wav = Path(art["vocals"])
    srt_path = Path(art["srt"])
    words_path = Path(art["words"])
    ass_path = Path(art["ass"])
    mix_wav = Path(art["audio_mix"])

    if not (inst_wav.exists() and voc_wav.exists()):
        src = Path(plan["input"])
        if not src.exists():
            S.die(f"input not found: {src}")
        raw_inst = inst_wav.with_name(f"{inst_wav.stem}.raw.wav")
        raw_voc = voc_wav.with_name(f"{voc_wav.stem}.raw.wav")
        separate_stems(src, raw_inst, raw_voc, plan["engine"])
        shift_key_and_tempo(raw_inst, inst_wav, plan["semitones"], plan["tempo"])
        shift_key_and_tempo(raw_voc, voc_wav, plan["semitones"], plan["tempo"])
        raw_inst.unlink(missing_ok=True)
        raw_voc.unlink(missing_ok=True)
        S.report(inst_wav, "instrumental stem")
        S.report(voc_wav, "vocal stem")

    ensure_karaoke_subtitles(
        voc_wav, srt_path, words_path, ass_path,
        brand=plan.get("brand"), accent=plan.get("accent"),
    )
    S.report(ass_path, "karaoke ASS subtitles")

    tokens = S.tokens(plan.get("brand"))
    target_i, target_tp = S.targets(tokens, args.target, args.true_peak)
    mix_karaoke_audio(
        inst_wav, voc_wav, plan.get("sing"), mix_wav,
        plan.get("guide_db"), plan.get("sing_reverb", 0.22),
        target_i, target_tp,
    )
    S.report(mix_wav, "mastered karaoke audio")
    S.report_loudness(mix_wav, target_i, target_tp)

    if dst.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv"):
        render_karaoke_video(mix_wav, ass_path, dst,
                             src_media=plan.get("input"), brand=plan.get("brand"))
        S.report(dst, "karaoke video")

    if plan.get("reaper") or dst.suffix.lower() == ".rpp":
        rpp_out = dst if dst.suffix.lower() == ".rpp" else stem_base.with_suffix(".rpp")
        session_py = Path(__file__).resolve().parent / "session.py"
        tracks = ["-t", f"{inst_wav}@0:0", "-t", f"{voc_wav}@0:-18"]
        if plan.get("sing") and Path(plan["sing"]).exists():
            tracks += ["-t", f"{plan['sing']}@0:0"]
        S.run([sys.executable, str(session_py), *tracks, "-o", str(rpp_out)])

    return 0


if __name__ == "__main__":
    sys.exit(main())
