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
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for sub in ("scripts", "skills/vidkit/scripts", "skills/artgen/scripts",
            "skills/raster/scripts", "skills/sound/scripts"):
    sys.path.insert(0, str(ROOT / sub))

import analyze  # noqa: E402
import autocut  # noqa: E402
import captions  # noqa: E402
import edit  # noqa: E402
import hostenv  # noqa: E402
import karaoke  # noqa: E402
import record  # noqa: E402
import segment  # noqa: E402
import session  # noqa: E402
import sync_skills  # noqa: E402
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

    def test_round_resets_compose_mode_before_shadow(self):
        ops = edit.parse_ops("resize:960,round:16,shadow,strip")
        argv, alpha = edit.build_args(ops, {}, 1000, 600)
        self.assertTrue(alpha)
        self.assertIn("CopyOpacity", argv)
        copy_idx = argv.index("CopyOpacity")
        layers_idx = argv.index("-layers")
        self.assertIn("Over", argv[copy_idx:layers_idx])


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


if __name__ == "__main__":
    unittest.main()
