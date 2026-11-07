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
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for sub in ("scripts", "skills/vidkit/scripts", "skills/artgen/scripts",
            "skills/raster/scripts"):
    sys.path.insert(0, str(ROOT / sub))

import analyze  # noqa: E402
import autocut  # noqa: E402
import captions  # noqa: E402
import segment  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
