"""Tests for scripts/range_sync.py: pyproject analysis: cross-major pin preservation and guarded-rewrite warnings.

The shared sample-tree builders live in _range_sync_support."""

from __future__ import annotations

import pytest

import range_sync


def test_pinned_cap_preserved_across_major():
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract>=1.2,<2"]},
        "tool": {"range-sync": {"pinned": ["tai42-contract"]}},
    }
    analysis = range_sync.analyze_pyproject(pyproject, first_party, "core/kit")
    assert analysis.changes == []  # left untouched
    assert [(p.dep_name, p.kept_range) for p in analysis.preserved] == [("tai42-contract", ">=1.2,<2")]
    assert analysis.warnings == []


def test_unpinned_cross_major_syncs_and_warns():
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {"project": {"dependencies": ["tai42-contract>=1.2,<2"]}}
    analysis = range_sync.analyze_pyproject(pyproject, first_party)
    # still rewrites (syncs as today) ...
    assert [c.new_req for c in analysis.changes] == ["tai42-contract>=2.0,<3"]
    assert analysis.preserved == []
    # ... but the unannotated cross-major cap is flagged for the --check warning
    assert [w.dep_name for w in analysis.warnings] == ["tai42-contract"]
    assert analysis.warnings[0].old_req == "tai42-contract>=1.2,<2"
    assert analysis.warnings[0].new_req == "tai42-contract>=2.0,<3"


def test_pinning_ignored_on_minor_sync():
    first_party = {"tai42-contract": "1.5.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract>=1.2,<2"]},
        "tool": {"range-sync": {"pinned": ["tai42-contract"]}},
    }
    analysis = range_sync.analyze_pyproject(pyproject, first_party)
    # a minor bump within the same major rewrites normally despite the pin
    assert [c.new_req for c in analysis.changes] == ["tai42-contract>=1.5,<2"]
    assert analysis.preserved == []
    assert analysis.warnings == []


def test_pin_name_matched_noncanonically():
    # a pin spelled non-canonically still resolves to its dependency
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract>=1.2,<2"]},
        "tool": {"range-sync": {"pinned": ["Tai42_Contract"]}},
    }
    analysis = range_sync.analyze_pyproject(pyproject, first_party)
    assert [p.dep_name for p in analysis.preserved] == ["tai42-contract"]
    assert analysis.changes == []


def test_malformed_pin_unknown_dep_raises():
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract>=1.2,<2"]},
        "tool": {"range-sync": {"pinned": ["tai42-nonesuch"]}},
    }
    with pytest.raises(RuntimeError, match="not first-party dependencies"):
        range_sync.analyze_pyproject(pyproject, first_party, "core/kit")


def test_malformed_pin_wrong_type_raises():
    pyproject = {"project": {"dependencies": []}, "tool": {"range-sync": {"pinned": "tai42-contract"}}}
    with pytest.raises(RuntimeError, match="must be a list"):
        range_sync.pinned_deps(pyproject)


def test_widened_cap_pin_preserved():
    # a DELIBERATELY WIDENED cap (<3) that the derived range narrows (<2) is a
    # cross at the cap end even though the floor major (1) does not move
    first_party = {"tai42-contract": "1.5.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract>=1.2,<3"]},
        "tool": {"range-sync": {"pinned": ["tai42-contract"]}},
    }
    analysis = range_sync.analyze_pyproject(pyproject, first_party, "core/kit")
    assert analysis.changes == []  # pin honoured; the widened cap is not narrowed
    assert [(p.dep_name, p.kept_range) for p in analysis.preserved] == [("tai42-contract", ">=1.2,<3")]
    assert analysis.warnings == []


def test_widened_cap_unpinned_syncs_and_warns():
    first_party = {"tai42-contract": "1.5.0"}
    pyproject = {"project": {"dependencies": ["tai42-contract>=1.2,<3"]}}
    analysis = range_sync.analyze_pyproject(pyproject, first_party)
    assert [c.new_req for c in analysis.changes] == ["tai42-contract>=1.5,<2"]
    assert analysis.preserved == []
    assert [w.dep_name for w in analysis.warnings] == ["tai42-contract"]


def test_compat_spec_pin_preserved():
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract~=1.2"]},
        "tool": {"range-sync": {"pinned": ["tai42-contract"]}},
    }
    analysis = range_sync.analyze_pyproject(pyproject, first_party, "core/kit")
    assert analysis.changes == []
    assert [p.kept_range for p in analysis.preserved] == ["~=1.2"]
    assert analysis.warnings == []


def test_exact_spec_unpinned_syncs_and_warns():
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {"project": {"dependencies": ["tai42-contract==1.2.3"]}}
    analysis = range_sync.analyze_pyproject(pyproject, first_party)
    assert [c.new_req for c in analysis.changes] == ["tai42-contract>=2.0,<3"]
    assert analysis.preserved == []
    assert [w.dep_name for w in analysis.warnings] == ["tai42-contract"]


def test_unparseable_spec_pin_preserved():
    # a spec with no parseable major structure (bare ``>``) is preserved
    # conservatively under a pin rather than rewritten blind
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract>1.2"]},
        "tool": {"range-sync": {"pinned": ["tai42-contract"]}},
    }
    analysis = range_sync.analyze_pyproject(pyproject, first_party, "core/kit")
    assert analysis.changes == []
    assert [p.kept_range for p in analysis.preserved] == [">1.2"]
    assert analysis.warnings == []


def test_unparseable_spec_unpinned_syncs_and_warns():
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {"project": {"dependencies": ["tai42-contract>1.2"]}}
    analysis = range_sync.analyze_pyproject(pyproject, first_party)
    assert [c.new_req for c in analysis.changes] == ["tai42-contract>=2.0,<3"]
    assert [w.dep_name for w in analysis.warnings] == ["tai42-contract"]
