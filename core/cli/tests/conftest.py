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
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
