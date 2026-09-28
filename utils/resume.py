"""Resume support: remember what each expensive artifact was produced from.

A long analysis is far too expensive to lose when the GUI/server dies
halfway (transcription ~10 min, LLM ranking ~30 min, rendering minutes).
Each expensive artifact therefore gets a small sidecar .meta.json that
records the inputs it was produced from; a later run reuses the artifact
only while those inputs still match, and otherwise recomputes it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def meta_path(path) -> Path:
    """Sidecar metadata path for an artifact (e.g. transcript.json.meta.json)."""
    p = Path(path)
    return p.with_name(p.name + ".meta.json")


def write_meta(path, **fields) -> None:
    """Record the inputs an artifact was produced from (best effort)."""
    try:
        meta_path(path).write_text(
            json.dumps(fields, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def read_meta(path) -> dict:
    """The recorded inputs, or {} when the artifact has no usable metadata."""
    try:
        data = json.loads(meta_path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def video_identity(video_path) -> dict:
    """Name + size, so artifacts from a different file are never reused."""
    try:
        p = Path(video_path)
        return {"video": p.name, "video_size": p.stat().st_size}
    except OSError:
        return {"video": str(video_path), "video_size": 0}


def _hash(parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def whisper_fingerprint(config) -> str:
    """Inputs that change the transcript (so changing the clip band or the
    number of clips does NOT force a 10-minute re-transcription)."""
    return _hash([
        "whisper",
        getattr(config, "whisper_model", ""),
        getattr(config, "language", ""),
    ])


def band_fingerprint(config) -> str:
    """Inputs that change the candidate list, and therefore the scores."""
    w = getattr(config, "scoring_weights", None)
    weights = "" if w is None else ",".join(
        str(getattr(w, k, "")) for k in
        ("hook", "content", "story", "emotion", "standalone", "ending")
    )
    return _hash([
        "band",
        getattr(config, "min_duration", 0),
        getattr(config, "max_duration", 0),
        getattr(config, "num_clips", 0),
        getattr(config, "split_evenly", False),
        getattr(config, "candidate_window_sizes", ()),
        getattr(config, "candidate_stride", 0),
        getattr(config, "max_candidates", 0),
        getattr(config, "ollama_model", ""),
        weights,
    ])


def reuse_if_fresh(path, fingerprint, config) -> bool:
    """True when `path` exists AND its sidecar matches this video+settings."""
    if not (getattr(config, "resume", True) and Path(path).exists()):
        return False
    meta = read_meta(path)
    if meta.get("fingerprint") != fingerprint:
        return False
    ident = video_identity(config.video_path)
    return (
        meta.get("video") == ident["video"]
        and meta.get("video_size") == ident["video_size"]
    )
