"""The platform's template-detach referees: the in-house door bindings that name a state template.

Armed skeleton-side at boot, every epoch (never through the public ``register_detach_referee``
seam; they are not plugins). Each answers the template-detach gate for a ``(state, template)``
with the live door bindings that still name that template on that state: a preset version, a
conversation config, a hook, or a schedule whose ``state_binding`` references it. Every
referee only reads (the detach preview calls them). An empty answer is no objection; a raising
referee fails the gate loudly: an unreadable holder store must never let a stranding detach
through.

The feature-OFF case is not an error: a referee whose backing store is unconfigured has no
holders and returns an empty answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_contract.app import tai42_app
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.tools import holder_kinds

if TYPE_CHECKING:
    from tai42_contract.states import StateBinding


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
    # to counts exactly like the active one. Every version of every preset comes from one read;
    # a deployment with no skeleton database has no stored presets.
    from tai42_skeleton.app import instance

    return [
        holder_kinds.PRESET_VERSION.holder(
            name=name,
            locator={"version": version},
            summary=f"preset {name!r} version {version}",
        )
        for name, versions in (await instance.app.presets.list_version_bodies()).items()
        for version, body in versions
        if _binding_names_template(body.state_binding, state, template)
    ]


async def _conversation_config_detach_referee(state: str, template: str) -> list[ReferenceHolder]:
    # A tool-target conversation config carries the door binding the route applies around the
    # tool turn. Feature-off (the redis conversations backend unset) holds none.
    from tai42_skeleton.conversations.cache import get_conversations_manager

    manager = get_conversations_manager()
    if not manager.durable:
        return []
    configs, _ = await manager.target_configs.list()
    return [
        holder_kinds.CONVERSATION_CONFIG.holder(
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
        holder_kinds.HOOK.holder(name=params.name, summary=f"hook {params.name!r}")
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
            holders.append(holder_kinds.SCHEDULE.holder(name=record.name, summary=f"schedule {record.name!r}"))
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
