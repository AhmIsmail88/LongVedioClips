"""Deterministic clip-count tests (Task A verification #1).

No ffmpeg, no Ollama, no whisper: stages 3 and 5 are exercised directly
against real candidate generation / quality filtering code with a
deterministic stub ranker (see tests/_harness.py).

Run:
    python tests\\test_selection.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (  # noqa: E402
    QuietLogger,
    REPO_ROOT,
    load_transcript,
    make_synthetic_transcript,
    run_stages_3_and_5,
    stub_score_candidates,
    transcript_span,
)

sys.path.insert(0, str(REPO_ROOT))

from config import (  # noqa: E402
    MIN_ADAPTIVE_CLIP_SECONDS,
    Config,
    apply_band_plan,
    max_nonoverlapping_clips,
    plan_clip_band,
)
from core import candidate_generator, quality_filter  # noqa: E402
from models.schemas import ScoredClip  # noqa: E402

STORED = {
    "batch_test_1 (56s)": REPO_ROOT / "output" / "batch_test_1" / "transcript.json",
    "batch_test_2 (60s)": REPO_ROOT / "output" / "batch_test_2" / "transcript.json",
}


class ShortSourceCollapseTests(unittest.TestCase):
    """The reported bug: ask for 5 clips from a ~1 minute video, get 1."""

    def _stored(self, key: str):
        path = STORED[key]
        if not path.exists():
            self.skipTest(f"{path} not available")
        return load_transcript(path)

    def test_old_band_demonstrably_causes_the_collapse(self):
        """With the requested 20-60s band and no adaptation, the stored
        short transcripts really do collapse (1-3 clips for 5 requested),
        and the overwhelming majority of candidates are dropped as
        overlapping - that is the mechanism, not a low score threshold.
        """
        for key in STORED:
            segs = self._stored(key)
            result = run_stages_3_and_5(segs, Config(num_clips=5), adapt_band=False)
            stats = result["stats"]
            with self.subTest(source=key):
                self.assertLess(
                    result["final"], 5,
                    f"{key} unexpectedly produced 5 clips without adaptation",
                )
                self.assertEqual(stats["rejected_below_threshold"], 0)
                self.assertGreater(
                    stats["dropped_as_overlapping"], 0.8 * result["scored"],
                    "the collapse should be dominated by overlap drops",
                )
                print(
                    f"    [old band] {key}: {result['candidates']} candidates -> "
                    f"{result['scored']} ranked -> {stats['after_quality_filter']} "
                    f"passed threshold -> {stats['dropped_as_overlapping']} dropped "
                    f"as overlapping -> {result['final']}/5 final"
                )

    def test_adaptive_band_honors_the_requested_count(self):
        """The same inputs with the adaptive band produce exactly the 5
        requested clips, all non-overlapping under the configured rule.
        """
        for key in STORED:
            segs = self._stored(key)
            result = run_stages_3_and_5(segs, Config(num_clips=5), adapt_band=True)
            plan = result["plan"]
            with self.subTest(source=key):
                self.assertLessEqual(
                    plan.max_duration, 60.0 + 1e-9, "band must never exceed the request"
                )
                self.assertLessEqual(plan.min_duration, 20.0 + 1e-9)
                self.assertGreaterEqual(plan.max_duration, MIN_ADAPTIVE_CLIP_SECONDS - 1e-9)
                self.assertEqual(result["final"], 5)
                self.assertEqual(len(result["final_clips"]), 5)
                # The final set must actually satisfy the overlap rule.
                for i, a in enumerate(result["final_clips"]):
                    for b in result["final_clips"][i + 1:]:
                        overlap = max(0.0, min(a.end, b.end) - max(a.start, b.start))
                        shorter = min(a.duration, b.duration)
                        self.assertLessEqual(
                            overlap / shorter, 0.3 + 1e-9,
                            f"{key}: final clips {a.index}/{b.index} overlap too much",
                        )
                print(
                    f"    [adaptive  ] {key}: band "
                    f"{plan.min_duration:.1f}-{plan.max_duration:.1f}s, "
                    f"{result['candidates']} candidates -> {result['final']}/5 final"
                )


class LongSourceTests(unittest.TestCase):
    """Adaptation must not touch sources that can already hold the clips."""

    def test_multi_minute_source_still_honors_the_request(self):
        for minutes, requested in ((20, 8), (60, 15)):
            segs = make_synthetic_transcript(minutes * 60)
            result = run_stages_3_and_5(
                segs, Config(num_clips=requested), adapt_band=True
            )
            with self.subTest(minutes=minutes):
                self.assertFalse(
                    result["plan"].adapted,
                    f"{minutes}min source should not need an adaptive band",
                )
                self.assertEqual(result["final"], requested)
                print(
                    f"    [{minutes}min] {result['candidates']} candidates -> "
                    f"{result['final']}/{requested} final "
                    f"(band {result['plan'].min_duration:.0f}-"
                    f"{result['plan'].max_duration:.0f}s, unadapted)"
                )

    def test_adapting_never_widens_the_users_band(self):
        for duration, num_clips in ((30, 3), (56, 5), (300, 10), (3000, 20)):
            plan = plan_clip_band(duration, num_clips, 20.0, 60.0, 0.3)
            with self.subTest(duration=duration, num_clips=num_clips):
                self.assertLessEqual(plan.max_duration, 60.0 + 1e-9)
                self.assertLessEqual(plan.min_duration, 20.0 + 1e-9)
                self.assertLessEqual(plan.min_duration, plan.max_duration + 1e-9)


class FeasibilityGuardTests(unittest.TestCase):
    """A request the source physically cannot satisfy must be reported as
    such - not silently under-produced."""

    def test_infeasible_request_reports_the_limit(self):
        # 30s of source, 20 clips requested: even at the 8s floor only 3
        # non-overlapping clips fit, so 20 is impossible.
        segments = make_synthetic_transcript(30)
        config = Config(num_clips=20)
        plan = plan_clip_band(30.0, 20, config.min_duration, config.max_duration, 0.3)
        self.assertFalse(plan.feasible)
        self.assertLess(plan.max_feasible_clips, 20)
        self.assertLess(plan.max_duration, 60.0)

        result = run_stages_3_and_5(segments, config, video_duration=30.0)
        stats = result["stats"]
        stats["batches_attempted"] = 4
        stats["batches_failed"] = 2
        stats["candidates_sent"] = 64
        stats["max_feasible_clips"] = plan.max_feasible_clips

        report = "\n".join(quality_filter.describe_shortfall(stats))
        print(f"    infeasible report:\n{report}")
        self.assertLess(result["final"], 20)
        self.assertIn("never physically possible", report)
        self.assertIn(f"at most {plan.max_feasible_clips}", report)

    def test_feasible_request_has_no_shortfall_report(self):
        segments = make_synthetic_transcript(600)
        result = run_stages_3_and_5(segments, Config(num_clips=10))
        self.assertEqual(result["final"], 10)
        self.assertEqual(quality_filter.describe_shortfall(result["stats"]), [])

    def test_capacity_formula_matches_the_overlap_rule(self):
        # A 60s clip in a 60s source: exactly one. Two 30s clips in 60s:
        # stride 21s, so 2 fit.
        self.assertEqual(max_nonoverlapping_clips(60, 60, 0.3), 1)
        self.assertEqual(max_nonoverlapping_clips(60, 30, 0.3), 2)
        self.assertEqual(max_nonoverlapping_clips(30, 60, 0.3), 0)


class ShortfallReportTests(unittest.TestCase):
    """The consolidated report must name the reasons with counts."""

    def test_report_names_llm_batch_failures_and_threshold_drops(self):
        segments = make_synthetic_transcript(600)
        config = Config(num_clips=10)
        candidates = candidate_generator.generate_candidates(
            segments,
            window_sizes=config.candidate_window_sizes,
            stride=config.candidate_stride,
            min_duration=config.min_duration,
            max_duration=config.max_duration,
            max_candidates=config.max_candidates,
        )
        scored = stub_score_candidates(candidates, config)
        # Simulate a run where most of the LLM batches failed and only a
        # handful of (chronologically early) candidates got scored.
        kept = scored[:6]
        for clip in kept:
            clip.score = 90.0
        stats: dict = {
            "batches_attempted": 20,
            "batches_failed": 17,
            "batches_partial": 1,
            "sub_batches_attempted": 4,
            "retries": 23,
            "candidates_sent": 600,
        }
        finals = quality_filter.select_final_clips(
            kept, config, 600.0, segments, QuietLogger(), stats_out=stats
        )
        report = "\n".join(quality_filter.describe_shortfall(stats))
        print(f"    LLM-failure report:\n{report}")
        self.assertIn("produced scores", report)
        self.assertIn("failed outright", report)
        self.assertIn("got a score", report)
        self.assertLessEqual(len(finals), 6)
        self.assertTrue(report.startswith("Produced"))

    def test_report_is_empty_when_the_request_was_met(self):
        stats = {"requested": 5, "final": 5}
        self.assertEqual(quality_filter.describe_shortfall(stats), [])

    def test_overlap_count_appears_in_the_report(self):
        stats = {
            "requested": 5,
            "final": 2,
            "candidates_scored": 40,
            "dropped_as_overlapping": 30,
            "max_overlap_ratio": 0.3,
        }
        report = "\n".join(quality_filter.describe_shortfall(stats))
        self.assertIn("30 candidate(s) overlapped", report)
        self.assertIn("30%", report)


class AdaptiveConfigTests(unittest.TestCase):
    def test_apply_band_plan_keeps_other_settings(self):
        config = Config(num_clips=5, fix_lighting=True, lighting_strength=0.5)
        plan = plan_clip_band(56.0, 5, config.min_duration, config.max_duration, 0.3)
        effective = apply_band_plan(config, plan)
        self.assertAlmostEqual(effective.max_duration, plan.max_duration)
        self.assertEqual(effective.candidate_stride, 1)
        self.assertTrue(effective.fix_lighting)
        self.assertEqual(effective.lighting_strength, 0.5)
        # The original config is untouched (the user's request stays on record).
        self.assertEqual(config.max_duration, 60.0)
        self.assertEqual(config.candidate_stride, 2)

    def test_no_adaptation_leaves_config_identical(self):
        config = Config(num_clips=10)
        plan = plan_clip_band(600.0, 10, config.min_duration, config.max_duration, 0.3)
        self.assertIs(apply_band_plan(config, plan), config)


class StubRankerSanityTests(unittest.TestCase):
    def test_stub_is_deterministic(self):
        segments = make_synthetic_transcript(120)
        config = Config(num_clips=3)
        candidates = candidate_generator.generate_candidates(
            segments, config.candidate_window_sizes, config.candidate_stride,
            config.min_duration, config.max_duration, config.max_candidates,
        )
        first = stub_score_candidates(candidates, config)
        second = stub_score_candidates(candidates, config)
        self.assertEqual(
            [(c.start, c.end, c.score) for c in first],
            [(c.start, c.end, c.score) for c in second],
        )
        self.assertTrue(all(c.score > 40 for c in first))


if __name__ == "__main__":
    unittest.main(verbosity=2)
