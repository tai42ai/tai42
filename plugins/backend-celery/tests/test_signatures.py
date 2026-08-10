"""Signature helpers: option injection, **kwargs placement, and FastMCP-context
handling."""

from __future__ import annotations

import inspect
from typing import Any

from fastmcp import Context

from tai42_backend_celery.core.signatures import add_signature_params, exclude_fastmcp_ctx_from_kwargs


def plain_tool(a: int, b: str = "x") -> str:
    return f"{a}-{b}"


def kwargs_tool(a: int, **extra: Any) -> int:
    return a


def ctx_tool(a: int, ctx: Context | None = None) -> int:
    return a


_OPTS: dict[str, Any] = {"queue": str | None, "priority": int | None}


def test_options_are_appended_keyword_only() -> None:
    sig = add_signature_params(plain_tool, _OPTS)
    params = sig.parameters
    assert list(params) == ["a", "b", "queue", "priority"]
    assert params["queue"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["queue"].default is None


def test_options_go_before_a_trailing_var_keyword() -> None:
    sig = add_signature_params(kwargs_tool, _OPTS)
    assert list(sig.parameters) == ["a", "queue", "priority", "extra"]
    assert sig.parameters["extra"].kind is inspect.Parameter.VAR_KEYWORD


def test_exclude_fastmcp_ctx_reannotates_the_context_param() -> None:
    sig = add_signature_params(ctx_tool, _OPTS, exclude_fastmcp_ctx=True)
    assert sig.parameters["ctx"].annotation is Any
    assert list(sig.parameters) == ["a", "ctx", "queue", "priority"]


def test_exclude_fastmcp_ctx_from_kwargs_drops_only_the_context() -> None:
    assert exclude_fastmcp_ctx_from_kwargs(ctx_tool, {"a": 1, "ctx": object()}) == {"a": 1}
    assert exclude_fastmcp_ctx_from_kwargs(ctx_tool, {"a": 1}) == {"a": 1}
    assert exclude_fastmcp_ctx_from_kwargs(plain_tool, {"a": 1, "b": "y"}) == {"a": 1, "b": "y"}
