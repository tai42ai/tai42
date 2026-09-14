"""Make the repo's ``scripts/`` importable so these tests can import the script
modules they exercise, and this ``tests/`` directory so a test module can import
a sibling test-support module."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _dir in (_HERE.parent, _HERE):
    if _dir.is_dir() and str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))
