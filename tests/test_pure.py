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

"""Unit tests for the skills' pure functions: timelines, captions, TTS
chunking and box maths. Standard library only, so CI runs them on a bare
python3 with no venv, no ffmpeg and no network.

    python3 -m unittest discover -s tests
"""

import argparse
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for sub in ("scripts", "skills/vidkit/scripts", "skills/artgen/scripts",
            "skills/raster/scripts", "skills/sound/scripts", "skills/shot/scripts",
            "skills/cad/scripts"):
    sys.path.insert(0, str(ROOT / sub))

import analyze  # noqa: E402
import autocut  # noqa: E402
import cadlib  # noqa: E402
import captions  # noqa: E402
import hostenv  # noqa: E402
import karaoke  # noqa: E402
import record  # noqa: E402
import scan  # noqa: E402
import segment  # noqa: E402
import session  # noqa: E402
import sync_skills  # noqa: E402
import timeline  # noqa: E402
import vidscan  # noqa: E402
import voice  # noqa: E402


class Args(argparse.Namespace):
    """build_plan reads many flags; any not given here is None."""

    def __getattr__(self, name):
        return None


class AutocutTest(unittest.TestCase):
    def test_keep_segments_inverts_pads_and_merges(self):
        silences = [(2.0, 4.0), (4.1, 6.0), (9.0, 12.0)]
        self.assertEqual(autocut.keep_segments(silences, 10.0, 0.2, 0.6),
                         [(0.0, 2.2), (5.8, 9.2)])

    def test_keep_segments_drops_short_clips(self):
        self.assertEqual(autocut.keep_segments([(0.3, 10.0)], 10.0, 0.0, 0.5), [])

    def test_subtract_splits_around_cuts(self):
        self.assertEqual(autocut.subtract([(0.0, 10.0)], [(3.0, 4.0)], 0.5),
                         [(0.0, 3.0), (4.0, 10.0)])

    def test_subtract_drops_slivers(self):
        self.assertEqual(autocut.subtract([(0.0, 10.0)], [(0.2, 10.0)], 0.5), [])

    def test_remap_collapses_cuts_and_ends_at_last_segment(self):
        fn = autocut.remap([(0.0, 2.0), (5.0, 7.0)])
        self.assertEqual(fn(1.0), 1.0)
        self.assertEqual(fn(3.0), 2.0)          # inside the cut: snaps forward
        self.assertEqual(fn(6.0), 3.0)
        self.assertIsNone(fn(8.0))

    def test_remap_cues_drops_cut_cues_and_clips_overruns(self):
        cues = [{"start": 0.5, "end": 1.5, "text": "kept"},
                {"start": 2.5, "end": 4.5, "text": "cut"},
                {"start": 6.0, "end": 9.0, "text": "tail"}]
        self.assertEqual(autocut.remap_cues(cues, [(0.0, 2.0), (5.0, 7.0)]), [
            {"start": 0.5, "end": 1.5, "text": "kept"},
            {"start": 3.0, "end": 4.0, "text": "tail"}])


class AnalyzeTest(unittest.TestCase):
    def test_clean_clamps_sorts_and_starts_chapters_at_zero(self):
        doc = {"title": " T ", "chapters": [{"start": 30, "title": " B "},
                                             {"start": 5, "title": "A"}],
               "cuts": [{"start": 50, "end": 70}, {"start": 8, "end": 8.1},
                        {"start": -1, "end": 2}],
               "broll": [{"at": 90, "duration": 2}]}
        out = analyze.clean(doc, 60.0)
        self.assertEqual(out["title"], "T")
        self.assertEqual(out["chapters"], [{"start": 0.0, "title": "A"},
                                           {"start": 30.0, "title": "B"}])
        self.assertEqual(out["cuts"], [{"start": 0.0, "end": 2.0},
                                       {"start": 50.0, "end": 60.0}])
        self.assertEqual(out["broll"], [{"at": 60.0, "duration": 2.0}])


class CaptionsTest(unittest.TestCase):
    def test_ass_color_and_time(self):
        self.assertEqual(captions.ass_color("#1A73E8"), "&H00E8731A")
        self.assertEqual(captions.ass_color("#fff", 0x80), "&H80FFFFFF")
        self.assertEqual(captions.ass_time(3725.456), "1:02:05.46")
        self.assertEqual(captions.ass_time(-1), "0:00:00.00")

    def test_escape_text_strips_markup_and_caps_lines(self):
        self.assertEqual(captions.escape_text("<b>a{b}</b>\nc\nd\ne", 40),
                         "a(b)\\Nc\\Nd")

    def test_time_words_uses_transcript_when_it_lines_up(self):
        cue = {"start": 1.0, "end": 2.0, "text": "hello world"}
        words = [(1.05, 1.4, "hello"), (1.5, 1.9, "world")]
        self.assertEqual(captions.time_words(cue, words),
                         [(1.05, 1.4, "hello"), (1.5, 1.9, "world")])

    def test_time_words_ignores_previous_cue_word_in_lead_in(self):
        # "prev" starts inside the 150ms lead-in but ends before the cue does.
        cue = {"start": 1.0, "end": 2.0, "text": "world"}
        words = [(0.9, 0.98, "prev"), (1.2, 1.8, "world")]
        self.assertEqual(captions.time_words(cue, words), [(1.2, 1.8, "world")])

    def test_time_words_estimates_when_counts_differ(self):
        cue = {"start": 0.0, "end": 3.0, "text": "aa bb"}
        out = captions.time_words(cue, [(0.0, 1.0, "aa")])
        self.assertEqual([w for *_, w in out], ["aa", "bb"])
        self.assertAlmostEqual(out[0][1], 1.5)
        self.assertAlmostEqual(out[-1][1], 3.0)

    def test_wrap_words_matches_greedy_fill(self):
        words = [(0, 0, w) for w in "the quick brown fox jumps".split()]
        self.assertEqual(captions.wrap_words(words, 10), {2, 4})


class VoiceTest(unittest.TestCase):
    def test_plain_strips_markdown(self):
        md = "# Title\n\nSee [docs](http://x) and **bold** `code`.\n\n```\nskip\n```"
        self.assertEqual(voice.plain(md).split(), "Title See docs and bold code.".split())

    def test_chunk_narration_splits_paragraphs_and_long_text(self):
        long = " ".join(["A sentence of about forty bytes here."] * 60)
        chunks = voice.chunk_narration(f"One.\n\nTwo.\n\n{long}")
        self.assertEqual([c["text"] for c in chunks[:2]], ["One.", "Two."])
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(voice.size(c["text"]) <= voice.MAX_BYTES for c in chunks))

    def test_parse_dialogue(self):
        self.assertEqual(voice.parse_dialogue("Ana: hi\nBo: hello\n"),
                         [("Ana", "hi"), ("Bo", "hello")])
        self.assertIsNone(voice.parse_dialogue("Step 1: open\nStep 2: run"))
        self.assertIsNone(voice.parse_dialogue("Ana: hi\njust prose"))

    def test_chunk_dialogue_never_splits_a_turn(self):
        line = "x" * (voice.MAX_BYTES // 2 + 1)
        chunks = voice.chunk_dialogue([("A", line), ("B", line), ("A", "short")])
        self.assertEqual([len(c["turns"]) for c in chunks], [1, 2])

    def test_tts_chain_puts_primary_first(self):
        chain = voice.tts_chain("gemini-2.5-pro-tts", True)
        self.assertEqual(chain[0], "gemini-2.5-pro-tts")
        self.assertEqual(sorted(chain), sorted(voice.SEQUENCE))

    def test_tts_chain_drops_3_8_on_an_old_sdk_even_when_asked_for(self):
        chain = voice.tts_chain("gemini-3.8-pro-tts", False)
        self.assertTrue(chain)
        self.assertTrue(all(voice.version(m) < 3.8 for m in chain), chain)

    def test_build_plan_gives_two_speakers_distinct_voices(self):
        script = "A: hi there\nB: hello back\n"
        plan = voice.build_plan(script, Args(engine="gemini"))
        self.assertEqual(plan["speakers"], {"A": voice.DEFAULT_VOICE, "B": voice.SECOND_VOICE})
        plan = voice.build_plan(script, Args(engine="gemini", voice=voice.SECOND_VOICE))
        self.assertEqual(plan["speakers"], {"A": voice.SECOND_VOICE, "B": voice.DEFAULT_VOICE})


class SegmentTest(unittest.TestCase):
    def test_box_px_and_valid_box(self):
        self.assertEqual(segment.box_px([100, 250, 500, 750], (200, 100)), (50, 10, 150, 50))
        self.assertTrue(segment._valid_box([0, 0, 10, 10]))
        self.assertFalse(segment._valid_box([10, 0, 5, 10]))
        self.assertFalse(segment._valid_box([0, 0, 10]))

    def test_parse_colour(self):
        self.assertEqual(segment.parse_colour("#202124"), (32, 33, 36, 255))
        self.assertEqual(segment.parse_colour("fff"), (255, 255, 255, 255))

    def probe(self, payload):
        done = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with mock.patch.object(segment.R.hostenv, "ffmpeg_bin", return_value="ffprobe"), \
                mock.patch.object(segment.subprocess, "run", return_value=done):
            return segment.video_meta("clip.mp4")

    def test_video_meta_reads_size_and_rate(self):
        stream = {"width": 321, "height": 241, "r_frame_rate": "30000/1001"}
        w, h, fps = self.probe({"streams": [stream]})
        self.assertEqual((w, h), (321, 241))
        self.assertAlmostEqual(fps, 29.97, places=2)

    def test_video_meta_falls_back_on_unknown_rate(self):
        stream = {"width": 2, "height": 2, "r_frame_rate": "0/0"}
        self.assertEqual(self.probe({"streams": [stream]}), (2, 2, 30.0))

    def test_video_meta_rejects_audio_only(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self.probe({"streams": []})

    def test_video_erase_gets_the_video_message_not_the_png_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "demo.mp4"
            clip.write_bytes(b"")
            record = Path(tmp) / "demo.segments.json"
            record.write_text("{}")
            argv = ["segment.py", "-i", str(clip), "--from-json", str(record),
                    "--op", "erase", "-o", str(Path(tmp) / "out.mp4")]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(segment.R, "die", side_effect=SystemExit) as die, \
                    self.assertRaises(SystemExit):
                segment.main()
        self.assertIn("no alpha channel", die.call_args.args[0])


class AudioRoutingAndKaraokeTest(unittest.TestCase):
    def test_parse_avfoundation_audio_extracts_indices_and_loopback_flags(self):
        sample = (
            "[AVFoundation indev @ 0x1] AVFoundation video devices:\n"
            "[AVFoundation indev @ 0x1] [0] Capture screen 0\n"
            "[AVFoundation indev @ 0x1] AVFoundation audio devices:\n"
            "[AVFoundation indev @ 0x1] [0] Volt 276\n"
            "[AVFoundation indev @ 0x1] [1] Loopback Audio\n"
            "[AVFoundation indev @ 0x1] [2] BlackHole 2ch\n"
        )
        devs = hostenv.parse_avfoundation_audio(sample)
        self.assertEqual([d["name"] for d in devs],
                         ["Volt 276", "Loopback Audio", "BlackHole 2ch"])
        self.assertEqual([d["loopback"] for d in devs], [False, True, True])

    def test_parse_system_profiler_marks_rogue_amoeba_virtual_device_as_loopback(self):
        payload = json.dumps({
            "SPAudioDataType": [{
                "_items": [
                    {
                        "_name": "Volt 276",
                        "coreaudio_device_input": 2,
                        "coreaudio_device_manufacturer": "Universal Audio",
                        "coreaudio_device_transport": "coreaudio_device_type_usb",
                        "coreaudio_default_audio_input_device": "spaudio_yes",
                    },
                    {
                        "_name": "Clean Mic",
                        "coreaudio_device_input": 2,
                        "coreaudio_device_manufacturer": "Rogue Amoeba Software, Inc.",
                        "coreaudio_device_transport": "coreaudio_device_type_virtual",
                    },
                ]
            }]
        })
        devs = hostenv.parse_system_profiler_audio(payload)
        self.assertEqual(len(devs), 2)
        self.assertFalse(devs[0]["loopback"])
        self.assertTrue(devs[0]["default"])
        self.assertTrue(devs[1]["loopback"])

        merged = hostenv.merge_avf_indices(
            devs,
            [{"index": 3, "name": "Volt 276", "backend": "avfoundation", "loopback": False}],
        )
        self.assertEqual(merged[0]["index"], 3)
        self.assertIsNone(merged[1]["index"])

    def test_karaoke_url_validation_and_plan_overrides(self):
        self.assertEqual(
            karaoke.validate_youtube_url("https://www.youtube.com/watch?v=0d3LzwjjTog"),
            "https://www.youtube.com/watch?v=0d3LzwjjTog",
        )
        for bad in ("--exec=id", "-o /tmp/pwn", "file:///etc/passwd", ""):
            with self.assertRaises(ValueError):
                karaoke.validate_youtube_url(bad)

        base_args = Args(
            youtube=None, input="raw/song.mp3", engine="center-cut",
            semitones=-2.0, tempo=1.0, bpm=98.0, guide_db="-18",
            sing=None, reverb=0.18, brand=None, accent="#22D3EE",
            reaper=False, arm_mic=False, monitoring="on", ocr_lyrics=False,
            apply_live=False, open=False, instrumental=None, vocals=None,
            srt=None, words=None,
        )
        plan = karaoke.build_karaoke_plan(
            base_args, Path("out/karaoke-practice"), Path("out/karaoke-practice.mp4")
        )
        self.assertEqual(plan["guide_db"], -18.0)
        self.assertEqual(plan["artifacts"]["video"], "out/karaoke-practice-video.mp4")
        self.assertEqual(plan["artifacts"]["audio_mix"], "out/karaoke-practice.mix.wav")

        override_args = Args(
            sing="out/live-take.mic-clean.wav", guide_db="off", reverb=0.24,
            reaper=True, arm_mic=False, monitoring="on", apply_live=True,
            open=False, brand=None, accent=None, srt=None, words=None,
        )
        argv = [
            "--from-plan", "out/karaoke-practice.karaoke.json",
            "--sing", "out/live-take.mic-clean.wav",
            "--reverb", "0.24", "--guide-db", "off",
            "--reaper", "--apply-live", "-o", "out/karaoke-master.mp4",
        ]
        updated = karaoke.apply_plan_overrides(
            plan, override_args, argv, Path("out/karaoke-master.mp4")
        )
        self.assertEqual(updated["sing"], "out/live-take.mic-clean.wav")
        self.assertIsNone(updated["guide_db"])
        self.assertAlmostEqual(updated["sing_reverb"], 0.24)
        self.assertTrue(updated["reaper"])
        self.assertTrue(updated["apply_live"])
        self.assertEqual(updated["artifacts"]["audio_mix"], "out/karaoke-master.mix.wav")

    def test_parse_pactl_sources_flags_monitors(self):
        sample = (
            "42\talsa_output.pci.analog-stereo.monitor\tPipeWire\ts32le 2ch 48000Hz\tIDLE\n"
            "43\talsa_input.usb-Universal_Audio_Volt_276\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
        )
        devs = hostenv.parse_pactl_sources(sample)
        self.assertEqual([d["loopback"] for d in devs], [True, False])

    def test_record_resolve_device_and_hijack_js(self):
        devs = [{"index": 0, "name": "Clean Mic", "backend": "avfoundation", "loopback": True}]
        self.assertEqual(record.resolve_device_spec("clean", devs)["name"], "Clean Mic")
        js = record.build_hijack_js("Karaoke Live", "start", "/tmp/out.wav")
        self.assertIn('app.sessionWithName("Karaoke Live")', js)
        self.assertIn("session.start()", js)
        self.assertIn('rec.fileName = "out"', js)

    def test_karaoke_filters_preserve_bass_and_bound_reverb(self):
        filt = karaoke.center_cut_instrumental_filter(130)
        self.assertIn("lowpass=f=130[bass]", filt)
        self.assertIn("highpass=f=130,pan=stereo|c0=c0-0.92*c1|c1=c1-0.92*c0[side]", filt)
        self.assertIn("aecho=", karaoke.vocal_reverb_chain(0.25))
        self.assertNotIn("aecho=", karaoke.vocal_reverb_chain(0.0))

    def test_karaoke_ocr_deduplicates_consecutive_lyric_screens(self):
        cues = [
            (4, "This is the letter\nthat I didn't write"),
            (10, "This is the letter\nthat I didn't write!"),
            (28, "But you won't let me breathe"),
        ]
        deduped = karaoke.deduplicate_ocr_cues(cues)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0][0], 4.0)
        self.assertIn("letter", deduped[0][1])
        self.assertEqual(deduped[1][0], 28.0)

    def test_session_reaper_gain_delay_and_automation_math(self):
        self.assertAlmostEqual(session.db_to_linear_gain(0.0), 1.0, places=6)
        self.assertAlmostEqual(session.db_to_linear_gain(-6.0206), 0.5, places=3)
        self.assertAlmostEqual(session.reaper_delay_musical_norm(2.0), 0.0078125, places=7)
        self.assertAlmostEqual(session.reaper_delay_musical_norm(8.0), 0.03125, places=7)

        windows = [(0.0, float("-inf")), (8.0, -25.0), (20.0, -18.0),
                   (40.0, -18.0), (150.0, -12.0), (170.0, float("-inf"))]
        start, length = session.detect_active_bounds(windows, 175.0)
        self.assertAlmostEqual(start, 7.2, places=1)
        self.assertAlmostEqual(length, 144.3, places=1)

        pts = session.compute_vocal_automation_points(windows)
        self.assertEqual(pts[0], (0.0, 1.0))
        # Quiet verse at 8s (-25 dBFS vs -18 dBFS median) gets boosted > 1.0
        self.assertGreater(dict(pts)[8.0], 1.3)
        # Loud outro at 150s (-12 dBFS vs -18 dBFS median) gets trimmed < 1.0
        self.assertLess(dict(pts)[150.0], 0.9)

    def test_session_parse_markers_and_produce_lua(self):
        md = (
            "# Lyrics\n"
            "| Timestamp | Section | Lyric |\n"
            "| :--- | :--- | :--- |\n"
            '| `0:04` | **Verse 1** | *"This is the letter"* |\n'
            "1:20 Chorus 2\n"
        )
        markers = session.parse_markers_text(md)
        self.assertEqual(markers, [(4.0, "Verse 1: This is the letter"), (80.0, "Chorus 2")])

        plan = {
            "eq": {"hpf_hz": 95.0, "low_shelf_hz": 180.0, "low_shelf_db": 1.2,
                   "mud_hz": 330.0, "mud_db": -4.0, "mud_bw": 0.75,
                   "presence_hz": 3400.0, "presence_db": 4.2, "presence_bw": 1.1,
                   "air_hz": 10500.0, "air_db": 3.2},
            "echo": {"send_db": -12.5, "eighth_notes": 2.0, "feedback_norm": 0.3548},
            "reverb": {"send_db": -10.5, "room_size": 0.72, "predelay_norm": 0.070},
        }
        lua = session.build_produce_lua("/tmp/song.rpp", "/tmp/fx-report.txt", plan, tempo=98.0)
        self.assertIn("noprompt:", lua)
        self.assertIn("10.0 ^ (db / 20.0)", lua)
        self.assertIn("0.00781250", lua)


class SyncSkillsTest(unittest.TestCase):
    def test_git_returns_124_on_timeout(self):
        exc = subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=5)
        with mock.patch.object(sync_skills.subprocess, "run", side_effect=exc):
            rc, out = sync_skills.git("fetch", timeout=5)
        self.assertEqual(rc, 124)
        self.assertIn("timed out after 5s", out)

    def test_prune_normalizes_relative_symlinks_and_keeps_known_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "creative-stack"
            vendor = root / "vendor"
            foreign = root / "other-repo"
            target = root / "harness" / "skills"
            repo.mkdir()
            vendor.mkdir()
            foreign.mkdir()
            target.mkdir(parents=True)

            # Broken relative symlink that resolves inside repo -> should be pruned
            stale_ours = target / "old-skill"
            stale_ours.symlink_to("../../creative-stack/skills/old-skill")

            # Broken relative symlink that traverses through repo into foreign -> keep
            stale_foreign = target / "foreign-skill"
            stale_foreign.symlink_to("../../creative-stack/../other-repo/foreign-skill")

            # Pending external skill (in EXTERNAL, omitted from available `skills`) -> keep
            pending_ext = target / "anot8"
            pending_ext.symlink_to(vendor / "anot8" / "skills" / "anot8")

            with mock.patch.object(sync_skills, "REPO_ROOT", repo), \
                    mock.patch.object(sync_skills, "VENDOR", vendor), \
                    mock.patch("builtins.print"):
                changed, problems = sync_skills.sync_dir(
                    target, ["Test"], {}, fix=True, prune=True, uninstall=False)

            self.assertEqual((changed, problems), (1, 0))
            self.assertFalse(stale_ours.is_symlink())
            self.assertTrue(stale_foreign.is_symlink())
            self.assertTrue(pending_ext.is_symlink())


def ocr_line(text, x=0, y=0, char_w=10, h=20):
    """Words of one OCR line, laid out monospace from (x, y)."""
    words = []
    for token in text.split(" "):
        words.append({"text": token, "box": [x, y, len(token) * char_w, h]})
        x += (len(token) + 1) * char_w
    return words


class ScanTest(unittest.TestCase):
    def kinds(self, text):
        words = ocr_line(text)
        line, _ = scan.line_text(words)
        return [(kind, line[s:e]) for kind, s, e in scan.find(words)]

    def test_parse_tsv_groups_lines_and_scales_back(self):
        tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop"
               "\twidth\theight\tconf\ttext\n"
               "1\t1\t0\t0\t0\t0\t0\t0\t2000\t1200\t-1\t\n"
               "5\t1\t1\t1\t1\t1\t20\t40\t100\t30\t96\ttoken:\n"
               "5\t1\t1\t1\t1\t2\t140\t40\t300\t30\t0\tacme\u2014x\n"
               "5\t1\t2\t1\t1\t1\t20\t400\t80\t30\t91\tother\n")
        size, lines = scan.parse_tsv(tsv, scale=2.0)
        self.assertEqual(size, (1000, 600))
        self.assertEqual([[w["text"] for w in line] for line in lines],
                         [["token:", "acme-x"], ["other"]])   # zero confidence kept
        self.assertEqual(lines[0][1]["box"], [70, 20, 150, 15])

    def test_known_key_survives_ocr_misreads_and_splits(self):
        self.assertEqual(self.kinds("key AlZzaSyFAKE_EXAMPLE_KEY_ 1234567"),
                         [("google-api-key", "AlZzaSyFAKE_EXAMPLE_KEY_ 1234567")])

    def test_glue_keeps_cli_flags_and_lone_dashes_apart(self):
        self.assertEqual(self.kinds("AIzaSyFAKE_EXAMPLE_KEY_1234567 --output x"),
                         [("google-api-key", "AIzaSyFAKE_EXAMPLE_KEY_1234567")])
        self.assertEqual(scan.glue("Run - my-service")[0], "Run - my-service")
        self.assertEqual(scan.glue("KEY_ 12 x _y")[0], "KEY_12 x_y")

    def test_ai_kind_never_keeps_the_label_text(self):
        self.assertEqual(scan.ai_kind("API key AIzaSyFAKE_EXAMPLE"), "ai-secret")
        self.assertEqual(scan.ai_kind("jane.doe@example.com email address"), "ai-email")
        self.assertEqual(scan.ai_kind("Profile photo"), "ai-avatar")
        self.assertEqual(scan.ai_kind("Jane Doe"), "ai-other")

    def test_labelled_values_blur_the_value_not_the_name(self):
        self.assertEqual(self.kinds("DB_PASSWORD=hunter2"), [("label", "hunter2")])
        self.assertEqual(self.kinds("Password: hunter2 x"), [("label", "hunter2 x")])

    def test_key_inside_label_is_one_hit(self):
        self.assertEqual(self.kinds("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"),
                         [("aws-access-key", "AKIAIOSFODNN7EXAMPLE")])

    def test_email_project_and_card(self):
        self.assertEqual(self.kinds("mail jane.doe@example.com now"),
                         [("email", "jane.doe@example.com")])
        self.assertEqual(self.kinds("gcloud config set project acme-billing-412907"),
                         [("project", "acme-billing-412907")])
        self.assertEqual(self.kinds("card 4242 4242 4242 4242"),
                         [("card", "4242 4242 4242 4242")])
        self.assertEqual(self.kinds("order 4242 4242 4242 4241"), [])   # fails Luhn

    def test_distractors_are_not_flagged(self):
        for text in ("9f2c1e7a4b3d5f60718293a4b5c6d7e8f9012345",
                     "Opening https://console.cloud.google.com/run/detail/us-central1/api",
                     "Wrote /Users/me/projects/site/build/index.html in 2.3s",
                     "Updated property [core/project]."):
            self.assertEqual(self.kinds(text), [], text)

    def test_unknown_random_token_is_flagged(self):
        self.assertEqual(self.kinds("id Zx9Qm2LpV7rT4kWb8NcY3hJd6FgS1aEu"),
                         [("entropy", "Zx9Qm2LpV7rT4kWb8NcY3hJd6FgS1aEu")])

    def test_scan_lines_clips_a_word_to_the_value_and_masks_it(self):
        [hit] = scan.scan_lines([ocr_line("GITHUB_TOKEN=ghp_abc")])
        self.assertEqual(hit["box"], [120, 0, 80, 20])     # one char of slack before
        self.assertEqual(hit["preview"], "ghp_… (7 chars)")
        self.assertNotIn("abc", json.dumps(hit))

    def test_merge_into_spec_skips_covered_and_puts_blurs_first(self):
        spec = {"annotations": [
            {"type": "box", "id": "scan-1", "x": 0, "y": 0, "width": 10, "height": 10},
            {"type": "blur", "x": "10%", "y": "10%", "width": "50%", "height": "20%"}]}
        findings = [{"kind": "email", "box": [120, 70, 200, 20]},    # inside the blur
                    {"kind": "label", "box": [600, 400, 100, 20]}]
        self.assertEqual(scan.merge_into_spec(spec, findings, (1000, 600)), 1)
        first = spec["annotations"][0]
        self.assertEqual((first["type"], first["id"]), ("blur", "scan-2"))
        self.assertEqual([first["x"], first["y"], first["width"], first["height"]],
                         [595, 395, 110, 30])
        self.assertEqual(len(spec["annotations"]), 3)


    def test_ocr_maps_trimmed_boxes_back_to_the_full_image(self):
        tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop"
               "\twidth\theight\tconf\ttext\n"
               "5\t1\t1\t1\t1\t1\t20\t40\t100\t30\t96\ttoken:\n")
        done = subprocess.CompletedProcess([], 0, stdout=tsv.encode(), stderr=b"")
        with mock.patch.object(scan, "prepare", return_value=(b"", 2.0, (1280, 800), (0, 16))), \
                mock.patch.object(scan.hostenv, "which", return_value="tesseract"), \
                mock.patch.object(scan.subprocess, "run", return_value=done):
            size, lines = scan.ocr(Path("frame.png"))
        self.assertEqual(size, (1280, 800))
        self.assertEqual(lines[0][0]["box"], [10, 36, 50, 15])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "needs Pillow")
    def test_prepare_keeps_a_margin_and_scales_by_the_full_frame(self):
        from PIL import Image
        img = Image.new("L", (800, 600), 255)
        img.paste(0, (390, 290, 430, 302))               # one small "word", no border
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "word.png"
            img.save(path)
            data, scale, size, (dx, dy) = scan.prepare(path)
        out = Image.open(io.BytesIO(data))
        self.assertEqual((size, scale), ((800, 600), 3.0))
        margin = scan.TRIM_MARGIN
        self.assertEqual((dx, dy), (390 - margin, 290 - margin))
        self.assertEqual(out.size, (round((40 + 2 * margin) * 3), round((12 + 2 * margin) * 3)))
        self.assertEqual(out.getpixel((0, 0)), 255)       # margin in the page's colour


class VidScanTest(unittest.TestCase):
    def hit(self, kind, box):
        return {"kind": kind, "preview": "x… (9 chars)", "box": box}

    def test_tracks_join_samples_and_bridge_one_missed_frame(self):
        key = self.hit("label", [100, 100, 200, 20])
        moved = self.hit("label", [104, 102, 200, 20])        # scroll jitter
        samples = [(0, []), (1, [key]), (2, [moved]), (3, []), (4, [key]),
                   (5, []), (6, []), (7, [key])]
        found = vidscan.tracks(samples, 1.0)
        self.assertEqual([(k["start"], k["end"]) for k in found], [(1, 4), (7, 7)])
        self.assertEqual(found[0]["box"], [100, 100, 204, 22])

    def test_tracks_bridge_a_missed_frame_at_fractional_intervals(self):
        key = self.hit("label", [100, 100, 200, 20])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(vidscan, "ffmpeg"):
            for i in range(4):
                (Path(tmp) / f"f{i:06d}.png").write_bytes(b"")
            times = [t for t, _ in vidscan.sample(Path("in.mp4"), 3.0, tmp)]
        samples = [(times[0], [key]), (times[1], []), (times[2], []), (times[3], [key])]
        self.assertEqual(len(vidscan.tracks(samples, 1 / 3.0)), 2)
        samples[2] = (times[2], [key])
        del samples[1]
        self.assertEqual(len(vidscan.tracks(samples, 1 / 3.0)), 1)

    def test_widen_leaves_the_end_open_when_duration_is_unknown(self):
        track = {"kind": "label", "preview": "", "start": 4.0, "end": 9.5,
                 "box": [100, 50, 100, 20]}
        self.assertEqual(vidscan.widen(track, 1.0, 0.0, (1280, 720))["end"], 11.5)

    def test_widened_tracks_cover_a_gap_tracks_does_not_bridge(self):
        # Seen at 0 and 3, missed at 1 and 2: two tracks, but their blurs
        # must still meet, or the secret is readable between them.
        key = self.hit("label", [0, 0, 100, 20])
        samples = [(0.0, [key]), (1.0, []), (2.0, []), (3.0, [key])]
        first, second = (vidscan.widen(k, 1.0, 10.0, (1280, 720))
                         for k in vidscan.tracks(samples, 1.0))
        self.assertGreaterEqual(first["end"], second["start"])

    def test_from_json_numbers_are_coerced_before_reaching_the_filter_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.json"
            path.write_text(json.dumps([{"kind": "label", "box": ["10", 20, 30, 40],
                                         "start": "1", "end": 2}]))
            self.assertEqual(vidscan.load_tracks(path),
                             [{"kind": "label", "preview": "", "start": 1.0,
                               "end": 2.0, "box": [10, 20, 30, 40]}])
            for bad in ({"box": [0, 0, 9, 9], "start": "1,0)'[x]", "end": 2},
                        {"box": [0, 0, 9], "start": 0, "end": 1},
                        {"box": [0, 0, 9, 9], "start": 2, "end": 1}):
                path.write_text(json.dumps([bad]))
                with self.assertRaises(SystemExit), \
                        contextlib.redirect_stderr(io.StringIO()), \
                        contextlib.redirect_stdout(io.StringIO()):
                    vidscan.load_tracks(path)

    def test_render_with_nothing_found_still_writes_the_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, out, tracks = (Path(tmp) / n for n in ("in.mp4", "out.mp4", "scan.json"))
            src.write_bytes(b"video")
            tracks.write_text("[]")
            argv = ["vidscan.py", "-i", str(src), "--from-json", str(tracks), "--render", str(out)]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(vidscan, "need"), \
                    mock.patch.object(vidscan, "probe", return_value={"has_video": True}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(vidscan.main(), 0)
            self.assertEqual(out.read_bytes(), b"video")

    def test_tracks_keep_kinds_and_places_apart(self):
        samples = [(0, [self.hit("label", [0, 0, 100, 20]),
                        self.hit("email", [0, 0, 100, 20]),
                        self.hit("label", [0, 300, 100, 20])])]
        self.assertEqual(len(vidscan.tracks(samples, 1.0)), 3)

    def test_widen_pads_time_and_rounds_box_to_even_pixels(self):
        track = {"kind": "label", "preview": "", "start": 0.5, "end": 9.5,
                 "box": [101, 51, 99, 21]}
        out = vidscan.widen(track, 1.0, 10.0, (1280, 721))
        self.assertEqual((out["start"], out["end"]), (0.0, 10.0))
        x, y, w, h = out["box"]
        self.assertTrue(all(v % 2 == 0 for v in out["box"]))
        self.assertTrue(x <= 101 and y <= 51 and x + w >= 200 and y + h >= 72)
        self.assertLessEqual(y + h, 720)

    def test_blur_graph_overlays_each_track_only_in_its_span(self):
        found = [{"box": [10, 20, 200, 40], "start": 1.0, "end": 3.5},
                 {"box": [0, 0, 6, 4], "start": 0.0, "end": 2.0}]
        graph = vidscan.blur_graph(found)
        self.assertTrue(graph.startswith("[0:v]split=3[base][c0][c1];"))
        self.assertIn("[c0]crop=200:40:10:20,boxblur=luma_radius=19:", graph)
        self.assertIn("chroma_radius=1:", graph)                   # tiny box stays legal
        self.assertIn("[base][b0]overlay=10:20:enable='between(t,1.0,3.5)'[v0];", graph)
        self.assertTrue(graph.rstrip().endswith("enable='between(t,0.0,2.0)'[vout]"))

class TimelineMltAndFossManifestTest(unittest.TestCase):
    def test_mlt_rate_handles_ntsc_and_integer_fps(self):
        self.assertEqual(timeline.mlt_rate(29.97003), (30000, 1001))
        self.assertEqual(timeline.mlt_rate(59.94), (60000, 1001))
        self.assertEqual(timeline.mlt_rate(23.976), (24000, 1001))
        self.assertEqual(timeline.mlt_rate(30.0), (30, 1))

    def test_build_mlt_xml_writes_inclusive_frames_and_kdenlive_guides(self):
        import xml.etree.ElementTree as ET

        doc = {
            "version": 1,
            "source": "/tmp/demo & talk.mp4",
            "duration": 10.0,
            "fps": 30.0,
            "width": 1920,
            "height": 1080,
            "segments": [
                {"start": 0.0, "end": 2.0},   # 60 frames: in=0, out=59
                {"start": 5.0, "end": 6.5},   # 45 frames: in=150, out=194
            ],
        }
        markers = [(0.0, "Intro"), (2.0, "Cut & Demo")]
        xml_text = timeline.build_mlt_xml(doc, "Walkthrough", markers)
        root = ET.fromstring(xml_text)

        self.assertEqual(root.tag, "mlt")
        profile = root.find("profile")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.attrib["frame_rate_num"], "30")
        self.assertEqual(profile.attrib["frame_rate_den"], "1")
        self.assertEqual(profile.attrib["width"], "1920")
        self.assertEqual(profile.attrib["height"], "1080")

        producer = root.find("producer")
        self.assertIsNotNone(producer)
        props = {p.attrib["name"]: p.text for p in producer.findall("property")}
        self.assertEqual(props["resource"], "/tmp/demo & talk.mp4")

        entries = root.findall("./playlist[@id='playlist0']/entry")
        self.assertEqual(len(entries), 2)
        self.assertEqual((entries[0].attrib["in"], entries[0].attrib["out"]), ("0", "59"))
        self.assertEqual((entries[1].attrib["in"], entries[1].attrib["out"]), ("150", "194"))

        tractor = root.find("./tractor[@id='tractor0']")
        self.assertIsNotNone(tractor)
        tprops = {p.attrib["name"]: p.text for p in tractor.findall("property")}
        guides = json.loads(tprops["kdenlive:docproperties.guides"])
        self.assertEqual(guides, [
            {"pos": 0, "comment": "Intro", "type": 0},
            {"pos": 60, "comment": "Cut & Demo", "type": 0},
        ])

    def test_melt_render_cmd_uses_libx264_aac_faststart(self):
        cmd = timeline.melt_render_cmd("/usr/bin/melt", Path("out/edit.mlt"), Path("out/final.mp4"))
        self.assertEqual(cmd[0], "/usr/bin/melt")
        self.assertIn("out/edit.mlt", cmd)
        self.assertIn("avformat:out/final.mp4", cmd)
        self.assertIn("vcodec=libx264", cmd)
        self.assertIn("acodec=aac", cmd)

    def test_manifest_and_hostenv_drop_resolve_and_include_mlt_kdenlive(self):
        manifest = json.loads((ROOT / "packages/manifest.json").read_text())
        ids = {p["id"] for g in manifest["groups"] for p in g["packages"]}
        self.assertNotIn("resolve", ids)
        self.assertIn("mlt", ids)
        self.assertIn("kdenlive", ids)
        self.assertIn("kdenlive", hostenv.APP_BRIDGES)
        self.assertNotIn("resolve", hostenv.describe())

        claude_pkg = next(
            p for g in manifest["groups"] for p in g["packages"] if p["id"] == "claude-code"
        )
        self.assertTrue(claude_pkg.get("optional"))
        self.assertIn("proprietary", claude_pkg.get("note", "").lower())


def _box_triangles(x0, y0, z0, dx, dy, dz):
    """12 outward-wound triangles for an axis-aligned [x0..x1]x[y0..y1]x[z0..z1] box."""
    x1, y1, z1 = x0 + dx, y0 + dy, z0 + dz
    p000, p100, p110, p010 = (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)
    p001, p101, p111, p011 = (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)
    return [
        (p000, p010, p110), (p000, p110, p100),  # Bottom (-Z)
        (p001, p101, p111), (p001, p111, p011),  # Top (+Z)
        (p000, p100, p101), (p000, p101, p001),  # Front (-Y)
        (p010, p011, p111), (p010, p111, p110),  # Back (+Y)
        (p000, p001, p011), (p000, p011, p010),  # Left (-X)
        (p100, p110, p111), (p100, p111, p101),  # Right (+X)
    ]


class CadKernelTest(unittest.TestCase):
    def test_freecad_in_manifest_and_bridges(self):
        manifest = json.loads((ROOT / "packages/manifest.json").read_text())
        ids = {p["id"] for g in manifest["groups"] for p in g["packages"]}
        self.assertIn("freecad", ids)
        self.assertIn("freecadcmd", hostenv.APP_BRIDGES)

    def test_binary_and_ascii_stl_parsing_and_closed_box_metrics(self):
        tris = _box_triangles(-5.0, -10.0, 0.0, 10.0, 20.0, 30.0)
        blob = cadlib.write_binary_stl_bytes(tris)
        parsed_bin = cadlib.parse_stl(blob)
        self.assertEqual(len(parsed_bin), 12)

        ascii_lines = ["solid box"]
        for a, b, c in tris:
            ascii_lines.append("  facet normal 0 0 0\n    outer loop")
            for vx, vy, vz in (a, b, c):
                ascii_lines.append(f"      vertex {vx} {vy} {vz}")
            ascii_lines.append("    endloop\n  endfacet")
        ascii_lines.append("endsolid box\n")
        parsed_ascii = cadlib.parse_stl("\n".join(ascii_lines).encode("utf-8"))
        self.assertEqual(len(parsed_ascii), 12)

        rep = cadlib.analyze_mesh(parsed_bin)
        self.assertTrue(rep["watertight"])
        self.assertEqual(rep["boundary_edges"], 0)
        self.assertEqual(rep["non_manifold_edges"], 0)
        self.assertEqual(rep["components"], 1)
        self.assertAlmostEqual(rep["volume_cm3"], 6.0, places=3)
        self.assertAlmostEqual(rep["bed_contact_mm2"], 200.0, places=2)
        self.assertAlmostEqual(rep["overhang"]["area_mm2"], 0.0, places=3)
        self.assertTrue(rep["fits_bed"])
        self.assertIsNone(rep["unit_warning"])

    def test_open_mesh_and_disconnected_bodies_detected(self):
        open_box = _box_triangles(0.0, 0.0, 0.0, 10.0, 10.0, 10.0)[:-1]
        rep_open = cadlib.analyze_mesh(open_box)
        self.assertFalse(rep_open["watertight"])
        self.assertEqual(rep_open["boundary_edges"], 3)

        two_parts = (
            _box_triangles(0.0, 0.0, 0.0, 10.0, 10.0, 5.0)
            + _box_triangles(0.0, 0.0, 5.2, 4.0, 4.0, 4.0)
        )
        rep_two = cadlib.analyze_mesh(two_parts)
        self.assertTrue(rep_two["watertight"])
        self.assertEqual(rep_two["components"], 2)

    def test_cantilever_bridge_flags_overhang_and_tiny_or_huge_warns(self):
        shelf = (
            _box_triangles(0.0, 0.0, 0.0, 10.0, 10.0, 20.0)
            + _box_triangles(10.0, 0.0, 10.0, 15.0, 10.0, 5.0)
        )
        rep = cadlib.analyze_mesh(shelf, max_overhang_deg=45.0)
        self.assertAlmostEqual(rep["overhang"]["area_mm2"], 150.0, places=2)

        tiny = cadlib.analyze_mesh(_box_triangles(0.0, 0.0, 0.0, 0.05, 0.05, 0.05))
        self.assertIsNotNone(tiny["unit_warning"])

        huge = cadlib.analyze_mesh(_box_triangles(0.0, 0.0, 0.0, 300.0, 100.0, 50.0),
                                   bed_size=(256.0, 256.0, 256.0))
        self.assertFalse(huge["fits_bed"])


if __name__ == "__main__":
    unittest.main()
