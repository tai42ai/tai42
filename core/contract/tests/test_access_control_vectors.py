"""The conformance vectors the contract publishes in its bundle for the served access-control rules."""

from __future__ import annotations

import json

from tai42_contract.access_control.models import PATH_COVERAGE_VECTORS, PATTERN_OPEN_PREFIX_VECTORS
from tai42_contract.schema_export import build_document_schemas
from tai42_contract.schema_export.registry import bundle_drift


def test_the_path_coverage_rows_follow_the_segment_rule():
    assert len(PATH_COVERAGE_VECTORS) == 8
    for row in PATH_COVERAGE_VECTORS:
        prefix, path = row["prefix"], row["path"]
        assert isinstance(prefix, str)
        assert isinstance(path, str)
        assert row["covered"] is (path == prefix or path.startswith(prefix + "/"))
    assert {row["covered"] for row in PATH_COVERAGE_VECTORS} == {True, False}


def test_the_open_prefix_rows_cover_a_prefix_and_every_decline():
    assert {row["open_prefix"] is None for row in PATTERN_OPEN_PREFIX_VECTORS} == {True, False}
    assert all(set(row) == {"pattern", "open_prefix"} for row in PATTERN_OPEN_PREFIX_VECTORS)


def test_the_bundle_carries_both_vector_tables():
    vectors = build_document_schemas()["vectors"]
    assert vectors == {
        "path_coverage": [dict(row) for row in PATH_COVERAGE_VECTORS],
        "pattern_open_prefix": [dict(row) for row in PATTERN_OPEN_PREFIX_VECTORS],
    }


def test_a_changed_vector_table_is_reported_as_drift():
    fresh = build_document_schemas()
    committed = json.loads(json.dumps(fresh))
    committed["vectors"]["path_coverage"][0]["covered"] = not committed["vectors"]["path_coverage"][0]["covered"]
    assert bundle_drift(fresh, committed) == ["the published vectors changed"]
