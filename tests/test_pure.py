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
            "skills/raster/scripts", "skills/sound/scripts", "skills/shot/scripts"):
    sys.path.insert(0, str(ROOT / sub))

import analyze  # noqa: E402
import autocut  # noqa: E402
import captions  # noqa: E402
import hostenv  # noqa: E402
import karaoke  # noqa: E402
import record  # noqa: E402
import scan  # noqa: E402
import segment  # noqa: E402
import session  # noqa: E402
import sync_skills  # noqa: E402
import vidscan  # noqa: E402
import voice  # noqa: E402

try:
    import PIL  # noqa: F401
    HAS_PIL = True
except ImportError:                      # bare CI python: the image tests skip
    HAS_PIL = False


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

    def test_speech_islands_inverts_silences(self):
        self.assertEqual(autocut.speech_islands([(1.0, 2.0), (3.0, float("inf"))], 4.0),
                         [(0.0, 1.0), (2.0, 3.0)])

    # Timings shaped like a real DTW run: each word's span lags its island.
    ISLANDS = [(0.0, 0.44), (0.66, 0.95), (1.19, 2.5), (2.72, 3.04), (3.29, 5.0)]
    WHOLE = [(0.0, 5.0)]

    def cut(self, words, fillers=autocut.FILLERS, islands=None, segments=None):
        cuts, left, gone = autocut.filler_cuts(words, islands or self.ISLANDS,
                                               segments or self.WHOLE, fillers)
        return [(round(a, 2), round(b, 2)) for a, b in cuts], left, gone

    def test_filler_cuts_take_the_island_and_the_pause_after(self):
        words = [(0.0, 0.52, "So,"), (0.74, 1.0, "Um,"), (1.24, 1.5, "today"),
                 (1.5, 2.2, "we go"), (2.24, 2.78, "to,"), (2.82, 3.12, "uh,"),
                 (3.44, 3.68, "deploy")]
        self.assertEqual(self.cut(words),
                         ([(0.66, 1.16), (2.72, 3.26)], 0, {1, 5}))

    def test_filler_cuts_leave_a_filler_run_into_speech(self):
        # "um" shares the third island with "today": cutting it would cut speech.
        words = [(0.0, 0.52, "So,"), (0.74, 1.0, "right"), (1.24, 1.5, "um"),
                 (1.5, 2.2, "today")]
        self.assertEqual(self.cut(words), ([], 1, set()))

    def test_filler_cuts_match_phrases_and_keep_like_by_default(self):
        words = [(0.0, 0.4, "like"), (0.7, 0.8, "You"), (0.8, 0.95, "know,"),
                 (1.24, 2.4, "deploy")]
        self.assertEqual(self.cut(words), ([], 0, set()))
        self.assertEqual(self.cut(words, ["like", "you know"]),
                         ([(0.0, 0.63), (0.66, 1.16)], 0, {0, 1, 2}))

    def test_filler_cuts_take_padding_at_segment_edges(self):
        # R-001: keep_segments padded speech by 0.15s. A filler opening a
        # segment takes the lead-in padding; one closing it takes the tail;
        # neither reaches across a cut silence into the next segment.
        islands = [(1.0, 1.3), (1.5, 2.0), (3.0, 3.3), (4.0, 4.6)]
        segments = [(0.85, 3.45), (3.85, 4.75)]
        words = [(0.9, 1.2, "Um,"), (1.4, 1.9, "today"), (2.9, 3.2, "uh"),
                 (3.9, 4.5, "deploy")]
        cuts, left, _ = self.cut(words, islands=islands, segments=segments)
        self.assertEqual((cuts, left), ([(0.85, 1.47), (3.0, 3.45)], 0))
        self.assertEqual(autocut.subtract(segments, cuts, 0.08),
                         [(1.47, 3.0), (3.85, 4.75)])

    def test_filler_cuts_take_a_phrase_whole_or_not_at_all(self):
        # R-002: "you know" split by a pause, "you" sharing an island with "Do".
        islands = [(0.0, 0.44), (0.66, 0.95), (1.19, 2.5)]
        words = [(0.0, 0.2, "Do"), (0.2, 0.44, "you"), (0.66, 0.95, "know"),
                 (1.24, 2.0, "this")]
        self.assertEqual(self.cut(words, ["you know"], islands, [(0.0, 2.5)]),
                         ([], 1, set()))

    def test_load_word_times_uses_dtw_and_ignores_punctuation(self):
        doc = {"transcription": [{"tokens": [
            {"text": "[_BEG_]", "t_dtw": -1},
            {"text": " So", "t_dtw": 52}, {"text": ",", "t_dtw": 74},
            {"text": " h", "t_dtw": 90}, {"text": "mm", "t_dtw": 100},
            {"text": ",", "t_dtw": 124}, {"text": "<|endoftext|>", "t_dtw": -1}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "talk.json"
            path.write_text(json.dumps(doc))
            self.assertEqual(autocut.load_word_times(path),
                             [(0.0, 0.52, "So,"), (0.74, 1.0, "hmm,")])

    def test_load_word_times_keeps_spaced_punctuation_off_the_word_list(self):
        # R-003: whisper emits " -" and " ..." as their own tokens. As words
        # they read as non-fillers and would block cutting the "um" beside them.
        doc = {"transcription": [{"tokens": [
            {"text": " um", "t_dtw": 100}, {"text": " -", "t_dtw": 110},
            {"text": " ...", "t_dtw": 115}, {"text": " so", "t_dtw": 150}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "talk.json"
            path.write_text(json.dumps(doc))
            self.assertEqual(autocut.load_word_times(path),
                             [(0.0, 1.0, "um - ..."), (1.15, 1.5, "so")])

    def test_drop_words_removes_cut_fillers_from_captions(self):
        # R-004: the audio lost "um" and "uh"; the captions must too.
        cues = [{"start": 0.0, "end": 2.0, "text": "So, um, today we\nare going to,"},
                {"start": 2.0, "end": 3.0, "text": "uh,"},
                {"start": 3.0, "end": 4.0, "text": "deploy, um"}]
        words = [(0, 0, w) for w in ("So,", "um,", "today", "we", "are", "going",
                                     "to,", "uh,", "deploy,", "um")]
        self.assertEqual(autocut.drop_words(cues, words, {1, 7}), [
            {"start": 0.0, "end": 2.0, "text": "So, today we\nare going to,"},
            {"start": 3.0, "end": 4.0, "text": "deploy, um"}])


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

    def test_live_match_scores_paraphrase_low(self):
        said = "Welcome to the build pipeline tour."
        self.assertEqual(voice.match(said, "welcome to the build pipeline tour"), 1.0)
        self.assertLess(voice.match(said, "Sure! Let me show you around the pipeline."),
                        voice.LIVE_MIN_MATCH)

    def test_cloned_plan_never_carries_a_voice_key(self):
        plan = voice.build_plan("One.\n\nTwo.", Args(engine="gemini", clone_from="me.wav"))
        self.assertTrue(plan["replicated"])
        self.assertEqual(plan["voice"], voice.CLONED)
        self.assertNotIn("voicekey", json.dumps(plan))

    def test_cloning_refuses_a_dialogue(self):
        with self.assertRaises(SystemExit):
            voice.build_plan("A: hi\nB: hello", Args(engine="gemini", clone_from="me.wav"))

    def test_live_take_reads_every_part_until_turn_complete(self):
        import asyncio
        from types import SimpleNamespace as NS

        def part(data=None):
            return NS(inline_data=NS(data=data, mime_type="audio/pcm;rate=24000") if data else None)

        events = [
            NS(server_content=None),
            NS(server_content=NS(model_turn=NS(parts=[part(b"\x01\x00"), part(b"\x02\x00")]),
                                 output_transcription=NS(text="Hello "), turn_complete=False)),
            NS(server_content=NS(model_turn=None, output_transcription=NS(text="there"),
                                 turn_complete=True)),
            NS(server_content=NS(model_turn=NS(parts=[part(b"\xff\xff")]),
                                 output_transcription=None, turn_complete=False)),
        ]
        sent = []

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def send_client_content(self, **kw):
                sent.append(kw)

            async def receive(self):
                for event in events:
                    yield event

        client = NS(aio=NS(live=NS(connect=lambda model, config: Session())))
        types = NS(**{n: (lambda **kw: NS(**kw)) for n in (
            "LiveConnectConfig", "SpeechConfig", "VoiceConfig", "PrebuiltVoiceConfig",
            "AudioTranscriptionConfig", "Content", "Part")})
        pcm, rate, heard = asyncio.run(
            voice.live_take(client, types, "m", "Hello there", "Puck", ""))
        self.assertEqual((pcm, rate, heard), (b"\x01\x00\x02\x00", 24000, "Hello there"))
        self.assertTrue(sent[0]["turn_complete"])


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

    def test_inpaint_is_an_op_that_follows_the_outline(self):
        self.assertIn("inpaint", segment.OPS)
        args = argparse.Namespace(op="inpaint", shape="auto")
        self.assertFalse(segment.boxy(args))
        args.shape = "box"
        self.assertTrue(segment.boxy(args))

    def test_op_grow_gives_inpaint_a_margin_only(self):
        self.assertEqual(segment.op_grow("inpaint", 0), segment.INPAINT_MIN_GROW)
        self.assertEqual(segment.op_grow("inpaint", 20), 20)
        self.assertEqual(segment.op_grow("blur", 0), 0)
        self.assertEqual(segment.op_grow(None, 3), 3)

    def test_aspect_matches(self):
        self.assertTrue(segment.aspect_matches((1000, 600), (1024, 614)))
        self.assertFalse(segment.aspect_matches((1000, 600), (1024, 1024)))

    def test_video_refuses_gemini_inpaint(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "demo.mp4"
            clip.write_bytes(b"")
            record = Path(tmp) / "demo.segments.json"
            record.write_text("{}")
            argv = ["segment.py", "-i", str(clip), "--from-json", str(record),
                    "--op", "inpaint", "--inpaint", "gemini",
                    "-o", str(Path(tmp) / "out.mp4")]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(segment.R, "die", side_effect=SystemExit) as die, \
                    self.assertRaises(SystemExit):
                segment.main()
        self.assertIn("still images", die.call_args.args[0])

    @unittest.skipUnless(HAS_PIL, "needs Pillow")
    def test_inpaint_changes_only_the_masked_pixels(self):
        from PIL import Image
        base = Image.new("RGB", (8, 4), (10, 20, 30))
        mask = Image.new("L", (8, 4), 0)
        mask.paste(255, (2, 1, 4, 3))
        red = lambda rgba, m: Image.new("RGBA", rgba.size, (255, 0, 0, 255))
        out = segment.apply_op(base, mask, "inpaint", 0, None, 0, red).convert("RGB")
        self.assertEqual(out.getpixel((2, 1)), (255, 0, 0))
        self.assertEqual(out.getpixel((0, 0)), (10, 20, 30))
        self.assertEqual(out.getpixel((5, 2)), (10, 20, 30))

    @unittest.skipUnless(HAS_PIL, "needs Pillow")
    def test_match_tone_removes_a_global_colour_drift(self):
        from PIL import Image
        base = Image.new("RGB", (40, 40), (100, 120, 140))
        drifted = Image.new("RGB", (40, 40), (110, 115, 150))
        mask = Image.new("L", (40, 40), 0)
        mask.paste(255, (15, 15, 25, 25))
        out = segment.match_tone(base, drifted, mask)
        self.assertEqual(out.convert("RGB").getpixel((20, 20)), (100, 120, 140))

    @unittest.skipUnless(HAS_PIL, "needs Pillow")
    def test_keyed_paints_the_hole_flat(self):
        from PIL import Image
        base = Image.new("RGB", (6, 6), (1, 2, 3))
        mask = Image.new("L", (6, 6), 0)
        mask.paste(255, (0, 0, 3, 3))
        out = segment.keyed(base, mask)
        self.assertEqual(out.getpixel((1, 1)), segment.INPAINT_KEY)
        self.assertEqual(out.getpixel((4, 4)), (1, 2, 3))


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

if __name__ == "__main__":
    unittest.main()
