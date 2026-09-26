"""
Tests for utils/audio_split.py.

`plan_parts` is pure and covered exhaustively; `split_audio` is exercised
against a *real* ffmpeg binary on a generated sine tone (no external
fixtures), following the same offline-but-real approach as
test_render_smoke.py / test_lighting_ffmpeg.py.

Temp dirs use tempfile.TemporaryDirectory so cleanup is automatic and no
manual deletion APIs are needed anywhere in this file.

Run:
    .venv\\Scripts\\python.exe tests\\test_audio_split.py
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils import audio_split  # noqa: E402
from utils.ffmpeg import get_ffmpeg_binaries, probe_duration  # noqa: E402
from utils.logger import PipelineError  # noqa: E402


class PlanPartsTests(unittest.TestCase):
    """The pure planner: window math and the guards, no ffmpeg."""

    def test_equal_shares_cover_the_whole_source(self):
        parts = audio_split.plan_parts(9.0, num_parts=3)
        self.assertEqual(len(parts), 3)
        self.assertAlmostEqual(parts[0][0], 0.0)
        self.assertAlmostEqual(parts[-1][1], 9.0)
        for start, end in parts:
            self.assertAlmostEqual(end - start, 3.0, places=6)
        for (_, end), (next_start, _) in zip(parts, parts[1:]):
            self.assertAlmostEqual(end, next_start, places=6)

    def test_uneven_division_still_covers_everything(self):
        parts = audio_split.plan_parts(10.0, num_parts=3)
        self.assertEqual(len(parts), 3)
        self.assertAlmostEqual(parts[0][1], 10.0 / 3, places=6)
        self.assertAlmostEqual(parts[-1][1], 10.0)
        for (_, end), (next_start, _) in zip(parts, parts[1:]):
            self.assertAlmostEqual(end, next_start, places=6)

    def test_single_part_is_the_whole_file(self):
        self.assertEqual(audio_split.plan_parts(12.0, num_parts=1), [(0.0, 12.0)])

    def test_duration_mode_splits_on_the_requested_length(self):
        self.assertEqual(
            audio_split.plan_parts(8.0, part_seconds=4.0),
            [(0.0, 4.0), (4.0, 8.0)],
        )

    def test_short_tail_is_merged_instead_of_shipped(self):
        # 9s in 4s chunks leaves a 1s tail; below the 3s floor it is folded
        # into the part before it instead of shipping a stub.
        parts = audio_split.plan_parts(9.0, part_seconds=4.0)
        self.assertEqual(len(parts), 2)
        self.assertAlmostEqual(parts[-1][0], 4.0)
        self.assertAlmostEqual(parts[-1][1], 9.0)

    def test_too_many_parts_is_rejected_with_the_feasible_ceiling(self):
        with self.assertRaises(ValueError) as ctx:
            audio_split.plan_parts(9.0, num_parts=10)
        self.assertIn("3", str(ctx.exception))

    def test_too_short_part_length_is_rejected(self):
        with self.assertRaises(ValueError):
            audio_split.plan_parts(60.0, part_seconds=1.0)

    def test_bad_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            audio_split.plan_parts(0.0, num_parts=2)
        with self.assertRaises(ValueError):
            audio_split.plan_parts(-5.0, num_parts=2)
        with self.assertRaises(ValueError):
            audio_split.plan_parts(10.0)
        with self.assertRaises(ValueError):
            audio_split.plan_parts(10.0, num_parts=2, part_seconds=5.0)
        with self.assertRaises(ValueError):
            audio_split.plan_parts(10.0, num_parts=0)
        with self.assertRaises(ValueError):
            audio_split.plan_parts("not-a-number", num_parts=2)


class SplitAudioTests(unittest.TestCase):
    """Real ffmpeg runs on a generated sine tone."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="audio_split_test_")
        cls.tmp = Path(cls._tmp.name)
        ffmpeg_bin, _ = get_ffmpeg_binaries()
        cls.source = cls.tmp / "tone.wav"
        result = subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=9",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(cls.source),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not cls.source.exists():
            raise unittest.SkipTest(
                f"ffmpeg could not build the sine fixture: {result.stderr[-300:]}"
            )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_parts_are_written_and_durations_add_up(self):
        parts = audio_split.split_audio(self.source, self.tmp / "even", num_parts=3)
        self.assertEqual(len(parts), 3)
        self.assertIn(parts[0].suffix, {".mp3", ".m4a"})
        durations = []
        for part in parts:
            self.assertTrue(part.exists())
            self.assertGreater(part.stat().st_size, 0)
            durations.append(probe_duration(str(part)))
        for duration in durations:
            self.assertAlmostEqual(duration, 3.0, delta=0.5)
        self.assertAlmostEqual(sum(durations), 9.0, delta=0.5)

    def test_part_count_matches_the_plan_in_duration_mode(self):
        parts = audio_split.split_audio(
            self.source, self.tmp / "by_seconds", part_seconds=4.0
        )
        expected = audio_split.plan_parts(
            probe_duration(str(self.source)), part_seconds=4.0
        )
        self.assertEqual(len(parts), len(expected))
        for part in parts:
            self.assertTrue(part.exists())
            self.assertGreater(part.stat().st_size, 0)

    def test_impossible_request_raises_a_clear_pipeline_error(self):
        with self.assertRaises(PipelineError) as ctx:
            audio_split.split_audio(self.source, self.tmp / "nope", num_parts=10)
        self.assertIn("minimum", str(ctx.exception))

    def test_unreadable_source_raises_a_pipeline_error(self):
        bad = self.tmp / "not_audio.mp3"
        bad.write_text("this is definitely not audio", encoding="utf-8")
        with self.assertRaises(PipelineError):
            audio_split.split_audio(bad, self.tmp / "bad", num_parts=2)

    def test_zip_holds_every_part(self):
        parts = audio_split.split_audio(self.source, self.tmp / "zipped", num_parts=2)
        data = audio_split.parts_zip_bytes(parts)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(
                sorted(archive.namelist()), sorted(part.name for part in parts)
            )
            self.assertIsNone(archive.testzip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
