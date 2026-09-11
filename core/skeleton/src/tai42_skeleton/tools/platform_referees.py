"""Platform-internal referees — the in-house holders of tool-name references (the
rename gate) and of door bindings that name state templates (the template-detach gate),
armed skeleton-side at boot (never through the public ``register_*_referee`` seams; they
are not plugins).

Each RENAME referee answers the rename gate for the OLD tool name with human-readable
descriptions of the live references it still has: a schedule firing it, a hook
targeting it, a conversation route pointing at it, a tool-extensions map entry
carrying it, a parked interaction whose resume continuation is it, or state
records keyed under it as a ``tool`` subject target. Each DETACH referee answers the
template-detach gate for a ``(state, template)`` with the live door bindings that still
name that template on that state: a preset version, a conversation config, a hook, or a
schedule whose ``state_binding`` references it. An empty answer is no objection; a raising
referee fails the gate loudly — an unreadable holder store must never let a stranding
rename or a stranding detach through.

The feature-OFF case is not an error: a referee whose backing store is unconfigured
has no holders and returns an empty list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_contract.app import tai42_app

if TYPE_CHECKING:
    from tai42_contract.states import StateBinding


async def _schedule_referee(old_name: str) -> list[str]:
    # Read every schedule's actual dispatch target from the cross-backend export surface
    # (``backend_export_schedules``), whose canonical ``ScheduleRecord`` rows carry the
    # fired tool name in ``kwargs[tool_name_arg]`` — the literal key the worker pops and
    # runs. This catches EVERY schedule that fires a tool, including operator-named ones,
    # not only the platform's derived ``<tool>_<hex>`` name convention.
    from tai42_skeleton.backend.settings import base_backend_settings
    from tai42_skeleton.operations import NotSupportedError
    from tai42_skeleton.operations.schedules import export_schedules_raw
    from tai42_skeleton.tools.binding import UnknownToolError

    try:
        rows = await export_schedules_raw()
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

    # The host (``BACKEND_TOOL_NAME_ARG``) side of the dispatch contract every scheduling
    # backend must inject its target tool name under too, through its own prefixed
    # ``*_TOOL_NAME_ARG`` — two env keys for one conceptual key, default-equal and pinned
    # equal by declaration (settings drift guard), the operator's to keep aligned at runtime.
    tool_name_arg = base_backend_settings().tool_name_arg
    # An unreadable/unexpected holder store must FAIL the rename, never silently drop every
    # schedule holder and let a stranding rename through — any shape the referee cannot
    # interpret raises loudly, including a record with no readable dispatch target.
    if not isinstance(rows, list):
        raise TypeError(
            f"schedule referee: export_schedules_raw() returned a {type(rows).__name__}, expected a list of rows"
        )
    holders: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError(f"schedule referee: schedule record is a {type(row).__name__}, expected a dict")
        name = row.get("name")
        if not isinstance(name, str):
            raise TypeError(f"schedule referee: schedule record carries no string 'name' (keys: {sorted(row)})")
        kwargs = row.get("kwargs")
        if not isinstance(kwargs, dict):
            raise TypeError(f"schedule referee: schedule {name!r} carries no dict 'kwargs' (keys: {sorted(row)})")
        target = kwargs.get(tool_name_arg)
        if not isinstance(target, str):
            # A backend configured with a ``*_TOOL_NAME_ARG`` that disagrees with the host's
            # ``BACKEND_TOOL_NAME_ARG`` injects the target under a different key, which reads
            # here as a missing target on the FIRST record and blocks EVERY rename until the
            # two are realigned. The over-block is fail-safe (never a silent stranding), so it
            # stays loud — but names the expected key and the dispatch contract, so the
            # misconfiguration is diagnosable rather than a bare shape error.
            raise TypeError(
                f"schedule referee: schedule {name!r} kwargs carry no string {tool_name_arg!r} dispatch "
                f"target (kwargs keys: {sorted(kwargs)}); {tool_name_arg!r} is the host "
                "BACKEND_TOOL_NAME_ARG side of the tool_name_arg dispatch contract the scheduling backend "
                "must inject its target under too, so realign a mismatched backend *_TOOL_NAME_ARG"
            )
        # A disabled (``enabled: false``) schedule is re-enable-able, so it is a live
        # strandable reference and counts exactly like an enabled one.
        if target == old_name:
            holders.append(f"schedule {name!r}")
    return holders


async def _hook_referee(old_name: str) -> list[str]:
    from tai42_skeleton.hooks.cache import get_hooks_manager

    hooks = await get_hooks_manager().list_hooks()
    return [f"hook {params.name!r}" for params in hooks.values() if params.tool == old_name]


async def _conversation_route_referee(old_name: str) -> list[str]:
    from tai42_skeleton.conversations import get_conversations_manager
    from tai42_skeleton.operations import NotSupportedError

    try:
        routes = await get_conversations_manager().list_routes()
    except NotSupportedError:
        # A backend without the routes capability holds no routes — no objection.
        return []
    return [
        f"conversation route {route.route_name!r}"
        for route in routes.values()
        if route.target_kind == "tool" and route.target_name == old_name
    ]


async def _tool_extensions_referee(old_name: str) -> list[str]:
    from tai42_skeleton.app import instance

    combos = instance.app.admin.live_manifest_typed.tool_extensions.get(old_name)
    return [f"tool-extensions map entry for {old_name!r}"] if combos else []


async def _parked_interaction_referee(old_name: str) -> list[str]:
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
    from tai42_skeleton.interactions.store import InteractionStore

    if not interactions_store_configured():
        return []
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as r:
        # BOTH live holder indices, each walked in FULL (never the capped list_pending):
        # an OPEN park still in pending:expiry, AND an answered/expired park whose durable
        # continuation-due record the reaper still re-fires as run_tool(<tool>). A park
        # resolves out of the first index into the second on answer/expiry, so a rename
        # landing in that window would strand the continuation unless both are counted.
        parked = await store.parked_continuation_tools(r)
        due = await store.continuation_due_tools(r)
    holders: list[str] = []
    open_count = sum(1 for tool in parked if tool == old_name)
    if open_count:
        parks = "park" if open_count == 1 else "parks"
        holders.append(f"{open_count} parked interaction {parks} resuming into {old_name!r}")
    due_count = sum(1 for tool in due if tool == old_name)
    if due_count:
        continuations = "continuation" if due_count == 1 else "continuations"
        holders.append(f"{due_count} answered {continuations} awaiting redelivery into {old_name!r}")
    return holders


async def _states_referee(old_name: str) -> list[str]:
    # A ``tool`` target renamed would strand every state record keyed under it — the
    # subject scope ``(tool, <old_name>)`` no longer names a live target. Count them across
    # every state; feature-off (the states component's database unbound) holds none.
    from tai42_skeleton.states.db import states_store_configured
    from tai42_skeleton.states.store import PostgresStatesStore

    if not states_store_configured():
        return []
    count = await PostgresStatesStore().count_records_for_target("tool", old_name)
    if count == 0:
        return []
    records = "record" if count == 1 else "records"
    return [f"{count} state {records} under target tool/{old_name}"]


def register_platform_rename_referees() -> None:
    """Arm the platform-internal referees on the live app's referee collection — a
    startup/reload handler (the collection is reset each ``start()``, so this re-arms
    every epoch). One registration per in-house holder surface."""
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
    """Whether ``binding`` still names ``template`` on ``state`` — a live reference a detach
    of that template would strand (the named template_jq/attachment would vanish)."""
    return binding is not None and any(
        attach.state == state and template in attach.templates for attach in binding.states
    )


async def _preset_detach_referee(state: str, template: str) -> list[str]:
    # EVERY version of every live preset is a strandable reference: a rollback re-activates
    # (and re-attaches) a historical version's binding, so a version that can be rolled back
    # to counts exactly like the active one. Feature-off (no configured store) holds none.
    from tai42_contract.presets import PresetBody
    from tai42_kit.db import component_store_configured

    from tai42_skeleton.app import instance
    from tai42_skeleton.db import SKELETON_COMPONENT

    if not component_store_configured(SKELETON_COMPONENT):
        return []
    store = instance.app.presets.store
    holders: list[str] = []
    for record in await store.list_presets():
        for version in await store.list_versions(record.name):
            body = PresetBody.model_validate(version.body)
            if _binding_names_template(body.state_binding, state, template):
                holders.append(f"preset {record.name!r} version {version.version}")
    return holders


async def _conversation_config_detach_referee(state: str, template: str) -> list[str]:
    # A tool-target conversation config carries the door binding the route applies around the
    # tool turn. Feature-off (the redis conversations backend unset) holds none.
    from tai42_skeleton.conversations.cache import get_conversations_manager
    from tai42_skeleton.conversations.managers.in_memory_conversations_manager import InMemoryConversationsManager
    from tai42_skeleton.conversations.settings import ConversationsSettings
    from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore

    if isinstance(get_conversations_manager(), InMemoryConversationsManager):
        return []
    configs = await ConversationTargetConfigStore(ConversationsSettings()).list()
    return [
        f"conversation config {config.target_kind}/{config.target_name}"
        for config in configs.values()
        if _binding_names_template(config.state_binding, state, template)
    ]


async def _hook_detach_referee(state: str, template: str) -> list[str]:
    from tai42_skeleton.hooks.cache import get_hooks_manager

    hooks = await get_hooks_manager().list_hooks()
    return [
        f"hook {params.name!r}"
        for params in hooks.values()
        if _binding_names_template(params.state_binding, state, template)
    ]


async def _schedule_detach_referee(state: str, template: str) -> list[str]:
    # Read each schedule's exported ``kwargs['state_binding']`` — the JSON the recurring fire
    # applies — from the same raw export surface the rename schedule referee reads. Feature-off
    # holds none; an unreadable holder store fails loudly (never a silent stranding).
    from tai42_contract.states import StateBinding

    from tai42_skeleton.operations import NotSupportedError
    from tai42_skeleton.operations.schedules import export_schedules_raw
    from tai42_skeleton.tools.binding import UnknownToolError

    try:
        rows = await export_schedules_raw()
    except NotSupportedError:
        return []
    except UnknownToolError as exc:
        raise RuntimeError(
            f"schedule detach referee: scheduling backend registers no {exc.tool_name!r}; schedule holders "
            "cannot be verified, detach blocked"
        ) from exc

    if not isinstance(rows, list):
        raise TypeError(
            f"schedule detach referee: export_schedules_raw() returned a {type(rows).__name__}, expected a list of rows"
        )
    holders: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError(f"schedule detach referee: schedule record is a {type(row).__name__}, expected a dict")
        name = row.get("name")
        if not isinstance(name, str):
            raise TypeError(f"schedule detach referee: schedule record carries no string 'name' (keys: {sorted(row)})")
        kwargs = row.get("kwargs")
        if not isinstance(kwargs, dict):
            raise TypeError(
                f"schedule detach referee: schedule {name!r} carries no dict 'kwargs' (keys: {sorted(row)})"
            )
        raw = kwargs.get("state_binding")
        if raw is None:
            continue
        binding = StateBinding.model_validate(raw)
        if _binding_names_template(binding, state, template):
            holders.append(f"schedule {name!r}")
    return holders


def register_platform_detach_referees() -> None:
    """Arm the platform-internal template-detach referees on the live app's referee
    collection — a startup/reload handler (the collection is reset each ``start()``, so this
    re-arms every epoch). One registration per in-house binding-holder surface."""
    for referee in (
        _preset_detach_referee,
        _conversation_config_detach_referee,
        _hook_detach_referee,
        _schedule_detach_referee,
    ):
        tai42_app.tools.register_detach_referee(referee)
