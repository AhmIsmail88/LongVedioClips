"""LLM batch robustness tests (Task A).

`clip_selector._call_ollama` is replaced with a deterministic fake, so
these tests exercise the real retry / halved-sub-batch / reporting logic
without Ollama. Three scenarios:

  * a batch that only fails at the full size is recovered by halving,
  * a batch that always fails is counted as failed (not silently dropped),
  * a response missing the 'clips' list is treated the same as a failure.

Run:
    python tests\\test_batch_retry.py
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import QuietLogger, REPO_ROOT, make_synthetic_transcript  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))

from config import Config  # noqa: E402
from core import candidate_generator, clip_selector, quality_filter  # noqa: E402

_SPAN_RE = re.compile(r"\[(\d+)\] start=([\d.]+) end=([\d.]+)")


def fake_ollama(fail_above: int | None = None, fail_always: bool = False,
                missing_clips: bool = False, empty_clips: bool = False):
    """Return a fake `_call_ollama` that answers like a well-behaved model,
    or fails on purpose. `fail_above=N` fails whenever the prompt contains
    more than N candidates - i.e. small sub-batches succeed.
    """
    calls: list[int] = []

    def fake(host, model, system, user, timeout):
        spans = [(float(s), float(e)) for _, s, e in _SPAN_RE.findall(user)]
        calls.append(len(spans))
        if fail_always or (fail_above is not None and len(spans) > fail_above):
            raise ValueError("simulated LLM timeout / bad JSON")
        if missing_clips:
            return json.dumps({"result": "no clips key here"})
        if empty_clips:
            return json.dumps({"clips": []})
        return json.dumps(
            {
                "clips": [
                    {
                        "start": s,
                        "end": e,
                        "score": 80,
                        "hook_score": 80,
                        "content_score": 80,
                        "story_score": 80,
                        "emotion_score": 80,
                        "standalone_score": 80,
                        "ending_score": 80,
                        "reason": "fake",
                        "title": "fake",
                    }
                    for s, e in spans
                ]
            }
        )

    fake.calls = calls
    return fake


class BatchRetryTests(unittest.TestCase):
    def setUp(self):
        self.segments = make_synthetic_transcript(600)
        self.config = Config(num_clips=8, llm_batch_size=8)
        self.candidates = candidate_generator.generate_candidates(
            self.segments,
            window_sizes=self.config.candidate_window_sizes,
            stride=self.config.candidate_stride,
            min_duration=self.config.min_duration,
            max_duration=self.config.max_duration,
            max_candidates=self.config.max_candidates,
        )
        self.assertTrue(self.candidates)
        self.original = clip_selector._call_ollama

    def tearDown(self):
        clip_selector._call_ollama = self.original

    def test_happy_path_scores_every_candidate_without_retries(self):
        clip_selector._call_ollama = fake_ollama()
        stats: dict = {}
        scored = clip_selector.score_candidates(
            self.candidates, self.config, QuietLogger(), stats_out=stats
        )
        self.assertEqual(len(scored), len(self.candidates))
        self.assertEqual(stats["batches_failed"], 0)
        self.assertEqual(stats["batches_partial"], 0)
        self.assertEqual(stats["sub_batches_attempted"], 0)
        self.assertEqual(stats["retries"], 0)

    def test_failed_batch_is_recovered_by_halving(self):
        # The whole batch (8 candidates) fails, but halves (4) succeed.
        clip_selector._call_ollama = fake_ollama(fail_above=4)
        logger = QuietLogger()
        stats: dict = {}
        scored = clip_selector.score_candidates(
            self.candidates, self.config, logger, stats_out=stats
        )
        print(
            f"    recovered: {len(scored)}/{len(self.candidates)} scored, "
            f"batches={stats['batches_attempted']} failed={stats['batches_failed']} "
            f"partial={stats['batches_partial']} "
            f"sub_batches={stats['sub_batches_attempted']} retries={stats['retries']}"
        )
        self.assertEqual(len(scored), len(self.candidates))
        self.assertEqual(stats["batches_failed"], 0)
        self.assertGreater(stats["batches_partial"], 0)
        self.assertEqual(
            stats["sub_batches_attempted"], 2 * stats["batches_partial"]
        )
        self.assertEqual(stats["sub_batches_failed"], 0)
        self.assertTrue(
            any("halves" in line for line in logger.warnings),
            "the halving retry should be logged, not silent",
        )

    def test_always_failing_batch_is_counted_and_reported(self):
        clip_selector._call_ollama = fake_ollama(fail_always=True)
        logger = QuietLogger()
        stats: dict = {}
        scored = clip_selector.score_candidates(
            self.candidates, self.config, logger, stats_out=stats
        )
        print(
            f"    all failed: scored={len(scored)} "
            f"batches={stats['batches_attempted']} failed={stats['batches_failed']} "
            f"sub_batches_failed={stats['sub_batches_failed']} "
            f"llm_calls={stats['llm_calls']}"
        )
        self.assertEqual(scored, [])
        self.assertEqual(stats["batches_failed"], stats["batches_attempted"])
        self.assertTrue(
            any("produced scores" in line or "incomplete" in line for line in logger.warnings),
        )
        # One retry round on the full batch plus one halved sub-batch round
        # - not an unbounded retry storm.
        self.assertEqual(
            stats["llm_calls"],
            stats["batches_attempted"] * (self.config.llm_max_retries + 1) * 3,
        )

    def test_missing_clips_key_is_treated_as_a_failure(self):
        clip_selector._call_ollama = fake_ollama(missing_clips=True)
        stats: dict = {}
        scored = clip_selector.score_candidates(
            self.candidates, self.config, QuietLogger(), stats_out=stats
        )
        self.assertEqual(scored, [])
        self.assertGreater(stats["batches_failed"], 0)

    def test_empty_clips_list_is_treated_as_a_failure(self):
        clip_selector._call_ollama = fake_ollama(empty_clips=True)
        stats: dict = {}
        scored = clip_selector.score_candidates(
            self.candidates, self.config, QuietLogger(), stats_out=stats
        )
        self.assertEqual(scored, [])
        self.assertGreater(stats["batches_failed"], 0)

    def test_tail_failure_does_not_collapse_the_clip_pool(self):
        """The reported long-video failure mode: if the *later* batches
        fail, only early-video candidates survive and they all overlap,
        collapsing the result to one clip. With halving, a transient
        failure on the tail no longer loses that part of the timeline.
        """
        stats: dict = {}
        clip_selector._call_ollama = fake_ollama(fail_above=4)
        scored = clip_selector.score_candidates(
            self.candidates, self.config, QuietLogger(), stats_out=stats
        )
        selected = quality_filter.select_final_clips(
            clip_selector.apply_weights(scored, self.config),
            self.config,
            600.0,
            self.segments,
            QuietLogger(),
            stats_out=stats,
        )
        print(
            f"    selected {len(selected)} clips; scored candidates span "
            f"{min(c.start for c in scored):.0f}s-{max(c.start for c in scored):.0f}s"
        )
        self.assertEqual(len(selected), self.config.num_clips)
        # Scoring must have covered the whole timeline, not just the start.
        self.assertGreater(max(c.start for c in scored), 600.0 * 0.9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
