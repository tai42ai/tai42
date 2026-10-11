"""The one lowercase path-segment slug law the contract owns, and the hook fields that read it."""

from __future__ import annotations

import pytest

from tai42_contract.plugins import PATH_SEGMENT_SLUG_RE
from tai42_contract.plugins.routes import PATH_SEGMENT_SLUG_RE as SOURCE_RE


def test_reexported_from_the_plugins_package():
    assert PATH_SEGMENT_SLUG_RE is SOURCE_RE


@pytest.mark.parametrize("value", ["a", "0abc", "events", "on-event", "hook_42"])
def test_accepts_one_lowercase_segment(value: str):
    assert PATH_SEGMENT_SLUG_RE.match(value)


@pytest.mark.parametrize("value", ["", "a/b", "Events", "on event", "a.b", "-lead", "_lead", "good\n", "café"])
def test_refuses_anything_else(value: str):
    assert PATH_SEGMENT_SLUG_RE.match(value) is None


def test_hook_name_and_topic_read_the_contract_law():
    from tai42_contract.hooks import models

    assert models.PATH_SEGMENT_SLUG_RE is PATH_SEGMENT_SLUG_RE
