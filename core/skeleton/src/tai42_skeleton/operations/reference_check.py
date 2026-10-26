"""The one check that every name a write stores resolves on the live registries and the store.

Every write door hands its references here; a miss is one 400 naming every unresolved
reference, one line each, in the order the door gave them. A ``template`` reference is a
stored template id, resolved by the storage provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from tai42_contract.tools import TemplateRef

from tai42_skeleton.app import instance
from tai42_skeleton.operations.errors import BadRequestError
from tai42_skeleton.template.references import template_fault_lines


@dataclass(frozen=True)
class RegistryReference:
    """A name a write stores, the registry that must serve it, and where the holder names it.

    ``kind`` is the caller-facing label ("base tool", "hook tool", "stored id"); ``site`` is
    the place inside the holder, set for references met at or under an extractor entry and
    for every ``template`` reference.
    """

    kind: str
    name: str
    registry: Literal["tool", "agent", "template", "channel"] = "tool"
    site: str | None = None


def _refusal_line(ref: RegistryReference) -> str:
    at = f" at {ref.site}" if ref.site is not None else ""
    return f"{ref.kind} {ref.name!r}{at} is not a registered {ref.registry}"


def reference_refusal(missing: Sequence[RegistryReference]) -> str:
    """One line per missing reference: ``<kind> '<name>'[ at <site>] is not a registered <registry>``."""
    return "\n".join(_refusal_line(ref) for ref in missing)


async def _unresolved_names(refs: list[RegistryReference]) -> set[RegistryReference]:
    """The tool, agent and channel refs the live registries do not serve.

    A tool is looked up by its name, each distinct name once; the agent and channel registries
    are read whole, at most once each.
    """
    tool_names = list(dict.fromkeys(ref.name for ref in refs if ref.registry == "tool"))
    served_tools = dict(
        zip(
            tool_names,
            await asyncio.gather(*(instance.app.tools.has_tool(name) for name in tool_names)),
            strict=True,
        )
    )
    agents: set[str] | None = None
    channels: set[str] | None = None
    missing: set[RegistryReference] = set()
    for ref in refs:
        if ref.registry == "tool":
            served = served_tools[ref.name]
        elif ref.registry == "agent":
            if agents is None:
                agents = set(instance.app.agents.all_agents())
            served = ref.name in agents
        elif ref.registry == "channel":
            if channels is None:
                channels = set(instance.app.channels.names())
            served = ref.name in channels
        else:
            continue
        if not served:
            missing.add(ref)
    return missing


def as_template_ref(ref: RegistryReference) -> TemplateRef:
    """A ``template`` reference as the contract's ``TemplateRef``; a site-less one is a programming error."""
    if ref.site is None:
        raise RuntimeError(f"stored id {ref.name!r} reference carries no site")
    return TemplateRef(id=ref.name, site=ref.site)


async def reference_fault_lines(refs: Iterable[RegistryReference]) -> list[str]:
    """One refusal line per unresolved ref, in input order, de-duplicated."""
    unique = list(dict.fromkeys(refs))
    missing = await _unresolved_names(unique)
    template_lines = await template_fault_lines(as_template_ref(ref) for ref in unique if ref.registry == "template")
    lines: list[str] = []
    for ref in unique:
        if ref.registry == "template":
            line = template_lines.get(as_template_ref(ref))
            if line is not None:
                lines.append(line)
        elif ref in missing:
            lines.append(_refusal_line(ref))
    return lines


async def assert_references_resolve(refs: Iterable[RegistryReference]) -> None:
    """Raise ``BadRequestError`` with every fault line, joined by newlines, when any ref does not resolve."""
    lines = await reference_fault_lines(refs)
    if lines:
        raise BadRequestError("\n".join(lines))


__all__ = [
    "RegistryReference",
    "as_template_ref",
    "assert_references_resolve",
    "reference_fault_lines",
    "reference_refusal",
]
