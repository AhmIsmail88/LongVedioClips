"""
Step 5 (spec sections 12-15) - Quality Filtering, Boundary Cleanup,
Duplicate Prevention, and Final Selection.

This module never trusts LLM output blindly:
- Timestamps are clamped to the real video duration.
- Segments that look like filler/silence/incomplete ideas are rejected.
- Near-duplicate / heavily overlapping clips are de-duplicated.
- Boundaries may be nudged outward slightly to preserve context.
"""

from __future__ import annotations

import bisect

import re
from dataclasses import replace

from config import Config
from models.schemas import FinalClip, ScoredClip, TranscriptSegment
from utils.logger import Logger

_FILLER_WORDS = {
    "um", "uh", "like", "you know", "i mean", "so yeah",
    "اه", "امم", "يعني بس", "امم يعني",
}

_SENTENCE_END_RE = re.compile(r"[.!?؟…]\s*$")

# Arabic diacritics/tatweel are stripped before comparing text, so
# the same word written with or without them still matches.
_TASHKEEL_RE = re.compile("[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
_TOKEN_RE = re.compile(r"[0-9A-Za-z\u0600-\u06FF]+")


_SHINGLE_SIZE = 8


def _token_list(text: str) -> list[str]:
    """Ordered word tokens of `text`, punctuation- and diacritic-
    insensitive, single letters dropped."""
    cleaned = _TASHKEEL_RE.sub("", text or "")
    return [w for w in _TOKEN_RE.findall(cleaned.lower()) if len(w) > 1]


def _shingles(text: str, size: int = _SHINGLE_SIZE) -> set[tuple[str, ...]]:
    """Overlapping word sequences (8 words by default) of `text`."""
    tokens = _token_list(text)
    if not tokens:
        return set()
    if len(tokens) < size:
        return {tuple(tokens)}
    return {tuple(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


def _text_similarity(a: ScoredClip, b: ScoredClip) -> float:
    """How much of the shorter clip's *word sequences* also appear, in the
    same order, in the other clip (0.0 - 1.0).

    Sequences, not a bag of words, on purpose: two clips that merely talk
    about the same subject share vocabulary, and a set-overlap metric
    wrongly merges them, whereas genuinely repeated content shares long
    runs of the same consecutive words. One-directional, so a short clip
    fully contained in a long one scores 1.0 - exactly the "same content
    told again at a different timestamp" case this gate exists for.
    """
    sa, sb = _shingles(a.text), _shingles(b.text)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))

# Rejection buckets tracked so a run that produces fewer clips than
# requested can explain itself with counts instead of leaving the user to
# dig through per-clip log lines.
REJECT_KEYS = (
    "rejected_invalid_span",
    "rejected_too_short",
    "rejected_below_threshold",
    "rejected_filler",
)


def _looks_like_filler_only(text: str) -> bool:
    stripped = text.strip().lower()
    if not stripped:
        return True
    words = re.findall(r"\w+", stripped)
    if not words:
        return True
    filler_count = sum(1 for w in words if w in _FILLER_WORDS)
    return len(words) <= 3 and filler_count >= max(1, len(words) - 1)


def _ends_mid_idea(text: str) -> bool:
    """Heuristic: does the text end without terminal punctuation and look
    like it trails off? This is intentionally lenient since transcripts
    often omit punctuation - it only flags obviously broken endings.
    """
    stripped = text.strip()
    if not stripped:
        return True
    # If the transcript uses punctuation at all, require it at the end.
    has_any_punctuation = bool(re.search(r"[.!?؟]", stripped))
    if has_any_punctuation and not _SENTENCE_END_RE.search(stripped):
        return True
    return False


def clamp_to_video_duration(clip: ScoredClip, video_duration: float) -> ScoredClip:
    clip.start = max(0.0, min(clip.start, video_duration))
    clip.end = max(0.0, min(clip.end, video_duration))
    if clip.end < clip.start:
        clip.start, clip.end = clip.end, clip.start
    return clip


def reject_low_quality(
    clips: list[ScoredClip],
    config: Config,
    video_duration: float,
    logger: Logger,
    counts: dict | None = None,
) -> list[ScoredClip]:
    """Apply the hard quality rules from spec section 12.

    `counts`, if given, is filled with the number of clips dropped per
    reason (see REJECT_KEYS) so callers can report the whole picture.
    """
    counts = counts if counts is not None else {}
    for key in REJECT_KEYS:
        counts.setdefault(key, 0)

    kept: list[ScoredClip] = []
    for clip in clips:
        clamp_to_video_duration(clip, video_duration)

        if clip.duration <= 0 or clip.end <= clip.start:
            counts["rejected_invalid_span"] += 1
            logger.info(f"Rejecting clip {clip.start:.1f}-{clip.end:.1f}: no usable span")
            continue

        if clip.duration < 3.0:
            counts["rejected_too_short"] += 1
            logger.info(f"Rejecting clip {clip.start:.1f}-{clip.end:.1f}: too short")
            continue

        if clip.score < config.min_score_threshold:
            counts["rejected_below_threshold"] += 1
            logger.info(
                f"Rejecting clip {clip.start:.1f}-{clip.end:.1f}: "
                f"score {clip.score:.1f} below threshold"
            )
            continue

        if _looks_like_filler_only(clip.text):
            counts["rejected_filler"] += 1
            logger.info(
                f"Rejecting clip {clip.start:.1f}-{clip.end:.1f}: filler/empty content"
            )
            continue

        if _ends_mid_idea(clip.text):
            # Not a hard rejection on its own - many transcripts lack
            # punctuation - but it lowers confidence significantly.
            clip.score *= 0.85

        kept.append(clip)

    return kept


def _overlap_ratio(a: ScoredClip, b: ScoredClip) -> float:
    overlap_start = max(a.start, b.start)
    overlap_end = min(a.end, b.end)
    overlap = max(0.0, overlap_end - overlap_start)
    shorter = min(a.duration, b.duration)
    if shorter <= 0:
        return 0.0
    return overlap / shorter


def _duplicate_reason(
    clip: ScoredClip,
    kept: list[ScoredClip],
    max_overlap_ratio: float,
    max_text_similarity: float,
) -> str | None:
    """Why `clip` repeats content `kept` already covers, if it does.

    Two independent signals, because they catch different real cases:
    time overlap catches two clips cut from the same stretch of speech,
    and text containment catches the same content delivered again
    elsewhere in the video. Returns "time", "text", or None.
    """
    for other in kept:
        if _overlap_ratio(clip, other) > max_overlap_ratio:
            return "time"
        if (
            max_text_similarity < 1.0
            and _text_similarity(clip, other) >= max_text_similarity
        ):
            return "text"
    return None


def _greedy_keep(
    clips: list[ScoredClip],
    max_overlap_ratio: float,
    max_text_similarity: float = 1.0,
) -> tuple[list[ScoredClip], int, int]:
    """Score-ordered greedy: keep a clip only if it does not repeat
    content an already-kept, higher-scoring clip covers.

    Returns (kept, dropped_as_overlapping, dropped_as_text_duplicate).
    """
    kept: list[ScoredClip] = []
    dropped_time = 0
    dropped_text = 0
    for clip in sorted(clips, key=lambda c: c.score, reverse=True):
        reason = _duplicate_reason(
            clip, kept, max_overlap_ratio, max_text_similarity
        )
        if reason == "time":
            dropped_time += 1
            continue
        if reason == "text":
            dropped_text += 1
            continue
        kept.append(clip)
    return kept, dropped_time, dropped_text


def _fill_chronologically(
    clips: list[ScoredClip],
    kept: list[ScoredClip],
    max_overlap_ratio: float,
    target_count: int,
    max_text_similarity: float = 1.0,
) -> int:
    """Add clips the score-ordered pass dropped, in chronological order,
    until `target_count` is reached. Only clips that satisfy the same
    overlap rule against everything already kept can be added, so this
    never invents an overlapping set - it just stops one strong clip from
    monopolising a stretch of video.
    """
    if len(kept) >= target_count:
        return 0
    kept_ids = {id(c) for c in kept}
    added = 0
    for clip in sorted(clips, key=lambda c: c.start):
        if len(kept) >= target_count:
            break
        if id(clip) in kept_ids:
            continue
        if (
            _duplicate_reason(
                clip, kept, max_overlap_ratio, max_text_similarity
            )
            is None
        ):
            kept.append(clip)
            kept_ids.add(id(clip))
            added += 1
    return added


def _trim_to_max(clip: ScoredClip, max_duration: float) -> ScoredClip:
    """Copy of `clip` with its end pulled in so it is at most
    `max_duration` long (start is kept, since sentence starts are the
    natural clip boundaries).

    The stored text is shortened to roughly the same fraction of its
    words, so analysis.json and the GUI don't advertise speech that is no
    longer inside the clip. It is an approximation - ScoredClip no longer
    carries the per-segment indices - and only ever happens on a run that
    would otherwise produce fewer clips than requested. Burned captions
    are unaffected: they are built from clip.start/end against the real
    transcript.
    """
    if clip.duration <= max_duration:
        return clip
    keep_fraction = max_duration / clip.duration
    words = clip.text.split()
    if words:
        keep_words = max(1, int(round(len(words) * keep_fraction)))
        text = " ".join(words[:keep_words])
    else:
        text = clip.text
    return replace(clip, end=clip.start + max_duration, text=text)


def remove_overlaps(
    clips: list[ScoredClip],
    max_overlap_ratio: float,
    logger: Logger,
    target_count: int | None = None,
    counts: dict | None = None,
    max_clip_duration: float | None = None,
    max_text_similarity: float = 1.0,
) -> list[ScoredClip]:
    """Greedy dedup, then - if a target count was given and greedy fell
    short - two escalating attempts to reach it:

    1. a chronological fill pass over the clips greedy dropped (same
       overlap rule, so it can only add clips that genuinely fit);
    2. if that still isn't enough, the same greedy+fill over candidates
       whose length has been trimmed to `max_clip_duration`.

    Step 2 is what makes a short source work: a 15s candidate next to an
    11s band target forces a 10.5s stride instead of 7.7s, and N clips
    stop fitting even though there is physical room for them. Trimming
    only ever shortens a clip to the band max, and only when the run
    would otherwise under-produce - the caller logs when it happens.
    """
    counts = counts if counts is not None else {}
    counts.setdefault("dropped_as_overlapping", 0)
    counts.setdefault("rejected_text_duplicate", 0)
    counts.setdefault("overlap_fill_added", 0)
    counts.setdefault("duration_trimmed_clips", 0)

    kept, dropped, dropped_text = _greedy_keep(
        clips, max_overlap_ratio, max_text_similarity
    )
    counts["dropped_as_overlapping"] += dropped
    counts["rejected_text_duplicate"] += dropped_text
    for clip in _dropped_clips(clips, kept):
        logger.info(
            f"Dropping overlapping clip {clip.start:.1f}-{clip.end:.1f} "
            f"(overlaps a higher-scored clip)"
        )

    if target_count is not None:
        added = _fill_chronologically(
            clips, kept, max_overlap_ratio, target_count, max_text_similarity
        )
        counts["overlap_fill_added"] += added
        if added:
            logger.info(
                f"Filled {added} slot(s) with clips that fit the overlap rule "
                f"but lost the score-order contest."
            )

    if (
        target_count is not None
        and len(kept) < target_count
        and max_clip_duration
    ):
        trimmed = [_trim_to_max(c, max_clip_duration) for c in clips]
        n_trimmed = sum(1 for a, b in zip(clips, trimmed) if a is not b)
        retry, _, _ = _greedy_keep(trimmed, max_overlap_ratio, max_text_similarity)
        _fill_chronologically(
            trimmed, retry, max_overlap_ratio, target_count, max_text_similarity
        )
        if len(retry) > len(kept):
            logger.info(
                f"Only {len(kept)} clip(s) fit at their original lengths - "
                f"trimming {n_trimmed} candidate(s) to the {max_clip_duration:.1f}s "
                f"band max and retrying gets to {len(retry)}."
            )
            kept = retry
            counts["duration_trimmed_clips"] = n_trimmed

    return kept


def _dropped_clips(
    clips: list[ScoredClip], kept: list[ScoredClip]
) -> list[ScoredClip]:
    """The clips greedy passed over (for logging only)."""
    kept_ids = {id(c) for c in kept}
    return sorted(
        (c for c in clips if id(c) not in kept_ids), key=lambda c: c.start
    )


def expand_context(
    clip: ScoredClip,
    segments: list[TranscriptSegment],
    max_expand: float,
    max_duration: float,
) -> ScoredClip:
    """Nudge the clip's boundaries outward to the nearest sentence
    boundary if it improves comprehension without breaking the max
    duration or absorbing long silence.
    """
    if not segments:
        return clip

    # Expand start backward to the start of the segment that begins the
    # sentence, if that segment starts within `max_expand` seconds prior
    # and doesn't add excessive silence.
    for seg in reversed(segments):
        if seg.end <= clip.start and (clip.start - seg.end) <= max_expand:
            gap = clip.start - seg.end
            if gap <= 1.5:  # avoid absorbing dead air
                clip.start = seg.start
                break
        elif seg.end <= clip.start:
            continue
        else:
            break

    # Expand end forward similarly, capped so total duration doesn't blow
    # past max_duration by more than the allowed context window.
    hard_cap_end = clip.start + max_duration + max_expand
    for seg in segments:
        if seg.start >= clip.end and (seg.start - clip.end) <= max_expand:
            gap = seg.start - clip.end
            if gap <= 1.5 and seg.end <= hard_cap_end:
                clip.end = seg.end
                break
        elif seg.start >= clip.end:
            break

    return clip


def _word_points(segments) -> list[tuple[float, float]]:
    """All spoken word spans, sorted by start time.

    Used for exact word-edge snapping: unlike speech runs (which merge
    everything until a real pause), a word span is short, so a boundary
    can always be moved onto a word edge regardless of how long the
    speaker talks without pausing.
    """
    points = [
        (w.start, w.end)
        for seg in segments
        for w in (seg.words or [])
        if w.text and w.text.strip() and w.end > w.start
    ]
    points.sort()
    return points


def snap_to_word_edges(clip, words: list[tuple[float, float]], duration_cap: float | None = None) -> bool:
    """Guarantee the clip never cuts through a spoken word.

    A start inside a word moves back to that word's start; an end inside a
    word extends to that word's end, completing the word. Unlike
    snap_to_speech_boundary there is no distance limit, because a word is
    a fraction of a second by nature - being inside one is exactly the
    "cut mid-word" artifact, at any position. The resulting length change
    is negligible and always preferable to chopping a word in half.

    Returns True when either boundary moved.
    """
    if not words:
        return False

    starts = [w[0] for w in words]
    cap = duration_cap if duration_cap else words[-1][1]
    moved = False

    # Start: last word starting at or before clip.start contains it?
    k = bisect.bisect_right(starts, clip.start) - 1
    if k >= 0 and words[k][0] < clip.start < words[k][1]:
        clip.start = words[k][0]
        moved = True

    # End: same lookup against clip.end.
    j = bisect.bisect_right(starts, clip.end) - 1
    if j >= 0 and words[j][0] < clip.end < words[j][1]:
        new_end = min(words[j][1], cap)
        if new_end > clip.start:
            clip.end = new_end
            moved = True

    return moved


def _speech_runs(
    segments: list[TranscriptSegment], pause_threshold: float
) -> list[tuple[float, float]]:
    """Contiguous stretches of speech, split wherever the silence between
    two spoken moments reaches `pause_threshold`.

    Word timestamps are used when the transcript has them (faster-whisper
    emits them for every language), falling back to segment spans. No
    punctuation is involved anywhere: dialectal Arabic transcripts have
    none, which is exactly why the old punctuation-based boundary logic
    never fired on them.
    """
    points: list[tuple[float, float]] = []
    for seg in segments:
        words = [w for w in (seg.words or []) if w.text and w.text.strip()]
        if words:
            points.extend(
                (w.start, w.end) for w in words if w.end > w.start
            )
        elif seg.end > seg.start:
            points.append((seg.start, seg.end))
    if not points:
        return []

    points.sort()
    runs: list[list[float]] = [[points[0][0], points[0][1]]]
    for start, end in points[1:]:
        if start - runs[-1][1] < pause_threshold:
            runs[-1][1] = max(runs[-1][1], end)
        else:
            runs.append([start, end])
    return [(a, b) for a, b in runs]


def snap_to_speech_boundary(
    clip: ScoredClip,
    runs: list[tuple[float, float]],
    max_snap: float,
    max_duration: float,
    video_duration: float,
) -> bool:
    """Move a clip's boundaries onto speech pauses.

    A cut that lands inside a phrase is what makes a clip open or close
    mid-sentence, so each boundary is moved onto a pause the speaker
    actually took, as long as one is within `max_snap` seconds:

    - inside a spoken run: onto that run's own edge, preferring the edge
      that keeps more of the clip (backward for a start, forward for an
      end) so a fragment is completed rather than cut off;
    - in a silence gap: onto the nearest run edge, which trims dead air
      (leading silence for a start, trailing silence for an end);
    - already on a pause: left exactly where it is.

    The clip is never inverted and never pushed past `max_duration`
    (measured from the final start). Returns True when anything moved.
    """
    if not runs:
        return False

    starts = [run[0] for run in runs]
    ends = [run[1] for run in runs]

    def containing(value: float) -> tuple[float, float] | None:
        for run_start, run_end in runs:
            if run_start < value < run_end:
                return run_start, run_end
        return None

    def on_pause(value: float) -> bool:
        return any(abs(value - point) <= 1e-6 for point in starts + ends)

    def previous_end(value: float) -> float | None:
        earlier = [end for end in ends if end < value]
        return max(earlier) if earlier else None

    def next_start(value: float) -> float | None:
        later = [start for start in starts if start > value]
        return min(later) if later else None

    moved = False

    # The start prefers to move outward (earlier) so it can pick up the
    # beginning of the thought; a start sitting in a silence gap instead
    # moves to the next run, which trims the leading dead air.
    new_start = None
    if not on_pause(clip.start):
        run = containing(clip.start)
        if run is not None:
            run_start, run_end = run
            if 0.0 < clip.start - run_start <= max_snap:
                new_start = run_start
            elif 0.0 < run_end - clip.start <= max_snap:
                new_start = run_end
        else:
            upcoming = next_start(clip.start)
            previous = previous_end(clip.start)
            if upcoming is not None and upcoming - clip.start <= max_snap:
                new_start = upcoming
            elif previous is not None and clip.start - previous <= max_snap:
                new_start = previous
    if new_start is not None and new_start < clip.end:
        clip.start = new_start
        moved = True

    # The end mirrors the start: outward (later) inside a run, back to the
    # last spoken moment when it sits in trailing silence.
    hard_cap = min(video_duration, clip.start + max_duration)
    new_end = None
    if not on_pause(clip.end):
        run = containing(clip.end)
        if run is not None:
            run_start, run_end = run
            if 0.0 < run_end - clip.end <= max_snap:
                new_end = run_end
            elif 0.0 < clip.end - run_start <= max_snap:
                new_end = run_start
        else:
            previous = previous_end(clip.end)
            upcoming = next_start(clip.end)
            if previous is not None and clip.end - previous <= max_snap:
                new_end = previous
            elif upcoming is not None and upcoming - clip.end <= max_snap:
                new_end = upcoming
    if new_end is not None and clip.start < new_end <= hard_cap:
        clip.end = new_end
        moved = True

    return moved

def describe_shortfall(stats: dict) -> list[str]:
    """Build the one summary block that explains an under-producing run.

    `stats` may be the merged output of clip_selector.score_candidates()
    and select_final_clips() (see both functions' `stats_out`), so the
    report can name the LLM batches that failed as well as everything the
    quality filter threw away.
    """
    requested = int(stats.get("requested", 0) or 0)
    final = int(stats.get("final", 0) or 0)
    if requested <= 0 or final >= requested:
        return []

    lines = [
        f"Produced {final} of {requested} requested clip(s). Why:",
    ]

    batches_failed = int(stats.get("batches_failed", 0) or 0)
    batches_attempted = int(stats.get("batches_attempted", 0) or 0)
    if batches_attempted:
        scored_n = int(stats.get("candidates_scored", 0) or 0)
        sent_n = int(stats.get("candidates_sent", 0) or 0)
        detail = (
            f"  - LLM ranking: {batches_attempted - batches_failed}/{batches_attempted} "
            f"batch(es) produced scores"
        )
        if batches_failed:
            detail += f", {batches_failed} failed outright"
        detail += f" -> {scored_n} of {sent_n} candidates got a score"
        lines.append(detail)
        partial = int(stats.get("batches_partial", 0) or 0)
        sub_batches = int(stats.get("sub_batches_attempted", 0) or 0)
        if partial or sub_batches:
            detail = (
                f"  - {partial} batch(es) only succeeded after being retried as "
                f"halved sub-batches ({sub_batches} sub-batch(es) run"
            )
            failed_sub = int(stats.get("sub_batches_failed", 0) or 0)
            if failed_sub:
                detail += f", {failed_sub} of them still failed"
            detail += f", {int(stats.get('retries', 0) or 0)} extra LLM call(s))"
            lines.append(detail)
        dropped_unmatched = int(stats.get("entries_dropped_unmatched", 0) or 0)
        if dropped_unmatched:
            lines.append(
                f"  - {dropped_unmatched} LLM score(s) matched no candidate "
                f"timestamps and were dropped"
            )
        reanchored = int(stats.get("entries_matched_by_index", 0) or 0) + int(
            stats.get("entries_matched_by_position", 0) or 0
        )
        if reanchored:
            lines.append(
                f"  - {reanchored} scored entry(ies) came back with invented "
                f"timestamps and were re-anchored to the candidate they referred "
                f"to (index/order)"
            )
        if not scored_n:
            lines.append(
                "  - nothing was ranked at all, so there is nothing to select from"
            )

    below = int(stats.get("rejected_below_threshold", 0) or 0)
    if below:
        lines.append(
            f"  - {below} candidate(s) scored below the "
            f"{stats.get('min_score_threshold', 0):.0f}-point threshold"
        )
    filler = int(stats.get("rejected_filler", 0) or 0)
    if filler:
        lines.append(f"  - {filler} candidate(s) were filler/empty text")
    too_short = int(stats.get("rejected_too_short", 0) or 0)
    if too_short:
        lines.append(f"  - {too_short} candidate(s) were too short")
    invalid = int(stats.get("rejected_invalid_span", 0) or 0)
    if invalid:
        lines.append(f"  - {invalid} candidate(s) had no usable span")

    dropped = int(stats.get("dropped_as_overlapping", 0) or 0)
    if dropped:
        lines.append(
            f"  - {dropped} candidate(s) overlapped an already-kept clip by more "
            f"than {100 * float(stats.get('max_overlap_ratio', 0.0)):.0f}%"
        )
    text_dupes = int(stats.get("rejected_text_duplicate", 0) or 0)
    if text_dupes:
        lines.append(
            f"  - {text_dupes} candidate(s) repeated content an already-kept "
            f"clip already covered (same words, different timestamps)"
        )

    capacity = int(stats.get("max_feasible_clips", 0) or 0)
    if capacity and requested > capacity:
        lines.append(
            f"  - the source is {stats.get('video_duration', 0):.0f}s long and can "
            f"hold at most {capacity} non-overlapping clip(s) of "
            f"{stats.get('min_duration', 0):.0f}-{stats.get('max_duration', 0):.0f}s, "
            f"so {requested} was never physically possible"
        )

    lines.append(
        "  Fixes: raise --max-duration (or lower --clips), or lower "
        "--min-score-threshold if the ranking was the bottleneck."
    )
    return lines


def select_final_clips(
    scored_clips: list[ScoredClip],
    config: Config,
    video_duration: float,
    segments: list[TranscriptSegment],
    logger: Logger,
    stats_out: dict | None = None,
) -> list[FinalClip]:
    """Run the full selection pipeline: reject low quality -> expand
    context -> remove overlaps -> take top N.

    `stats_out`, if given, receives per-stage counts (including the
    reject/dedup breakdown) so a caller can report why fewer clips than
    requested came out - see describe_shortfall().
    """
    stats = stats_out if stats_out is not None else {}
    stats["requested"] = config.num_clips
    stats["video_duration"] = video_duration
    stats["min_duration"] = config.min_duration
    stats["max_duration"] = config.max_duration
    stats["max_overlap_ratio"] = config.max_overlap_ratio
    stats["min_score_threshold"] = config.min_score_threshold
    stats["candidates_scored"] = len(scored_clips)

    filtered = reject_low_quality(
        scored_clips, config, video_duration, logger, counts=stats
    )
    stats["after_quality_filter"] = len(filtered)

    # Boundary cleanup. Word-level snapping is used whenever the
    # transcript carries word timestamps, which is the normal case and
    # the only thing that works on unpunctuated transcripts; otherwise
    # the legacy punctuation/segment-based expansion is kept as-is.
    runs = (
        _speech_runs(segments, config.pause_threshold_seconds)
        if any(seg.words for seg in segments)
        else []
    )
    stats["speech_runs"] = len(runs)
    word_edges = _word_points(segments)
    stats["word_points"] = len(word_edges)
    word_snapped = 0
    snapped = 0
    for clip in filtered:
        # Exact first: never cut through a word (works on continuous
        # speech where no pause is near the boundary).
        if word_edges and snap_to_word_edges(clip, word_edges, video_duration):
            word_snapped += 1
        if runs:
            if snap_to_speech_boundary(
                clip,
                runs,
                config.boundary_snap_max_seconds,
                config.max_duration,
                video_duration,
            ):
                snapped += 1
        else:
            expand_context(
                clip, segments, config.context_expand_max_seconds, config.max_duration
            )
        clamp_to_video_duration(clip, video_duration)
    stats["boundary_snapped"] = snapped
    stats["boundary_word_snapped"] = word_snapped

    deduped = remove_overlaps(
        filtered,
        config.max_overlap_ratio,
        logger,
        target_count=config.num_clips,
        counts=stats,
        max_clip_duration=config.max_duration,
        max_text_similarity=config.max_text_similarity,
    )
    stats["after_dedup"] = len(deduped)
    deduped.sort(key=lambda c: c.score, reverse=True)

    top = deduped[: config.num_clips]
    stats["final"] = len(top)
    if len(top) < config.num_clips:
        # Detail-level warning; app.analyze() prints the consolidated
        # describe_shortfall() block once, with the LLM batch counts too.
        logger.warn(
            f"Only {len(top)} high-quality clip(s) found "
            f"(requested {config.num_clips}). "
            f"Returning fewer clips rather than padding with weak ones."
        )

    # Present final clips in chronological order for a more natural
    # output listing, even though selection was score-based.
    top.sort(key=lambda c: c.start)

    return [
        FinalClip(
            index=i + 1,
            start=c.start,
            end=c.end,
            score=c.score,
            reason=c.reason,
            text=c.text,
            title=c.title,
        )
        for i, c in enumerate(top)
    ]
