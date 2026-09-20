"""PEP 503 first-party name matching tests for scripts/range_sync.py — the shared sample-tree builders
live in _range_sync_support."""

from __future__ import annotations

import range_sync


def test_noncanonical_dep_name_is_matched():
    # A first-party dep spelled non-canonically (underscore / mixed case) must
    # still resolve to its member so a stale range cannot false-green as "in
    # sync". The rewritten literal preserves the original spelling.
    first_party = {"tai42-kit": "0.3.0"}
    pyproject = {"project": {"dependencies": ["tai42_Kit>=0.2,<0.4"]}}
    changes = range_sync.compute_pyproject_changes(pyproject, first_party)
    assert len(changes) == 1
    assert changes[0].new_req == "tai42_Kit>=0.3,<0.4"


def test_normalize_name():
    assert range_sync._normalize_name("tai42_Kit") == "tai42-kit"
    assert range_sync._normalize_name("tai42-kit") == "tai42-kit"
    assert range_sync._normalize_name("Tai42.Contract") == "tai42-contract"
