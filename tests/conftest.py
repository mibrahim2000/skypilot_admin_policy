"""Pytest hooks: SkyPilot imports touch SQLite under HOME; use a writable project-local HOME."""

from __future__ import annotations

import os
from pathlib import Path

# Must run before test modules import `main` -> `sky` (side effects on import).
_root = Path(__file__).resolve().parent.parent
_pytest_home = _root / ".pytest_home"
_pytest_home.mkdir(exist_ok=True)
(_pytest_home / ".sky").mkdir(exist_ok=True)
# Always set HOME so sandbox/CI use a workspace-writable dir (do not use setdefault: real HOME may be unwritable).
os.environ["HOME"] = str(_pytest_home)
