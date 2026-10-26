# -*- coding: utf-8 -*-
"""تقرير بيئة التشغيل — يفيد في تشخيص نسخة الـEXE.

التشغيل:
    MomentForgeAI.exe --run-module core.diagnose
    MomentForgeAI.exe --diagnose
    .venv\\Scripts\\python.exe core\\diagnose.py
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

MODULES = (
    "faster_whisper",
    "ctranslate2",
    "av",
    "tokenizers",
    "huggingface_hub",
    "onnxruntime",
    "static_ffmpeg",
    "yt_dlp",
    "PySide6",
)


def _module_report(name: str) -> dict:
    try:
        module = importlib.import_module(name)
        version = getattr(module, "__version__", None)
        return {"ok": True, "version": str(version) if version else None, "error": None}
    except Exception as exc:  # أي عطل في الاستيراد يهمنا نعرفه
        return {"ok": False, "version": None, "error": f"{type(exc).__name__}: {exc}"}


def _ffmpeg_report() -> dict:
    try:
        from utils.ffmpeg import get_ffmpeg_binaries

        ffmpeg, ffprobe = get_ffmpeg_binaries()
        return {"ffmpeg": ffmpeg, "ffprobe": ffprobe,
                "ffmpeg_exists": Path(ffmpeg).is_file() if os.sep in str(ffmpeg) else None}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _hf_cache_report() -> dict:
    root = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub"
    try:
        models = sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.startswith("models--"))
    except OSError:
        models = []
    return {"path": str(root), "exists": root.is_dir(), "models": models}


def _data_root() -> Path:
    try:
        from core.app_paths import DATA_ROOT

        return DATA_ROOT
    except Exception:
        return Path.cwd()


def collect() -> dict:
    """لقطة قابلة للتحويل إلى JSON عن البيئة التي يعمل فيها التطبيق الآن."""
    root = Path(__file__).resolve().parents[1]
    try:
        from core.ui_theme import resource_path

        icon_ok = resource_path("assets", "icon.png").is_file()
    except Exception:
        icon_ok = False
    return {
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "repo_root": str(root),
        "data_root": str(_data_root()),
        "cwd": os.getcwd(),
        "modules": {name: _module_report(name) for name in MODULES},
        "ffmpeg": _ffmpeg_report(),
        "hf_cache": _hf_cache_report(),
        "assets_icon": icon_ok,
    }


def main() -> int:
    report = collect()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    target = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    try:
        (target / "diagnose.json").write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"تعذّر كتابة diagnose.json: {exc}")
    if sys.stdout is not None:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
