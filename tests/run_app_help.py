"""Run the CLI (`python app.py ...`) on this machine's standalone Python.

That interpreter runs in "safe path" mode (sys.flags.safe_path is True and
PYTHONPATH is ignored), so `python app.py` cannot import the repo's local
modules by itself. A normal venv Python does not have this restriction -
this shim just inserts the repo root and delegates, so the real argparse
output can still be verified here.

Usage:
    python tests\\run_app_help.py            # same as: python app.py --help
    python tests\\run_app_help.py --batch x   # extra args are forwarded
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.argv = ["app.py", *sys.argv[1:]]

try:
    runpy.run_path(str(REPO_ROOT / "app.py"), run_name="__main__")
except SystemExit as exc:
    print(f"[runner] app.py exited with status {exc.code}")
    raise
