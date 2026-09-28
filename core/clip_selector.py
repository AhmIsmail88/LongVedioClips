"""
Step 4 - LLM Selection and Ranking.

Uses a local LLM through Ollama (default: qwen3.5:9b) to *evaluate*
candidate segments across multiple weighted dimensions, rather than
just asking it to output timestamps directly. The LLM's JSON output is
validated before anything downstream trusts it, and its timestamps are
never trusted blindly - clip_processor/quality_filter re-validate
against the real video duration.
"""

from __future__ import annotations

import json
import re
import time

from pathlib import Path

from config import Config
from models.schemas import CandidateSegment, ScoredClip
from utils.logger import Logger, PipelineError

SYSTEM_PROMPT = """You are an expert short-form video editor. You will be given \
transcript candidate segments from a long video (podcast, interview, lecture, \
or talk). For EACH candidate, score how well it would work as a standalone \
vertical short-form clip (TikTok / Reels / Shorts).

Score each of these from 0-100:
- hook_score: how strong is the opening line/moment at grabbing attention?
- content_score: how valuable/interesting/informative is the content?
- story_score: how good is it as a story or narrative arc?
- emotion_score: how much emotional impact does it have?
- standalone_score: can a viewer understand it WITHOUT the rest of the video?
- ending_score: does it end on a satisfying, complete note (not mid-sentence)?

Also provide:
- score: overall score 0-100 (your own holistic judgment)
- reason: max 8 words explaining the score
- title: a short, catchy on-screen title (max 6 words) for this clip, written \
in the SAME language as the clip's text - the kind of punchy hook text you'd \
see overlaid at the top of a viral short-form clip.

Include "i" on every entry: copy the candidate's [i] number from the list so \
each entry can be traced back to the candidate it scores.

Respond with ONLY valid JSON in this exact structure, no other text, no markdown \
code fences:
{"clips": [{"i": <the [i] number from the candidate list>, "start": <number>, \
"end": <number>, "score": <number>, "hook_score": <number>, \
"content_score": <number>, "story_score": <number>, "emotion_score": <number>, \
"standalone_score": <number>, "ending_score": <number>, \
"reason": "<short string>", "title": "<short catchy title>"}]}
"""


# How much candidate transcript one LLM request should carry, and how
# much text a single candidate contributes to it. Both matter:
#  * a batch of 16 is fine when each candidate is about a minute long,
#    but with much longer clips the same batch hands a small local model
#    several times the text and it starts answering with unusable JSON
#    (observed: a real 51-minute run with a 20-110s band failed every
#    batch and had to retry and halve each one);
#  * an unbounded candidate makes the request enormous no matter how the
#    batch is sized - a real 6-clip run (509s per clip) sent ~500s of
#    Arabic per candidate and reported a 13-hour ETA at 4 per request.
# Ranking only needs enough of a candidate to judge it, so the text is
# bounded (head and tail, the parts that decide the hook and the ending)
# and the batch is sized from the payload that actually gets sent.
_MAX_CANDIDATE_CHARS = 1200
_CANDIDATE_HEAD_CHARS = 850
# Slightly under the previous 16000 so the model always has room to
# finish its JSON answer inside the context window (Arabic runs ~2.5
# chars/token; overflow showed up as truncated batches scoring 1/13).
_BATCH_TARGET_CHARS = 14000
# How many times a failing batch may be halved: 13 -> 6/7 -> 3/4.
_MAX_SPLIT_DEPTH = 2
_BATCH_TARGET_SECONDS = 1000.0
_MIN_BATCH_SIZE = 4


def _candidate_text_for_prompt(text: str) -> str:
    """Bound one candidate's text, keeping its head and tail."""
    text = (text or "").strip()
    if len(text) <= _MAX_CANDIDATE_CHARS:
        return text
    tail_chars = _MAX_CANDIDATE_CHARS - _CANDIDATE_HEAD_CHARS
    return (
        text[:_CANDIDATE_HEAD_CHARS]
        + " [...] "
        + text[-tail_chars:]
    )


def llm_batch_size_for(config, candidates=None) -> int:
    """Candidates per LLM request, sized from the text actually sent.

    When the candidates are known, the batch is derived from their
    (bounded) average length, which is what really drives prompt size and
    therefore model latency. Falls back to the clip-length heuristic when
    no candidates are given. Never above the configured `llm_batch_size`
    (so a user who lowered it stays lowered) and never below
    `_MIN_BATCH_SIZE`.
    """
    configured = int(getattr(config, "llm_batch_size", 16) or 16)

    if candidates:
        lengths = [len(_candidate_text_for_prompt(c.text or "")) for c in candidates]
        average = sum(lengths) / len(lengths)
        if average > 0:
            scaled = int(round(_BATCH_TARGET_CHARS / average))
            return max(_MIN_BATCH_SIZE, min(configured, scaled))

    max_duration = float(getattr(config, "max_duration", 60.0) or 60.0)
    scaled = int(round(_BATCH_TARGET_SECONDS / max(1.0, max_duration)))
    return max(_MIN_BATCH_SIZE, min(configured, scaled))


def _build_user_prompt(candidates: list[CandidateSegment]) -> str:
    lines = ["Candidates:\n"]
    for i, c in enumerate(candidates):
        lines.append(
            f"[{i}] start={c.start:.2f} end={c.end:.2f}\n"
            f"text: {_candidate_text_for_prompt(c.text)}\n"
        )
    lines.append(
        "\nReturn one JSON object per candidate above, using the SAME start/end "
        "values given. Do not invent new timestamps."
    )
    return "\n".join(lines)


def _extract_json(raw_text: str) -> dict:
    """Best-effort extraction of a JSON object from an LLM response, in case
    the model wraps it in markdown fences or adds stray text.
    """
    text = raw_text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError("No JSON object found in LLM response.")


def _call_ollama(
    host: str,
    model: str,
    system: str,
    user: str,
    timeout: int,
    num_ctx: int | None = None,
    num_predict: int | None = None,
) -> str:
    import urllib.error
    import urllib.request

    # Ollama's default context window is small enough to silently
    # truncate a full ranking prompt (system prompt included), which
    # makes the model return unusable output rather than an error. An
    # explicit num_ctx guarantees the whole prompt is seen.
    options: dict = {"temperature": 0.2}
    if num_ctx:
        options["num_ctx"] = int(num_ctx)
    if num_predict:
        options["num_predict"] = int(num_predict)

    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            # Qwen3 and similar reasoning models default to a "thinking"
            # pass before answering. We don't need chain-of-thought for a
            # scoring task, so disabling it saves real time per batch.
            # Ollama silently ignores unknown fields, so this is a no-op
            # on models that don't support thinking mode.
            "think": False,
            "options": options,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise PipelineError(
            "Could not reach Ollama.",
            hint=(
                "Make sure Ollama is running (try: ollama serve) and that "
                f"the model is pulled: ollama pull {model}"
            ),
        ) from e

    message = body.get("message", {})
    content = message.get("content", "")
    if not content:
        raise PipelineError("Ollama returned an empty response.")
    return content


def _entry_index(item: dict) -> int | None:
    """The `[i]` candidate number the model was asked to echo back, if the
    entry carries one that is unambiguously an integer.

    Anything else (missing, a string like "candidate 3", a non-integral
    float) returns None: a garbled value must never be guessed into a
    mapping between an entry and a candidate.
    """
    if not isinstance(item, dict):
        return None
    raw = item.get("i")
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _candidate_for_index(
    item: dict, batch: list[CandidateSegment]
) -> CandidateSegment | None:
    """The batch candidate named by an entry's echoed `i`, or None when the
    entry has no usable index or points outside this batch.
    """
    idx = _entry_index(item)
    if idx is None or not (0 <= idx < len(batch)):
        return None
    return batch[idx]


def _parse_entries(
    parsed: dict,
    batch: list[CandidateSegment],
    candidate_by_span: dict,
    logger: Logger,
    stats: dict,
) -> list[ScoredClip]:
    """Turn one parsed LLM response into ScoredClips.

    The model is asked to echo the start/end values it was given, but
    qwen3:8b in particular often invents plausible-looking ones (0-100,
    100-200, ... for every entry) instead of copying them. Dropping every
    entry whose timestamps don't match threw away a whole batch of
    perfectly usable scores, which starved the run of candidates - the
    mechanism behind "it only produces one clip". Matching is therefore
    layered, strongest first; a weaker method never overrides a stronger
    one:

      1. timestamps: exact (start, end), then nearest candidate within the
         5s tolerance. This is the only method that proves the model used
         real timestamps.
      2. the `i` candidate number the model was asked to echo.
      3. position, but only when *nothing* in the batch matched by
         timestamp and the response has exactly one usable entry per
         candidate - otherwise we cannot know which entry is which.

    Whichever method matched, the candidate's real span and text win; the
    model's score, sub-scores, reason and title are kept, since the scores
    are the part we actually want from it. Entries that match none of the
    three are dropped: a wrong-length response with no usable index is
    never force-mapped onto candidates.
    """
    clips_data = parsed.get("clips", [])
    if not isinstance(clips_data, list):
        raise ValueError("LLM response missing a 'clips' list.")

    entries: list[tuple[ScoredClip, dict]] = []
    for item in clips_data:
        try:
            clip = ScoredClip.from_dict(item)
        except (KeyError, TypeError, ValueError):
            stats["entries_dropped_malformed"] += 1
            stats["entries_dropped"] += 1
            logger.warn(f"Dropping malformed clip entry: {item}")
            continue
        entries.append((clip, item if isinstance(item, dict) else {}))

    # Pass 1: timestamp matches (the strongest signal). Never trust LLM
    # timestamps blindly: snap to a candidate span we actually sent, so a
    # clip can't be built from timestamps the model hallucinated.
    timestamp_matches: list[CandidateSegment | None] = []
    for clip, _ in entries:
        match = candidate_by_span.get((clip.start, clip.end))
        if match is None:
            match = _closest_candidate(clip, batch)
        timestamp_matches.append(match)

    matched_by_timestamp = sum(1 for m in timestamp_matches if m is not None)

    # Positional mapping is only safe when nothing matched by timestamp
    # (so no stronger signal exists to contradict it) and there is exactly
    # one usable entry per candidate. Any other length means we cannot
    # know which entry belongs to which candidate, so we refuse to guess.
    allow_position = matched_by_timestamp == 0 and len(entries) == len(batch)

    by_index = 0
    by_position = 0
    clips: list[ScoredClip] = []
    for k, (clip, item) in enumerate(entries):
        match = timestamp_matches[k]
        if match is not None:
            stats["entries_matched_by_timestamp"] += 1
        else:
            match = _candidate_for_index(item, batch)
            if match is not None:
                by_index += 1
                stats["entries_matched_by_index"] += 1
            elif allow_position:
                match = batch[k]
                by_position += 1
                stats["entries_matched_by_position"] += 1
            else:
                stats["entries_dropped_unmatched"] += 1
                stats["entries_dropped"] += 1
                logger.warn(
                    f"LLM returned timestamps with no matching candidate "
                    f"({clip.start}-{clip.end}) and no usable candidate index; "
                    f"dropping."
                )
                continue
            clip.start = match.start
            clip.end = match.end

        clip.text = match.text
        clips.append(clip)

    if by_index or by_position:
        logger.warn(
            f"Re-anchored {by_index + by_position} of {len(entries)} scored "
            f"entry(ies) to their candidates ({by_index} by index, "
            f"{by_position} by order) - timestamps were unusable."
        )
    return clips


def _save_scores(path, clips, fingerprint, config) -> None:
    """Persist scores after every batch so an interrupted run can resume."""
    from utils.resume import video_identity, write_meta

    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"clips": [c.to_dict() for c in clips]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
        write_meta(path, fingerprint=fingerprint, **video_identity(config.video_path))
    except OSError:
        pass  # scoring continues even if the checkpoint cannot be written


def _load_saved_scores(path, fingerprint, config, logger):
    """Scores from an earlier run of this same video+settings, if any."""
    from models.schemas import ScoredClip
    from utils.resume import reuse_if_fresh

    if not reuse_if_fresh(path, fingerprint, config):
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        clips = [ScoredClip.from_dict(d) for d in data.get("clips", [])]
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.warn(f"Could not reuse the saved scores ({e}); ranking again.")
        return []
    return clips


def _score_batch(
    batch: list[CandidateSegment],
    config: Config,
    logger: Logger,
    candidate_by_span: dict,
    stats: dict,
    depth: int = 0,
) -> tuple[list[ScoredClip], bool]:
    """Score a single batch, retrying it, and if it still fails, splitting
    it into two halves and retrying those once (no deeper recursion - the
    point is to salvage a slice of the video, not to multiply LLM calls
    while the model is misbehaving).

    Why the split matters: batches are consecutive chunks of the
    candidate list, which is chronological. A batch that fails outright
    removes a whole slice of the video from consideration - repeatedly
    failing on the tail of a long video would leave only early-video
    candidates, which then all overlap each other and collapse to a
    single clip downstream. Halving a failing batch usually succeeds
    (smaller prompt, less JSON to emit), so far less is lost.

    Returns (clips, produced_anything).
    """
    attempts = max(1, config.llm_max_retries + 1)
    user_prompt = _build_user_prompt(batch)
    last_error: Exception | str | None = None

    for attempt in range(attempts):
        stats["llm_calls"] += 1
        if attempt:
            stats["retries"] += 1
        try:
            raw = _call_ollama(
                config.ollama_host,
                config.ollama_model,
                SYSTEM_PROMPT,
                user_prompt,
                config.llm_timeout_seconds,
                num_ctx=getattr(config, "llm_num_ctx", 8192),
                num_predict=getattr(config, "llm_num_predict", 4096),
            )
            parsed = _extract_json(raw)
            clips = _parse_entries(parsed, batch, candidate_by_span, logger, stats)
            if clips:
                return clips, True
            # A parseable response with nothing usable in it is just as
            # lost as a failed call - retry it rather than silently
            # dropping the whole slice of the video.
            last_error = "response contained no usable clip entries"
        except PipelineError:
            # Ollama unreachable / empty response: that's an environment
            # problem affecting every batch, so fail fast and loudly
            # instead of retrying into a shrinking clip pool.
            raise
        except Exception as e:  # noqa: BLE001 - parse errors, timeouts
            last_error = e

    if len(batch) > 1 and depth < _MAX_SPLIT_DEPTH:
        mid = len(batch) // 2
        stats["sub_batches_attempted"] += 2
        logger.warn(
            f"LLM batch of {len(batch)} candidates failed after {attempts} "
            f"attempt(s) ({last_error}). Retrying it as two halves of "
            f"{mid} and {len(batch) - mid} candidates..."
        )
        left, ok_left = _score_batch(
            batch[:mid], config, logger, candidate_by_span, stats, depth=depth + 1
        )
        right, ok_right = _score_batch(
            batch[mid:], config, logger, candidate_by_span, stats, depth=depth + 1
        )
        if not ok_left:
            stats["sub_batches_failed"] += 1
        if not ok_right:
            stats["sub_batches_failed"] += 1
        if depth == 0:
            if ok_left or ok_right:
                stats["batches_partial"] += 1
            else:
                stats["batches_failed"] += 1
        return left + right, ok_left or ok_right

    if depth == 0:
        stats["batches_failed"] += 1
    logger.warn(
        f"Skipping a batch of {len(batch)} candidate(s) after {attempts} failed "
        f"attempt(s) ({last_error}) - those candidates cannot be selected."
    )
    return [], False


def score_candidates(
    candidates: list[CandidateSegment],
    config: Config,
    logger: Logger,
    batch_size: int | None = None,
    stats_out: dict | None = None,
) -> list[ScoredClip]:
    """Score all candidates via the local LLM, in batches to keep prompts
    a reasonable size. A batch that fails is retried, then split in half
    and retried again before being given up on; whatever is ultimately
    lost is reported with counts at the end (see
    quality_filter.describe_shortfall) so a shrunken candidate pool is
    never silent. Progress is logged per batch (with an ETA) so long runs
    aren't a black box.

    `stats_out`, if given, receives the batch/candidate totals.
    """
    stats = stats_out if stats_out is not None else {}
    for key in (
        "candidates_sent",
        "batches_attempted",
        "batches_failed",
        "batches_partial",
        "batches_retried",
        "sub_batches_attempted",
        "sub_batches_failed",
        "entries_dropped_malformed",
        "entries_dropped_unmatched",
        "entries_matched_by_timestamp",
        "entries_matched_by_index",
        "entries_matched_by_position",
        "entries_dropped",
        "llm_calls",
        "retries",
    ):
        stats.setdefault(key, 0)

    if not candidates:
        return []

    if batch_size is None:
        batch_size = llm_batch_size_for(config, candidates)

    from utils.resume import band_fingerprint

    scores_path = Path(config.project_output_dir()) / "scores.json"
    fingerprint = band_fingerprint(config)
    reused = _load_saved_scores(scores_path, fingerprint, config, logger)
    reused_spans = {(round(c.start, 3), round(c.end, 3)) for c in reused}
    pending = [
        c
        for c in candidates
        if (round(c.start, 3), round(c.end, 3)) not in reused_spans
    ]
    if reused:
        logger.info(
            f"Resume: {len(reused)} of {len(candidates)} candidate(s) were "
            f"already scored; ranking the remaining {len(pending)}."
        )
    if reused and not pending:
        logger.info("Resume: every candidate already scored - skipping ranking.")
        return reused
    candidates = pending

    scored: list[ScoredClip] = []
    candidate_by_span = {(c.start, c.end): c for c in candidates}
    batches_reanchored = 0

    total_batches = (len(candidates) + batch_size - 1) // batch_size
    ranking_started = time.perf_counter()
    logger.info(f"Ranking {len(candidates)} candidates in {total_batches} batch(es) of {batch_size}...")

    for batch_no, batch_start in enumerate(
        range(0, len(candidates), batch_size), start=1
    ):
        batch = candidates[batch_start : batch_start + batch_size]
        stats["batches_attempted"] += 1
        stats["candidates_sent"] += len(batch)

        before = len(scored)
        reanchored_before = (
            stats["entries_matched_by_index"] + stats["entries_matched_by_position"]
        )
        batch_clips, produced = _score_batch(
            batch, config, logger, candidate_by_span, stats
        )
        if (
            stats["entries_matched_by_index"] + stats["entries_matched_by_position"]
            > reanchored_before
        ):
            batches_reanchored += 1
        scored.extend(batch_clips)
        if batch_clips:
            _save_scores(scores_path, reused + scored, fingerprint, config)
        if not produced:
            # _score_batch already counted this failed top-level batch.
            logger.info(
                f"Batch {batch_no}/{total_batches}: no candidates could be scored."
            )

        if total_batches > 1:
            elapsed = time.perf_counter() - ranking_started
            eta = elapsed / batch_no * (total_batches - batch_no)
            logger.info(
                f"Batch {batch_no}/{total_batches} done, "
                f"{len(scored) - before} scored "
                f"({elapsed:.0f}s elapsed, ~{eta:.0f}s remaining)"
            )

    reanchored_total = (
        stats["entries_matched_by_index"] + stats["entries_matched_by_position"]
    )
    if reanchored_total:
        logger.info(
            f"Re-anchored {reanchored_total} scored entry(ies) in "
            f"{batches_reanchored} batch(es) total "
            f"({stats['entries_matched_by_index']} by index, "
            f"{stats['entries_matched_by_position']} by order) - the model's "
            f"timestamps were unusable."
        )

    if stats["batches_failed"] or stats["batches_partial"]:
        logger.warn(
            f"LLM ranking incomplete: {stats['batches_attempted'] - stats['batches_failed']}"
            f"/{stats['batches_attempted']} batch(es) produced scores, "
            f"{stats['batches_partial']} only after being split in half, "
            f"{stats['batches_failed']} failed outright. "
            f"{len(scored)} of {len(candidates)} candidates were scored - "
            f"the rest cannot be selected and the run may produce fewer clips "
            f"than requested."
        )

    return reused + scored


def _closest_candidate(clip: ScoredClip, batch: list[CandidateSegment]) -> CandidateSegment | None:
    """Find the batch candidate whose span most closely matches the LLM's
    (possibly slightly-off) reported timestamps.
    """
    best = None
    best_dist = None
    for c in batch:
        dist = abs(c.start - clip.start) + abs(c.end - clip.end)
        if dist > 5.0:  # tolerance in seconds
            continue
        if best_dist is None or dist < best_dist:
            best = c
            best_dist = dist
    return best


def apply_weights(scored: list[ScoredClip], config: Config) -> list[ScoredClip]:
    """Recompute the overall `score` using configurable weights, so the
    LLM's holistic score doesn't silently override the user's weighting.
    """
    weights = config.scoring_weights
    for clip in scored:
        clip.score = round(weights.weighted_score(clip), 2)
    return scored
