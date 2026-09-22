"""The MCP ``tools/call`` edge reads the caller's subject off ``_meta["tai42/subject"]``.

``extras``/``continues_chain`` and the subject ride in-process or in request ``_meta`` — never as a
tool argument. This pins the ``_meta`` read: a well-formed subject validates, an absent one is
``None``, and a malformed one raises loudly.
"""

from __future__ import annotations

import mcp.types as mcp_types
import pytest
from tai42_contract.states import StateSubject

from tai42_skeleton.tools.dispatch_scope import _mcp_call_subject


def _message(meta: dict | None) -> mcp_types.CallToolRequestParams:
    return mcp_types.CallToolRequestParams.model_validate(
        {"name": "x", "arguments": {}, **({"_meta": meta} if meta is not None else {})}
    )


def test_reads_a_wellformed_subject_from_meta() -> None:
    subject = _mcp_call_subject(
        _message({"tai42/subject": {"target_kind": "tool", "target_name": "n", "kind": "thread", "key": "k"}})
    )
    assert subject == StateSubject(target_kind="tool", target_name="n", kind="thread", key="k")


def test_none_when_no_subject_named() -> None:
    assert _mcp_call_subject(_message(None)) is None
    assert _mcp_call_subject(_message({"progressToken": "p"})) is None


def test_a_malformed_subject_raises_loudly() -> None:
    with pytest.raises(ValueError):  # noqa: PT011 — a pydantic ValidationError is a ValueError subclass
        _mcp_call_subject(_message({"tai42/subject": {"target_kind": "tool"}}))
