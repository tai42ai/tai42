"""The committed served-document bundle equals a fresh rebuild in SHAPE.

This is the platform half of the anti-drift guarantee: a served-document shape change
that forgets to regenerate ``schemas/contract-schema.json`` reds the contract's own
build here, and the failure names every document that drifted. Freshness is judged on
the parsed structure, never on bytes, so a version-only bump — applied to
``contract_version`` by the release tooling — never stales the file; the version is a
separate, distinct equality against the running package version.
"""

from __future__ import annotations

import json

from tai42_contract.schema_export import build_document_schemas, committed_bundle_path
from tai42_contract.schema_export.registry import bundle_drift


def test_committed_bundle_matches_a_fresh_rebuild() -> None:
    committed = json.loads(committed_bundle_path().read_text(encoding="utf-8"))
    fresh = build_document_schemas()
    drift = bundle_drift(fresh, committed)
    assert not drift, (
        "the committed served-document bundle is stale — regenerate with "
        "`tai42-contract-schemas --out core/contract/src/tai42_contract/schemas/contract-schema.json`; "
        "drift: " + "; ".join(drift)
    )


def test_version_only_difference_is_reported_as_a_version_mismatch() -> None:
    # A bundle whose only divergence is contract_version — the release-train case — must
    # be flagged as a version mismatch and NOT as any shape drift.
    fresh = build_document_schemas()
    committed = json.loads(json.dumps(fresh))
    committed["contract_version"] = "0.0.0-not-the-running-version"
    drift = bundle_drift(fresh, committed)
    assert any("contract_version" in line for line in drift)
    shape_words = ("changed shape", "is new", "was removed", "$defs")
    assert not any(word in line for line in drift for word in shape_words)


def test_shape_difference_is_reported_by_document_name() -> None:
    # A reshaped document is named, and with contract_version untouched no version
    # mismatch is reported.
    fresh = build_document_schemas()
    committed = json.loads(json.dumps(fresh))
    committed["documents"]["PresetBody"]["properties"]["_added_by_test"] = {"type": "string"}
    drift = bundle_drift(fresh, committed)
    assert any("PresetBody" in line and "changed shape" in line for line in drift)
    assert not any("contract_version" in line for line in drift)
