"""One holder gather per action, shared by the action's gate and its preview.

A gate refuses on exactly the holders its preview door reports, because both read the same
gather. Every gather only reads: a referee answers holders and changes nothing. The refusal and
the preview render the caller's view: a holder under a feature tag the caller may not read is
shown as a count, never by name.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tai42_contract.errors import OperationError
from tai42_contract.tools import ReferenceHolder, ToolDeleteReferee, ToolRenameReferee

from tai42_skeleton.access_control.feature_reach import readable_feature_tags
from tai42_skeleton.app import instance
from tai42_skeleton.operations.errors import ConflictError, OperationFailedError
from tai42_skeleton.operations.reference_graph import StoredPresets, _routes_bound_to
from tai42_skeleton.tools import holder_kinds

logger = logging.getLogger(__name__)

_ByNameReferee = ToolRenameReferee | ToolDeleteReferee


def bound_branches(name: str) -> list[str]:
    """The extension branch tools bound off preset ``name``, in registration order.

    ``[]`` for a quarantined record: it was never registered, so a tool holding its name is a
    different tool and the branches bound off it are that tool's.
    """
    if instance.app.preset_manager.is_quarantined(name):
        return []
    return instance.app.tools.branches_of(name)


def _provider_name(provider: object) -> str:
    module = getattr(provider, "__module__", None) or type(provider).__module__
    qualname = getattr(provider, "__qualname__", None) or type(provider).__qualname__
    return f"{module}.{qualname}"


def _checked(provider: object, answer: object) -> list[ReferenceHolder]:
    """``answer`` when it is a list of ``ReferenceHolder``; any other answer is the provider's bug, raised."""
    if not isinstance(answer, list) or not all(isinstance(entry, ReferenceHolder) for entry in answer):
        raise TypeError(
            f"referee {_provider_name(provider)} answered {answer!r}; a referee answers a list of ReferenceHolder"
        )
    return answer


def _checked_by_name(provider: object, answer: object, asked: frozenset[str]) -> dict[str, list[ReferenceHolder]]:
    """``answer`` when it maps asked names to lists of ``ReferenceHolder``; any other answer is the provider's bug."""
    if not isinstance(answer, dict) or not all(
        key in asked and isinstance(holders, list) and all(isinstance(entry, ReferenceHolder) for entry in holders)
        for key, holders in answer.items()
    ):
        raise TypeError(
            f"referee {_provider_name(provider)} answered {answer!r} for {sorted(asked)!r}; a referee answers "
            "a dict from asked tool names to lists of ReferenceHolder"
        )
    return answer


async def _asked_once(
    referees: Sequence[_ByNameReferee], names: Sequence[str]
) -> list[dict[str, list[ReferenceHolder]]]:
    """Each referee's answer for all of ``names`` together, one ask per referee, in registration order."""
    asked = frozenset(names)
    return [_checked_by_name(referee, await referee(asked), asked) for referee in referees]


async def rename_holders_by_tool(
    tools: Mapping[str, Sequence[str]], stored: StoredPresets
) -> dict[str, list[ReferenceHolder]]:
    """Every live holder a rename of each tool in ``tools`` would strand, keyed by that tool.

    ``tools`` maps each tool to the extension branches bound off it. A tool's holders are the
    OTHER presets in ``stored`` whose active body composes the tool or one of its branches, then
    every rename referee's answer for the tool and each of its branches, in that order. Every
    rename referee is asked once, for every tool and branch together.
    """
    names = list(dict.fromkeys(name for tool, branches in tools.items() for name in (tool, *branches)))
    answers = await _asked_once(instance.app.tools.rename_referees(), names)
    by_tool: dict[str, list[ReferenceHolder]] = {}
    for tool, branches in tools.items():
        holders = [
            holder_kinds.PRESET.holder(name=composer, summary=f"preset {composer!r}")
            for composer in await stored.composers_of(tool)
        ]
        for answer in answers:
            for name in (tool, *branches):
                holders.extend(answer.get(name, []))
        by_tool[tool] = holders
    return by_tool


async def rename_holders(name: str, branches: Sequence[str], stored: StoredPresets) -> list[ReferenceHolder]:
    """Every live holder a rename of ``name`` and its ``branches`` would strand (:func:`rename_holders_by_tool`)."""
    return (await rename_holders_by_tool({name: branches}, stored))[name]


async def delete_holders(name: str, branches: Sequence[str]) -> list[ReferenceHolder]:
    """Every live holder a delete of ``name`` would strand.

    Every delete veto's answer for each of ``[name, *branches]`` (each veto asked once, for all
    of them together), then one holder per conversation route bound to one of those names,
    sorted by route; a route bound to a branch names the branch.
    """
    names = list(dict.fromkeys([name, *branches]))
    holders: list[ReferenceHolder] = []
    for answer in await _asked_once(instance.app.tools.delete_referees(), names):
        for tool_name in names:
            holders.extend(answer.get(tool_name, []))
    routes = await _routes_bound_to({name, *branches})
    for route_name, route in sorted(routes.items()):
        summary = f"conversation route {route_name!r}"
        if route.target_name != name:
            summary = f"{summary} targets {route.target_name!r}"
        holders.append(holder_kinds.CONVERSATION_ROUTE.holder(name=route_name, summary=summary))
    return holders


async def detach_holders(state: str, template: str) -> list[ReferenceHolder]:
    """Every live binding that still names ``template`` on ``state``, from every detach referee."""
    holders: list[ReferenceHolder] = []
    for referee in instance.app.tools.detach_referees():
        holders.extend(_checked(referee, await referee(state, template)))
    return holders


@dataclass(frozen=True)
class _CallerView:
    """The holders as the acting caller may read them.

    ``holders``: each holder under a tag the caller may read as it is; the others of one
    ``(kind, tag)`` collapsed, at the first one's place, into one count holder that names
    nothing (``name`` the tag, locator ``count``). ``withheld_tags``: the collapsed tags,
    sorted. ``named``: how many holders are shown as they are.
    """

    holders: list[ReferenceHolder]
    withheld_tags: list[str]
    named: int


def _count_holder(kind: str, tag: str, count: int) -> ReferenceHolder:
    return ReferenceHolder(
        kind=kind, tag=tag, name=tag, locator={"count": count}, summary=f"{kind}: {count} not readable with your role"
    )


async def _caller_view(holders: Sequence[ReferenceHolder]) -> _CallerView:
    readable = await readable_feature_tags({holder.tag for holder in holders})
    counts: dict[tuple[str, str], int] = {}
    for holder in holders:
        if holder.tag not in readable:
            key = (holder.kind, holder.tag)
            counts[key] = counts.get(key, 0) + 1
    view: list[ReferenceHolder] = []
    placed: set[tuple[str, str]] = set()
    for holder in holders:
        key = (holder.kind, holder.tag)
        if key not in counts:
            view.append(holder)
        elif key not in placed:
            placed.add(key)
            view.append(_count_holder(holder.kind, holder.tag, counts[key]))
    return _CallerView(
        holders=view,
        withheld_tags=sorted({tag for _kind, tag in counts}),
        named=len(holders) - sum(counts.values()),
    )


def _quoted(tags: Sequence[str]) -> str:
    quoted = [repr(tag) for tag in tags]
    return quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} and {quoted[-1]}"


async def holders_refusal(head: str, holders: Sequence[ReferenceHolder], tail: str) -> ConflictError:
    """The 409 a gate raises over the caller's view of ``holders``: ``head``, one line per holder, then what to do.

    ``tail`` is what clears the refusal (``"remove those references first"``). When every
    holder is withheld from the caller, it reads who can do that; when some are, it reads that
    the others need someone with access to their tags. The view's holders ride in ``extra``.
    """
    view = await _caller_view(holders)
    if view.withheld_tags:
        others = _quoted(view.withheld_tags)
        if view.named:
            tail = f"{tail}; the others need someone with access to {others}"
        else:
            tail = f"ask someone with access to {others} to {tail}"
    lines = "\n".join(f"- {holder.summary}" for holder in view.holders)
    return ConflictError(
        f"{head} — held by:\n{lines}\n{tail}",
        extra={"holders": [holder.model_dump() for holder in view.holders]},
    )


async def holders_preview(action: str, holders: Sequence[ReferenceHolder]) -> dict[str, Any]:
    """A preview door's answer: ``action`` and the caller's view of ``holders`` (:func:`holders_refusal`'s view)."""
    return {"action": action, "holders": [holder.model_dump() for holder in (await _caller_view(holders)).holders]}


async def template_delete_holders(ids: frozenset[str]) -> list[ReferenceHolder]:
    """Every holder of one of ``ids``: each template-delete referee's answer, in registration order."""
    holders: list[ReferenceHolder] = []
    for referee in instance.app.storage.template_delete_referees():
        holders.extend(_checked(referee, await referee(ids)))
    return holders


async def refuse_held_templates(ids: frozenset[str], head: str) -> None:
    """Raise the 409 naming every holder of one of ``ids``; nothing held passes.

    An ``OperationError`` a referee raises propagates as it is. Any other fault is logged and
    raised as a 500 whose text carries only the fault's type, never its message (a settings
    validation error quotes the value).
    """
    try:
        holders = await template_delete_holders(ids)
    except OperationError:
        raise
    except Exception as exc:
        logger.exception("%s: a holder could not be read", head)
        raise OperationFailedError(f"{head}: a holder could not be read ({type(exc).__name__})") from exc
    if holders:
        raise await holders_refusal(head, holders, "remove those references first")
