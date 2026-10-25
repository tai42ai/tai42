"""One holder gather per action, shared by the action's gate and its preview.

A gate refuses on exactly the holders its preview door reports, because both read the same
gather. Every gather only reads: a referee answers holders and changes nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from tai42_contract.errors import OperationError
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.app import instance
from tai42_skeleton.operations.errors import ConflictError, OperationFailedError
from tai42_skeleton.operations.reference_graph import StoredPresets, _routes_bound_to
from tai42_skeleton.tools import holder_kinds

logger = logging.getLogger(__name__)


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


async def rename_holders(name: str, branches: Sequence[str], stored: StoredPresets) -> list[ReferenceHolder]:
    """Every live holder a rename of ``name`` would strand.

    The OTHER presets in ``stored`` whose active body composes ``name`` or one of ``branches``,
    then every rename referee's answer for each of ``[name, *branches]``.
    """
    holders = [
        ReferenceHolder(kind=holder_kinds.PRESET, name=composer, summary=f"preset {composer!r}")
        for composer in await stored.composers_of(name)
    ]
    for referee in instance.app.tools.rename_referees():
        for tool_name in [name, *branches]:
            holders.extend(_checked(referee, await referee(tool_name)))
    return holders


async def delete_holders(name: str, branches: Sequence[str]) -> list[ReferenceHolder]:
    """Every live holder a delete of ``name`` would strand.

    Every delete veto's answer for each of ``[name, *branches]``, then one holder per
    conversation route bound to one of those names, sorted by route; a route bound to a branch
    names the branch.
    """
    holders: list[ReferenceHolder] = []
    for referee in instance.app.tools.delete_referees():
        for tool_name in [name, *branches]:
            holders.extend(_checked(referee, await referee(tool_name)))
    routes = await _routes_bound_to({name, *branches})
    for route_name, route in sorted(routes.items()):
        summary = f"conversation route {route_name!r}"
        if route.target_name != name:
            summary = f"{summary} targets {route.target_name!r}"
        holders.append(ReferenceHolder(kind=holder_kinds.CONVERSATION_ROUTE, name=route_name, summary=summary))
    return holders


async def detach_holders(state: str, template: str) -> list[ReferenceHolder]:
    """Every live binding that still names ``template`` on ``state``, from every detach referee."""
    holders: list[ReferenceHolder] = []
    for referee in instance.app.tools.detach_referees():
        holders.extend(_checked(referee, await referee(state, template)))
    return holders


def holders_refusal(head: str, holders: Sequence[ReferenceHolder], tail: str) -> ConflictError:
    """The 409 a gate raises: ``head``, one line per holder's summary, ``tail``; the holders ride in ``extra``."""
    lines = "\n".join(f"- {holder.summary}" for holder in holders)
    return ConflictError(
        f"{head} — held by:\n{lines}\n{tail}",
        extra={"holders": [holder.model_dump() for holder in holders]},
    )


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
        raise holders_refusal(head, holders, "remove those references first")
