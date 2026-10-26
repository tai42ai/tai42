"""The platform's template-delete referees: one per stored shape that can name a stored template id.

Each reads its stored rows through the reader its write door checks, so what a write refuses
to store and what a delete refuses to remove are the same list. Only active rows are read: a
rollback is a write door and checks the ids of the version it re-activates. A referee whose
feature is off answers ``[]`` without reading a store; a row it cannot read raises.

Each holder: locator ``{"template": <id>, "site": <site>}`` plus the kind's own fields,
summary ``<kind> '<name>' names '<id>' at <site>``; one holder per (row, id, site), in the
row's read order. Each holder carries its kind's feature tag (:mod:`~tai42_skeleton.tools.holder_kinds`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.tools import ReferenceHolder, TemplateDeleteReferee, TemplateRef

from tai42_skeleton.tools import holder_kinds
from tai42_skeleton.tools.holder_kinds import HolderKind


def _holders(
    kind: HolderKind, name: str, refs: Iterable[TemplateRef], ids: frozenset[str], **locator: Any
) -> list[ReferenceHolder]:
    holders: list[ReferenceHolder] = []
    for ref in dict.fromkeys(refs):
        if ref.id not in ids:
            continue
        holders.append(
            kind.holder(
                name=name,
                locator={"template": ref.id, "site": ref.site, **locator},
                summary=f"{kind.kind} {name!r} names {ref.id!r} at {ref.site}",
            )
        )
    return holders


def _skeleton_store_configured() -> bool:
    from tai42_kit.db import component_store_configured

    from tai42_skeleton.db import SKELETON_COMPONENT

    return component_store_configured(SKELETON_COMPONENT)


async def _preset_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    from tai42_contract.states import binding_template_refs

    from tai42_skeleton.app import instance
    from tai42_skeleton.operations.reference_check import as_template_ref
    from tai42_skeleton.operations.reference_graph import schema_references, stored_body_references

    if not _skeleton_store_configured():
        return []
    holders: list[ReferenceHolder] = []
    for name, body in sorted((await instance.app.presets.list_active_bodies()).items()):
        refs = [
            as_template_ref(ref)
            for ref in [
                *schema_references(body.output_schema, body.input_schema),
                *await stored_body_references(name, body),
            ]
            if ref.registry == "template"
        ]
        if body.state_binding is not None:
            refs += binding_template_refs(body.state_binding)
        holders += _holders(holder_kinds.PRESET, name, refs, ids)
    return holders


async def _hook_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    from tai42_contract.states import binding_template_refs

    from tai42_skeleton.hooks.cache import get_hooks_manager
    from tai42_skeleton.operations.reference_graph import kwargs_template_refs
    from tai42_skeleton.template.stored_refs import hook_template_refs

    holders: list[ReferenceHolder] = []
    for name, params in sorted((await get_hooks_manager().list_hooks()).items()):
        refs = hook_template_refs(params)
        if params.state_binding is not None:
            refs += binding_template_refs(params.state_binding)
        refs += await kwargs_template_refs(params.tool, params.tool_kwargs)
        holders += _holders(holder_kinds.HOOK, name, refs, ids)
    return holders


def _schedule_tool_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """A stored schedule's tool arguments: its kwargs without the reserved keys the platform stamps."""
    from tai42_contract.backend.scheduling import SCHEDULE_CADENCE_KEY
    from tai42_kit.utils.schedule_subject import RESERVED_SCHEDULE_PREFIX, SCHEDULE_STATE_BINDING_REQUEST_KEY

    return {
        key: value
        for key, value in kwargs.items()
        if not key.startswith(RESERVED_SCHEDULE_PREFIX)
        and key not in (SCHEDULE_CADENCE_KEY, SCHEDULE_STATE_BINDING_REQUEST_KEY)
    }


async def _schedule_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    from tai42_contract.interactions import ParkableDoorMixin
    from tai42_contract.states import StateBinding, binding_template_refs
    from tai42_kit.utils.schedule_subject import SCHEDULE_CONTRACT_ARG, SCHEDULE_STATE_BINDING_ARG

    from tai42_skeleton.operations import NotSupportedError
    from tai42_skeleton.operations.reference_graph import kwargs_template_refs
    from tai42_skeleton.operations.schedules import export_schedules_raw
    from tai42_skeleton.template.stored_refs import door_contract_template_refs

    try:
        records = await export_schedules_raw()
    except NotSupportedError:
        return []
    holders: list[ReferenceHolder] = []
    for record in records:
        refs = await kwargs_template_refs(record.tool, _schedule_tool_kwargs(record.kwargs))
        raw_binding = record.kwargs.get(SCHEDULE_STATE_BINDING_ARG)
        if raw_binding is not None:
            refs += binding_template_refs(StateBinding.model_validate(raw_binding))
        raw_contract = record.kwargs.get(SCHEDULE_CONTRACT_ARG)
        if raw_contract is not None:
            refs += door_contract_template_refs(ParkableDoorMixin.model_validate(raw_contract))
        holders += _holders(holder_kinds.SCHEDULE, record.name, refs, ids)
    return holders


async def _conversation_route_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    from tai42_skeleton.conversations.cache import get_conversations_manager
    from tai42_skeleton.operations import NotSupportedError
    from tai42_skeleton.template.stored_refs import route_template_refs

    try:
        routes, unreadable = await get_conversations_manager().list_routes()
    except NotSupportedError:
        return []
    if unreadable:
        raise RuntimeError(f"{unreadable} stored conversation route(s) could not be read")
    holders: list[ReferenceHolder] = []
    for name, route in sorted(routes.items()):
        holders += _holders(holder_kinds.CONVERSATION_ROUTE, name, route_template_refs(route), ids)
    return holders


async def _conversation_config_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    from tai42_contract.states import binding_template_refs

    from tai42_skeleton.conversations.cache import get_conversations_manager

    manager = get_conversations_manager()
    if not manager.durable:
        return []
    configs, unreadable = await manager.target_configs.list()
    if unreadable:
        raise RuntimeError(f"{unreadable} stored conversation config(s) could not be read")
    holders: list[ReferenceHolder] = []
    for (target_kind, target_name), config in sorted(configs.items()):
        if config.state_binding is None:
            continue
        refs = binding_template_refs(config.state_binding)
        holders += _holders(holder_kinds.CONVERSATION_CONFIG, target_name, refs, ids, target_kind=target_kind)
    return holders


async def _state_declaration_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    from tai42_skeleton.app import instance
    from tai42_skeleton.states.db import states_store_configured
    from tai42_skeleton.states.template_refs import declaration_template_refs

    if not states_store_configured():
        return []
    holders: list[ReferenceHolder] = []
    for decl in sorted(await instance.app._states_service.list_declarations(), key=lambda d: d.name):
        holders += _holders(holder_kinds.STATE_DECLARATION, decl.name, declaration_template_refs(decl), ids)
    return holders


async def _state_template_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    from tai42_skeleton.app import instance
    from tai42_skeleton.states.db import states_store_configured
    from tai42_skeleton.states.template_refs import state_template_template_refs

    if not states_store_configured():
        return []
    holders: list[ReferenceHolder] = []
    for doc in sorted(await instance.app._states_service.list_templates(), key=lambda d: d.name):
        holders += _holders(holder_kinds.STATE_TEMPLATE, doc.name, state_template_template_refs(doc), ids)
    return holders


async def _access_control_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    from tai42_skeleton.access_control.management import is_minted_policy
    from tai42_skeleton.access_control.store import access_control_store
    from tai42_skeleton.template.stored_refs import condition_template_refs

    if not _skeleton_store_configured():
        return []
    holders: list[ReferenceHolder] = []
    for user_id, body in await access_control_store().list_conditioned_policies():
        kind = holder_kinds.API_KEY if is_minted_policy(body) else holder_kinds.PRINCIPAL
        holders += _holders(kind, user_id, condition_template_refs(body.get("condition")), ids)
    return holders


async def _role_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    from tai42_skeleton.access_control.roles import role_store
    from tai42_skeleton.template.stored_refs import condition_template_refs

    if not _skeleton_store_configured():
        return []
    holders: list[ReferenceHolder] = []
    for role in sorted(await role_store().list_roles(), key=lambda r: r["name"]):
        holders += _holders(holder_kinds.ROLE, role["name"], condition_template_refs(role.get("condition")), ids)
    return holders


def settings_env_var(cls: type, field: str) -> str:
    """The environment variable the kit's settings registry records for ``cls.field``; an unregistered field raises."""
    from tai42_kit.settings import registered_settings

    qualname = f"{cls.__module__}.{cls.__qualname__}"
    for info in registered_settings():
        if info.qualname == qualname:
            for entry in info.fields:
                if entry.name == field:
                    return entry.env_var
    raise LookupError(f"settings field {qualname}.{field} is not registered")


async def _summary_prompt_referee(ids: frozenset[str]) -> list[ReferenceHolder]:
    from tai42_kit.llm.settings import SummarizationMiddlewareSettings, summarization_middleware_settings

    prompt = summarization_middleware_settings().summary_prompt
    if prompt is None or prompt.id is None:
        return []
    name = settings_env_var(SummarizationMiddlewareSettings, "summary_prompt")
    return _holders(holder_kinds.SETTING, name, [TemplateRef(id=prompt.id, site="summary_prompt")], ids)


PLATFORM_TEMPLATE_REFEREES: tuple[TemplateDeleteReferee, ...] = (
    _preset_referee,
    _hook_referee,
    _schedule_referee,
    _conversation_route_referee,
    _conversation_config_referee,
    _state_declaration_referee,
    _state_template_referee,
    _access_control_referee,
    _role_referee,
    _summary_prompt_referee,
)


def register_platform_template_referees() -> None:
    """Register the platform's template-delete referees into the epoch being built (a startup and reload handler)."""
    for referee in PLATFORM_TEMPLATE_REFEREES:
        tai42_app.storage.register_template_delete_referee(referee)


__all__ = ["PLATFORM_TEMPLATE_REFEREES", "register_platform_template_referees", "settings_env_var"]
