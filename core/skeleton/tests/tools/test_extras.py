"""The per-tool declared door-``extras`` registry."""

from __future__ import annotations

import pytest

from tai42_skeleton.tools.extras import ToolExtrasRegistry


def test_register_and_get():
    reg = ToolExtrasRegistry()
    assert reg.get("t") is None
    reg.register("t", frozenset({"warm_start"}))
    assert reg.get("t") == frozenset({"warm_start"})


def test_duplicate_registration_raises():
    reg = ToolExtrasRegistry()
    reg.register("t", frozenset({"a"}))
    with pytest.raises(ValueError, match="already registered"):
        reg.register("t", frozenset({"b"}))


def test_reset_clears():
    reg = ToolExtrasRegistry()
    reg.register("t", frozenset({"a"}))
    reg.reset()
    assert reg.get("t") is None


def test_tools_facet_extras_reads_the_ambient_frame():
    from tai42_contract.tools import tool_call_frame

    from tai42_skeleton.app.server import TaiMCP

    app = TaiMCP(name="extras")
    # Outside any door frame the ambient extras are empty.
    assert app.tools.extras() == {}
    with tool_call_frame("t", extras={"warm_start": {"node": "n1"}}):
        assert app.tools.extras() == {"warm_start": {"node": "n1"}}
    assert app.tools.extras() == {}
