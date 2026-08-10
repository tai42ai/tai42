"""The ``tai`` root callback calls ``load_dotenv()``, which writes process
environment variables directly — a real write outside ``monkeypatch`` that would
otherwise leak into the rest of the suite. This autouse fixture snapshots and
restores ``os.environ`` around every CLI test to keep them hermetic.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _restore_environ() -> Iterator[None]:
    saved = os.environ.copy()
    # Every CLI test runs against a deterministic wide, plain terminal so output
    # asserts never depend on the runner's tty: Typer/rich folds error and help
    # panels mid-phrase at whatever narrow width it detects off a real tty (an
    # empty COLUMNS collapses to a handful of columns in CI), breaking substring
    # asserts. Pinned here for every invoke, including those bypassing the harness.
    os.environ["COLUMNS"] = "200"
    os.environ["NO_COLOR"] = "1"
    os.environ["TERM"] = "dumb"
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
