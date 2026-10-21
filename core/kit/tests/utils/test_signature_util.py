import inspect
import sys
import types
from typing import Any

import pytest
from fastmcp import Context

from tai42_kit.utils.lc.signature_util import (
    add_signature_params,
    exclude_fastmcp_ctx_from_kwargs,
    resolved_type_hints,
)

_POSTPONED_SOURCE = """
from __future__ import annotations

from typing import Any


def keep(payload: dict[str, Any]) -> dict[str, Any]:
    return payload


def broken(payload: Missing) -> None:
    return None
"""


@pytest.fixture
def postponed_module(monkeypatch):
    """A loaded tool module under postponed annotations: ``keep``'s hints are strings
    naming ``Any``, which only this module's globals hold; ``broken``'s name nothing."""
    module = types.ModuleType("postponed_tool_module")
    exec(_POSTPONED_SOURCE, vars(module))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


class TestResolvedTypeHints:
    def test_plain_function_resolves_in_its_module(self, postponed_module):
        hints = resolved_type_hints(postponed_module.keep)

        assert hints == {"payload": dict[str, Any], "return": dict[str, Any]}

    def test_branch_resolves_in_the_module_it_names(self, postponed_module):
        # The shape a makefun extension branch has: ``keep``'s string hints, code compiled
        # in a namespace that holds no ``Any``, and ``__module__`` naming ``keep``'s module.
        namespace: dict[str, Any] = {}
        exec("def keep_branch(payload: 'dict[str, Any]') -> 'dict[str, Any]': ...", namespace)
        branch = namespace["keep_branch"]
        branch.__module__ = postponed_module.keep.__module__

        hints = resolved_type_hints(branch)

        assert hints == {"payload": dict[str, Any], "return": dict[str, Any]}

    def test_function_of_an_unloaded_module_resolves_in_its_own_globals(self, postponed_module):
        keep = postponed_module.keep
        keep.__module__ = "not_a_loaded_module"

        assert resolved_type_hints(keep) == {"payload": dict[str, Any], "return": dict[str, Any]}

    def test_unresolvable_hint_raises(self, postponed_module):
        with pytest.raises(NameError, match="Missing"):
            resolved_type_hints(postponed_module.broken)


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
