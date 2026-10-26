# -*- coding: utf-8 -*-
"""Saved analysis presets + remembered last settings (no Qt, unit-testable)."""
from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1] / "projects"
FILE = ROOT / "presets.json"

# Only these keys are stored: transient UI state never leaks into a preset.
KNOWN = (
    "mode", "part_length", "clips", "platform", "strategy", "top_share",
    "manual_duration", "min_duration", "max_duration", "captions", "title",
    "lighting", "reframe", "screening", "language", "device", "ollama_model",
    "ollama_host", "whisper_model", "layout", "projects_root",
    "glossary", "asr_hotwords",
)
FORBIDDEN = '\\/:*?"<>|'


def path(root=None):
    return pathlib.Path(root) / "presets.json" if root else FILE


def _empty():
    return {"presets": {}, "last": {}, "last_name": ""}


def load(root=None):
    try:
        data = json.loads(path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    result = _empty()
    if isinstance(data.get("presets"), dict):
        result["presets"] = data["presets"]
    if isinstance(data.get("last"), dict):
        result["last"] = data["last"]
    if isinstance(data.get("last_name"), str):
        result["last_name"] = data["last_name"]
    return result


def save(data, root=None):
    target = path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)
    return target


def snapshot(settings):
    return {key: settings[key] for key in KNOWN if key in settings}


def clean_name(name):
    name = " ".join(str(name).split()).strip()
    if not name:
        raise ValueError("اكتب اسمًا للإعداد")
    if len(name) > 40:
        raise ValueError("اسم الإعداد طويل جدًا (40 حرفًا كحد أقصى)")
    if any(char in name for char in FORBIDDEN):
        raise ValueError("اسم الإعداد يحتوي رموزًا غير مسموحة")
    return name


def save_preset(name, settings, root=None):
    name = clean_name(name)
    data = load(root)
    data["presets"][name] = snapshot(settings)
    data["last_name"] = name
    save(data, root)
    return data


def delete_preset(name, root=None):
    data = load(root)
    if data["presets"].pop(name, None) is not None:
        if data.get("last_name") == name:
            data["last_name"] = ""
        save(data, root)
    return data


def remember(settings, name="", root=None):
    data = load(root)
    data["last"] = snapshot(settings)
    if name and name in data["presets"]:
        data["last_name"] = name
    elif data.get("last_name") not in data["presets"]:
        data["last_name"] = ""
    save(data, root)
    return data
