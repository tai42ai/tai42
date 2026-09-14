"""Per-entry manifest-section add/remove doors (mcp / tools / agents) + set_mcp_config.

One add-mutator and one remove-mutator, parameterized on the section key, back the
six thin section ops — the sections share title-keyed identity and the same collision /
membership contract. Deep per-entry validation stays with the pipeline
(``_validate_manifest``), exactly as ``set_mcp_config``.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app.responses import ApplyResponse

from tai42_skeleton.app.boot_rules import BackendNeedsBusError
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations._broadcast import apply_response, translate_orphan_env_write

from .models import AgentsEntriesAdd, McpConfigUpdate, McpEntriesAdd, ToolsEntriesAdd


@operation(
    summary="Replace the MCP config section and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError],
    request_model=McpConfigUpdate,
    response_model=ApplyResponse,
)
async def set_mcp_config(mcp: list[Any]) -> dict:
    # Replace the manifest's ``mcp`` section through the pipeline: the mutator edits
    # the PRESERVED document in place, then ConfigService validates, persists, reloads
    # locally, and broadcasts the reload to the whole fleet. A malformed entry fails
    # validation inside the transaction (nothing persisted) and maps to a loud 400.
    # (No docstring here, so the route description in projection falls back to the
    # operation summary.)
    def mutator(document: dict[str, Any]) -> None:
        document["mcp"] = mcp

    with translate_orphan_env_write():
        try:
            result = await ConfigService.from_app().apply_change(mutator)
        except BackendNeedsBusError as exc:
            # The invariant is a RuntimeError (a boot-time refusal must still crash loudly),
            # so the mutate-time path maps it explicitly to a loud, actionable 400 naming
            # TAI_BUS_REDIS_URL rather than letting it escape as a 500.
            raise BadRequestError(str(exc)) from exc
        except ValueError as exc:
            raise BadRequestError(f"invalid mcp config: {exc}") from exc
        return apply_response(result)


def _merged_entries(current: list[Any], entries: list[Any], replace: bool) -> list[Any]:
    """The new section list from ``current`` + incoming ``entries``. Every entry must be
    a dict carrying a non-empty ``title`` string (else a ``ValueError`` naming the
    position); duplicate incoming titles are refused. A title already present is refused
    unless ``replace`` swaps the entry in at its current index; non-colliding entries
    append in given order. Pure / re-runnable: builds a fresh list from the arguments."""
    incoming_titles: list[str] = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"entry at position {position} must be a mapping carrying a 'title'")
        title = entry.get("title")
        if not isinstance(title, str) or not title:
            raise ValueError(f"entry at position {position} must carry a non-empty 'title' string")
        incoming_titles.append(title)
    duplicates = sorted({t for t in incoming_titles if incoming_titles.count(t) > 1})
    if duplicates:
        raise ValueError(f"duplicate titles within entries: {duplicates}")

    current_titles = [e.get("title") if isinstance(e, dict) else None for e in current]
    by_title = dict(zip(incoming_titles, entries, strict=True))
    collisions = sorted(set(incoming_titles) & {t for t in current_titles if t is not None})
    if collisions and not replace:
        raise ValueError(f"entries already present (use replace): {collisions}")

    collided = set(collisions)
    kept = zip(current, current_titles, strict=True)
    result = [by_title[title] if title in collided else entry for entry, title in kept]
    result.extend(entry for title, entry in zip(incoming_titles, entries, strict=True) if title not in collided)
    return result


def _without_entry(current: list[Any], title: str) -> list[Any]:
    """``current`` minus the entry whose ``title`` matches; a missing title is a
    ``LookupError`` (the mutator aborts the transaction). Pure / re-runnable."""
    result = [e for e in current if not (isinstance(e, dict) and e.get("title") == title)]
    if len(result) == len(current):
        raise LookupError(title)
    return result


async def _apply_entries_add(section: str, entries: list[Any], replace: bool) -> dict:
    # Empty ``entries`` is refused INSIDE the try (op-level, so the projection path is
    # refused identically) — a guard above the try would escape as a 500.
    with translate_orphan_env_write():
        try:
            if not entries:
                raise ValueError("entries must be a non-empty list")

            def mutator(document: dict[str, Any]) -> None:
                document[section] = _merged_entries(document.get(section) or [], entries, replace)

            result = await ConfigService.from_app().apply_change(mutator)
        except BackendNeedsBusError as exc:
            raise BadRequestError(str(exc)) from exc
        except ValueError as exc:
            raise BadRequestError(f"invalid {section} config: {exc}") from exc
        return apply_response(result)


async def _apply_entry_remove(section: str, title: str) -> dict:
    with translate_orphan_env_write():
        try:

            def mutator(document: dict[str, Any]) -> None:
                document[section] = _without_entry(document.get(section) or [], title)

            result = await ConfigService.from_app().apply_change(mutator)
        except BackendNeedsBusError as exc:
            raise BadRequestError(str(exc)) from exc
        except LookupError as exc:
            raise NotFoundError(f"unknown {section} entry title: {title!r}") from exc
        except ValueError as exc:
            raise BadRequestError(f"invalid {section} config: {exc}") from exc
        return apply_response(result)


@operation(
    summary="Add or replace MCP config entries and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError],
    request_model=McpEntriesAdd,
    response_model=ApplyResponse,
)
async def add_mcp_entries(entries: list[Any], replace: bool = False) -> dict:
    return await _apply_entries_add("mcp", entries, replace)


@operation(
    summary="Remove one MCP config entry by title and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, NotFoundError],
    response_model=ApplyResponse,
)
async def remove_mcp_entry(title: str) -> dict:
    return await _apply_entry_remove("mcp", title)


@operation(
    summary="Add or replace tools config entries and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError],
    request_model=ToolsEntriesAdd,
    response_model=ApplyResponse,
)
async def add_tools_entries(entries: list[Any], replace: bool = False) -> dict:
    return await _apply_entries_add("tools", entries, replace)


@operation(
    summary="Remove one tools config entry by title and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, NotFoundError],
    response_model=ApplyResponse,
)
async def remove_tools_entry(title: str) -> dict:
    return await _apply_entry_remove("tools", title)


@operation(
    summary="Add or replace agents config entries and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError],
    request_model=AgentsEntriesAdd,
    response_model=ApplyResponse,
)
async def add_agents_entries(entries: list[Any], replace: bool = False) -> dict:
    return await _apply_entries_add("agents", entries, replace)


@operation(
    summary="Remove one agents config entry by title and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, NotFoundError],
    response_model=ApplyResponse,
)
async def remove_agents_entry(title: str) -> dict:
    return await _apply_entry_remove("agents", title)
