"""Tests for the app-facade composition — the per-feature sub-protocols partitioned into
``tai42_contract.app.facets`` and re-assembled into ``TaiApp``."""

from __future__ import annotations

import inspect
import typing
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

_PROTOCOL_SCAFFOLDING = {
    "_is_protocol",
    "_is_runtime_protocol",
    "__protocol_attrs__",
    "__init__",
    "__subclasshook__",
    "__class_getitem__",
}


def protocol_members(proto: type) -> set[str]:
    """Public members of a ``typing.Protocol`` (dunders + scaffolding stripped)."""
    members = set(typing.get_protocol_members(proto))
    return {m for m in members if m not in _PROTOCOL_SCAFFOLDING and not m.startswith("__")}


# The frozen facade surface: the 137 (sub-protocol, member) pairs over 133
# distinct flat names. This is the
# contract's own source of truth — no external lookup needed. Two leaf names
# are shared: ``store`` (versioning + presets + tool_meta) and ``register``/``get``
# (webhook_verifiers + channels), so the distinct-name union is four
# fewer than the pair count.
EXPECTED_FACADE = {
    # tools (16)
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
    "register_rename_referee",
    "register_delete_referee",
    "register_detach_referee",
    "tool_refs_extractor",
    "register_tier",
    "tier",
    "extras",
    "declared_extras",
    # agents (3)
    "agent",
    "get_agent",
    "all_agents",
    # backends (2)
    "register_backend",
    "backend",
    # sandboxes (4)
    "register_sandbox",
    "sandbox",
    "require_sandbox",
    "sandbox_policy",
    # storage (2)
    "register_storage",
    "resource_manager",
    # connectors (3)
    "register_connector",
    "token_store",
    "resolve_connection_auth",
    # accounts (1)
    "active_provider",
    # webhook_verifiers (2)
    "register",
    "get",
    # channels (5) — ``register`` and ``get`` share their leaf names with
    # webhook_verifiers above; ``names``, ``handle_inbound_answer`` (the shared
    # inbound-answer ladder) and ``record_send_receipt`` (the tier-2 send
    # delivery-receipt seam) channel plugins reach through the contract are distinct
    "names",
    "handle_inbound_answer",
    "record_send_receipt",
    # conversations (4)
    "accept",
    "pending_messages",
    "record_delivery_status",
    "register_target_validator",
    # monitoring (2)
    "register_monitoring",
    "active",
    # sandboxes and interactions expose the facade seams a plugin reads without
    # importing the skeleton; ``ask`` and ``check_answer`` are the interactions facet's members,
    # ``visit`` and the generic ``list_parked``/``resume_parked``/``cancel_parked`` drive parked
    # runs, and the platform's resume/delivery authorization + redelivery-horizon facets a driver reaches.
    "ask",
    "check_answer",
    "visit",
    "park_answer",
    "normalise_started",
    "list_parked",
    "list_parked_for",
    "resume_parked",
    "cancel_parked",
    "current_fire_identity",
    "bound_execution_identity_for_fire",
    "assert_resume_authorized",
    "assert_delivery_authorized",
    "redelivery_horizon_seconds",
    # extensions (2)
    "extension",
    "available_extensions",
    # http (4)
    "middleware",
    "custom_route",
    "mount_base",
    "use_raw_path_key",
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
    # presets (10) — `store` shared with versioning above
    "bind",
    "create",
    "save_version",
    "register_write_validator",
    "register_seed",
    "register_input_schema_support",
    "input_schema_support",
    "register_registration_tier",
    "registration_tier",
    "get_active_versioned_body",
    "used_by",
    # tool_meta (2) — `store` shared with versioning and presets above
    "patch",
    # states (29)
    "list_declarations",
    "get_declaration",
    "put_declaration",
    "delete_declaration",
    "stats",
    "list_templates",
    "get_template",
    "put_template",
    "delete_template",
    "list_attachments",
    "attach",
    "update_attachment_declarations",
    "detach",
    "read",
    "replace",
    "merge",
    "apply",
    "apply_batch",
    "eval_template_jq",
    "apply_template_jq",
    "erase",
    "fold",
    "list_subjects",
    "search",
    "writes",
    "prune_expired",
    "context",
    "register_consumer_lister",
    "consumers",
    "register_template_seed",
    "register_attach_validator",
    "register_attach_reconciler",
}


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
        AppInteractions,
        AppLifecycle,
        AppMonitoring,
        AppPresets,
        AppSandboxes,
        AppStates,
        AppStorage,
        AppSubApp,
        AppToolMeta,
        AppTools,
        AppVersioning,
        AppWebhookVerifiers,
    )

    subs = [
        AppTools,
        AppAgents,
        AppBackends,
        AppSandboxes,
        AppStorage,
        AppConnectors,
        AppAccounts,
        AppWebhookVerifiers,
        AppChannels,
        AppConversations,
        AppMonitoring,
        AppExtensions,
        AppInteractions,
        AppHttp,
        AppClients,
        AppLifecycle,
        AppAdmin,
        AppConfig,
        AppBackup,
        AppSubApp,
        AppVersioning,
        AppPresets,
        AppToolMeta,
        AppStates,
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
    # 137 (sub-protocol, member) pairs over 133 distinct names — ``store`` is exposed
    # by AppVersioning, AppPresets and AppToolMeta (two duplicate pairs), and
    # ``register``/``get`` by both AppWebhookVerifiers and AppChannels (one each).
    assert len(union) == 133, f"union={len(union)}"
    assert total == 137 == len(union) + 4, f"partition broken: sum={total} union={len(union)}"


def test_taiapp_exposes_twenty_four_namespaces():
    from tai42_contract.app import TaiApp

    assert protocol_members(TaiApp) == {
        "tools",
        "agents",
        "backends",
        "sandboxes",
        "storage",
        "connectors",
        "accounts",
        "webhook_verifiers",
        "channels",
        "conversations",
        "monitoring",
        "extensions",
        "interactions",
        "http",
        "clients",
        "lifecycle",
        "admin",
        "config",
        "backup",
        "sub_app",
        "versioning",
        "presets",
        "tool_meta",
        "states",
    }


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


def test_custom_route_carries_self_describing_metadata():
    # custom_route registers a route AND its OpenAPI metadata: summary + tags +
    # response_model are keyword-only REQUIRED (no default, so a route cannot
    # silently omit them); request_model + query_model + authed are keyword-only
    # with defaults.
    from tai42_contract.app import AppHttp

    sig = inspect.signature(AppHttp.custom_route)
    for name in (
        "summary",
        "tags",
        "response_model",
        "request_model",
        "query_model",
        "authed",
        "no_body_reason",
        "enveloped",
    ):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, f"{name} must be keyword-only"
    empty = inspect.Parameter.empty
    assert sig.parameters["summary"].default is empty
    assert sig.parameters["tags"].default is empty
    assert sig.parameters["response_model"].default is empty
    assert sig.parameters["request_model"].default is None
    assert sig.parameters["query_model"].default is None
    assert sig.parameters["authed"].default is None
    # no_body_reason is the required-when-response_model-is-None justification (a route
    # declares a typed body OR a reasoned no-body): keyword-only, defaulting to None.
    assert sig.parameters["no_body_reason"].default is None
    # enveloped selects the {"data": ...} wrapper (default) vs a raw top-level body:
    # keyword-only, defaulting to True so an ordinary route wraps unchanged.
    assert sig.parameters["enveloped"].default is True


def test_mount_base_is_a_zero_arg_str_query():
    # mount_base takes no arguments beyond self and returns the resolved absolute
    # mount base a declared plugin route module captures at import.
    from tai42_contract.app import AppHttp

    sig = inspect.signature(AppHttp.mount_base)
    assert list(sig.parameters) == ["self"]
    assert sig.return_annotation == "str"


def test_extension_registration_carries_requires_body_locality():
    # The registration surface stores the body-locality marker the apply site
    # reads to order stacked combos: keyword-only, defaulting to False so an
    # ordinary extension registers unchanged.
    from tai42_contract.app import AppExtensions

    sig = inspect.signature(AppExtensions.extension)
    param = sig.parameters["requires_body_locality"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is False


def test_app_conversations_is_runtime_checkable_and_shaped():
    from tai42_contract.app import AppConversations

    class _Ok:
        async def accept(
            self, channel: str, our_identity: str, client_address: str, text: str, provider_message_id: str
        ) -> str:
            return "m-1"

        async def record_delivery_status(self, channel: str, provider_message_id: str, status: object) -> None:
            return None

        async def pending_messages(self, thread_id: str, *, after: str) -> list[object]:
            return []

        def register_target_validator(self, target_kind: object, validator: object) -> None:
            return None

    class _Missing:
        async def accept(
            self, channel: str, our_identity: str, client_address: str, text: str, provider_message_id: str
        ) -> str:
            return "m-1"

    assert isinstance(_Ok(), AppConversations)
    assert not isinstance(_Missing(), AppConversations)


def test_facet_methods_are_coroutines_with_the_expected_parameters():
    from tai42_contract.app import AppConversations

    assert inspect.iscoroutinefunction(AppConversations.accept)
    assert list(inspect.signature(AppConversations.accept).parameters) == [
        "self",
        "channel",
        "our_identity",
        "client_address",
        "cap_key",
        "text",
        "provider_message_id",
        "params",
        "form",
        "attachments",
        "location",
        "locale",
    ]
    assert inspect.iscoroutinefunction(AppConversations.record_delivery_status)
    assert list(inspect.signature(AppConversations.record_delivery_status).parameters) == [
        "self",
        "channel",
        "provider_message_id",
        "status",
    ]
    assert inspect.iscoroutinefunction(AppConversations.pending_messages)
    pending_sig = inspect.signature(AppConversations.pending_messages)
    assert list(pending_sig.parameters) == ["self", "thread_id", "after"]
    assert pending_sig.parameters["after"].kind is inspect.Parameter.KEYWORD_ONLY
