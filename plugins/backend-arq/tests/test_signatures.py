"""Signature helpers: keyword-only option injection and FastMCP-context handling."""

from __future__ import annotations

import inspect
from typing import Any

from fastmcp import Context

from tai42_backend_arq.signatures import add_signature_params, exclude_fastmcp_ctx_from_kwargs


async def _tool_with_ctx(text: str, ctx: Context, **extra: Any) -> str:
    return text


async def _plain_tool(text: str) -> str:
    return text


def test_added_params_go_before_var_keyword_and_ctx_annotation_loosens() -> None:
    sig = add_signature_params(_tool_with_ctx, {"countdown": int | None}, exclude_fastmcp_ctx=True)
    names = list(sig.parameters)
    assert names == ["text", "ctx", "countdown", "extra"]
    added = sig.parameters["countdown"]
    assert added.kind is inspect.Parameter.KEYWORD_ONLY
    assert added.default is None
    # The request-scoped Context type must not leak into the presented schema.
    assert sig.parameters["ctx"].annotation is Any


def test_ctx_annotation_kept_without_exclude() -> None:
    # Deferred annotations leave the parameter annotation as its source text.
    sig = add_signature_params(_tool_with_ctx, {"countdown": int | None})
    assert sig.parameters["ctx"].annotation == "Context"


def test_added_params_append_without_var_keyword() -> None:
    sig = add_signature_params(_plain_tool, {"expires": str | float | None})
    assert list(sig.parameters) == ["text", "expires"]


def test_exclude_fastmcp_ctx_from_kwargs() -> None:
    assert exclude_fastmcp_ctx_from_kwargs(_tool_with_ctx, {"text": "hi", "ctx": object()}) == {"text": "hi"}
    # No context parameter / not supplied: kwargs pass through unchanged.
    assert exclude_fastmcp_ctx_from_kwargs(_plain_tool, {"text": "hi"}) == {"text": "hi"}
    assert exclude_fastmcp_ctx_from_kwargs(_tool_with_ctx, {"text": "hi"}) == {"text": "hi"}
