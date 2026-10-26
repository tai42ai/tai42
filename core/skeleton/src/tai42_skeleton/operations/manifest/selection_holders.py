"""The refusal of an MCP selection change that would stop picking a tool something still uses.

Every manifest write passes :class:`~tai42_skeleton.config.service.ConfigService`, which runs
:func:`refuse_held_unpicks` on the stored document and the one it would persist. The check
decides from the stored manifest, the stored presets and the server's own listing, never from
what the answering worker has bound, so every worker gives the same verdict.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.app import instance
from tai42_skeleton.manifest import selection_admits
from tai42_skeleton.operations.errors import UpstreamError
from tai42_skeleton.operations.holders import holders_refusal, rename_holders_by_tool
from tai42_skeleton.operations.reference_graph import StoredPresets
from tai42_skeleton.tools import holder_kinds
from tai42_skeleton.tools.mcp_health import redact_probe_error
from tai42_skeleton.tools.naming import normalized_name

if TYPE_CHECKING:
    from tai42_contract.manifest import TaiMCPConfig

    from tai42_skeleton.manifest import Manifest

_TAIL = "remove those references first, or keep those tools picked"


async def unpick_holders(names: Sequence[str], stored: StoredPresets) -> dict[str, list[ReferenceHolder]]:
    """Who still uses each tool in ``names``, keyed by tool, each summary naming the tool.

    For each tool: every preset in ``stored`` whose ``base_tool`` is that tool, then everything
    :func:`rename_holders_by_tool` names for it and the extension branches this worker has bound
    off it over ``stored``; a holder both legs name is listed once. Every rename referee is
    asked once, for all of ``names`` together.
    """
    renamed = await rename_holders_by_tool({name: instance.app.tools.branches_of(name) for name in names}, stored)
    by_tool: dict[str, list[ReferenceHolder]] = {}
    for name in names:
        holders = [
            holder_kinds.PRESET.holder(name=preset, summary=f"preset {preset!r}")
            for preset, body in sorted(stored.bodies.items())
            if body.base_tool == name
        ]
        for holder in renamed[name]:
            if holder not in holders:
                holders.append(holder)
        by_tool[name] = [
            holder.model_copy(update={"summary": f"{holder.summary}, which uses {name!r}"}) for holder in holders
        ]
    return by_tool


def _listed_names(entry: TaiMCPConfig) -> list[str]:
    """The names ``entry``'s lists carry that are served names of its title."""
    names = [*entry.include, *entry.exclude]
    return [name for name in dict.fromkeys(names) if normalized_name(entry.title, name) == name]


async def _served(entry: TaiMCPConfig, served_names: Callable[[TaiMCPConfig], Awaitable[Sequence[str]]]) -> set[str]:
    try:
        return set(await served_names(entry))
    except Exception as exc:
        raise UpstreamError(
            f"tool selection of {entry.title!r} cannot be checked: the server did not list its tools: "
            f"{redact_probe_error(str(exc))}"
        ) from exc


class _StoredOnce:
    """The stored presets, read at the first ask and kept for the rest of one check."""

    def __init__(self) -> None:
        self._stored: StoredPresets | None = None

    async def get(self) -> StoredPresets:
        if self._stored is None:
            self._stored = await StoredPresets.read()
        return self._stored


async def _blocked_unpicks(
    old: TaiMCPConfig,
    new: TaiMCPConfig,
    served_names: Callable[[TaiMCPConfig], Awaitable[Sequence[str]]],
    stored: _StoredOnce,
) -> dict[str, list[ReferenceHolder]]:
    """The names ``new`` stops picking that something still uses, each with its holders."""

    def leaves(name: str) -> bool:
        return selection_admits(name, old.include, old.exclude) and not selection_admits(name, new.include, new.exclude)

    leaving = {name for name in [*_listed_names(old), *_listed_names(new)] if leaves(name)}
    served: set[str] | None = None
    if not old.include and new.include:
        served = await _served(old, served_names)
        leaving |= {name for name in served if leaves(name)}

    held: dict[str, list[ReferenceHolder]] = {}
    if leaving:
        unpicked = await unpick_holders(sorted(leaving), await stored.get())
        held = {name: holders for name, holders in unpicked.items() if holders}
    if not held:
        return {}
    if served is None:
        served = await _served(old, served_names)
    bodies = (await stored.get()).bodies
    return {name: holders for name, holders in held.items() if name in served and name not in bodies}


async def refuse_held_unpicks(
    before: Manifest,
    after: Manifest,
    served_names: Callable[[TaiMCPConfig], Awaitable[Sequence[str]]],
) -> None:
    """Raise ``ConflictError`` when ``after`` would stop picking a tool something still uses.

    For each ``mcp`` title in both manifests whose ``include`` or ``exclude`` differs, the
    leaving names are the entry's listed names (those carrying the title's served-name prefix)
    that ``selection_admits`` turns from picked under ``before`` to not picked under ``after``,
    plus, when ``before`` picks by exclusion and ``after`` by an include list, the served names
    it turns so (``served_names`` lists ``before``'s entry for that). The stored presets' active
    bodies are read once, at the first leaving name, each walked at most once for the whole
    check, and the leaving names are put to :func:`unpick_holders` over them together. When a leaving
    name has a holder, the server is listed (once per title): a held name it does not serve is
    not bound through this entry and does not refuse, a served one that a stored preset owns is
    dropped (the binder binds no MCP tool under a stored preset's name), and any other refuses.
    A listing failure raises ``UpstreamError`` naming the title: the check never passes on a
    server it could not list.
    """
    before_entries = {entry.title: entry for entry in before.mcp}
    stored = _StoredOnce()
    heads: list[str] = []
    refusing: list[ReferenceHolder] = []
    for entry in after.mcp:
        old = before_entries.get(entry.title)
        if old is None or (list(old.include), list(old.exclude)) == (list(entry.include), list(entry.exclude)):
            continue
        blocked = await _blocked_unpicks(old, entry, served_names, stored)
        if blocked:
            heads.append(f"tool selection of {entry.title!r} cannot unpick {', '.join(repr(n) for n in blocked)}")
            for holders in blocked.values():
                refusing.extend(holders)
    if heads:
        raise await holders_refusal("; ".join(heads), refusing, _TAIL)
