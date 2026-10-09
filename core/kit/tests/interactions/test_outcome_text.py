"""The text rendering of a failed-terminal outcome."""

from __future__ import annotations

import json

import pytest

from tai42_kit.interactions.outcome_text import failed_outcome_text


def test_failed_outcome_text_renders_the_whole_nested_outcome_as_json():
    # A TEXT delivery door renders the WHOLE failed outcome as JSON — every nested level present,
    # no key read out of it — so a text caller receives the driver's own graceful surface intact.
    outcome = {
        "status": "error",
        "result": {"on_error": [{"step": "retry", "note": "deep"}], "graceful": "DEEP_MARKER"},
    }
    text = failed_outcome_text(outcome)
    assert json.loads(text) == outcome
    assert "DEEP_MARKER" in text


def test_failed_outcome_text_keeps_non_ascii_as_itself():
    # Non-ASCII text survives as the real characters (``ensure_ascii=False``), never ``\uXXXX`` —
    # a rendered outcome must not mangle the driver's own words.
    outcome = {"detail": "retención fállida 日本語"}
    text = failed_outcome_text(outcome)
    assert "retención fállida 日本語" in text
    assert "\\u" not in text
    assert json.loads(text) == outcome


def test_failed_outcome_text_raises_loudly_on_an_unencodable_outcome():
    # An outcome holding a member no JSON encoder can render is surfaced LOUDLY — ``json.dumps``
    # raises — never dropped nor rewritten to get past it.
    outcome = {"bad": {1, 2, 3}}  # a set is not JSON-serializable
    with pytest.raises(TypeError):
        failed_outcome_text(outcome)
