"""Tests for the two selection-quality fixes:

1. repeated-content rejection (tightened time-overlap rule + an ordered
   word-sequence similarity gate), and
2. boundary snapping onto the nearest speech pause using word timestamps,
   which is what works on unpunctuated transcripts.

These are the two defects a real 51-minute Arabic run exposed: two clips
sharing 17%/25% of their length (identical sentences exported twice) and
clips opening/closing mid-sentence.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config, plan_clip_band  # noqa: E402
from core import clip_selector  # noqa: E402
from core import quality_filter as qf  # noqa: E402
from models.schemas import ScoredClip, TranscriptSegment, Word  # noqa: E402


def clip(start, end, score, text, title=""):
    return ScoredClip(
        start=start,
        end=end,
        score=score,
        hook_score=0.0,
        content_score=0.0,
        story_score=0.0,
        emotion_score=0.0,
        standalone_score=0.0,
        ending_score=0.0,
        reason="",
        text=text,
        title=title,
    )


def words(text, start, per_word=0.4, gap=0.05):
    out = []
    t = start
    for token in text.split():
        out.append(Word(start=t, end=t + per_word, text=token))
        t += per_word + gap
    return out


def speech_segments():
    """Two spoken runs separated by a 1.5s pause, with word timestamps.

    Run A: 0.0 - 4.0    Run B: 5.5 - 10.0
    """
    a_text = "one two three four five six seven eight"
    b_text = "nine ten eleven twelve thirteen fourteen"
    return [
        TranscriptSegment(start=0.0, end=4.0, text=a_text, words=words(a_text, 0.0)),
        TranscriptSegment(start=5.5, end=10.0, text=b_text, words=words(b_text, 5.5)),
    ]


class DuplicateRejectionTests(unittest.TestCase):
    def test_overlapping_pair_keeps_only_the_better_one(self):
        """The real clip_01/clip_02 case: 17% of the shorter clip shared."""
        first = clip(509.0, 551.6, 88.2, "first clip distinct wording here")
        second = clip(544.5, 587.7, 88.2, "second clip different wording entirely")
        kept, dropped_time, dropped_text = qf._greedy_keep([first, second], 0.15, 0.5)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped_time, 1)
        self.assertEqual(dropped_text, 0)

    def test_a_lax_ratio_would_have_kept_both(self):
        first = clip(509.0, 551.6, 88.2, "first clip distinct wording here")
        second = clip(544.5, 587.7, 88.2, "second clip different wording entirely")
        kept, _, _ = qf._greedy_keep([first, second], 0.30, 1.0)
        self.assertEqual(len(kept), 2)

    def test_adjacent_non_overlapping_clips_are_both_kept(self):
        first = clip(0.0, 40.0, 80.0, "alpha beta gamma delta epsilon zeta")
        second = clip(45.0, 85.0, 70.0, "eta theta iota kappa lambda mu")
        kept, dropped_time, dropped_text = qf._greedy_keep([first, second], 0.15, 0.5)
        self.assertEqual(len(kept), 2)
        self.assertEqual((dropped_time, dropped_text), (0, 0))

    def test_same_content_retold_elsewhere_is_rejected_by_text(self):
        text = " ".join(f"tok{i}" for i in range(30))
        first = clip(0.0, 40.0, 90.0, text, "Same Title")
        second = clip(1800.0, 1840.0, 60.0, text, "Same Title")
        kept, dropped_time, dropped_text = qf._greedy_keep([first, second], 0.15, 0.5)
        self.assertEqual(len(kept), 1)
        self.assertEqual((dropped_time, dropped_text), (0, 1))

    def test_shared_vocabulary_in_a_different_order_is_not_a_duplicate(self):
        """A bag-of-words metric would wrongly merge these two."""
        vocab = [f"tok{i}" for i in range(20)]
        text_a = " ".join(vocab)
        text_b = " ".join(reversed(vocab))
        first = clip(0.0, 40.0, 90.0, text_a)
        second = clip(1800.0, 1840.0, 60.0, text_b)
        kept, _, _ = qf._greedy_keep([first, second], 0.15, 0.5)
        self.assertEqual(len(kept), 2)

    def test_empty_text_never_counts_as_a_duplicate(self):
        first = clip(0.0, 40.0, 90.0, "")
        second = clip(1800.0, 1840.0, 60.0, "")
        kept, _, _ = qf._greedy_keep([first, second], 0.15, 0.5)
        self.assertEqual(len(kept), 2)


class BoundarySnappingTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.runs = qf._speech_runs(
            speech_segments(), self.config.pause_threshold_seconds
        )

    def test_runs_split_on_the_pause(self):
        self.assertEqual(len(self.runs), 2)
        self.assertAlmostEqual(self.runs[0][0], 0.0, places=2)
        self.assertAlmostEqual(self.runs[1][0], 5.5, places=2)

    def test_start_snaps_back_to_the_speech_pause(self):
        c = clip(1.2, 8.0, 80.0, "text")
        moved = qf.snap_to_speech_boundary(
            c, self.runs, self.config.boundary_snap_max_seconds, 60.0, 600.0
        )
        self.assertTrue(moved)
        self.assertAlmostEqual(c.start, 0.0, places=2)

    def test_end_inside_a_run_snaps_forward_to_its_end(self):
        c = clip(5.5, 7.0, 80.0, "text")
        moved = qf.snap_to_speech_boundary(
            c, self.runs, self.config.boundary_snap_max_seconds, 60.0, 600.0
        )
        self.assertTrue(moved)
        self.assertAlmostEqual(c.end, 8.15, places=2)

    def test_end_in_a_silence_gap_trims_the_dead_air(self):
        c = clip(5.5, 9.4, 80.0, "text")
        moved = qf.snap_to_speech_boundary(
            c, self.runs, self.config.boundary_snap_max_seconds, 60.0, 600.0
        )
        self.assertTrue(moved)
        self.assertAlmostEqual(c.end, 8.15, places=2)

    def test_boundary_already_on_a_pause_is_left_alone(self):
        c = clip(5.5, 10.0, 80.0, "text")
        qf.snap_to_speech_boundary(c, self.runs, 2.5, 60.0, 600.0)
        self.assertAlmostEqual(c.start, 5.5, places=2)

    def test_snap_is_bounded_and_never_inverts_the_clip(self):
        c = clip(3.0, 3.4, 80.0, "text")
        qf.snap_to_speech_boundary(c, self.runs, 2.5, 60.0, 600.0)
        self.assertLess(c.start, c.end)

    def test_max_duration_is_respected(self):
        c = clip(1.0, 20.0, 80.0, "text")
        qf.snap_to_speech_boundary(c, self.runs, 2.5, 20.0, 600.0)
        self.assertLessEqual(c.end, c.start + 20.0 + 1e-6)

    def test_no_runs_means_no_change(self):
        c = clip(3.0, 8.0, 80.0, "text")
        self.assertFalse(qf.snap_to_speech_boundary(c, [], 2.5, 60.0, 600.0))
        self.assertEqual((c.start, c.end), (3.0, 8.0))


class UnpunctuatedTranscriptTests(unittest.TestCase):
    def test_no_punctuation_anywhere_still_produces_the_requested_count(self):
        """The real transcripts have no punctuation at all; the old
        punctuation-based logic never fired on them."""
        segments = [
            TranscriptSegment(start=float(i) * 6, end=float(i) * 6 + 5.5,
                              text=f"words without any punctuation number {i}")
            for i in range(20)
        ]
        for seg in segments:
            seg.words = words(seg.text, seg.start)
        scored = [
            clip(seg.start, min(seg.end, seg.start + 12.0), 70.0, seg.text)
            for seg in segments
        ]
        config = Config(num_clips=4)
        final = qf.select_final_clips(scored, config, 120.0, segments, QuietLogger())
        self.assertEqual(len(final), 4)
        self.assertTrue(all(f.end > f.start for f in final))

    def test_transcript_without_word_timestamps_falls_back(self):
        segments = [
            TranscriptSegment(start=float(i) * 6, end=float(i) * 6 + 5.0,
                              text=f"segment number {i}")
            for i in range(20)
        ]
        scored = [
            clip(seg.start, min(seg.end, seg.start + 10.0), 70.0, seg.text)
            for seg in segments
        ]
        config = Config(num_clips=3)
        final = qf.select_final_clips(scored, config, 120.0, segments, QuietLogger())
        self.assertEqual(len(final), 3)


class BandFollowsClipCountTests(unittest.TestCase):
    """The clip length must track how many clips were asked for."""

    def test_band_shrinks_to_the_share_per_clip(self):
        plan = plan_clip_band(3057.0, 30, 20.0, 110.0)
        self.assertAlmostEqual(plan.max_duration, 3057.0 / 30, places=1)
        self.assertTrue(plan.adapted)

    def test_band_untouched_when_the_user_asked_for_shorter_clips(self):
        plan = plan_clip_band(3057.0, 30, 20.0, 60.0)
        self.assertAlmostEqual(plan.max_duration, 60.0)
        self.assertFalse(plan.adapted)

    def test_band_untouched_for_a_handful_of_clips(self):
        plan = plan_clip_band(3057.0, 13, 20.0, 60.0)
        self.assertAlmostEqual(plan.max_duration, 60.0)
        self.assertFalse(plan.adapted)

    def test_band_never_goes_below_the_floor(self):
        plan = plan_clip_band(120.0, 40, 20.0, 60.0)
        self.assertGreaterEqual(plan.max_duration, 8.0)


class BatchSizeFollowsClipLengthTests(unittest.TestCase):
    """Long candidates need smaller LLM batches or the model chokes."""

    def test_short_clips_keep_the_configured_batch(self):
        self.assertEqual(clip_selector.llm_batch_size_for(Config(max_duration=60.0)), 16)

    def test_long_clips_shrink_the_batch(self):
        self.assertEqual(clip_selector.llm_batch_size_for(Config(max_duration=110.0)), 9)

    def test_batch_never_drops_below_the_floor(self):
        self.assertGreaterEqual(
            clip_selector.llm_batch_size_for(Config(max_duration=400.0)), 4
        )

    def test_a_user_lowered_batch_size_is_respected(self):
        self.assertEqual(
            clip_selector.llm_batch_size_for(Config(max_duration=20.0, llm_batch_size=6)), 6
        )


class EvenSplitTests(unittest.TestCase):
    """"Split the video into N parts": the clip length comes from the count."""

    def test_three_clips_split_the_video_evenly(self):
        plan = plan_clip_band(3057.0, 3, 20.0, 60.0, split_evenly=True)
        self.assertAlmostEqual(plan.max_duration, 3057.0 / 3, places=1)
        self.assertTrue(plan.adapted)

    def test_the_share_ignores_the_duration_sliders(self):
        # Without even-split the 60s slider would cap it at 60s.
        self.assertAlmostEqual(
            plan_clip_band(3057.0, 3, 20.0, 60.0).max_duration, 60.0
        )

    def test_thirty_clips_match_the_share_too(self):
        plan = plan_clip_band(3057.0, 30, 20.0, 110.0, split_evenly=True)
        self.assertAlmostEqual(plan.max_duration, 3057.0 / 30, places=1)

    def test_the_share_wins_over_min_duration_on_short_videos(self):
        plan = plan_clip_band(56.0, 5, 20.0, 60.0, split_evenly=True)
        self.assertAlmostEqual(plan.max_duration, 56.0 / 5, places=1)

    def test_the_split_is_capped_for_very_long_shares(self):
        plan = plan_clip_band(36000.0, 2, 20.0, 60.0, split_evenly=True)
        self.assertLessEqual(plan.max_duration, 1800.0)

    def test_even_split_stays_feasible(self):
        for num_clips in (2, 3, 13, 30):
            plan = plan_clip_band(3057.0, num_clips, 20.0, 60.0, split_evenly=True)
            self.assertGreaterEqual(plan.max_feasible_clips, num_clips)


class QuietLogger:
    def stage(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warn(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
