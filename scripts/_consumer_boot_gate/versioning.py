"""The bump-class verdict, and the loud-fail helper the whole gate raises through."""

from __future__ import annotations

import sys
from typing import NoReturn


def _fail(message: str) -> NoReturn:
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def break_is_accepted(bump: str) -> bool:
    """A consumer boot break may ship only in a major bump."""
    return bump == "major"
