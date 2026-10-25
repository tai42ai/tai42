"""Platform-internal referees for the rename gate and the template-detach gate.

The in-house holders of tool-name references (the rename gate) and of door bindings that
name state templates (the template-detach gate), armed skeleton-side at boot (never
through the public ``register_*_referee`` seams; they are not plugins).

Each RENAME referee answers the rename gate for the OLD tool name with one
:class:`~tai42_contract.tools.ReferenceHolder` per live reference it still has: a schedule
firing it, a hook targeting it, a conversation route pointing at it, a tool-extensions map
entry carrying it, a parked interaction whose resume continuation is it, a pending state save
under it, or state records keyed under it as a ``tool`` subject target. Each DETACH referee
answers the template-detach gate for a ``(state, template)`` with the live door bindings that
still name that template on that state: a preset version, a conversation config, a hook, or a
schedule whose ``state_binding`` references it. Every referee only reads (the previews call
them). An empty answer is no objection; a raising referee fails the gate loudly — an
unreadable holder store must never let a stranding rename or a stranding detach through.

The feature-OFF case is not an error: a referee whose backing store is unconfigured
has no holders and returns an empty list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_contract.app import tai42_app
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.tools import holder_kinds

if TYPE_CHECKING:
    from tai42_contract.states import StateBinding


async def _schedule_referee(old_name: str) -> list[ReferenceHolder]:
    # Read every schedule's fired tool from the cross-backend export surface
    # (``backend_export_schedules``), whose ``ScheduleRecord`` rows name it in ``tool`` — the
    # backend splits its own tool-name key out of the stored job. This catches EVERY schedule
    # that fires a tool, including operator-named ones, not only the platform's derived
    # ``<tool>_<hex>`` name convention.
    from tai42_skeleton.operations import NotSupportedError
    from tai42_skeleton.operations.schedules import export_schedules_raw
    from tai42_skeleton.tools.binding import UnknownToolError

    try:
        records = await export_schedules_raw()
    except NotSupportedError:
        # Feature-off: no scheduling backend is installed, so there are no holders.
        return []
    except UnknownToolError as exc:
        # Markers present but the backend registers no export tool: its schedules' dispatch
        # targets cannot be read, so a stranding rename cannot be ruled out — block loudly,
        # never a silent miss.
        raise RuntimeError(
            f"schedule referee: scheduling backend registers no {exc.tool_name!r}; schedule holders "
            "cannot be verified, rename blocked"
        ) from exc
    # A disabled (``enabled: false``) schedule is re-enable-able, so it is a live strandable
    # reference and counts exactly like an enabled one.
    return [
        ReferenceHolder(kind=holder_kinds.SCHEDULE, name=record.name, summary=f"schedule {record.name!r}")
        for record in records
        if record.tool == old_name
    ]


async def _hook_referee(old_name: str) -> list[ReferenceHolder]:
    from tai42_skeleton.hooks.cache import get_hooks_manager

    hooks = await get_hooks_manager().list_hooks()
    return [
        ReferenceHolder(kind=holder_kinds.HOOK, name=params.name, summary=f"hook {params.name!r}")
        for params in hooks.values()
        if params.tool == old_name
    ]


async def _conversation_route_referee(old_name: str) -> list[ReferenceHolder]:
    from tai42_skeleton.conversations import get_conversations_manager
    from tai42_skeleton.operations import NotSupportedError

    try:
        routes, _ = await get_conversations_manager().list_routes()
    except NotSupportedError:
        # A backend without the routes capability holds no routes — no objection.
        return []
    return [
        ReferenceHolder(
            kind=holder_kinds.CONVERSATION_ROUTE,
            name=route.route_name,
            summary=f"conversation route {route.route_name!r}",
        )
        for route in routes.values()
        if route.target_kind == "tool" and route.target_name == old_name
    ]


async def _tool_extensions_referee(old_name: str) -> list[ReferenceHolder]:
    from tai42_skeleton.app import instance

    combos = instance.app.admin.live_manifest_typed.tool_extensions.get(old_name)
    if not combos:
        return []
    return [
        ReferenceHolder(
            kind=holder_kinds.TOOL_EXTENSIONS, name=old_name, summary=f"tool-extensions map entry for {old_name!r}"
        )
    ]


async def _parked_interaction_referee(old_name: str) -> list[ReferenceHolder]:
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
    from tai42_skeleton.interactions.store import InteractionStore

    if not interactions_store_configured():
        return []
    store = InteractionStore(interactions_settings().key_prefix)
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        # BOTH live holder indices, each walked in FULL (never the capped list_pending):
        # an OPEN park still in pending:expiry, AND an answered/expired park whose durable
        # continuation-due record the reaper still re-fires as run_tool(<tool>). A park
        # resolves out of the first index into the second on answer/expiry, so a rename
        # landing in that window would strand the continuation unless both are counted.
        parked = await store.parked_continuation_tools(r)
        due = await store.continuation_due_tools(r)
    holders: list[ReferenceHolder] = []
    open_count = sum(1 for tool in parked if tool == old_name)
    if open_count:
        parks = "park" if open_count == 1 else "parks"
        holders.append(
            ReferenceHolder(
                kind=holder_kinds.PARKED_INTERACTIONS,
                name=old_name,
                locator={"count": open_count},
                summary=f"{open_count} parked interaction {parks} resuming into {old_name!r}",
            )
        )
    due_count = sum(1 for tool in due if tool == old_name)
    if due_count:
        continuations = "continuation" if due_count == 1 else "continuations"
        holders.append(
            ReferenceHolder(
                kind=holder_kinds.ANSWERED_CONTINUATIONS,
                name=old_name,
                locator={"count": due_count},
                summary=f"{due_count} answered {continuations} awaiting redelivery into {old_name!r}",
            )
        )
    return holders


async def _states_referee(old_name: str) -> list[ReferenceHolder]:
    # A ``tool`` target renamed would strand every state record keyed under it — the
    # subject scope ``(tool, <old_name>)`` no longer names a live target — and every pending
    # save still outstanding under it. A read: nothing is applied, so the preview and the gate
    # see the same holders. Feature-off (the states component's database unbound) holds none.
    from tai42_skeleton.states.db import states_store_configured
    from tai42_skeleton.states.outbox.keys import target_key
    from tai42_skeleton.states.store import PostgresStatesStore

    if not states_store_configured():
        return []
    store = PostgresStatesStore()
    where = f"target tool/{old_name}"
    holders = [
        ReferenceHolder(
            kind=holder_kinds.PENDING_SAVE,
            name=str(row_id),
            locator={"status": status},
            summary=_pending_save_summary(row_id, status, where),
        )
        for row_id, status, _keys in await store.outbox_outstanding_on_target(target_key("tool", old_name))
    ]
    count = await store.count_records_for_target("tool", old_name)
    if count == 0:
        return holders
    records = "record" if count == 1 else "records"
    holders.append(
        ReferenceHolder(
            kind=holder_kinds.STATE_RECORDS,
            name=old_name,
            locator={"count": count},
            summary=f"{count} state {records} under target tool/{old_name}",
        )
    )
    return holders


def _pending_save_summary(row_id: int, status: str, where: str) -> str:
    if status == "failed":
        return f"failed pending state save {row_id} holds writes under {where}; an operator must retry or discard it"
    if status in ("calls", "running"):
        return f"pending state save {row_id} under {where} is still running its deferred calls"
    if status == "pending":
        return f"pending state save {row_id} under {where} is not applied yet"
    raise ValueError(f"pending state save {row_id} under {where} has an unknown status {status!r}")


def register_platform_rename_referees() -> None:
    """Arm the platform-internal referees on the live app's referee collection.

    A startup/reload handler (the collection is reset each ``start()``, so this re-arms
    every epoch). One registration per in-house holder surface.
    """
    for referee in (
        _schedule_referee,
        _hook_referee,
        _conversation_route_referee,
        _tool_extensions_referee,
        _parked_interaction_referee,
        _states_referee,
    ):
        tai42_app.tools.register_rename_referee(referee)


# --------------------------------------------------------------------------- #
# Template-detach referees                                                     #
# --------------------------------------------------------------------------- #
def _binding_names_template(binding: StateBinding | None, state: str, template: str) -> bool:
    """Whether ``binding`` still names ``template`` on ``state``.

    A live reference a detach of that template would strand (the named
    template_jq/attachment would vanish).
    """
    return binding is not None and any(
        attach.state == state and template in attach.templates for attach in binding.states
    )


async def _preset_detach_referee(state: str, template: str) -> list[ReferenceHolder]:
    # EVERY version of every live preset is a strandable reference: a rollback re-activates
    # (and re-attaches) a historical version's binding, so a version that can be rolled back
    # to counts exactly like the active one. The population read answers no presets on a
    # deployment with no skeleton database.
    from tai42_skeleton.app import instance
    from tai42_skeleton.presets.store import stored_preset_body

    store = instance.app.presets.store
    holders: list[ReferenceHolder] = []
    for name in await instance.app.presets.list_active_bodies():
        for version in await store.list_versions(name):
            body = stored_preset_body(name, version.body)
            if _binding_names_template(body.state_binding, state, template):
                holders.append(
                    ReferenceHolder(
                        kind=holder_kinds.PRESET_VERSION,
                        name=name,
                        locator={"version": version.version},
                        summary=f"preset {name!r} version {version.version}",
                    )
                )
    return holders


async def _conversation_config_detach_referee(state: str, template: str) -> list[ReferenceHolder]:
    # A tool-target conversation config carries the door binding the route applies around the
    # tool turn. Feature-off (the redis conversations backend unset) holds none.
    from tai42_skeleton.conversations.cache import get_conversations_manager

    manager = get_conversations_manager()
    if not manager.durable:
        return []
    configs, _ = await manager.target_configs.list()
    return [
        ReferenceHolder(
            kind=holder_kinds.CONVERSATION_CONFIG,
            name=config.target_name,
            locator={"target_kind": config.target_kind},
            summary=f"conversation config {config.target_kind}/{config.target_name}",
        )
        for config in configs.values()
        if _binding_names_template(config.state_binding, state, template)
    ]


async def _hook_detach_referee(state: str, template: str) -> list[ReferenceHolder]:
    from tai42_skeleton.hooks.cache import get_hooks_manager

    hooks = await get_hooks_manager().list_hooks()
    return [
        ReferenceHolder(kind=holder_kinds.HOOK, name=params.name, summary=f"hook {params.name!r}")
        for params in hooks.values()
        if _binding_names_template(params.state_binding, state, template)
    ]


async def _schedule_detach_referee(state: str, template: str) -> list[ReferenceHolder]:
    # Read each schedule's exported door binding — the JSON the recurring fire applies — from the
    # same export surface the rename schedule referee reads. Feature-off holds none; an unreadable
    # holder store fails loudly (never a silent stranding).
    from tai42_contract.states import StateBinding
    from tai42_kit.utils.schedule_subject import SCHEDULE_STATE_BINDING_ARG

    from tai42_skeleton.operations import NotSupportedError
    from tai42_skeleton.operations.schedules import export_schedules_raw
    from tai42_skeleton.tools.binding import UnknownToolError

    try:
        records = await export_schedules_raw()
    except NotSupportedError:
        return []
    except UnknownToolError as exc:
        raise RuntimeError(
            f"schedule detach referee: scheduling backend registers no {exc.tool_name!r}; schedule holders "
            "cannot be verified, detach blocked"
        ) from exc

    holders: list[ReferenceHolder] = []
    for record in records:
        # A stored schedule carries its door binding under the kit's reserved key — the create
        # door's ``state_binding`` is re-stamped to ``backend_schedule_state_binding`` and the
        # plain key popped, so that reserved key is where a live binding actually lives.
        raw = record.kwargs.get(SCHEDULE_STATE_BINDING_ARG)
        if raw is None:
            continue
        binding = StateBinding.model_validate(raw)
        if _binding_names_template(binding, state, template):
            holders.append(
                ReferenceHolder(kind=holder_kinds.SCHEDULE, name=record.name, summary=f"schedule {record.name!r}")
            )
    return holders


def register_platform_detach_referees() -> None:
    """Arm the platform-internal template-detach referees on the live app's referee collection.

    A startup/reload handler (the collection is reset each ``start()``, so this re-arms
    every epoch). One registration per in-house binding-holder surface.
    """
    for referee in (
        _preset_detach_referee,
        _conversation_config_detach_referee,
        _hook_detach_referee,
        _schedule_detach_referee,
    ):
        tai42_app.tools.register_detach_referee(referee)
