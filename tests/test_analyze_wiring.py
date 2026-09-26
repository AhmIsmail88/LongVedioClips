"""End-to-end wiring tests for app.analyze() (Task A).

Stubs only the two things this machine cannot run - whisper transcription
and the Ollama ranker - and lets the *real* app.analyze() drive stages
1-5: duration probe, adaptive band, candidate generation, ranking,
quality filtering, analysis.json writing and the shortfall report.

Run:
    python tests\\test_analyze_wiring.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (  # noqa: E402
    QuietLogger,
    REPO_ROOT,
    load_transcript,
    stub_score_candidates,
)

sys.path.insert(0, str(REPO_ROOT))

import app  # noqa: E402
from config import Config  # noqa: E402

STORED_TRANSCRIPT = REPO_ROOT / "output" / "batch_test_1" / "transcript.json"


class AnalyzeWiringTests(unittest.TestCase):
    def setUp(self):
        if not STORED_TRANSCRIPT.exists():
            self.skipTest(f"{STORED_TRANSCRIPT} not available")
        self.segments = load_transcript(STORED_TRANSCRIPT)
        self.duration = max(s.end for s in self.segments)
        self.tmp = tempfile.TemporaryDirectory(prefix="lvc_wiring_")

        # Patch only the two unavailable stages, on the module objects the
        # real code looks them up on.
        self._orig = (
            app.transcriber.transcribe,
            app.clip_selector.score_candidates,
            app.probe_duration,
            app.check_ffmpeg_installed,
        )
        app.transcriber.transcribe = lambda **kwargs: self.segments
        app.clip_selector.score_candidates = (
            lambda candidates, config, logger, stats_out=None: stub_score_candidates(
                candidates, config, stats_out=stats_out
            )
        )
        app.probe_duration = lambda path: self.duration
        app.check_ffmpeg_installed = lambda: None

    def tearDown(self):
        (
            app.transcriber.transcribe,
            app.clip_selector.score_candidates,
            app.probe_duration,
            app.check_ffmpeg_installed,
        ) = self._orig
        self.tmp.cleanup()

    def _config(self, num_clips: int) -> Config:
        return Config(
            video_path=str(Path(self.tmp.name) / "fake_video.mp4"),
            output_dir=str(Path(self.tmp.name) / "out"),
            num_clips=num_clips,
            whisper_model="tiny",
        )

    def test_analyze_produces_the_requested_count_and_writes_analysis(self):
        config = self._config(5)
        logger = QuietLogger()
        stats: dict = {}
        finals, segments, duration = app.analyze(config, logger, stats_out=stats)

        self.assertEqual(len(finals), 5)
        self.assertAlmostEqual(duration, self.duration, places=3)
        self.assertEqual(len(segments), len(self.segments))
        self.assertEqual(stats["band_adapted"], True)
        self.assertEqual(stats["requested"], 5)

        analysis_path = config.analysis_path()
        self.assertTrue(analysis_path.exists())
        with open(analysis_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 5)
        # analysis.json's schema is unchanged.
        for entry in data:
            self.assertEqual(
                set(entry),
                {"index", "start", "end", "duration", "score", "reason", "text",
                 "title", "output_path"},
            )

        joined = "\n".join(logger.lines)
        self.assertIn("Shrinking the max clip length", joined)
        self.assertIn("Adjusted clip band", joined)
        self.assertFalse(
            any("Produced " in w and "requested clip(s)" in w for w in logger.warnings),
            "a successful run must not print a shortfall report",
        )
        print(f"    analyze: {len(finals)}/5 clips, band {stats['min_duration']:.1f}-"
              f"{stats['max_duration']:.1f}s")

    def test_analyze_reports_an_impossible_request(self):
        config = self._config(60)  # 60 clips out of a 56s video
        logger = QuietLogger()
        stats: dict = {}
        finals, _, _ = app.analyze(config, logger, stats_out=stats)

        self.assertLess(len(finals), 60)
        self.assertFalse(stats["band_adapted"] is False)
        warnings = "\n".join(logger.warnings)
        print(f"    impossible request -> {len(finals)} clip(s); warning:\n{warnings}")
        self.assertIn("cannot all be produced", warnings)
        self.assertIn("Produced", warnings)
        self.assertIn("never physically possible", warnings)

    def test_pipeline_error_when_nothing_can_be_scored(self):
        app.clip_selector.score_candidates = (
            lambda candidates, config, logger, stats_out=None: []
        )
        config = self._config(5)
        with self.assertRaises(app.PipelineError) as ctx:
            app.analyze(config, QuietLogger(), stats_out={})
        self.assertIn("did not return any usable scored clips", ctx.exception.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
