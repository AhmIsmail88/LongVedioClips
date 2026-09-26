"""Shared helpers for the deterministic (stdlib-only) pipeline tests.

These tests deliberately avoid the two heavy dependencies that make the
full pipeline untestable offline:

* faster-whisper - instead we load already-saved `transcript.json`
  files, or synthesize transcripts directly.
* Ollama / the Qwen ranker - instead `stub_score_candidates` replaces
  `clip_selector.score_candidates` with a deterministic, content-blind
  scorer. It is NOT an attempt to imitate the LLM's judgment; it exists
  purely so stage 3 (candidate generation) and stage 5 (quality
  filtering / selection) can be exercised repeatably. Any conclusion
  drawn from it about *how many clips the selector can return* holds for
  a given set of scores, whatever produced those scores.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import Config, apply_band_plan, plan_clip_band  # noqa: E402
from core import candidate_generator, clip_selector, quality_filter  # noqa: E402
from models.schemas import CandidateSegment, ScoredClip, TranscriptSegment  # noqa: E402


class QuietLogger:
    """Logger-compatible recorder that never prints - keeps test output
    readable while still letting the modules under test call warn()/info().
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.warnings: list[str] = []

    def stage(self, index: int, message: str) -> None:
        self.lines.append(f"[{index}] {message}")

    def info(self, message: str) -> None:
        self.lines.append(message)

    def warn(self, message: str) -> None:
        self.lines.append(f"WARNING: {message}")
        self.warnings.append(message)

    def error(self, message: str) -> None:
        self.lines.append(f"ERROR: {message}")

    def timed(self, label: str):
        from contextlib import nullcontext

        return nullcontext()

    def summary(self) -> None:
        pass


def _stable_unit(*parts: float) -> float:
    """Deterministic pseudo-random value in [0, 1) derived from the inputs.

    Deliberately not `hash()` (randomized per process for strings) and not
    `random` (needs seeding and call-order discipline).
    """
    x = 0.0
    for i, p in enumerate(parts):
        x += (p + 1.0) * (i * 7.31 + 1.0)
    frac = (x * 0.61803398875) % 1.0
    return frac


def stub_score_candidates(
    candidates: list[CandidateSegment],
    config: Config,
    logger=None,
    batch_size: int | None = None,
    stats_out: dict | None = None,
) -> list[ScoredClip]:
    """Drop-in replacement for clip_selector.score_candidates.

    Gives every candidate a deterministic mid-to-high score (roughly the
    spread a real LLM produces: most clips pass a 40-point threshold, a
    few strong ones stand out) so the selection logic - not the scorer -
    is what determines the final clip count. Candidates are scored in
    chronological order, one entry per candidate, exactly like a
    perfectly successful set of LLM batches would.
    """
    if stats_out is not None:
        # Mirror the counters the real score_candidates fills in, for the
        # "all batches succeeded" case.
        size = batch_size or getattr(config, "llm_batch_size", 8)
        stats_out.setdefault("candidates_sent", 0)
        stats_out.setdefault("batches_attempted", 0)
        stats_out.setdefault("batches_failed", 0)
        stats_out.setdefault("batches_partial", 0)
        stats_out.setdefault("batches_retried", 0)
        stats_out.setdefault("sub_batches_attempted", 0)
        stats_out.setdefault("sub_batches_failed", 0)
        stats_out.setdefault("entries_dropped_malformed", 0)
        stats_out.setdefault("entries_dropped_unmatched", 0)
        stats_out.setdefault("llm_calls", 0)
        stats_out.setdefault("retries", 0)
        stats_out["candidates_sent"] += len(candidates)
        stats_out["batches_attempted"] += (len(candidates) + size - 1) // size

    scored: list[ScoredClip] = []
    for c in candidates:
        base = 52.0 + 40.0 * _stable_unit(c.start, c.end, len(c.text))
        hook = min(100.0, base + 5.0 * _stable_unit(c.start + 1.0, 3.0))
        content = min(100.0, base)
        story = min(100.0, base - 2.0)
        emotion = min(100.0, base + 2.0)
        standalone = min(100.0, base - 4.0)
        ending = min(100.0, base + 1.0)
        scored.append(
            ScoredClip(
                start=c.start,
                end=c.end,
                score=base,
                hook_score=hook,
                content_score=content,
                story_score=story,
                emotion_score=emotion,
                standalone_score=standalone,
                ending_score=ending,
                reason="stub",
                text=c.text,
                title="stub title",
            )
        )
    return scored


def load_transcript(path: str | os.PathLike) -> list[TranscriptSegment]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [TranscriptSegment.from_dict(d) for d in data]


def transcript_span(segments: list[TranscriptSegment]) -> float:
    return max((s.end for s in segments), default=0.0)


def make_synthetic_transcript(
    duration_seconds: float, segment_seconds: float = 4.0, text: str = "كلمة "
) -> list[TranscriptSegment]:
    """Build a synthetic transcript covering `duration_seconds` densely
    enough that candidate generation has plenty of windows to work with.
    """
    segments: list[TranscriptSegment] = []
    t = 0.0
    i = 0
    while t < duration_seconds - 1e-6:
        end = min(t + segment_seconds, duration_seconds)
        segments.append(
            TranscriptSegment(start=t, end=end, text=f"{text}{i}. ", words=[])
        )
        t = end
        i += 1
    return segments


def run_stages_3_and_5(
    segments: list[TranscriptSegment],
    config: Config,
    video_duration: float | None = None,
    logger=None,
    adapt_band: bool = True,
) -> dict:
    """Run candidate generation -> (stubbed) ranking -> quality filtering,
    returning per-stage counts.

    Mirrors what app.analyze() does between stages 3 and 5, minus ffmpeg
    and the LLM. With `adapt_band=True` (the default, and what the app
    now does) the effective band/stride come from config.plan_clip_band -
    pass False to reproduce the old, un-adapted behaviour.
    """
    logger = logger or QuietLogger()
    if video_duration is None:
        video_duration = transcript_span(segments)

    plan = plan_clip_band(
        video_duration,
        config.num_clips,
        config.min_duration,
        config.max_duration,
        config.max_overlap_ratio,
    )
    effective = apply_band_plan(config, plan) if adapt_band else config

    candidates = candidate_generator.generate_candidates(
        segments,
        window_sizes=effective.candidate_window_sizes,
        stride=effective.candidate_stride,
        min_duration=effective.min_duration,
        max_duration=effective.max_duration,
        max_candidates=effective.max_candidates,
    )
    scored = stub_score_candidates(candidates, effective, logger)
    scored = clip_selector.apply_weights(scored, effective)

    stats: dict = {}
    finals = quality_filter.select_final_clips(
        scored, effective, video_duration, segments, logger, stats_out=stats
    )
    return {
        "video_duration": video_duration,
        "plan": plan,
        "effective_config": effective,
        "candidates": len(candidates),
        "scored": len(scored),
        "final": len(finals),
        "requested": effective.num_clips,
        "final_clips": finals,
        "stats": stats,
    }
