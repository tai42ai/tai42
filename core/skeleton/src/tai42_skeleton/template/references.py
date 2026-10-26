"""The one check that a stored template id a write names is held by the storage provider.

An id resolves when it is a canonical store key and the provider's ``stat`` answers it.
The bare id is checked for every slot: a template a slot names by id has its default
stored, whatever locale variants sit beside it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable

from tai42_contract.tools import TemplateRef

from tai42_skeleton.template.path_guard import UnsafeTemplatePathError, safe_template_path


async def _id_fault(resource_id: str) -> str | None:
    """Why ``resource_id`` does not resolve (the text after ``stored id '<id>' at <site>``), else ``None``.

    A provider failure other than ``FileNotFoundError`` propagates: a store that cannot
    answer is not a refusal.
    """
    from tai42_skeleton.app import instance  # the template layer sits below the app package

    try:
        safe_template_path(resource_id)
    except UnsafeTemplatePathError as exc:
        return f": {exc}"
    provider = instance.app.storage.provider
    if provider is None:
        return " is not a stored template: no storage provider is installed"
    try:
        await provider.stat(resource_id)
    except FileNotFoundError:
        return " is not a stored template"
    return None


async def template_fault_lines(refs: Iterable[TemplateRef]) -> dict[TemplateRef, str]:
    """The fault line of each reference that does not resolve, keyed by reference, in input order.

    De-duplicated by (id, site); each distinct id is checked once, every check in flight at
    once in one task group. A provider failure cancels the checks still in flight and
    propagates: a lone failure as itself, failures that landed together as their
    ``ExceptionGroup``.
    """
    unique = list(dict.fromkeys(refs))
    ids = list(dict.fromkeys(ref.id for ref in unique))
    try:
        async with asyncio.TaskGroup() as group:
            checks = [group.create_task(_id_fault(resource_id)) for resource_id in ids]
    except ExceptionGroup as failed:
        if len(failed.exceptions) == 1:
            raise failed.exceptions[0] from None
        raise
    faults = {resource_id: check.result() for resource_id, check in zip(ids, checks, strict=True)}
    return {ref: f"stored id {ref.id!r} at {ref.site}{faults[ref.id]}" for ref in unique if faults[ref.id] is not None}


async def template_reference_faults(refs: Iterable[TemplateRef]) -> list[str]:
    """One line per reference that does not resolve, in input order, de-duplicated by (id, site)."""
    return list((await template_fault_lines(refs)).values())


async def assert_templates_stored(refs: Iterable[TemplateRef], error: Callable[[str], Exception]) -> None:
    """Raise ``error`` with every fault line, joined by newlines, when any reference does not resolve."""
    lines = await template_reference_faults(refs)
    if lines:
        raise error("\n".join(lines))


__all__ = ["assert_templates_stored", "template_fault_lines", "template_reference_faults"]
