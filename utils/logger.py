"""
Simple, dependency-free logging/progress reporting.

The spec calls for clear staged progress like:
    [1/5] Loading video...
and actionable error messages instead of raw tracebacks for expected
failure modes. This module centralizes that formatting.
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager


class Logger:
    def __init__(self, total_stages: int = 5, verbose: bool = True):
        self.total_stages = total_stages
        self.verbose = verbose
        self._stage_times: dict[str, float] = {}

    def stage(self, index: int, message: str) -> None:
        print(f"[{index}/{self.total_stages}] {message}", flush=True)

    def info(self, message: str) -> None:
        if self.verbose:
            print(f"    - {message}", flush=True)

    def warn(self, message: str) -> None:
        print(f"WARNING: {message}", file=sys.stderr, flush=True)

    def error(self, message: str) -> None:
        print(f"ERROR: {message}", file=sys.stderr, flush=True)

    @contextmanager
    def timed(self, label: str):
        """Context manager that logs how long a block took."""
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            self._stage_times[label] = elapsed
            self.info(f"{label} took {elapsed:.1f}s")

    def summary(self) -> None:
        if not self._stage_times:
            return
        print("\n--- Timing summary ---", flush=True)
        total = 0.0
        for label, elapsed in self._stage_times.items():
            print(f"  {label:<30} {elapsed:6.1f}s", flush=True)
            total += elapsed
        print(f"  {'TOTAL':<30} {total:6.1f}s", flush=True)


class PipelineError(Exception):
    """Raised for expected, user-facing failures.

    app.py catches this and prints a clean message instead of a traceback.
    """

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
