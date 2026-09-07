"""The platform's own four state-consumer listers — hooks, schedules, agents and presets.

A state's Consumers view is the union of every registered consumer lister
(:meth:`~tai42_skeleton.states.service.StatesService.consumers`). A plugin registers
its own (a plugin registers the kinds it binds with); the platform registers the four below for the
features it OWNS, so a state referenced only by a hook, a schedule, an agent or a preset is
both shown as bound and refused deletion by the ``DeclarationInUseError`` guard.

Each lister takes a state NAME and returns its :class:`~tai42_contract.states.ConsumerRow`
rows:

* **hooks** — every registered hook whose ``subject.kind`` is one of the state's declared
  ``subject_kinds`` (a hook targeting that subject family writes the state on fire).
* **schedules** — every schedule carrying a subject (stamped at creation under the kit's
  ``backend_schedule_subject`` arg) whose ``kind`` is one of the declared kinds. Read
  through ``backend_export_schedules`` (the same canonical ``ScheduleRecord`` source the
  tool-rename schedule referee reads): the friendly ``list_schedules`` rows carry only
  name/cadence, never the fired tool's arguments, so the subject a schedule targets is
  legible ONLY on the export surface. With no scheduling backend installed the lister
  emits ONE muted ``unavailable`` row — surfaced, never swallowed.
* **agents** — every registered agent whose statically-declared tool set names a
  ``state_*`` builtin. The state builtins address any state by argument, so such an agent
  is a potential writer of every declared state.
* **presets** — every agent-run-tool preset whose baked ``fixed_kwargs.tool_names`` name a
  ``state_*`` builtin. The preset bakes those tools onto its agent base, so the authored
  agent tool is a potential writer of every declared state — the same reach an agent that
  declares the builtin directly has, through a distinct bound tool.

Registered each epoch beside the states wiring (``app/instance.py``): the consumer-lister
registry is reset on every ``start()``/reload so plugin re-imports re-register cleanly,
and these platform-owned listers re-arm the same way — exactly the platform-rename-referee
pattern.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from tai42_contract.agent import Agent
from tai42_contract.hooks import HookParams
from tai42_contract.presets import PresetBody
from tai42_contract.states.models import ConsumerLink, ConsumerRow

from tai42_skeleton.app import instance

#: Consumer ``kind`` labels the Consumers view badges by.
_HOOK_KIND = "hook"
_SCHEDULE_KIND = "schedule"
_AGENT_KIND = "agent"
_PRESET_KIND = "preset"

#: The Studio route tokens each family's ``Open`` link points at (the shell resolves the
#: token to a path; all four feature pages take no search parameters).
_HOOKS_TOKEN = "hooks"
_SCHEDULING_TOKEN = "scheduling"
_AGENTS_TOKEN = "agents"
_PRESETS_TOKEN = "presets"

#: The builtin state tools share this name prefix; an agent that binds one consumes state.
_STATE_TOOL_PREFIX = "state_"

#: The muted line shown when scheduling is not deployed.
_NO_SCHEDULING_BACKEND = "no scheduling backend"


async def _declared_subject_kinds(state: str) -> set[str] | None:
    """The state's declared ``subject_kinds`` as a set, or ``None`` when no such state is
    declared — the listers report nothing for an undeclared state."""
    decl = await instance.app.states.get_declaration(state)
    return None if decl is None else set(decl.subject_kinds)


# --------------------------------------------------------------------------- #
# Pure row builders (registry data in, rows out — the matching logic, tested   #
# without the live process registries)                                         #
# --------------------------------------------------------------------------- #
def hook_consumer_rows(hooks: Iterable[HookParams], subject_kinds: set[str]) -> list[ConsumerRow]:
    """The hook rows for a state: one per hook whose ``subject.kind`` is declared."""
    rows: list[ConsumerRow] = []
    for hook in hooks:
        subject = hook.subject
        if subject is not None and subject.kind in subject_kinds:
            rows.append(
                ConsumerRow(
                    kind=_HOOK_KIND,
                    name=hook.name,
                    detail=f"supplies kind {subject.kind}",
                    link=ConsumerLink(token=_HOOKS_TOKEN),
                )
            )
    return rows


def schedule_consumer_rows(records: Iterable[dict[str, Any]], subject_kinds: set[str]) -> list[ConsumerRow]:
    """The schedule rows for a state: one per exported ``ScheduleRecord`` whose stamped
    subject's ``kind`` is declared. A malformed stamped subject raises loudly inside
    :func:`pop_schedule_subject` — a schedule that could never resolve is a bug, not a
    silently dropped row."""
    from tai42_kit.utils.schedule_subject import pop_schedule_subject

    rows: list[ConsumerRow] = []
    for record in records:
        subject = pop_schedule_subject(dict(record.get("kwargs") or {}))
        if subject is not None and subject.kind in subject_kinds:
            rows.append(
                ConsumerRow(
                    kind=_SCHEDULE_KIND,
                    name=record.get("name"),
                    detail=f"carries subject kind {subject.kind}",
                    link=ConsumerLink(token=_SCHEDULING_TOKEN),
                )
            )
    return rows


def _agent_state_tools(agent: Agent) -> list[str]:
    """The ``state_*`` builtins in an agent's statically-declared tool set, sorted.

    Read from the agent's declared ``tool_names`` (the fixed tool set an agent binds).
    An agent that resolves its tools at run time declares none, so it names no state
    tool here — the truthful answer, never a guess."""
    declared = getattr(agent, "tool_names", None)
    if declared is None:
        field = agent.ToolInput.model_fields.get("tool_names")
        declared = field.get_default(call_default_factory=True) if field is not None else None
    if not isinstance(declared, Iterable) or isinstance(declared, str | bytes):
        return []
    return sorted({name for name in declared if isinstance(name, str) and name.startswith(_STATE_TOOL_PREFIX)})


def agent_consumer_rows(agents: Mapping[str, Agent]) -> list[ConsumerRow]:
    """The agent rows: one per registered agent whose tool set names a ``state_*``
    builtin (the state builtins address any state, so such an agent consumes them all)."""
    rows: list[ConsumerRow] = []
    for name, agent in agents.items():
        tools = _agent_state_tools(agent)
        if tools:
            rows.append(
                ConsumerRow(
                    kind=_AGENT_KIND,
                    name=name,
                    detail=f"uses {', '.join(tools)}",
                    link=ConsumerLink(token=_AGENTS_TOKEN),
                )
            )
    return rows


def _preset_state_tools(body: PresetBody) -> list[str]:
    """The ``state_*`` builtins in a preset's baked ``fixed_kwargs.tool_names``, sorted.

    An agent-run-tool preset bakes its agent base's ``tool_names`` under
    ``fixed_kwargs``; a preset that bakes none (or a non-agent base whose kwargs carry no
    ``tool_names`` list) names no state tool here — the truthful answer, never a guess."""
    baked = body.fixed_kwargs.get("tool_names")
    if not isinstance(baked, list):
        return []
    return sorted({name for name in baked if isinstance(name, str) and name.startswith(_STATE_TOOL_PREFIX)})


def preset_consumer_rows(bodies: Mapping[str, PresetBody]) -> list[ConsumerRow]:
    """The preset rows: one per preset whose baked ``tool_names`` name a ``state_*``
    builtin (the preset bakes those tools onto its agent base, so the authored agent tool
    consumes every declared state — the same reach as :func:`agent_consumer_rows`)."""
    rows: list[ConsumerRow] = []
    for name, body in sorted(bodies.items()):
        tools = _preset_state_tools(body)
        if tools:
            rows.append(
                ConsumerRow(
                    kind=_PRESET_KIND,
                    name=name,
                    detail=f"binds {', '.join(tools)}",
                    link=ConsumerLink(token=_PRESETS_TOKEN),
                )
            )
    return rows


# --------------------------------------------------------------------------- #
# The registered listers (process registries in, rows out)                     #
# --------------------------------------------------------------------------- #
async def hooks_lister(state: str) -> Sequence[ConsumerRow]:
    kinds = await _declared_subject_kinds(state)
    if kinds is None:
        return []
    from tai42_skeleton.hooks.cache import get_hooks_manager

    hooks = await get_hooks_manager().list_hooks()
    return hook_consumer_rows(hooks.values(), kinds)


async def schedules_lister(state: str) -> Sequence[ConsumerRow]:
    kinds = await _declared_subject_kinds(state)
    if kinds is None:
        return []
    from tai42_skeleton.operations import NotSupportedError
    from tai42_skeleton.operations.schedules import export_schedules_raw

    try:
        records = await export_schedules_raw()
    except NotSupportedError:
        # No scheduling backend is installed — the family cannot be listed. Surface the
        # muted row instead of a silent empty (§4.11: "never swallowed").
        return [ConsumerRow(kind=_SCHEDULE_KIND, unavailable=_NO_SCHEDULING_BACKEND)]
    if not isinstance(records, list):
        raise TypeError(
            f"schedules consumer lister: export_schedules_raw() returned a {type(records).__name__}, "
            "expected a list of ScheduleRecord rows"
        )
    return schedule_consumer_rows(records, kinds)


async def agents_lister(state: str) -> Sequence[ConsumerRow]:
    if await _declared_subject_kinds(state) is None:
        return []
    return agent_consumer_rows(instance.app.agents.all_agents())


async def presets_lister(state: str) -> Sequence[ConsumerRow]:
    if await _declared_subject_kinds(state) is None:
        return []
    from tai42_kit.db import component_store_configured

    from tai42_skeleton.db import SKELETON_COMPONENT

    # Presets live in the skeleton component's versioned store; a store-less deploy has
    # none, so skip the read rather than fault (the presets list route guards the same way).
    if not component_store_configured(SKELETON_COMPONENT):
        return []
    return preset_consumer_rows(await instance.app.presets.list_active_bodies())


def register_platform_consumer_listers() -> None:
    """Register the platform's own four consumer listers on the live states facet.

    Called each ``start()``/reload (the consumer-lister registry is reset each epoch), so
    the platform families re-arm alongside the plugin re-registrations — the
    platform-rename-referee pattern."""
    states = instance.app.states
    states.register_consumer_lister(_HOOK_KIND, hooks_lister)
    states.register_consumer_lister(_SCHEDULE_KIND, schedules_lister)
    states.register_consumer_lister(_AGENT_KIND, agents_lister)
    states.register_consumer_lister(_PRESET_KIND, presets_lister)
