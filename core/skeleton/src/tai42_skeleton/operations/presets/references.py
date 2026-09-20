"""The preset reference graph and the agent-name space.

Which tools a preset body composes, the uses / used_by maps, and the rename /
delete referee unions.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from tai42_contract.agent.base import PresetSpec
from tai42_contract.presets import PresetBody

from tai42_skeleton.app import instance


def _agent_tool_names() -> set[str]:
    """Every registered agent's declared ``tool_name``.

    A ``tool_name`` can differ from the decorator-registration name: the run tool
    binds under the REGISTRATION name (so that name is already a live tool the
    ``name_conflicts`` guard catches), but the ``tool_name`` may not be a bound tool.
    Keeping the authored-agent name off BOTH sets keeps one unambiguous agent-name
    space.
    """
    return {agent.tool_name for agent in instance.app.agents.all_agents().values()}


def _check_tool_names(node: dict[str, Any], tools: set[str], where: str) -> str | None:
    """The ``tool_names`` leg of one spec node, or the first violation message.

    A list whose every entry is a string naming a registered tool.
    """
    tool_names = node.get("tool_names", [])
    if not isinstance(tool_names, list):
        return f"{where}.tool_names must be a list of tool names"
    for tool_name in tool_names:
        if not isinstance(tool_name, str):
            return f"{where}.tool_names entries must be strings"
        if tool_name not in tools:
            return f"{where}.tool_names references unknown tool {tool_name!r}"
    return None


def _check_inline_presets(
    node: dict[str, Any], tools: set[str], preset_names: frozenset[str], where: str
) -> str | None:
    """The inline ``presets`` leg of one spec node, or the first violation.

    A list of self-contained :class:`PresetSpec`, each with a non-empty description and
    a ``base_tool`` that is a registered NON-preset tool (the flat-preset create rule).
    """
    presets = node.get("presets", [])
    if not isinstance(presets, list):
        return f"{where}.presets must be a list of preset specs"
    for i, entry in enumerate(presets):
        try:
            preset = PresetSpec.model_validate(entry)
        except ValidationError as exc:
            return f"{where}.presets[{i}] is not a valid preset spec: {exc}"
        # An inline preset becomes a bound tool whose docstring IS its description,
        # fed verbatim to the LLM — an empty one is a behavioral defect, refused at
        # authoring exactly like the create door refuses it (side door closed).
        if not preset.description.strip():
            return f"{where}.presets[{i}] description must not be empty"
        if preset.base_tool in preset_names:
            return f"{where}.presets[{i}] base_tool {preset.base_tool!r} is itself a preset"
        if preset.base_tool not in tools:
            return f"{where}.presets[{i}] base_tool {preset.base_tool!r} is not a registered tool"
    return None


def _check_subagents(node: dict[str, Any], tools: set[str], preset_names: frozenset[str], where: str) -> str | None:
    """The recursive ``subagents`` leg of one spec node, or the first violation.

    Each entry is an object whose own references resolve at every depth (recurses into
    :func:`_spec_reference_error`).
    """
    subagents = node.get("subagents", [])
    if not isinstance(subagents, list):
        return f"{where}.subagents must be a list of sub-agent specs"
    for i, entry in enumerate(subagents):
        if not isinstance(entry, dict):
            return f"{where}.subagents[{i}] must be an object"
        nested = _spec_reference_error(entry, tools, preset_names, f"{where}.subagents[{i}]")
        if nested is not None:
            return nested
    return None


def _spec_reference_error(
    node: dict[str, Any], tools: set[str], preset_names: frozenset[str], where: str
) -> str | None:
    """The first unresolved reference in one spec node, or ``None`` if all resolve.

    Checks the node's ``tool_names`` (each must be a registered tool), its inline
    ``presets`` (each a self-contained ``PresetSpec`` whose ``base_tool`` is a
    registered NON-preset tool — the same flat-preset rule as the create route), and
    recurses into every inline ``subagents`` spec, so a bad reference at ANY depth is
    caught loudly rather than silently dropped.
    """
    return (
        _check_tool_names(node, tools, where)
        or _check_inline_presets(node, tools, preset_names, where)
        or _check_subagents(node, tools, preset_names, where)
    )


def _referenced_tool_names(node: dict[str, Any]) -> set[str]:
    """Every tool name a spec node composes in its ``tool_names`` at any depth, recursing ``subagents``.

    The SAME traversal :func:`_spec_reference_error` walks, read-only. Only
    ``tool_names`` can name a preset: inline ``presets`` entries and a ``base_tool`` are
    rejected at authoring if they name a preset, so neither is scanned here. This is the
    builtin ``fixed_kwargs`` walk the combined collector :func:`_preset_references`
    unions with the base tool's declared extractor.
    """
    names: set[str] = set()
    tool_names = node.get("tool_names", [])
    if isinstance(tool_names, list):
        names.update(name for name in tool_names if isinstance(name, str))
    subagents = node.get("subagents", [])
    if isinstance(subagents, list):
        for entry in subagents:
            if isinstance(entry, dict):
                names.update(_referenced_tool_names(entry))
    return names


def _preset_references(body: PresetBody) -> set[str]:
    """Every tool name a preset body composes as tools.

    The UNION of the builtin ``fixed_kwargs`` walk (:func:`_referenced_tool_names`) and
    the names the base tool's DECLARED ``tool_refs`` extractor reads from
    ``fixed_kwargs`` (only when one is registered for ``body.base_tool``). Population
    intersection, self-exclusion and sorting are the callers' concern. The extractor is
    called raw: an exception propagates loudly, and a non-string entry it returns is a
    plugin bug raised here — never silently dropped, never an empty-list fallback.
    """
    names = _referenced_tool_names(body.fixed_kwargs)
    extractor = instance.app.tools.tool_refs_extractor(body.base_tool)
    if extractor is not None:
        for entry in extractor(body.fixed_kwargs):
            if not isinstance(entry, str):
                raise TypeError(
                    f"tool_refs extractor for base tool {body.base_tool!r} returned a non-string reference {entry!r}"
                )
            names.add(entry)
    return names


def _reference_maps(bodies: dict[str, PresetBody]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """The ``uses`` and ``used_by`` maps over the active preset population, built in one pass.

    ``uses[X]`` is the sorted OTHER preset names X's active body composes as tools (the
    combined collector :func:`_preset_references`, intersected with the population so
    base and foreign tools never appear); ``used_by[X]`` is the sorted OTHER presets
    whose active bodies compose X. Self is never listed on either side. Every population
    name is a key on both maps, empty when it has no references.
    """
    population = set(bodies)
    uses: dict[str, set[str]] = {name: set() for name in bodies}
    used_by: dict[str, set[str]] = {name: set() for name in bodies}
    for name, body in bodies.items():
        for referenced in _preset_references(body) & population:
            if referenced == name:
                continue
            uses[name].add(referenced)
            used_by[referenced].add(name)
    return (
        {name: sorted(names) for name, names in uses.items()},
        {name: sorted(names) for name, names in used_by.items()},
    )


def _referencing_presets(old_name: str, bodies: dict[str, PresetBody]) -> list[str]:
    """Every OTHER preset whose ACTIVE body composes ``old_name`` as a tool, sorted.

    Uses the combined collector :func:`_preset_references`, so a DECLARED reference
    counts too; sorted for a stable, fully-listed answer. This is the PRESET-BODY leg of
    the rename referee union — :func:`_rename_referees` unions it with every registered
    referee (platform wiring + plugin providers). Only active bodies are walked: a
    non-active historical version may still name the old tool, loud at authoring / run
    time if ever rolled back (delete's existing posture).
    """
    return sorted(name for name, body in bodies.items() if name != old_name and old_name in _preset_references(body))


async def _rename_referees(name: str) -> list[str]:
    """Every live reference a rename of ``name`` would strand — the full union the rename gate blocks on.

    Also what the referees preview door reports: the OTHER presets whose active body
    composes ``name`` (:func:`_referencing_presets`) plus every registered rename
    referee's descriptions (the platform-internal wiring referees + any plugin
    provider). Each referee is consulted for the OLD name; a referee RAISING propagates
    loudly — a rename never proceeds past an unreadable holder store (no silent bypass).
    """
    holders = _referencing_presets(name, await instance.app.presets.list_active_bodies())
    for referee in instance.app.tools.rename_referees():
        holders.extend(await referee(name))
    return holders


async def _delete_referees(name: str) -> list[str]:
    """Every VETO a delete of preset ``name`` draws from the registered delete referees.

    Referees are plugin providers holding resources keyed on the name — e.g. per-node
    state bindings referencing the preset. Each referee either CASCADES its own cleanup
    and returns empty (allow) or returns non-empty descriptions to block; a referee
    RAISING propagates loudly — a delete never proceeds past an unreadable holder store
    (no silent bypass).
    """
    blockers: list[str] = []
    for referee in instance.app.tools.delete_referees():
        blockers.extend(await referee(name))
    return blockers
