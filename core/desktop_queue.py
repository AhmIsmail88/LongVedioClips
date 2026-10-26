# -*- coding: utf-8 -*-
"""Persistent multi-source analysis queue (pure store, no Qt)."""
from __future__ import annotations

import json
import pathlib
import time
import uuid

from core.app_paths import DATA_ROOT as _DATA_ROOT

ROOT = _DATA_ROOT / "projects"
FILE = ROOT / "desktop_queue.json"

STATES = ("pending", "running", "done", "failed", "skipped")


def path(root=None):
    return pathlib.Path(root) / "desktop_queue.json" if root else FILE


def _empty():
    return {"items": []}


def load(root=None):
    try:
        data = json.loads(path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return _empty()
    items = [item for item in data["items"] if isinstance(item, dict) and item.get("source")]
    return {"items": items}


def save(data, root=None):
    target = path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)
    return target


def same_source(left, right):
    try:
        return pathlib.Path(left).resolve() == pathlib.Path(right).resolve()
    except OSError:
        return False


def add(source, settings=None, root=None):
    """Add one video to the end of the queue; duplicates raise instead of doubling."""
    source = str(pathlib.Path(source).expanduser())
    if not pathlib.Path(source).is_file():
        raise ValueError("ملف المصدر غير موجود: " + pathlib.Path(source).name)
    data = load(root)
    if any(same_source(item["source"], source) for item in data["items"]):
        raise ValueError("هذا الملف موجود في الطابور بالفعل")
    item = {
        "id": uuid.uuid4().hex[:12],
        "source": source,
        "settings": dict(settings or {}),
        "state": "pending",
        "folder": "",
        "error": "",
        "added": time.time(),
        "finished": None,
    }
    data["items"].append(item)
    save(data, root)
    return item


def remove(item_id, root=None):
    data = load(root)
    data["items"] = [item for item in data["items"] if item.get("id") != item_id]
    save(data, root)
    return data


def clear_finished(root=None):
    data = load(root)
    data["items"] = [item for item in data["items"] if item.get("state") not in ("done", "failed", "skipped")]
    save(data, root)
    return data


def update(item_id, root=None, **fields):
    data = load(root)
    for item in data["items"]:
        if item.get("id") == item_id:
            item.update(fields)
            break
    save(data, root)
    return data


def pending(root=None):
    return [item for item in load(root)["items"] if item.get("state") == "pending"]


def reset_failed(root=None):
    """يعيد العناصر الفاشلة إلى الانتظار لمحاولة ثانية."""
    data = load(root)
    changed = 0
    for item in data["items"]:
        if item.get("state") == "failed":
            item.update(state="pending", error="", finished=None)
            changed += 1
    if changed:
        save(data, root)
    return changed


def next_pending(root=None):
    items = pending(root)
    return items[0] if items else None


def recover(root=None):
    """A queue that was interrupted mid-run must not stay 'running' forever."""
    data = load(root)
    changed = False
    for item in data["items"]:
        if item.get("state") == "running":
            item["state"] = "pending"
            item["error"] = ""
            changed = True
    if changed:
        save(data, root)
    return data


def summary(root=None):
    counts = {state: 0 for state in STATES}
    for item in load(root)["items"]:
        counts[item.get("state", "pending")] = counts.get(item.get("state", "pending"), 0) + 1
    return counts
