import inspect
from typing import Any

from fastmcp import Context

from tai42_kit.utils.lc.signature_util import (
    add_signature_params,
    exclude_fastmcp_ctx_from_kwargs,
)


class TestExcludeFastmcpCtxFromKwargs:
    def test_ctx_param_removed(self):
        def tool(a: int, ctx: Context): ...

        out = exclude_fastmcp_ctx_from_kwargs(tool, {"a": 1, "ctx": "live-ctx"})
        assert out == {"a": 1}

    def test_no_ctx_param_unchanged(self):
        def tool(a: int, b: str): ...

        out = exclude_fastmcp_ctx_from_kwargs(tool, {"a": 1, "b": "x"})
        assert out == {"a": 1, "b": "x"}

    def test_ctx_param_present_but_not_in_args(self):
        def tool(a: int, ctx: Context): ...

        out = exclude_fastmcp_ctx_from_kwargs(tool, {"a": 1})
        assert out == {"a": 1}


class TestAddSignatureParams:
    def test_additional_params_appended_keyword_only(self):
        def tool(a: int): ...

        sig = add_signature_params(tool, {"extra": str})
        assert "extra" in sig.parameters
        extra = sig.parameters["extra"]
        assert extra.kind == inspect.Parameter.KEYWORD_ONLY
        assert extra.annotation is str
        assert extra.default is None

    def test_added_params_inserted_before_trailing_var_keyword(self):
        # A func ending in **kwargs must not blow up: the keyword-only params are
        # inserted before the VAR_KEYWORD, which stays last in the signature.
        def tool(a: int, **kwargs): ...

        sig = add_signature_params(tool, {"extra": str})
        names = list(sig.parameters)
        assert names == ["a", "extra", "kwargs"]
        assert sig.parameters["extra"].kind == inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["kwargs"].kind == inspect.Parameter.VAR_KEYWORD

    def test_ctx_annotation_replaced_with_any_when_excluded(self):
        def tool(a: int, ctx: Context): ...

        sig = add_signature_params(tool, {}, exclude_fastmcp_ctx=True)
        assert sig.parameters["ctx"].annotation is Any

    def test_ctx_annotation_preserved_when_not_excluded(self):
        def tool(a: int, ctx: Context): ...

        sig = add_signature_params(tool, {}, exclude_fastmcp_ctx=False)
        assert sig.parameters["ctx"].annotation is Context
