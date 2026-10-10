"""The ``tai42/*`` keys a registrant sets in a tool's ``meta``: a wire convention FastMCP stores, so pinned."""

from __future__ import annotations

from tai42_contract.tools import TOOL_META_BADGES, TOOL_META_CRASH_RESUME, TOOL_META_HIDDEN, TOOL_META_PAUSES


def test_tool_meta_key_values():
    assert TOOL_META_HIDDEN == "tai42/hidden"
    assert TOOL_META_BADGES == "tai42/badges"
    assert TOOL_META_CRASH_RESUME == "tai42/crash_resume"
    assert TOOL_META_PAUSES == "tai42/pauses"
