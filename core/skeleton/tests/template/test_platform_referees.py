"""The platform's template-delete referees: one holder per stored row naming a deleted id, by id only."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM
from tai42_contract.backend import CrontabCadence
from tai42_contract.backend.scheduling import ScheduleRecord
from tai42_contract.conversations import ApiConversationRoute
from tai42_contract.conversations.targets import TargetConversationConfig
from tai42_contract.hooks import HookParams
from tai42_contract.presets import PresetBody
from tai42_contract.states import StateAttach, StateBinding, StateDeclaration, StateTemplateDocument
from tai42_contract.template import TemplatedText
from tai42_contract.tools import ReferenceHolder
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.access_control.roles as roles_module
import tai42_skeleton.access_control.store as ac_store_module
import tai42_skeleton.conversations.cache as conversations_cache
import tai42_skeleton.hooks.cache as hooks_cache
import tai42_skeleton.operations.schedules as schedules_ops
import tai42_skeleton.states.db as states_db
from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import NotSupportedError
from tai42_skeleton.operations.errors import ConflictError
from tai42_skeleton.template import platform_referees
from tai42_skeleton.template.platform_referees import PLATFORM_TEMPLATE_REFEREES
from tai42_skeleton.tools import holder_kinds
from tai42_skeleton.tools.holder_kinds import HolderKind

from .._fakes.caller_reach import bound_caller

GATE = frozenset({"jq/gate"})
OTHER = frozenset({"jq/other"})
_BY_ID = TemplatedText(id="jq/gate")
_INLINE = TemplatedText(content=".a")

_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures"],
    "tools": [
        {"title": "fx", "module": "tests.presets._fixtures", "include": ["echo"], "extensions": {"echo": [["jqpass"]]}}
    ],
}


def _holder(kind: HolderKind, name: str, site: str, **locator: Any) -> ReferenceHolder:
    return kind.holder(
        name=name,
        locator={"template": "jq/gate", "site": site, **locator},
        summary=f"{kind.kind} {name!r} names 'jq/gate' at {site}",
    )


def _within_app(body: Any) -> None:
    async def run() -> None:
        async with instance.app.app_context(Manifest.model_validate(_MANIFEST)):
            await body()

    asyncio.run(run())


async def _boom(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("store read failed")


async def _never(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("a referee whose feature is off reads no store")


def _binding(subject_expr: TemplatedText) -> StateBinding:
    return StateBinding(states=[StateAttach(state="status", subject_expr=subject_expr)])


def test_the_platform_referees_are_the_ten_stored_shapes_in_order() -> None:
    assert [referee.__name__ for referee in PLATFORM_TEMPLATE_REFEREES] == [
        "_preset_referee",
        "_hook_referee",
        "_schedule_referee",
        "_conversation_route_referee",
        "_conversation_config_referee",
        "_state_declaration_referee",
        "_state_template_referee",
        "_access_control_referee",
        "_role_referee",
        "_summary_prompt_referee",
    ]


# -- preset ------------------------------------------------------------------------------------


@pytest.fixture
def presets(monkeypatch: pytest.MonkeyPatch) -> dict[str, PresetBody]:
    rows: dict[str, PresetBody] = {}

    async def _bodies(_facet: Any) -> dict[str, PresetBody]:
        return rows

    monkeypatch.setattr(platform_referees, "_skeleton_store_configured", lambda: True)
    # Patched on the facet's class: undoing a patch on the shared facet instance would leave the
    # bound method in its ``__dict__``, shadowing every later test's class-level patch.
    monkeypatch.setattr(type(instance.app._presets_facet), "list_active_bodies", _bodies)
    return rows


def test_the_preset_referee_answers_a_schema_id_a_fixed_kwargs_id_and_a_binding_slot(presets) -> None:
    async def body() -> None:
        presets["p"] = PresetBody(
            base_tool="echo_jqpass",
            description="Gate by id.",
            output_schema=_BY_ID,
            fixed_kwargs={"jq_expression": {"id": "jq/gate"}},
            state_binding=_binding(_BY_ID),
        )
        presets["q"] = PresetBody(
            base_tool="echo_jqpass",
            description="Gate inline.",
            output_schema=_INLINE,
            fixed_kwargs={"jq_expression": {"content": ".a"}},
            state_binding=_binding(_INLINE),
        )
        assert await platform_referees._preset_referee(GATE) == [
            _holder(holder_kinds.PRESET, "p", "output_schema"),
            _holder(holder_kinds.PRESET, "p", "jq_expression"),
            _holder(holder_kinds.PRESET, "p", "states[0].subject_expr"),
        ]
        assert await platform_referees._preset_referee(OTHER) == []

    _within_app(body)


async def test_the_preset_referee_with_no_store_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_referees, "_skeleton_store_configured", lambda: False)
    monkeypatch.setattr(type(instance.app._presets_facet), "list_active_bodies", _never)
    assert await platform_referees._preset_referee(GATE) == []


async def test_the_preset_referee_propagates_a_store_fault(presets, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(instance.app._presets_facet), "list_active_bodies", _boom)
    with pytest.raises(RuntimeError, match="store read failed"):
        await platform_referees._preset_referee(GATE)


# -- hook --------------------------------------------------------------------------------------


def _hook(name: str, expr: TemplatedText) -> HookParams:
    return HookParams(
        name=name,
        topic="events",
        tool="echo",
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        condition=expr,
    )


async def test_the_hook_referee_answers_the_hook_naming_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = {"h": _hook("h", _BY_ID), "i": _hook("i", _INLINE)}
    manager = SimpleNamespace(list_hooks=lambda: _answer(rows))
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: manager)
    assert await platform_referees._hook_referee(GATE) == [_holder(holder_kinds.HOOK, "h", "condition")]
    assert await platform_referees._hook_referee(OTHER) == []


async def test_the_hook_referee_propagates_a_store_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: SimpleNamespace(list_hooks=_boom))
    with pytest.raises(RuntimeError, match="store read failed"):
        await platform_referees._hook_referee(GATE)


def test_the_hook_referee_reads_the_tool_arguments_at_the_site_the_walk_names(monkeypatch) -> None:
    async def body() -> None:
        hook = HookParams(
            name="h",
            topic="events",
            tool="echo_jqpass",
            execution_key="k-fire",
            execution_key_fingerprint="fp-fire",
            tool_kwargs={"jq_expression": {"id": "jq/gate"}},
        )
        manager = SimpleNamespace(list_hooks=lambda: _answer({"h": hook}))
        monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: manager)
        assert await platform_referees._hook_referee(GATE) == [_holder(holder_kinds.HOOK, "h", "jq_expression")]

    _within_app(body)


async def _answer(value: Any) -> Any:
    return value


# -- schedule ----------------------------------------------------------------------------------


def _schedule(name: str, kwargs: dict[str, Any]) -> ScheduleRecord:
    return ScheduleRecord(name=name, tool="echo", kwargs=kwargs, cadence=CrontabCadence(minute="0"), enabled=True)


async def test_the_schedule_referee_answers_the_schedule_naming_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.utils.schedule_subject import SCHEDULE_STATE_BINDING_ARG

    rows = [
        _schedule("s", {SCHEDULE_STATE_BINDING_ARG: _binding(_BY_ID).model_dump(mode="json")}),
        _schedule("t", {SCHEDULE_STATE_BINDING_ARG: _binding(_INLINE).model_dump(mode="json")}),
    ]
    monkeypatch.setattr(schedules_ops, "export_schedules_raw", lambda: _answer(rows))
    assert await platform_referees._schedule_referee(GATE) == [
        _holder(holder_kinds.SCHEDULE, "s", "states[0].subject_expr")
    ]
    assert await platform_referees._schedule_referee(OTHER) == []


async def test_the_schedule_referee_with_no_backend_answers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _off() -> Any:
        raise NotSupportedError("no schedules backend")

    monkeypatch.setattr(schedules_ops, "export_schedules_raw", _off)
    assert await platform_referees._schedule_referee(GATE) == []


async def test_the_schedule_referee_propagates_a_store_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedules_ops, "export_schedules_raw", _boom)
    with pytest.raises(RuntimeError, match="store read failed"):
        await platform_referees._schedule_referee(GATE)


# -- conversation route and config -------------------------------------------------------------


def _route(name: str, expr: TemplatedText) -> ApiConversationRoute:
    return ApiConversationRoute(
        route_name=name,
        door="api",
        target_kind="tool",
        target_name="echo",
        execution_key="k-fire",
        callback_url="https://example.com/cb",
        execution_key_fingerprint="fp-fire",
        reply_expr=expr,
    )


def _conversations(monkeypatch: pytest.MonkeyPatch, **manager: Any) -> None:
    monkeypatch.setattr(conversations_cache, "get_conversations_manager", lambda: SimpleNamespace(**manager))


async def test_the_route_referee_answers_the_route_naming_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {"r": _route("r", _BY_ID), "s": _route("s", _INLINE)}
    _conversations(monkeypatch, list_routes=lambda: _answer((routes, 0)))
    assert await platform_referees._conversation_route_referee(GATE) == [
        _holder(holder_kinds.CONVERSATION_ROUTE, "r", "reply_expr")
    ]
    assert await platform_referees._conversation_route_referee(OTHER) == []


async def test_the_route_referee_with_no_backend_answers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _off() -> Any:
        raise NotSupportedError("no routes backend")

    _conversations(monkeypatch, list_routes=_off)
    assert await platform_referees._conversation_route_referee(GATE) == []


async def test_the_route_referee_raises_on_a_fault_or_an_unreadable_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _conversations(monkeypatch, list_routes=_boom)
    with pytest.raises(RuntimeError, match="store read failed"):
        await platform_referees._conversation_route_referee(GATE)
    _conversations(monkeypatch, list_routes=lambda: _answer(({}, 2)))
    with pytest.raises(RuntimeError, match="2 stored conversation route"):
        await platform_referees._conversation_route_referee(GATE)


def _config(name: str, expr: TemplatedText) -> TargetConversationConfig:
    return TargetConversationConfig(target_kind="tool", target_name=name, state_binding=_binding(expr))


async def test_the_config_referee_answers_the_config_naming_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    configs = {("tool", "echo"): _config("echo", _BY_ID), ("tool", "plain"): _config("plain", _INLINE)}
    store = SimpleNamespace(list=lambda: _answer((configs, 0)))
    _conversations(monkeypatch, durable=True, target_configs=store)
    assert await platform_referees._conversation_config_referee(GATE) == [
        _holder(holder_kinds.CONVERSATION_CONFIG, "echo", "states[0].subject_expr", target_kind="tool")
    ]
    assert await platform_referees._conversation_config_referee(OTHER) == []


async def test_the_config_referee_with_no_durable_store_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _conversations(monkeypatch, durable=False, target_configs=SimpleNamespace(list=_never))
    assert await platform_referees._conversation_config_referee(GATE) == []


async def test_the_config_referee_raises_on_a_fault_or_an_unreadable_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _conversations(monkeypatch, durable=True, target_configs=SimpleNamespace(list=_boom))
    with pytest.raises(RuntimeError, match="store read failed"):
        await platform_referees._conversation_config_referee(GATE)
    _conversations(monkeypatch, durable=True, target_configs=SimpleNamespace(list=lambda: _answer(({}, 1))))
    with pytest.raises(RuntimeError, match="1 stored conversation config"):
        await platform_referees._conversation_config_referee(GATE)


# -- state declaration and state template ------------------------------------------------------


def _states(monkeypatch: pytest.MonkeyPatch, *, configured: bool = True, **service: Any) -> None:
    monkeypatch.setattr(states_db, "states_store_configured", lambda: configured)
    monkeypatch.setattr(instance.app._serving_core, "_states_service", SimpleNamespace(**service))


def _declaration(name: str, schema: TemplatedText) -> StateDeclaration:
    return StateDeclaration(name=name, schema=schema, subject_kinds=["person"], default_subject_kind="person")


async def test_the_declaration_referee_answers_the_declaration_naming_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_declaration("status", _BY_ID), _declaration("plain", _INLINE)]
    _states(monkeypatch, list_declarations=lambda: _answer(rows))
    assert await platform_referees._state_declaration_referee(GATE) == [
        _holder(holder_kinds.STATE_DECLARATION, "status", "schema")
    ]
    assert await platform_referees._state_declaration_referee(OTHER) == []


async def test_the_declaration_referee_with_no_store_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _states(monkeypatch, configured=False, list_declarations=_never)
    assert await platform_referees._state_declaration_referee(GATE) == []


async def test_the_declaration_referee_propagates_a_store_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    _states(monkeypatch, list_declarations=_boom)
    with pytest.raises(RuntimeError, match="store read failed"):
        await platform_referees._state_declaration_referee(GATE)


async def test_the_state_template_referee_answers_the_template_naming_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [StateTemplateDocument(name="t", schema=_BY_ID), StateTemplateDocument(name="u", schema=_INLINE)]
    _states(monkeypatch, list_templates=lambda: _answer(rows))
    assert await platform_referees._state_template_referee(GATE) == [
        _holder(holder_kinds.STATE_TEMPLATE, "t", "schema")
    ]
    assert await platform_referees._state_template_referee(OTHER) == []


async def test_the_state_template_referee_with_no_store_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _states(monkeypatch, configured=False, list_templates=_never)
    assert await platform_referees._state_template_referee(GATE) == []


async def test_the_state_template_referee_propagates_a_store_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    _states(monkeypatch, list_templates=_boom)
    with pytest.raises(RuntimeError, match="store read failed"):
        await platform_referees._state_template_referee(GATE)


# -- access control and roles ------------------------------------------------------------------


def _policies(monkeypatch: pytest.MonkeyPatch, *, configured: bool = True, rows: Any = None) -> None:
    monkeypatch.setattr(platform_referees, "_skeleton_store_configured", lambda: configured)
    reader = rows if callable(rows) else (lambda: _answer(rows))
    store = SimpleNamespace(list_conditioned_policies=reader)
    monkeypatch.setattr(ac_store_module, "access_control_store", lambda: store)


async def test_the_access_control_referee_answers_an_api_key_and_a_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    minted = {"condition": {"id": "jq/gate"}, "policy_data": {KEY_FINGERPRINT_CLAIM: "fp"}}
    principal = {"condition": {"id": "jq/gate"}, "policy_data": {}}
    inline = {"condition": {"content": ".a"}, "policy_data": {}}
    _policies(monkeypatch, rows=[("alice", principal), ("key-1", minted), ("bob", inline)])
    assert await platform_referees._access_control_referee(GATE) == [
        _holder(holder_kinds.PRINCIPAL, "alice", "condition"),
        _holder(holder_kinds.API_KEY, "key-1", "condition"),
    ]
    assert await platform_referees._access_control_referee(OTHER) == []


async def test_the_access_control_referee_with_no_store_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _policies(monkeypatch, configured=False, rows=_never)
    assert await platform_referees._access_control_referee(GATE) == []


async def test_the_access_control_referee_propagates_a_store_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    _policies(monkeypatch, rows=_boom)
    with pytest.raises(RuntimeError, match="store read failed"):
        await platform_referees._access_control_referee(GATE)


async def _refused_template_delete(monkeypatch: pytest.MonkeyPatch) -> ConflictError:
    from tai42_skeleton.operations import holders as holders_module

    minted = {"condition": {"id": "jq/gate"}, "policy_data": {KEY_FINGERPRINT_CLAIM: "fp"}}
    principal = {"condition": {"id": "jq/gate"}, "policy_data": {}}
    _policies(monkeypatch, rows=[("alice", principal), ("key-1", minted), ("bob", principal)])
    monkeypatch.setattr(holders_module, "template_delete_holders", platform_referees._access_control_referee)
    with pytest.raises(ConflictError) as refused:
        await holders_module.refuse_held_templates(GATE, head="stored template 'jq/gate' cannot be deleted")
    return refused.value


async def test_a_refused_template_delete_counts_identities_a_role_cannot_read(monkeypatch: pytest.MonkeyPatch) -> None:
    with bound_caller(monkeypatch, {"states": "write"}):
        refused = await _refused_template_delete(monkeypatch)
    assert str(refused) == (
        "stored template 'jq/gate' cannot be deleted — held by:\n"
        "- principal: 2 not readable with your role\n"
        "- api key: 1 not readable with your role\n"
        "ask someone with access to 'access-control' to remove those references first"
    )
    assert not any(user in repr(refused.extra) for user in ("alice", "bob", "key-1"))


async def test_a_refused_template_delete_names_the_identities_to_an_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    with bound_caller(monkeypatch, admin=True):
        refused = await _refused_template_delete(monkeypatch)
    assert str(refused) == (
        "stored template 'jq/gate' cannot be deleted — held by:\n"
        "- principal 'alice' names 'jq/gate' at condition\n"
        "- api key 'key-1' names 'jq/gate' at condition\n"
        "- principal 'bob' names 'jq/gate' at condition\n"
        "remove those references first"
    )


def _roles(monkeypatch: pytest.MonkeyPatch, *, configured: bool = True, reader: Any) -> None:
    monkeypatch.setattr(platform_referees, "_skeleton_store_configured", lambda: configured)
    monkeypatch.setattr(roles_module, "role_store", lambda: SimpleNamespace(list_roles=reader))


async def test_the_role_referee_answers_the_role_naming_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"name": "viewer", "condition": {"id": "jq/gate"}}, {"name": "editor", "condition": {"content": ".a"}}]
    _roles(monkeypatch, reader=lambda: _answer(rows))
    assert await platform_referees._role_referee(GATE) == [_holder(holder_kinds.ROLE, "viewer", "condition")]
    assert await platform_referees._role_referee(OTHER) == []


async def test_the_role_referee_with_no_store_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _roles(monkeypatch, configured=False, reader=_never)
    assert await platform_referees._role_referee(GATE) == []


async def test_the_role_referee_propagates_a_store_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    _roles(monkeypatch, reader=_boom)
    with pytest.raises(RuntimeError, match="store read failed"):
        await platform_referees._role_referee(GATE)


# -- setting -----------------------------------------------------------------------------------

_PROMPT_ENV = "SUMMARIZATION_MIDDLEWARE_SUMMARY_PROMPT"
_SETTING = _holder(holder_kinds.SETTING, _PROMPT_ENV, "summary_prompt")


@pytest.mark.parametrize(
    ("value", "methods", "expected"),
    [
        ({"id": "jq/gate"}, None, [_SETTING]),
        ({"id": "jq/gate"}, ["context_editing"], [_SETTING]),
        ({"id": "other"}, None, []),
        (None, None, []),
        ({"content": "Summarize."}, None, []),
    ],
)
async def test_the_setting_referee_reads_the_summary_prompt(
    monkeypatch: pytest.MonkeyPatch, value: Any, methods: Any, expected: list[ReferenceHolder]
) -> None:
    if value is not None:
        monkeypatch.setenv(_PROMPT_ENV, json.dumps(value))
    if methods is not None:
        monkeypatch.setenv("CONTEXT_OVERFLOW_METHODS", json.dumps(methods))
    reset_all_settings()
    assert await platform_referees._summary_prompt_referee(GATE) == expected


async def test_the_setting_referee_propagates_a_malformed_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_PROMPT_ENV, json.dumps({"id": "jq/gate", "content": ".a"}))
    reset_all_settings()
    with pytest.raises(ValidationError):
        await platform_referees._summary_prompt_referee(GATE)
