"""Before/after demonstration for the captured live payload (Task B #3).

Reproduces the payload qwen3:8b actually returned for a 16-candidate batch
in the live 51-minute run - timestamps 0.0-100.0, 100.0-200.0, ... with no
`i` on any entry - and shows:

  BEFORE (span-only matcher, the previous code): 0 of 16 entries usable,
          16 dropped, so the batch failed, was retried, and got halved.
  AFTER  (layered matcher): 16 of 16 entries recovered, mapped onto the
          real candidate spans in order (and by index when `i` is given,
          which is what the updated prompt now asks for).

Ollama is never contacted: `clip_selector._call_ollama` is replaced with a
fake that replays the captured payload verbatim. No GPU work, no
transcription, no pipeline run.

Run:
    .venv\\Scripts\\python.exe tests\\demo_reanchor_recovery.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import QuietLogger, REPO_ROOT  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))

from config import Config  # noqa: E402
from core import clip_selector  # noqa: E402
from models.schemas import CandidateSegment, ScoredClip  # noqa: E402

N = 16


def make_candidates(n: int = N) -> list[CandidateSegment]:
    out = []
    for i in range(n):
        start = 10.0 + i * 137.5
        out.append(
            CandidateSegment(start=start, end=start + 41.25, text=f"candidate {i} text")
        )
    return out


def captured_payload(with_index: bool) -> dict:
    """The captured response shape: one entry per candidate, timestamps
    invented as 0-100, 100-200, ... `with_index` adds the `i` the updated
    prompt requests (the live capture predates that, so it had none).
    """
    clips = []
    for i in range(N):
        entry = {
            "start": float(i * 100),
            "end": float((i + 1) * 100),
            "score": 70.0 + i,
            "hook_score": 70.0 + i,
            "content_score": 70.0 + i,
            "story_score": 70.0 + i,
            "emotion_score": 70.0 + i,
            "standalone_score": 70.0 + i,
            "ending_score": 70.0 + i,
            "reason": f"reason {i}",
            "title": f"title {i}",
        }
        if with_index:
            entry["i"] = i
        clips.append(entry)
    return {"clips": clips}


def old_parse_entries(parsed, batch, candidate_by_span, stats) -> list[ScoredClip]:
    """Faithful copy of the previous span-only matcher, kept here purely to
    quantify what the captured payload used to cost. (The real one no
    longer exists in clip_selector.)
    """
    clips: list[ScoredClip] = []
    for item in parsed.get("clips", []):
        try:
            clip = ScoredClip.from_dict(item)
        except (KeyError, TypeError, ValueError):
            stats["dropped"] += 1
            continue
        match = candidate_by_span.get((clip.start, clip.end))
        if match is None:
            match = clip_selector._closest_candidate(clip, batch)
            if match is None:
                stats["dropped"] += 1
                continue
            clip.start, clip.end = match.start, match.end
        clip.text = match.text
        clips.append(clip)
    return clips


def run_new(with_index: bool):
    payload = captured_payload(with_index)
    clip_selector._call_ollama = lambda host, model, system, user, timeout: json.dumps(
        payload
    )
    config = Config(llm_batch_size=N, llm_max_retries=0)
    stats: dict = {}
    logger = QuietLogger()
    scored = clip_selector.score_candidates(
        make_candidates(), config, logger, stats_out=stats
    )
    return scored, stats, logger


def main() -> int:
    batch = make_candidates()
    by_span = {(c.start, c.end): c for c in batch}

    old_stats = {"dropped": 0}
    old_clips = old_parse_entries(captured_payload(False), batch, by_span, old_stats)

    print(f"Candidates sent: {N} (spans {batch[0].start:.2f}-{batch[0].end:.2f} "
          f".. {batch[-1].start:.2f}-{batch[-1].end:.2f})")
    print("Captured payload: 16 entries, timestamps 0.0-100.0 .. 1500.0-1600.0")
    print()
    print("[BEFORE] old span-only matcher")
    print(f"  entries kept   : {len(old_clips)}")
    print(f"  entries dropped: {old_stats['dropped']}")
    print(f"  WARNING: LLM returned timestamps with no matching candidate "
          f"(0.0-100.0); dropping.   x{old_stats['dropped']}")
    print()
    assert len(old_clips) == 0 and old_stats["dropped"] == N, "old-logic baseline drifted"

    for with_index in (False, True):
        scored, stats, _ = run_new(with_index)
        label = "payload as captured (no i)" if not with_index else "payload with i"
        print(f"[AFTER] layered matcher - {label}")
        print(f"  entries recovered      : {len(scored)} of {N}")
        print(f"  matched by timestamp   : {stats['entries_matched_by_timestamp']}")
        print(f"  matched by index       : {stats['entries_matched_by_index']}")
        print(f"  matched by position    : {stats['entries_matched_by_position']}")
        print(f"  dropped                : {stats['entries_dropped']}")
        assert len(scored) == N, f"{label}: expected all 16 recovered"
        assert stats["entries_dropped"] == 0
        if with_index:
            assert stats["entries_matched_by_index"] == N
            assert stats["entries_matched_by_position"] == 0
        else:
            assert stats["entries_matched_by_position"] == N
        for clip, cand in zip(scored, batch):
            assert (clip.start, clip.end) == (cand.start, cand.end), "span not re-anchored"
        print(f"  -> all {N} entries live on their real candidate spans: "
              f"{scored[0].start:.2f}-{scored[0].end:.2f} .. "
              f"{scored[-1].start:.2f}-{scored[-1].end:.2f}")
        print()

    print(f"BEFORE: {len(old_clips)}/{N} usable ({old_stats['dropped']} dropped)  "
          f"AFTER: {N}/{N} recovered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
