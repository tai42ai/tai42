"""Module-level jobs a compute-pool worker imports by name: picklable, and free of any app access."""

import re
import time

# Catastrophic backtracking: the regex engine holds the interpreter lock for the whole match.
_BACKTRACKING = re.compile(r"(a+)+$")


def backtrack(length: int) -> bool:
    """Match a subject of ``length`` characters that forces exponential backtracking; never matches."""
    return _BACKTRACKING.match("a" * length + "b") is not None


def refuse(code: int) -> bool:
    """Raise ``ValueError`` naming ``code``."""
    raise ValueError(f"the job refused input {code}")


def linger(seconds: int) -> bool:
    """Sleep ``seconds`` seconds."""
    time.sleep(seconds)
    return True
