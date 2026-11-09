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

"""Build a karaoke backing track, word-timed ASS/OCR lyrics, Reaper session, and 1080p video.

    # pull a YouTube karaoke track, extract OCR lyrics, and build a record-armed Reaper session
    python3 karaoke.py --youtube "https://www.youtube.com/watch?v=0d3LzwjjTog" \\
        --ocr-lyrics --reaper --arm-mic --tempo 98 -o out/over-each-other/over-each-other.rpp

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
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import soundlib as S  # noqa: E402
import hostenv  # noqa: E402


def validate_youtube_url(url):
    """Ensure `url` is a valid http(s) URL and cannot be parsed as a CLI option."""
    text = str(url or "").strip()
    parsed = urllib.parse.urlparse(text)
    if not text or text.startswith("-") or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"invalid YouTube URL (expected http:// or https://): {url!r}")
    return text


def download_youtube_karaoke(url, out_dir, stem="karaoke"):
    """Download a YouTube karaoke video (1080p MP4) and extract a 48kHz 24-bit WAV backing track."""
    try:
        safe_url = validate_youtube_url(url)
    except ValueError as exc:
        S.die(str(exc))
    yt_dlp = hostenv.which("yt-dlp")
    if not yt_dlp:
        S.die("yt-dlp not found. " + hostenv.package_hint("yt-dlp"))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    video_mp4 = out_dir / f"{stem}-video.mp4"
    backing_wav = out_dir / f"{stem}-backing.wav"

    if not video_mp4.exists():
        print(f"⬇️  downloading karaoke video from YouTube → {video_mp4.name} …")
        S.run([
            yt_dlp, "--no-playlist",
            "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", str(video_mp4), "--", safe_url,
        ])
    if not backing_wav.exists():
        print(f"🎵 extracting 48 kHz / 24-bit WAV backing track → {backing_wav.name} …")
        S.ffmpeg([
            "-i", str(video_mp4), "-vn",
            "-ar", str(S.RATE), "-ac", "2", "-c:a", "pcm_s24le",
            str(backing_wav),
        ])
    return video_mp4, backing_wav


def clean_ocr_lyric_text(raw_text):
    """Strip OCR noise from a CD+G / karaoke video frame and return a single-line summary."""
    lines = []
    for raw in (raw_text or "").splitlines():
        cleaned = re.sub(r"[^A-Za-z0-9'\",.?!\-\s]", " ", raw)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) < 4:
            continue
        low = cleaned.lower()
        if any(skip in low for skip in ("karaoke", "subscribe", "instrumental", "www.", "http")):
            continue
        lines.append(cleaned)
    return " / ".join(lines[:3])


def deduplicate_ocr_cues(raw_cues, similarity_threshold=0.65):
    """Collapse consecutive OCR frames displaying the same lyric screen into `(sec, text)`."""
    deduped = []
    prev_text = ""
    for sec, raw in raw_cues:
        cleaned = clean_ocr_lyric_text(raw)
        if not cleaned:
            continue
        if prev_text:
            ratio = difflib.SequenceMatcher(None, prev_text.lower(), cleaned.lower()).ratio()
            if ratio >= similarity_threshold:
                continue
        deduped.append((float(sec), cleaned))
        prev_text = cleaned
    return deduped


def extract_video_lyrics_ocr(video_path, lyrics_md_path, interval_sec=10):
    """Sample frames from a karaoke video via ffmpeg + tesseract OCR and write `lyrics.md`."""
    tesseract = hostenv.which("tesseract")
    if not tesseract:
        S.die("tesseract not found. " + hostenv.package_hint("tesseract"))
    info = S.probe(video_path)
    duration = int(info["duration"])
    raw_cues = []
    print(f"🔎 extracting on-screen lyrics via OCR ({video_path.name}) …")
    with tempfile.TemporaryDirectory(prefix="cs-karaoke-ocr-") as tmp:
        for sec in range(4, duration, max(4, int(interval_sec))):
            frame_png = Path(tmp) / f"frame_{sec:04d}.png"
            S.ffmpeg([
                "-ss", str(sec), "-i", str(video_path),
                "-frames:v", "1", "-update", "1", str(frame_png),
            ], check=False)
            if not frame_png.exists():
                continue
            proc = subprocess.run(
                [tesseract, str(frame_png), "stdout", "--psm", "6"],
                capture_output=True, text=True, check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                raw_cues.append((sec, proc.stdout))

    cues = deduplicate_ocr_cues(raw_cues)
    md_lines = [
        f"# Lyrics & Timeline Cues — {Path(video_path).stem}",
        "",
        "| Timestamp | Section | Lyric Cue |",
        "| :--- | :--- | :--- |",
        '| `0:00` | **Intro** | *"[Instrumental Intro]"* |',
    ]
    for idx, (sec, text) in enumerate(cues, 1):
        m, s = divmod(int(sec), 60)
        md_lines.append(f'| `{m}:{s:02d}` | **Cue {idx}** | *"{text}"* |')
    Path(lyrics_md_path).write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return cues


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
    has_ass = bool(ass_path and Path(ass_path).exists())
    # A quote closes the '...' string, appears escaped for both of ffmpeg's
    # parsing levels, and reopens it; a bare \' would end the quoting and let
    # the rest of the path parse as filters.
    esc_ass = (str(ass_path).replace("\\", "\\\\").replace(":", r"\:")
               .replace("'", r"'\\\''") if has_ass else "")

    has_video = False
    if src_media and Path(src_media).exists():
        probe_out = S.run([
            hostenv.ffmpeg_bin("ffprobe"), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(src_media)
        ], capture=True).stdout.strip()
        has_video = bool(probe_out)

    print(f"🎬 rendering karaoke video ({width}x{height}) → {out_mp4.name} …")
    if has_video:
        vf_chain = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x{bg_hex}"
        )
        if has_ass:
            vf_chain += f",boxblur=20:2,eq=brightness=-0.22,ass='{esc_ass}'"
        S.ffmpeg([
            "-i", str(src_media), "-i", str(audio_wav),
            "-vf", vf_chain,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k",
            "-shortest", "-movflags", "+faststart", str(out_mp4),
        ])
        return

    vf_tail = f",ass='{esc_ass}'" if has_ass else ""
    S.ffmpeg([
        "-f", "lavfi", "-i",
        f"color=c=0x{bg_hex}:s={width}x{height}:r=30:d={info['duration']:.3f}",
        "-i", str(audio_wav),
        "-filter_complex",
        f"[1:a]showwaves=s={width}x220:mode=cline:rate=30:colors=0x{accent_hex}@0.55[wave];"
        f"[0:v][wave]overlay=0:(H-h)/2-40{vf_tail}[v]",
        "-map", "[v]", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k",
        "-shortest", "-movflags", "+faststart", str(out_mp4),
    ])


def build_karaoke_plan(args, stem_base, dst):
    """Create the reviewable `<stem>.karaoke.json` plan dictionary."""
    engine = "none" if args.youtube else resolve_engine(args.engine)
    return {
        "youtube": args.youtube,
        "input": str(args.input) if args.input else None,
        "output": str(dst),
        "engine": engine,
        "semitones": float(args.semitones),
        "tempo": float(args.tempo),
        "bpm": float(args.bpm),
        "guide_db": None if args.guide_db == "off" else float(args.guide_db),
        "sing": str(args.sing) if args.sing else None,
        "sing_reverb": float(args.reverb),
        "brand": args.brand,
        "accent": args.accent,
        "reaper": bool(args.reaper or dst.suffix.lower() == ".rpp"),
        "arm_mic": bool(args.arm_mic),
        "monitoring": args.monitoring,
        "ocr_lyrics": bool(args.ocr_lyrics),
        "apply_live": bool(args.apply_live),
        "open": bool(args.open),
        "artifacts": {
            "instrumental": str(args.instrumental or stem_base.with_name(f"{stem_base.name}.instrumental.wav")),
            "vocals": str(args.vocals or stem_base.with_name(f"{stem_base.name}.vocals.wav")),
            "video": str(stem_base.with_name(f"{stem_base.name}-video.mp4")),
            "lyrics_md": str(stem_base.with_name(f"{stem_base.name}.lyrics.md")),
            "srt": str(args.srt or stem_base.with_name(f"{stem_base.name}.srt")),
            "words": str(args.words or stem_base.with_name(f"{stem_base.name}.json")),
            "ass": str(stem_base.with_name(f"{stem_base.name}.ass")),
            "audio_mix": str(dst if dst.suffix.lower() in (".wav", ".flac", ".mp3", ".m4a", ".opus", ".ogg")
                             else stem_base.with_name(f"{stem_base.name}.mix.wav")),
        },
    }


def apply_plan_overrides(plan, args, argv, dst):
    """Apply CLI flag overrides onto a loaded `.karaoke.json` plan."""
    plan["output"] = str(dst)
    if args.sing:
        plan["sing"] = str(args.sing)
    if "--guide-db" in argv or any(a.startswith("--guide-db=") for a in argv):
        plan["guide_db"] = None if args.guide_db == "off" else float(args.guide_db)
    if "--reverb" in argv or any(a.startswith("--reverb=") for a in argv):
        plan["sing_reverb"] = float(args.reverb)
    if args.reaper or dst.suffix.lower() == ".rpp":
        plan["reaper"] = True
    if args.arm_mic:
        plan["arm_mic"] = True
    if "--monitoring" in argv or any(a.startswith("--monitoring=") for a in argv):
        plan["monitoring"] = args.monitoring
    if args.apply_live:
        plan["apply_live"] = True
    if args.open:
        plan["open"] = True
    if args.brand:
        plan["brand"] = args.brand
    if args.accent:
        plan["accent"] = args.accent
    if args.srt:
        plan.setdefault("artifacts", {})["srt"] = str(args.srt)
    if args.words:
        plan.setdefault("artifacts", {})["words"] = str(args.words)
    if dst.suffix.lower() in (".wav", ".flac", ".mp3", ".m4a", ".opus", ".ogg"):
        plan.setdefault("artifacts", {})["audio_mix"] = str(dst)
    else:
        plan.setdefault("artifacts", {})["audio_mix"] = str(dst.with_suffix(".mix.wav"))
    return plan


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", help="input song or video file")
    ap.add_argument("--youtube", "-y",
                    help="YouTube karaoke URL to download (1080p MP4 + 48kHz 24-bit WAV)")
    ap.add_argument("--output", "-o", required=True,
                    help="output .mp4 karaoke video, .rpp Reaper session, or .wav/.mp3/.flac mix")
    ap.add_argument("--engine", default="auto",
                    choices=["auto", "demucs", "center-cut", "none"],
                    help="vocal separation engine (default: auto)")
    ap.add_argument("--instrumental", help="use an existing instrumental WAV/MP3")
    ap.add_argument("--vocals", help="use an existing isolated vocal WAV/MP3")
    ap.add_argument("--srt", help="existing .srt lyric file (skips whisper transcription)")
    ap.add_argument("--words", help="existing whisper -ojf word-timing JSON")
    ap.add_argument("--ocr-lyrics", action="store_true",
                    help="extract timestamped lyric cues from on-screen karaoke video via OCR")
    ap.add_argument("--semitones", type=float, default=0.0,
                    help="shift key in semitones via rubberband, e.g. -2 or +1.5")
    ap.add_argument("--tempo", type=float, default=1.0,
                    help="tempo multiplier via rubberband, e.g. 0.95 (default 1.0)")
    ap.add_argument("--bpm", type=float, default=120.0,
                    help="project tempo in BPM for Reaper session & delay sync (default 120)")
    ap.add_argument("--guide-db", default="off",
                    help="guide vocal level in dB (e.g. -18), or 'off' for pure instrumental")
    ap.add_argument("--sing", help="recorded singer vocal file to clean, reverb, and mix in")
    ap.add_argument("--reverb", type=float, default=0.22,
                    help="vocal plate reverb amount 0.0-0.6 for --sing (default 0.22)")
    ap.add_argument("--reaper", action="store_true",
                    help="also emit a multi-track Reaper .rpp session alongside the output")
    ap.add_argument("--arm-mic", action="store_true",
                    help="arm Track 1 ('Lead Vocals (Mic)') for recording in the Reaper session")
    ap.add_argument("--monitoring", default="on", choices=["on", "off"],
                    help="input monitoring for --arm-mic (default: on)")
    ap.add_argument("--apply-live", action="store_true",
                    help="when --sing + --reaper are used, apply native Reaper FX live via ReaScript")
    ap.add_argument("--open", action="store_true",
                    help="open the Reaper session when ready")
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
        apply_plan_overrides(plan, args, sys.argv[1:], dst)
    else:
        if not (args.input or args.youtube or (args.instrumental and args.vocals)):
            ap.error("provide --input (-i), --youtube (-y), or --from-plan")
        plan = build_karaoke_plan(args, stem_base, dst)
        plan_path = stem_base.with_name(f"{stem_base.name}.karaoke.json")
        plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        S.report(plan_path, "karaoke plan")
        if args.plan_only:
            return 0

    art = plan["artifacts"]
    inst_wav = Path(art["instrumental"])
    voc_wav = Path(art["vocals"])
    video_mp4 = Path(art.get("video", stem_base.with_name(f"{stem_base.name}-video.mp4")))
    lyrics_md = Path(art.get("lyrics_md", stem_base.with_name(f"{stem_base.name}.lyrics.md")))
    srt_path = Path(art["srt"])
    words_path = Path(art["words"])
    ass_path = Path(art["ass"])
    mix_wav = Path(art["audio_mix"])

    if plan.get("youtube"):
        dl_video, dl_backing = download_youtube_karaoke(
            plan["youtube"], dst.parent, stem=stem_base.name)
        video_mp4 = dl_video
        if abs(plan.get("semitones", 0.0)) > 1e-3 or abs(plan.get("tempo", 1.0) - 1.0) > 1e-3:
            shift_key_and_tempo(dl_backing, inst_wav, plan["semitones"], plan["tempo"])
        elif dl_backing.resolve() != inst_wav.resolve():
            shutil.copyfile(dl_backing, inst_wav)
        S.report(video_mp4, "karaoke lyric video")
        S.report(inst_wav, "instrumental backing track")
    elif not (inst_wav.exists() and voc_wav.exists()):
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

    vid_source = video_mp4 if video_mp4.exists() else (Path(plan["input"]) if plan.get("input") else None)
    if plan.get("ocr_lyrics") and vid_source and vid_source.exists():
        extract_video_lyrics_ocr(vid_source, lyrics_md)
        S.report(lyrics_md, "OCR timestamped lyric cues")

    # If the caller asked only for a Reaper session (.rpp) from a YouTube karaoke track,
    # build the session directly without re-encoding a full video unless requested.
    if dst.suffix.lower() != ".rpp":
        if voc_wav.exists() or srt_path.exists():
            ensure_karaoke_subtitles(
                voc_wav, srt_path, words_path, ass_path,
                brand=plan.get("brand"), accent=plan.get("accent"),
            )
            S.report(ass_path, "karaoke ASS subtitles")

        tokens = S.tokens(plan.get("brand"))
        target_i, target_tp = S.targets(tokens, args.target, args.true_peak)
        mix_karaoke_audio(
            inst_wav, voc_wav if voc_wav.exists() else None, plan.get("sing"), mix_wav,
            plan.get("guide_db"), plan.get("sing_reverb", 0.22),
            target_i, target_tp,
        )
        S.report(mix_wav, "mastered karaoke audio")
        S.report_loudness(mix_wav, target_i, target_tp)

        if dst.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv"):
            render_karaoke_video(mix_wav, ass_path if ass_path.exists() else None, dst,
                                 src_media=vid_source, brand=plan.get("brand"))
            S.report(dst, "karaoke video")

    if plan.get("reaper") or dst.suffix.lower() == ".rpp":
        rpp_out = dst if dst.suffix.lower() == ".rpp" else stem_base.with_suffix(".rpp")
        session_py = Path(__file__).resolve().parent / "session.py"
        cmd = [sys.executable, str(session_py), "-o", str(rpp_out),
               "--karaoke-backing", str(inst_wav),
               "--tempo", str(plan.get("bpm", 120.0))]
        if voc_wav.exists() and not plan.get("youtube"):
            cmd += ["-t", f"{voc_wav}@0:-18"]
        if vid_source and vid_source.exists():
            cmd += ["--video", str(vid_source)]
        if lyrics_md.exists():
            cmd += ["--markers", str(lyrics_md)]
        if plan.get("sing") and Path(plan["sing"]).exists():
            cmd += ["--produce-vocal", str(plan["sing"])]
            if plan.get("apply_live"):
                cmd.append("--apply-live")
        elif plan.get("arm_mic"):
            cmd += ["--arm-mic", "--monitoring", plan.get("monitoring", "on")]
        if plan.get("open"):
            cmd.append("--open")
        S.run(cmd)

    return 0


if __name__ == "__main__":
    sys.exit(main())
