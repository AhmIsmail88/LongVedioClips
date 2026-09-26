"""
Step 3 - Candidate Generation.

Rather than asking the LLM to blindly search the whole transcript, we
generate candidate windows using sliding windows of transcript segments
at multiple sizes. This gives the LLM a manageable, pre-filtered set of
options to rank instead of the full transcript.

The exact windowing strategy is intentionally simple for the MVP and can
be replaced later (e.g. semantic segmentation) without touching any
other stage - it only needs to keep producing CandidateSegment objects.
"""

from __future__ import annotations

import re

from models.schemas import CandidateSegment, TranscriptSegment


def _join_text(segments: list[TranscriptSegment], indices: list[int]) -> str:
    return " ".join(segments[i].text for i in indices).strip()


def _local_priority(
    cand: CandidateSegment, min_duration: float, max_duration: float
) -> float:
    """Cheap, content-free quality heuristic used only to pick WHICH
    candidates reach the LLM when there are far too many of them: prefer
    durations near the middle of the preferred band, text that ends on a
    sentence boundary, and a healthy words-per-second density (low
    density = mostly silence/waffle).
    """
    target = (min_duration + max_duration) / 2.0
    duration = max(cand.duration, 1e-6)
    dur_score = 1.0 - min(1.0, abs(duration - target) / max(target, 1.0))
    text = cand.text.strip()
    ends_score = 1.0 if re.search(r"[.!?\u061f\u2026]\s*$", text) else 0.5
    words = len(text.split())
    density_score = min(1.0, (words / duration) / 2.5)
    return dur_score * 0.5 + ends_score * 0.25 + density_score * 0.25


def _cap_candidates(
    candidates: list[CandidateSegment],
    max_candidates: int,
    min_duration: float,
    max_duration: float,
) -> list[CandidateSegment]:
    """Keep at most `max_candidates` windows, spread across the whole
    timeline: split the video into that many time slots and keep the
    highest-priority window per slot (so no part of a long video gets
    starved just because its neighbours scored a little higher).
    """
    if max_candidates <= 0 or len(candidates) <= max_candidates:
        return candidates

    ranked = sorted(
        candidates,
        key=lambda c: (-_local_priority(c, min_duration, max_duration), c.start),
    )
    total_span = max((c.end for c in candidates), default=1.0) or 1.0
    buckets: list[CandidateSegment | None] = [None] * max_candidates
    for cand in ranked:
        idx = min(max_candidates - 1, int(cand.start / total_span * max_candidates))
        if buckets[idx] is None:
            buckets[idx] = cand

    picked = [c for c in buckets if c is not None]
    if len(picked) < max_candidates:
        chosen = {id(c) for c in picked}
        for cand in ranked:
            if len(picked) >= max_candidates:
                break
            if id(cand) not in chosen:
                picked.append(cand)
    picked.sort(key=lambda c: c.start)
    return picked


def generate_candidates(
    segments: list[TranscriptSegment],
    window_sizes: tuple[int, ...],
    stride: int,
    min_duration: float,
    max_duration: float,
    max_candidates: int = 600,
) -> list[CandidateSegment]:
    """Generate overlapping candidate windows over the transcript.

    For each window size (measured in number of sentences), slide across
    the transcript with the given stride, keeping only windows whose
    duration roughly fits [min_duration, max_duration] (with some
    tolerance, since duration is a preference, not a hard rule per spec).
    """
    if not segments:
        return []

    candidates: list[CandidateSegment] = []
    seen_spans: set[tuple[int, int]] = set()

    # Tolerance around the preferred duration band - content completeness
    # matters more than strict duration per spec section 9.
    soft_min = max(5.0, min_duration * 0.5)
    soft_max = max_duration * 1.5

    n = len(segments)
    for window_size in window_sizes:
        if window_size < 1:
            continue
        for start_idx in range(0, n, max(1, stride)):
            end_idx = start_idx + window_size - 1
            if end_idx >= n:
                break
            span = (start_idx, end_idx)
            if span in seen_spans:
                continue

            start_time = segments[start_idx].start
            end_time = segments[end_idx].end
            duration = end_time - start_time

            if duration < soft_min or duration > soft_max:
                continue

            seen_spans.add(span)
            indices = list(range(start_idx, end_idx + 1))
            candidates.append(
                CandidateSegment(
                    start=start_time,
                    end=end_time,
                    text=_join_text(segments, indices),
                    segment_indices=indices,
                )
            )

    # Also try to grow windows around natural stopping points (e.g. every
    # sentence as an anchor, strided like the window loop above) to catch
    # complete short ideas even if they don't align with a fixed size.
    for anchor in range(0, n, max(1, stride)):
        accumulated_indices: list[int] = []
        for end_idx in range(anchor, n):
            accumulated_indices.append(end_idx)
            start_time = segments[anchor].start
            end_time = segments[end_idx].end
            duration = end_time - start_time
            if duration > soft_max:
                break
            if duration < min_duration:
                continue
            span = (anchor, end_idx)
            if span in seen_spans:
                continue
            seen_spans.add(span)
            candidates.append(
                CandidateSegment(
                    start=start_time,
                    end=end_time,
                    text=_join_text(segments, accumulated_indices),
                    segment_indices=list(accumulated_indices),
                )
            )

    return _cap_candidates(candidates, max_candidates, min_duration, max_duration)
