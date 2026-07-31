"""The signature helpers behind the extension branch tools."""

from __future__ import annotations

import inspect
from typing import Any

from fastmcp import Context

from tai42_backend_rq.signatures import add_signature_params, exclude_fastmcp_ctx_from_kwargs


async def plain(x: int, note: str = "hi") -> str:
    return note


async def with_ctx(x: int, ctx: Context) -> int:
    return x


async def with_var_keyword(x: int, **extra: Any) -> int:
    return x


def test_add_params_appends_keyword_only_options():
    sig = add_signature_params(plain, {"countdown": int | None})
    params = sig.parameters
    assert list(params) == ["x", "note", "countdown"]
    assert params["countdown"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["countdown"].default is None


def test_add_params_widens_context_annotation():
    sig = add_signature_params(with_ctx, {"countdown": int | None}, exclude_fastmcp_ctx=True)
    assert sig.parameters["ctx"].annotation is Any
    assert "countdown" in sig.parameters


def test_add_params_keeps_var_keyword_last():
    sig = add_signature_params(with_var_keyword, {"countdown": int | None})
    assert list(sig.parameters) == ["x", "countdown", "extra"]
    assert sig.parameters["extra"].kind is inspect.Parameter.VAR_KEYWORD


def test_exclude_ctx_strips_only_the_context_kwarg():
    kwargs = exclude_fastmcp_ctx_from_kwargs(with_ctx, {"x": 1, "ctx": object()})
    assert kwargs == {"x": 1}


def test_exclude_ctx_no_context_is_a_no_op():
    kwargs = exclude_fastmcp_ctx_from_kwargs(plain, {"x": 1})
    assert kwargs == {"x": 1}
