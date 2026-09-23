"""The MCP ``tools/call`` edge reads the caller's subject off the request ``_meta["tai42/subject"]``.

``extras``/``continues_chain`` and the subject ride in-process or in request ``_meta`` — never as a
tool argument. The client's request ``_meta`` arrives on the middleware's request context
(``fastmcp_context.request_context.meta``), not on the ``CallToolRequestParams`` message, so these
drive the real middleware context shape: a well-formed subject validates, an absent one is ``None``,
and a malformed one raises loudly. The ``CallToolRequestParams`` message carries no ``_meta`` —
reading it there would silently drop every caller-named subject.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import mcp.types as mcp_types
import pytest
from fastmcp import FastMCP
from fastmcp.server.context import Context
from fastmcp.server.middleware.middleware import MiddlewareContext
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from tai42_contract.states import StateSubject

from tai42_skeleton.tools.dispatch_scope import _mcp_call_subject

_SERVER = FastMCP("test-mcp-subject")


def _mw_context(meta: dict | None) -> Iterator[MiddlewareContext]:
    """A real ``on_call_tool`` middleware context whose request ``_meta`` carries ``meta``.

    The subject rides the request context's ``meta`` (extra fields), exactly where the FastMCP
    transport lands a client's ``_meta`` — the bare ``CallToolRequestParams`` message carries none.
    """
    request_context = RequestContext(
        request_id=1,
        meta=mcp_types.RequestParams.Meta.model_validate(meta) if meta is not None else None,
        # The subject read never touches the session or lifespan context; only the request meta.
        session=cast(Any, None),
        lifespan_context=None,
    )
    token = request_ctx.set(request_context)
    try:
        yield MiddlewareContext(
            message=mcp_types.CallToolRequestParams.model_validate({"name": "x", "arguments": {}}),
            fastmcp_context=Context(_SERVER),
        )
    finally:
        request_ctx.reset(token)


def _subject_from_meta(meta: dict | None) -> StateSubject | None:
    for context in _mw_context(meta):
        return _mcp_call_subject(context)
    raise AssertionError  # the generator always yields once


def test_reads_a_wellformed_subject_from_request_meta() -> None:
    subject = _subject_from_meta(
        {"tai42/subject": {"target_kind": "tool", "target_name": "n", "kind": "thread", "key": "k"}}
    )
    assert subject == StateSubject(target_kind="tool", target_name="n", kind="thread", key="k")


def test_none_when_no_subject_named() -> None:
    assert _subject_from_meta(None) is None
    assert _subject_from_meta({"progressToken": "p"}) is None


def test_a_malformed_subject_raises_loudly() -> None:
    with pytest.raises(ValueError):  # noqa: PT011 — a pydantic ValidationError is a ValueError subclass
        _subject_from_meta({"tai42/subject": {"target_kind": "tool"}})
