# -*- coding: utf-8 -*-
"""Temporary download-to-watch cache (deleted when playback stops)."""
from __future__ import annotations

import hashlib
import pathlib
import tempfile
import time


def cache_dir(root=None) -> pathlib.Path:
    base = pathlib.Path(root) if root else pathlib.Path(tempfile.gettempdir()) / "lvc-watch"
    base.mkdir(parents=True, exist_ok=True)
    return base


def target_for(url: str, root=None) -> pathlib.Path:
    digest = hashlib.sha256((url or "").strip().encode("utf-8")).hexdigest()[:16]
    return cache_dir(root) / f"{digest}.mp4"


def cleanup(max_age_seconds: int = 86400, root=None) -> int:
    """يحذف بقايا جلسات قديمة (يوم كامل افتراضيًا)."""
    removed = 0
    now = time.time()
    for file in cache_dir(root).glob("*.mp4"):
        try:
            if now - file.stat().st_mtime > max_age_seconds:
                file.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def remove(path) -> bool:
    try:
        pathlib.Path(path).unlink()
        return True
    except OSError:
        return False
