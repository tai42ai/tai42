"""Self-contained contract tests — the only suite. No application package, no
private dependency: a public clone runs these green with the `dev` extra.

They prove the contract is internally sound and stable: imports, runtime purity
(pydantic-only, with the one whitelisted behavioral member ``tai42_app``), protocol
shape, and the OS-clean state (no tenant coupling in connectors, vendor-neutral
monitoring, no `Nexus` brand). The app-facade partition lives beside the facets in
``tests/app/test_facets.py`` and the monitoring models in ``tests/monitoring/test_models.py``."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import TYPE_CHECKING

import pydantic
import pytest

import tai42_contract


def _all_contract_models() -> list[type[pydantic.BaseModel]]:
    out: list[type[pydantic.BaseModel]] = []
    for m in pkgutil.walk_packages(tai42_contract.__path__, "tai42_contract."):
        mod = importlib.import_module(m.name)
        for obj in vars(mod).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, pydantic.BaseModel)
                and obj.__module__.startswith("tai42_contract")
            ):
                out.append(obj)
    return out


def test_every_module_imports():
    names = [m.name for m in pkgutil.walk_packages(tai42_contract.__path__, "tai42_contract.")]
    for name in names:
        importlib.import_module(name)
    assert len(names) >= 30


def test_runtime_purity_models_rebuild():
    # `from __future__ import annotations` defers field resolution; a vendor type
    # smuggled into a model field would only raise here, on rebuild. (Vendor libs
    # may be installed for pyright, but no model FIELD references one.)
    models = _all_contract_models()
    for model in models:
        model.model_rebuild()
    assert len(models) > 100


@pytest.mark.parametrize(
    ("dotted", "expected"),
    [
        ("tai42_contract.extensions.ExtensionKind", {"WRAPPER", "TRANSFORMER", "BACKEND"}),
        ("tai42_contract.connectors.models.AuthHealthState", {"HEALTHY", "RECONNECT_REQUIRED", "REFRESH_FAILING"}),
        ("tai42_contract.monitoring.SpanKind", {"CHAIN", "EVENT", "LLM", "TOOL"}),
        ("tai42_contract.monitoring.MonitoringLevel", {"DEBUG", "DEFAULT", "WARNING", "ERROR"}),
        ("tai42_contract.monitoring.MetricsView", {"OBSERVATIONS", "TRACES"}),
    ],
)
def test_enums_have_expected_members(dotted: str, expected: set[str]):
    mod, name = dotted.rsplit(".", 1)
    enum_cls = getattr(importlib.import_module(mod), name)
    assert {m.name for m in enum_cls} == expected


def test_abc_contracts_are_abstract():
    # The ABC contracts must be genuinely abstract (non-instantiable, with abstract
    # methods), not bare classes.
    from tai42_contract.agent import Agent
    from tai42_contract.backend import Backend
    from tai42_contract.connectors import ConnectorTokenStore
    from tai42_contract.storage import Storage

    for abc_cls in (Agent, Backend, Storage, ConnectorTokenStore):
        assert getattr(abc_cls, "__abstractmethods__", frozenset[str]()), f"{abc_cls.__name__} has no abstract methods"
        with pytest.raises(TypeError):
            abc_cls()  # abstract → not instantiable  # pyright: ignore[reportAbstractUsage]

    # ``launch`` is the Backend contract's one abstract member — the task
    # runtime — pinned by name.
    assert "launch" in Backend.__abstractmethods__


def test_toolinfo_is_a_model_and_constructs():
    from tai42_contract.tools import ToolInfo

    assert issubclass(ToolInfo, pydantic.BaseModel)
    ti = ToolInfo(name="x")
    assert ti.name == "x"
    assert ti.base == ""


def test_runtime_checkable_protocols_accept_and_reject():
    from tai42_contract.transport import Transport

    class Conforms:
        def connect_session(self, **kw: object): ...

    class Missing:
        pass

    # @runtime_checkable: isinstance must accept a structural match and reject a miss.
    assert isinstance(Conforms(), Transport)
    assert not isinstance(Missing(), Transport)


# -- Purity gate: pydantic-only runtime, tai42_app the sole behavioral member -----


def test_contract_imports_no_tai_package():
    # The contract must not import any tai-* package at runtime (it is the root of
    # the dependency graph). Scan every module's source for a `tai_` import that is
    # not `tai42_contract` itself.
    import ast
    from pathlib import Path

    root = next(iter(tai42_contract.__path__))
    offenders: list[str] = []
    for mod in pkgutil.walk_packages(tai42_contract.__path__, "tai42_contract."):
        spec = importlib.import_module(mod.name).__spec__
        assert spec
        assert spec.origin
        tree = ast.parse(Path(spec.origin).read_text())
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module
                else []
            )
            for name in names:
                top = name.split(".")[0]
                if top.startswith("tai42_") and top != "tai42_contract":
                    offenders.append(f"{mod.name}: {name}")
    assert not offenders, offenders
    assert root  # sanity


def test_tai_app_is_the_only_behavioral_member():
    # Purity: every contract member is a pydantic model / Protocol / ABC / enum /
    # plain function-free interface — EXCEPT the whitelisted `tai42_app` handle, which
    # is the sole object carrying forwarding behavior + mutable state.
    from tai42_contract.app.handle import _TaiAppHandle, tai42_app  # pyright: ignore[reportPrivateUsage]

    assert isinstance(tai42_app, _TaiAppHandle)
    # The handle forwards; it is not a pydantic model / Protocol / ABC.
    assert not isinstance(tai42_app, pydantic.BaseModel)


def test_tai_app_raises_before_bind_and_forwards_after():
    # A fresh handle (not the module singleton) raises loudly on any access before
    # bind, then forwards every attribute to the injected impl after bind.
    from tai42_contract.app.handle import _TaiAppHandle  # pyright: ignore[reportPrivateUsage]

    handle = _TaiAppHandle()
    # AttributeError (not RuntimeError) so the attribute protocol stays intact:
    # hasattr sees absence instead of crashing, while the message is still loud.
    with pytest.raises(AttributeError, match="accessed before bind"):
        _ = handle.anything
    # hasattr must report absence (not crash) — the protocol the fix restores.
    assert hasattr(handle, "bind")
    assert not hasattr(handle, "anything")

    class Impl:
        tools = "T"

        def update(self) -> str:
            return "ok"

    handle.bind(Impl())
    assert handle.tools == "T"
    assert handle.update() == "ok"


def test_tai_app_scoped_bind_restores_its_predecessor():
    # ``bound`` restores exactly what it replaced, nesting included.
    from tai42_contract.app.handle import _TaiAppHandle  # pyright: ignore[reportPrivateUsage]

    class Impl:
        def __init__(self, tag: str) -> None:
            self.tag = tag

    handle = _TaiAppHandle()
    outer = Impl("outer")
    handle.bind(outer)

    with handle.bound(Impl("inner")):
        assert handle.tag == "inner"
        with handle.bound(Impl("innermost")):
            assert handle.tag == "innermost"
        assert handle.tag == "inner"
    assert handle.tag == "outer"

    # A scope on an unbound handle restores the unbound state, raise or not.
    fresh = _TaiAppHandle()

    def _raise_inside_scope() -> None:
        with fresh.bound(Impl("scoped")):
            assert fresh.tag == "scoped"
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _raise_inside_scope()
    with pytest.raises(AttributeError, match="accessed before bind"):
        _ = fresh.tag


# -- BaseClient poolable-client Protocol ---------------------------------------


def test_base_client_protocol_runtime_checkable():
    from tai42_contract.clients import BaseClient

    class Pooled:
        def current(self, **kwargs: object): ...

        async def close(self, **kwargs: object): ...

    class Missing:
        pass

    assert isinstance(Pooled(), BaseClient)
    assert not isinstance(Missing(), BaseClient)


# -- OS-clean state: no tenant coupling + vendor-neutral monitoring ------------


def test_connector_token_store_put_exposes_compare_and_set():
    # put offers an atomic compare-and-set: a keyword-only `expected_blob` guard,
    # orthogonal to `create_only`, and a bool return (committed vs. CAS-miss) so a
    # caller that loses a cross-replica race learns it lost instead of clobbering.
    from tai42_contract.connectors import ConnectorTokenStore

    sig = inspect.signature(ConnectorTokenStore.put)
    for name in ("create_only", "expected_blob"):
        param = sig.parameters[name]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, f"{name} must be keyword-only"
    assert sig.parameters["expected_blob"].default is None
    assert sig.return_annotation in ("bool", bool)


def test_taimcpconfig_present_and_no_nexus_brand():
    from tai42_contract.manifest import TaiMCPConfig

    assert issubclass(TaiMCPConfig, pydantic.BaseModel)
    # no `Nexus`-branded symbol leaks from the manifest namespace
    import tai42_contract.manifest as manifest

    assert not [n for n in dir(manifest) if "Nexus" in n]


# -- Static typing guarantees (checked by pyright, not exercised at runtime) ----

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import assert_type

    from tai42_contract import tai42_app
    from tai42_contract.app import TaiApp
    from tai42_contract.tools import AppTools

    def _typing_probes(app: TaiApp, tools: AppTools, fn: Callable[[int], str]) -> None:  # pyright: ignore[reportUnusedFunction]
        # The exported tai42_app carries the protocol types, not Any.
        assert_type(tai42_app.tools, AppTools)
        assert_type(app.tools, AppTools)
        # tool/toolkit preserve the decorated callable's type.
        assert_type(tools.tool(fn), Callable[[int], str])
        assert_type(tools.tool(force=True)(fn), Callable[[int], str])
        assert_type(tools.toolkit(fn), Callable[[int], str])
