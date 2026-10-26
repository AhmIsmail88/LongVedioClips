# -*- coding: utf-8 -*-
"""Helper-process entry point that works both from source and from a frozen EXE.

From source: ``python -u -m core.export_queue <job>``
Frozen app:   ``LongVideoClips.exe --run-module core.export_queue <job>``
              (``--run-module`` is dispatched in ``desktop_app.py``)
"""
from __future__ import annotations

import os
import runpy
import sys

FROZEN_FLAG = "--run-module"
CHILD_ENV = {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}

# Keep rebound stdio wrappers alive: dropping them would close fd 1/2.
_STDIO: list = []


def python_command(module: str, *args) -> list[str]:
    """argv that starts `module` in a sibling process of the running app."""
    tail = [str(item) for item in args]
    if getattr(sys, "frozen", False):
        return [sys.executable, FROZEN_FLAG, module, *tail]
    return [sys.executable, "-u", "-m", module, *tail]


def child_env(base: dict | None = None) -> dict:
    """Environment for helper processes: UTF-8 output and unbuffered logs."""
    return {**(base if base is not None else os.environ), **CHILD_ENV}


def _repair_fd(fd: int, flags: int) -> None:
    """A windowed build can start with invalid 0/1/2 handles; point them at
    devnull so child processes inherit handles CreateProcess accepts."""
    try:
        os.fstat(fd)
        return
    except OSError:
        pass
    try:
        null = os.open(os.devnull, flags)
    except OSError:
        return
    try:
        os.dup2(null, fd)
    finally:
        os.close(null)


def ensure_stdio() -> None:
    """In a windowed build sys.stdout/stderr are None and fd 0/1/2 may be
    invalid; rebind them so logging, prints and ffmpeg subprocesses work."""
    _repair_fd(0, os.O_RDWR)
    _repair_fd(1, os.O_WRONLY)
    _repair_fd(2, os.O_WRONLY)
    for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
        if getattr(sys, name) is not None:
            continue
        fd = {"stdin": 0, "stdout": 1, "stderr": 2}[name]
        try:
            stream = os.fdopen(fd, mode, encoding="utf-8", errors="replace",
                               buffering=1 if mode == "w" else -1)
            if mode == "w":
                stream.write("")
                stream.flush()
        except OSError:
            stream = open(os.devnull, mode, encoding="utf-8", errors="replace")
        _STDIO.append(stream)
        setattr(sys, name, stream)


def dispatch(argv: list[str]) -> bool:
    """Run a helper module requested by a frozen parent. Returns True if handled."""
    if len(argv) >= 3 and argv[1] == FROZEN_FLAG:
        ensure_stdio()
        module = argv[2]
        sys.argv = [module, *argv[3:]]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return True
    return False
