"""Self-contained contract tests — the only suite. No application package, no
private dependency: a public clone runs these green with the `dev` extra.

They prove the contract is internally sound and stable: imports, runtime purity
(pydantic-only, with the one whitelisted behavioral member ``tai42_app``), protocol
shape, the facade partition against the frozen member surface, and the OS-clean
state (no tenant coupling in connectors, vendor-neutral monitoring, no `Nexus` brand)."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import TYPE_CHECKING, Any

import pydantic
import pytest
from _helpers import protocol_members  # stdlib-only helper, no application package

import tai42_contract

# The frozen facade surface: the 60 (sub-protocol, member) pairs over 57
# distinct flat names, grouped into the 19 sub-protocols. This is the
# contract's own source of truth — no external lookup needed. Three leaf names
# are shared: ``store`` (versioning + presets) and ``register``/``get``
# (webhook_verifiers + channels), so the distinct-name union (57) is three
# fewer than the pair count (60).
EXPECTED_FACADE = {
    # tools (11)
    "tool",
    "toolkit",
    "get_tool",
    "get_tools",
    "get_client_tools",
    "run_tool",
    "tool_title",
    "remove_tool",
    "register_tool_info",
    "unregister_tool_info",
    "unregister_tool_base",
    # agents (3)
    "agent",
    "get_agent",
    "all_agents",
    # backends (2)
    "register_backend",
    "backend",
    # storage (2)
    "register_storage",
    "resource_manager",
    # connectors (2)
    "register_connector",
    "token_store",
    # accounts (1)
    "active_provider",
    # webhook_verifiers (2)
    "register",
    "get",
    # channels (3) — ``register`` and ``get`` share their leaf names with
    # webhook_verifiers above; ``names`` is the one new distinct name
    "names",
    # conversations (2)
    "accept",
    "record_delivery_status",
    # monitoring (2)
    "register_monitoring",
    "active",
    # extensions (2)
    "extension",
    "available_extensions",
    # http (2)
    "middleware",
    "custom_route",
    # clients (2)
    "client_ctx",
    "shutdown_clients",
    # lifecycle (6)
    "on_startup",
    "on_shutdown",
    "on_reload",
    "on_post_swap",
    "on_fleet_op_applied",
    "wait_until_ready",
    # admin (9)
    "reload_mcp",
    "deregister_mcp",
    "reload_config",
    "tool_reloader",
    "run_tool_reload",
    "reload_failed_mcps",
    "list_failed_mcps",
    "live_mcp_status",
    "live_manifest",
    # config (1)
    "config_manager",
    # backup (4)
    "register_section",
    "sections",
    "export_section",
    "import_section",
    # sub_app (1)
    "mcp_sub_app_router",
    # versioning (1)
    "store",
    # presets (2) — `store` shared with versioning above
    "bind",
}


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


def test_declared_route_metadata_reexported():
    # The public imports the skeleton codes against (route registry + http facet).
    from tai42_contract import DeclaredRouteMetadata as TopLevel
    from tai42_contract.app import DeclaredRouteMetadata as FromApp
    from tai42_contract.app.facets import DeclaredRouteMetadata as Source

    assert TopLevel is Source
    assert FromApp is Source


def test_facade_partition_against_frozen_surface():
    from tai42_contract.app import (
        AppAccounts,
        AppAdmin,
        AppAgents,
        AppBackends,
        AppBackup,
        AppChannels,
        AppClients,
        AppConfig,
        AppConnectors,
        AppConversations,
        AppExtensions,
        AppHttp,
        AppLifecycle,
        AppMonitoring,
        AppPresets,
        AppStorage,
        AppSubApp,
        AppTools,
        AppVersioning,
        AppWebhookVerifiers,
    )

    subs = [
        AppTools,
        AppAgents,
        AppBackends,
        AppStorage,
        AppConnectors,
        AppAccounts,
        AppWebhookVerifiers,
        AppChannels,
        AppConversations,
        AppMonitoring,
        AppExtensions,
        AppHttp,
        AppClients,
        AppLifecycle,
        AppAdmin,
        AppConfig,
        AppBackup,
        AppSubApp,
        AppVersioning,
        AppPresets,
    ]
    union: set[str] = set()
    total = 0
    for sub in subs:
        members = protocol_members(sub)
        union |= members
        total += len(members)
    assert union == EXPECTED_FACADE, (
        f"only-facade={sorted(union - EXPECTED_FACADE)} only-frozen={sorted(EXPECTED_FACADE - union)}"
    )
    # 60 (sub-protocol, member) pairs over 57 distinct names — ``store`` is
    # exposed by both AppVersioning and AppPresets, and ``register``/``get``
    # by both AppWebhookVerifiers and AppChannels.
    assert len(union) == 57, f"union={len(union)}"
    assert total == 60 == len(union) + 3, f"partition broken: sum={total} union={len(union)}"


def test_taiapp_exposes_twenty_namespaces():
    from tai42_contract.app import TaiApp

    assert protocol_members(TaiApp) == {
        "tools",
        "agents",
        "backends",
        "storage",
        "connectors",
        "accounts",
        "webhook_verifiers",
        "channels",
        "conversations",
        "monitoring",
        "extensions",
        "http",
        "clients",
        "lifecycle",
        "admin",
        "config",
        "backup",
        "sub_app",
        "versioning",
        "presets",
    }


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


def test_backend_surface_is_task_runtime_only():
    # The Backend ABC is a task-execution contract: ``launch`` is its ONLY
    # abstract member, carrying no fleet control-plane surface — a concrete
    # backend implementing only ``launch`` instantiates cleanly.
    from collections.abc import Sequence

    from tai42_contract.backend import Backend

    assert Backend.__abstractmethods__ == frozenset({"launch"})

    class TaskOnlyBackend(Backend):
        async def launch(self, args: Sequence[str]) -> None: ...

    TaskOnlyBackend()  # implementing only ``launch`` satisfies the ABC


def test_config_manager_both_transaction_seams_required():
    # A ConfigManager implementing BOTH new seams (plus the existing abstracts)
    # satisfies the ABC. ``mutate_manifest`` is a whole-span transaction: a
    # mutator that raises persists nothing; ``replace_manifest`` deletes keys
    # absent from the new document.
    from tai42_contract.config import ConfigManager

    assert {"mutate_manifest", "replace_manifest", "replace_env"} <= ConfigManager.__abstractmethods__

    class FakeConfigManager(ConfigManager):
        def __init__(self) -> None:
            self._manifest: dict[str, Any] = {"a": 1, "b": 2}
            self._env: dict[str, str] = {}

        def read_env(self) -> dict[str, str]:
            return dict(self._env)

        def write_env(self, config: dict[str, str]) -> None:
            self._env.update(config)

        def replace_env(self, config: dict[str, str]) -> None:
            self._env = {k: v for k, v in config.items() if v != ""}

        def read_manifest(self) -> dict[str, Any]:
            return dict(self._manifest)

        def read_manifest_preserved(self) -> dict[str, Any]:
            return dict(self._manifest)

        def read_defaults_manifest(self) -> dict[str, Any]:
            return {}

        def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
            working = dict(self._manifest)
            mutator(working)  # a raise here aborts before the commit below
            self._manifest = working
            return dict(self._manifest)

        def replace_manifest(self, document: dict[str, Any]) -> dict[str, Any]:
            self._manifest = dict(document)
            return dict(self._manifest)

    mgr = FakeConfigManager()  # both seams present → concrete, instantiable

    def _boom(doc: dict[str, Any]) -> None:
        doc["a"] = 99
        raise ValueError("mutator failed")

    with pytest.raises(ValueError, match="mutator failed"):
        mgr.mutate_manifest(_boom)
    assert mgr.read_manifest() == {"a": 1, "b": 2}  # nothing persisted on abort

    result = mgr.mutate_manifest(lambda doc: doc.update({"a": 10}))
    assert result == {"a": 10, "b": 2}

    replaced = mgr.replace_manifest({"a": 5})
    assert replaced == {"a": 5}  # ``b`` absent from the document → deleted

    # ``replace_env`` is a whole-map replace: a key present before but absent from the
    # new map is deleted, and an empty value is filtered out.
    mgr.write_env({"KEEP": "1", "DROP": "2"})
    mgr.replace_env({"KEEP": "1", "BLANK": ""})
    assert mgr.read_env() == {"KEEP": "1"}  # DROP deleted (absent), BLANK filtered


def test_app_lifecycle_accepts_one_arg_fleet_op_handler():
    # AppLifecycle gains ``on_fleet_op_applied`` alongside the zero-arg
    # siblings; its handler takes ONE argument (the op name). A registrar
    # exposing all four members conforms to the runtime-checkable protocol.
    from tai42_contract.app import AppLifecycle

    assert "on_fleet_op_applied" in protocol_members(AppLifecycle)
    sig = inspect.signature(AppLifecycle.on_fleet_op_applied)
    assert "func" in sig.parameters
    # The fleet-op handler takes ONE argument (the op name) — the deliberate
    # deviation from the zero-arg siblings. Pin the arity so a regression that
    # flips the annotation back to ``Callable[[], Any]`` fails here.
    assert sig.parameters["func"].annotation == "Callable[[str], Any]"
    sibling_sig = inspect.signature(AppLifecycle.on_startup)
    assert sibling_sig.parameters["func"].annotation == "Callable[[], Any]"

    class Registrar:
        def on_startup(self, func: object) -> object:
            return func

        def on_shutdown(self, func: object) -> object:
            return func

        def on_reload(self, func: object) -> object:
            return func

        def on_post_swap(self, func: object) -> object:
            return func

        def on_fleet_op_applied(self, func: object) -> object:
            return func

        async def wait_until_ready(self) -> None:
            return None

    assert isinstance(Registrar(), AppLifecycle)

    class MissingHook:
        def on_startup(self, func: object) -> object:
            return func

        def on_shutdown(self, func: object) -> object:
            return func

        def on_reload(self, func: object) -> object:
            return func

    assert not isinstance(MissingHook(), AppLifecycle)


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


def test_custom_route_carries_self_describing_metadata():
    # custom_route registers a route AND its OpenAPI metadata: summary + tags +
    # response_model are keyword-only REQUIRED (no default, so a route cannot
    # silently omit them); request_model + authed are keyword-only with defaults.
    from tai42_contract.app import AppHttp

    sig = inspect.signature(AppHttp.custom_route)
    for name in ("summary", "tags", "response_model", "request_model", "authed"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, f"{name} must be keyword-only"
    empty = inspect.Parameter.empty
    assert sig.parameters["summary"].default is empty
    assert sig.parameters["tags"].default is empty
    assert sig.parameters["response_model"].default is empty
    assert sig.parameters["request_model"].default is None
    assert sig.parameters["authed"].default is True


def test_extension_registration_carries_requires_body_locality():
    # The registration surface stores the body-locality marker the apply site
    # reads to order stacked combos: keyword-only, defaulting to False so an
    # ordinary extension registers unchanged.
    from tai42_contract.app import AppExtensions

    sig = inspect.signature(AppExtensions.extension)
    param = sig.parameters["requires_body_locality"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is False


def test_set_trace_metadata_has_no_client_name():
    from tai42_contract.monitoring import Span

    params = inspect.signature(Span.set_trace_metadata).parameters
    assert "client_name" not in params
    assert {"name", "tags"} <= set(params)


def test_taimcpconfig_present_and_no_nexus_brand():
    from tai42_contract.manifest import TaiMCPConfig

    assert issubclass(TaiMCPConfig, pydantic.BaseModel)
    # no `Nexus`-branded symbol leaks from the manifest namespace
    import tai42_contract.manifest as manifest

    assert not [n for n in dir(manifest) if "Nexus" in n]


def test_monitoring_get_trace_return_is_non_optional():
    # get_trace never returns None: it returns a trace or raises. The resolved
    # return hint is the bare MonitoringTrace, not MonitoringTrace | None.
    import typing

    from tai42_contract.monitoring.models import MonitoringTrace
    from tai42_contract.monitoring.reader import MonitoringReader

    hints = typing.get_type_hints(MonitoringReader.get_trace)
    assert hints["return"] is MonitoringTrace


# -- Static typing guarantees (checked by pyright, not exercised at runtime) ----

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import assert_type

    from tai42_contract import tai42_app
    from tai42_contract.app import TaiApp
    from tai42_contract.tools import AppTools

    def _typing_probes(app: TaiApp, tools: AppTools, fn: Callable[[int], str]) -> None:  # pyright: ignore[reportUnusedFunction]
        # Item 7: the exported tai42_app carries the protocol types, not Any.
        assert_type(tai42_app.tools, AppTools)
        assert_type(app.tools, AppTools)
        # Item 6: tool/toolkit preserve the decorated callable's type.
        assert_type(tools.tool(fn), Callable[[int], str])
        assert_type(tools.tool(force=True)(fn), Callable[[int], str])
        assert_type(tools.toolkit(fn), Callable[[int], str])
