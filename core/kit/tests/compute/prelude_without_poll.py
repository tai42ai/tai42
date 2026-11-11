"""A process prelude of the kit's tests: it takes ``select.poll`` away, as a prelude that reshapes the interpreter may.

It names no library, so a test run under it proves a seam serves any prelude.
"""

from __future__ import annotations

import select


def remove_poll() -> None:
    """Delete ``select.poll`` from the process."""
    del select.poll
