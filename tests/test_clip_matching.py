"""Layered LLM-entry -> candidate matching tests (Task B).

`clip_selector._call_ollama` is replaced with a deterministic fake, so the
real matching code runs offline: no Ollama, no whisper, no ffmpeg.

The failure being covered is the one captured from a live 51-minute run:
qwen3:8b answered a 16-candidate ranking batch with invented timestamps
(0.0-100.0, 100.0-200.0, ...) instead of echoing the spans it was given.
The old matcher only knew how to match by span, so it dropped all 16
entries - a whole batch of usable scores - every time.

Run:
    python tests\\test_clip_matching.py
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import QuietLogger, REPO_ROOT  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))

from config import Config  # noqa: E402
from core import clip_selector, quality_filter  # noqa: E402
from models.schemas import CandidateSegment  # noqa: E402

N = 16
_SPAN_RE = re.compile(r"\[(\d+)\] start=([\d.]+) end=([\d.]+)")


def make_candidates(n: int = N) -> list[CandidateSegment]:
    """Candidates with irregular spans, like real transcript windows - the
    invented 0-100/100-200 payload must not accidentally coincide with a
    real candidate span (or land within the 5s tolerance of one).
    """
    out = []
    for i in range(n):
        start = 10.0 + i * 137.5
        out.append(
            CandidateSegment(
                start=start,
                end=start + 41.25,
                text=f"text of candidate {i}",
            )
        )
    return out


def _make_entry(i: int, start: float, end: float, score: float) -> dict:
    return {
        "i": i,
        "start": start,
        "end": end,
        "score": score,
        "hook_score": score,
        "content_score": score,
        "story_score": score,
        "emotion_score": score,
        "standalone_score": score,
        "ending_score": score,
        "reason": f"reason for {i}",
        "title": f"title for {i}",
    }


def fake_ollama(mode: str):
    """Return a fake `_call_ollama` that answers with the entry layout the
    named scenario needs. The candidate spans are read out of the real
    user prompt, so the fake stays faithful to what the model was sent.
    """

    def fake(host, model, system, user, timeout):
        spans = [(int(i), float(s), float(e)) for i, s, e in _SPAN_RE.findall(user)]
        half = len(spans) // 2
        entries = []
        for pos, (label, s, e) in enumerate(spans):
            if mode == "exact":
                start, end = s, e
            elif mode == "partial" and pos < half:
                start, end = s, e
            else:
                # Invented timestamps, exactly the shape seen live:
                # 0-100, 100-200, ... one per entry.
                start, end = pos * 100.0, (pos + 1) * 100.0
            entry = _make_entry(label, start, end, 70.0 + label)
            if mode in ("invented_unindexed", "invented_short_unindexed",
                        "captured_live"):
                entry.pop("i")
            entries.append(entry)
        if mode == "invented_short_unindexed":
            entries = entries[:-1]
        if mode == "invented_short_indexed":
            entries = entries[:-1]
        return json.dumps({"clips": entries})

    return fake


class LayeredMatchingTests(unittest.TestCase):
    def setUp(self):
        self.candidates = make_candidates()
        self.config = Config(llm_batch_size=N, llm_max_retries=0)
        self.logger = QuietLogger()
        self.stats: dict = {}
        self.original = clip_selector._call_ollama

    def tearDown(self):
        clip_selector._call_ollama = self.original

    def _run(self, mode: str):
        clip_selector._call_ollama = fake_ollama(mode)
        self.stats = {}
        return clip_selector.score_candidates(
            self.candidates, self.config, self.logger, stats_out=self.stats
        )

    def test_all_timestamps_correct_match_by_timestamp_only(self):
        scored = self._run("exact")
        self.assertEqual(len(scored), N)
        self.assertEqual(self.stats["entries_matched_by_timestamp"], N)
        self.assertEqual(self.stats["entries_matched_by_index"], 0)
        self.assertEqual(self.stats["entries_matched_by_position"], 0)
        self.assertEqual(self.stats["entries_dropped"], 0)
        self.assertFalse(
            any("Re-anchored" in w for w in self.logger.warnings),
            "a fully timestamp-matched batch must not be reported as re-anchored",
        )
        for clip, cand in zip(scored, self.candidates):
            self.assertEqual((clip.start, clip.end), (cand.start, cand.end))

    def test_invented_timestamps_recovered_by_index(self):
        scored = self._run("invented_indexed")
        self.assertEqual(len(scored), N)
        self.assertEqual(self.stats["entries_matched_by_timestamp"], 0)
        self.assertEqual(self.stats["entries_matched_by_index"], N)
        self.assertEqual(self.stats["entries_matched_by_position"], 0)
        self.assertEqual(self.stats["entries_dropped"], 0)
        for k, (clip, cand) in enumerate(zip(scored, self.candidates)):
            # The real candidate span and text win ...
            self.assertEqual((clip.start, clip.end), (cand.start, cand.end))
            self.assertEqual(clip.text, cand.text)
            # ... while the model's score, sub-scores, reason and title stay.
            self.assertEqual(clip.score, 70.0 + k)
            self.assertEqual(clip.hook_score, 70.0 + k)
            self.assertEqual(clip.reason, f"reason for {k}")
            self.assertEqual(clip.title, f"title for {k}")
        self.assertTrue(
            any("Re-anchored 16 of 16" in w and "16 by index, 0 by order" in w
                and "timestamps were unusable." in w
                for w in self.logger.warnings),
            f"expected a single re-anchor summary line, got {self.logger.warnings}",
        )
        # ... plus exactly one totals line once ranking finishes.
        totals = [
            line for line in self.logger.lines if "batch(es) total" in line
        ]
        self.assertEqual(len(totals), 1, totals)
        self.assertIn(
            "Re-anchored 16 scored entry(ies) in 1 batch(es) total "
            "(16 by index, 0 by order)",
            totals[0],
        )

    def test_invented_timestamps_without_index_recovered_by_order(self):
        scored = self._run("invented_unindexed")
        self.assertEqual(len(scored), N)
        self.assertEqual(self.stats["entries_matched_by_timestamp"], 0)
        self.assertEqual(self.stats["entries_matched_by_index"], 0)
        self.assertEqual(self.stats["entries_matched_by_position"], N)
        self.assertEqual(self.stats["entries_dropped"], 0)
        for k, (clip, cand) in enumerate(zip(scored, self.candidates)):
            self.assertEqual((clip.start, clip.end), (cand.start, cand.end))
            self.assertEqual(clip.text, cand.text)
            self.assertEqual(clip.score, 70.0 + k)
        self.assertTrue(
            any("0 by index, 16 by order" in w for w in self.logger.warnings),
        )

    def test_wrong_entry_count_without_index_is_still_dropped(self):
        scored = self._run("invented_short_unindexed")
        self.assertEqual(scored, [], "a wrong-length, unindexed batch must not be guessed")
        self.assertEqual(self.stats["entries_matched_by_index"], 0)
        self.assertEqual(self.stats["entries_matched_by_position"], 0)
        self.assertGreater(self.stats["entries_dropped"], 0)
        self.assertGreater(self.stats["entries_dropped_unmatched"], 0)
        self.assertEqual(self.stats["entries_matched_by_timestamp"], 0)

    def test_wrong_entry_count_with_index_still_recovered_by_index(self):
        # The count guard only applies to positional matching: an entry
        # that names its candidate is trustworthy however many there are.
        scored = self._run("invented_short_indexed")
        self.assertEqual(len(scored), N - 1)
        self.assertEqual(self.stats["entries_matched_by_index"], N - 1)
        self.assertEqual(self.stats["entries_matched_by_position"], 0)
        self.assertEqual(self.stats["entries_matched_by_timestamp"], 0)
        for k, (clip, cand) in enumerate(zip(scored, self.candidates)):
            self.assertEqual((clip.start, clip.end), (cand.start, cand.end))

    def test_partial_batch_timestamp_matches_win_and_only_leftovers_reanchor(self):
        scored = self._run("partial")
        half = N // 2
        self.assertEqual(len(scored), N)
        self.assertEqual(self.stats["entries_matched_by_timestamp"], half)
        self.assertEqual(self.stats["entries_matched_by_index"], half)
        self.assertEqual(self.stats["entries_matched_by_position"], 0)
        self.assertEqual(self.stats["entries_dropped"], 0)
        for clip, cand in zip(scored, self.candidates):
            self.assertEqual((clip.start, clip.end), (cand.start, cand.end))
        self.assertTrue(
            any("Re-anchored 8 of 16" in w and "8 by index, 0 by order" in w
                for w in self.logger.warnings),
        )

    def test_captured_live_payload_is_fully_recovered(self):
        """The exact captured shape: 16 entries, timestamps 0-100/100-200/
        ... with no index at all. The old span-only matcher dropped all 16;
        the positional fallback now recovers all 16.
        """
        scored = self._run("captured_live")
        self.assertEqual(len(scored), N)
        self.assertEqual(self.stats["entries_matched_by_timestamp"], 0)
        self.assertEqual(self.stats["entries_matched_by_position"], N)
        self.assertEqual(self.stats["entries_dropped"], 0)
        for clip, cand in zip(scored, self.candidates):
            self.assertEqual((clip.start, clip.end), (cand.start, cand.end))
            self.assertEqual(clip.text, cand.text)


class ShortfallReportTests(unittest.TestCase):
    def test_report_names_reanchored_entries(self):
        stats = {
            "requested": 5,
            "final": 2,
            "batches_attempted": 4,
            "batches_failed": 0,
            "candidates_scored": 64,
            "candidates_sent": 64,
            "entries_matched_by_index": 12,
            "entries_matched_by_position": 2,
        }
        report = "\n".join(quality_filter.describe_shortfall(stats))
        print(f"    re-anchor report:\n{report}")
        self.assertIn(
            "14 scored entry(ies) came back with invented timestamps "
            "and were re-anchored",
            report,
        )
        self.assertIn("index/order", report)

    def test_report_has_no_reanchor_bullet_without_reanchoring(self):
        stats = {"batches_attempted": 4, "candidates_scored": 40,
                 "candidates_sent": 64}
        report = "\n".join(quality_filter.describe_shortfall(
            dict(stats, requested=5, final=2)
        ))
        self.assertNotIn("re-anchored", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
