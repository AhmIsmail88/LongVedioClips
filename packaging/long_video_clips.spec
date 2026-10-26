# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the desktop app (onedir build).

Build (recommended):
    .\\packaging\\build.ps1 -Install

Notes
- Helper processes are started through ``desktop_app.py --run-module <module>``
  (see core/child_process.py), which is what makes the frozen build work.
- Model weights (Whisper / Ollama) are downloaded on first use, not bundled.
- ffmpeg is bundled through the ``static_ffmpeg`` package.
- Smart reframing pulls in torch/ultralytics; set LVC_WITH_TORCH=1 to include it.
"""
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, root)

datas = [
    (os.path.join(root, "core", "desktop_translations.json"), "core"),
    (os.path.join(root, "profanity.txt"), "."),
    (os.path.join(root, "assets", "icon.png"), "assets"),
    (os.path.join(root, "assets", "icon.ico"), "assets"),
]
weights = os.path.join(root, "yolov8n.pt")
if os.path.isfile(weights):
    datas.append((weights, "."))
datas += collect_data_files("static_ffmpeg")
# nudenet ships its 320n.onnx next to the module; without it screening cannot run.
datas += collect_data_files("nudenet")

hiddenimports = collect_submodules("yt_dlp.extractor") + [
    "yt_dlp.networking.impersonate",
    "faster_whisper",
    "cv2",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "core.child_process",
    "core.desktop_pipeline",
    "core.export_queue",
    "core.media_download",
    "core.desktop_tasks",
    "core.audio_export",
]

excludes = [
    "streamlit",
    "tkinter",
    "pytest",
    "PyQt5",
    "PyQt6",
    "matplotlib",
    "IPython",
    # Transitive analysis-only libraries (numba/llvmlite/scipy/pyarrow ≈ 190MB).
    # Smart reframing (the only feature that needed them) is excluded below.
    "numba",
    "llvmlite",
    "pyarrow",
    "scipy",
    "pandas",
    "sklearn",
]
if os.environ.get("LVC_WITH_TORCH") != "1":
    excludes += ["torch", "torchvision", "ultralytics", "transformers", "librosa", "soundfile"]

a = Analysis(
    [os.path.join(root, "desktop_app.py")],
    pathex=[root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MomentForgeAI",
    icon=os.path.join(root, "assets", "icon.ico"),
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MomentForgeAI",
)
