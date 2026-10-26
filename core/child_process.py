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


def ensure_stdio() -> None:
    """In a windowed build sys.stdout/stderr are None; rebind to the inherited
    handles (or devnull) so logging and progress prints never crash."""
    for name, fd in (("stdout", 1), ("stderr", 2)):
        if getattr(sys, name) is not None:
            continue
        try:
            stream = os.fdopen(fd, "w", encoding="utf-8", errors="replace", buffering=1)
        except OSError:
            stream = open(os.devnull, "w", encoding="utf-8")
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
