"""Templates operations — the Studio templates surface (``/api/*template*``).

A thin skin over the app's resource manager (``tai42_app.storage.resource_manager``):

* ``list_templates`` — the stored template ids/paths.
* ``get_template`` — one template's content plus its inferred input schema.
* ``upload_template`` — write (create or overwrite) a template.
* ``delete_template`` — remove a stored template.
* ``render_template`` — render inline content OR a stored template with kwargs.
* ``clear_templates_cache`` — drop the compiled-template cache.

**Fleet eviction:** the compiled template is held in a per-worker cache, so a store
write on one worker leaves every sibling rendering the old body. Each write op writes
the store, then broadcasts an ``evict_template`` op (``clear_templates_cache`` broadcasts
``clear_template_cache``) over the worker bus with the same primitive the config reload
door uses, so every worker drops the stale compilation; the response embeds the per-worker
fleet report and an unconfirmed worker is logged loudly.

**Path-argument hardening:** every logical template key reaching the store runs
through the shared lexical containment guard (:func:`safe_template_path`) INSIDE the
operation, so the guard defends the MCP tool and CLI edges as well as the HTTP route
(a ``..`` escape, absolute key, embedded backslash/NUL, or empty key is refused loudly).
Defense in depth over the store's own root check.
"""

from __future__ import annotations

import os
from typing import Any

from jinja2 import TemplateError
from pydantic import BaseModel
from tai42_contract.app import tai42_app

from tai42_skeleton.app.bus import FleetResult
from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations._broadcast import broadcast, fleet_fanout
from tai42_skeleton.template import TemplateNotFoundError
from tai42_skeleton.template.path_guard import _TEMPLATE_ROOT, UnsafeTemplatePathError, safe_template_path


class TemplateFetch(BaseModel):
    """Fetch a template's content and its input schema by id."""

    template_id: str


class TemplateUpload(BaseModel):
    """Upload (create or overwrite) a template at ``path``."""

    path: str
    content: str


class TemplateDelete(BaseModel):
    """Delete the template at ``path``."""

    path: str


class TemplateDirDelete(BaseModel):
    """Delete every stored template under the directory ``path``."""

    path: str


class TemplateRender(BaseModel):
    """Render a template — inline ``content`` OR a stored ``template_id`` — with
    ``kwargs``."""

    content: str | None = None
    template_id: str | None = None
    kwargs: dict[str, object] = {}


def _safe_key(key: object) -> str:
    """Guard a caller-supplied template key, mapping an escape to a loud ``400``."""
    try:
        return safe_template_path(key)
    except UnsafeTemplatePathError as exc:
        raise BadRequestError(str(exc)) from exc


def _resolves_to_template_root(key: str) -> bool:
    """True when a guard-passed key targets the template ROOT itself (``.`` / ``a/..``).

    The shared lexical guard rejects resolution ABOVE the root but admits a key landing
    EXACTLY on it; resolving under the guard's anchor and comparing catches that case.
    """
    root = os.path.realpath(_TEMPLATE_ROOT)
    return os.path.realpath(os.path.join(root, key)) == root


@operation(summary="List templates", tags=["templates"])
async def list_templates() -> list[str]:
    """List the stored template ids/paths from the active storage provider."""
    return await tai42_app.storage.resource_manager.list_resources()


@operation(
    summary="Fetch a template and its schema",
    tags=["templates"],
    errors=[BadRequestError, NotFoundError],
    request_model=TemplateFetch,
)
async def get_template(template_id: str) -> dict:
    """Return a stored template's content and its inferred input schema.

    A missing stored template is a ``404`` (never a leaked storage ``500``); a
    stored template with broken Jinja is author error — the schema inference parses
    it — surfaced as a ``400``. Genuine storage failures raise other types (``500``).
    """
    # A field-specific 400 for a blank/absent id, ahead of the lexical path guard, so
    # the message names ``template_id`` rather than the guard's generic ``path``.
    if not isinstance(template_id, str) or not template_id:
        raise BadRequestError("template_id must be a non-empty string")
    key = _safe_key(template_id)
    manager = tai42_app.storage.resource_manager
    try:
        content = await manager.fetch_template(key)
        schema = await manager.get_template_schema(template_id=key)
    except TemplateNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except TemplateError as exc:
        raise BadRequestError(f"template error: {exc}") from exc
    return {"template": content, "schema": schema}


@operation(
    summary="Upload a template",
    tags=["templates"],
    destructive=True,
    errors=[BadRequestError],
    request_model=TemplateUpload,
)
async def upload_template(path: str, content: str) -> dict:
    """Write ``content`` to the template store under ``path`` (create or overwrite).

    The write target is guarded against a root escape before it reaches the store.

    The store write is a single durable act, but the compiled template is held in a
    per-worker cache, so the write is followed by a fleet ``evict_template`` broadcast:
    this worker's local apply persists and evicts, and every sibling drops the stale
    compilation on dispatch. The response embeds the per-worker fleet report so a
    caller has deterministic proof the eviction propagated; an unconfirmed worker is
    logged loudly, matching the reload broadcast.
    """
    key = _safe_key(path)
    if not isinstance(content, str):
        raise BadRequestError("content must be a string")
    manager = tai42_app.storage.resource_manager
    fleet = FleetResult.model_validate(
        await broadcast(
            {"op": "evict_template", "path": key},
            None,
            lambda: manager.upload_template(path=key, content=content),
        )
    )
    return {"path": key, "uploaded": True, "fanout": fleet_fanout(fleet)}


@operation(
    summary="Delete a template",
    tags=["templates"],
    destructive=True,
    errors=[BadRequestError],
    request_model=TemplateDelete,
)
async def delete_template(path: str) -> dict:
    """Delete the stored template at ``path``.

    Idempotent: an already-absent path is a no-op success (the store treats a
    missing template as a no-op rather than raising), so this returns ``200``.

    The store delete is followed by a fleet ``evict_template`` broadcast so every
    worker drops the stale compilation, not only the one that served this call; the
    response embeds the per-worker fleet report.
    """
    key = _safe_key(path)
    manager = tai42_app.storage.resource_manager
    fleet = FleetResult.model_validate(
        await broadcast(
            {"op": "evict_template", "path": key},
            None,
            lambda: manager.delete_template(key),
        )
    )
    return {"path": key, "deleted": True, "fanout": fleet_fanout(fleet)}


@operation(
    summary="Delete a template directory",
    tags=["templates"],
    destructive=True,
    errors=[BadRequestError, NotFoundError],
    request_model=TemplateDirDelete,
)
async def delete_template_dir(path: str) -> dict:
    """Delete every stored template under the directory ``path``.

    Unlike the idempotent single-template delete, a directory matching nothing is a loud
    ``404`` (the provider's ``FileNotFoundError``). A key resolving to the template ROOT
    would wipe the whole store, so it is refused with a ``400`` HERE, ahead of the provider;
    a provider reporting a root violation as ``ValueError`` maps to ``400`` too.
    """
    key = _safe_key(path)
    # The shared guard admits a key resolving EXACTLY to the root; refuse it here before
    # the provider rather than rely on each backend's own root check.
    if _resolves_to_template_root(key):
        raise BadRequestError("refusing to delete the template root; a directory path is required")
    manager = tai42_app.storage.resource_manager
    # The store delete is this worker's local apply; the fleet ``evict_template`` op
    # carries the directory key (``prefix`` marks the prefix semantics) so every sibling
    # drops every stale compilation under it.
    #
    # Failure splits by phase. A pre-mutation failure deleted nothing — a missing
    # directory (``FileNotFoundError`` → 404) or a rejected path (``ValueError`` → 400)
    # — so it aborts before any broadcast, siblings untouched, and its typed outcome is
    # mapped here unchanged. A failure once destructive deletion may have begun leaves
    # the store partially mutated, so the eviction MUST still fan out (it is idempotent —
    # a spurious evict costs one re-render) before the original error propagates loudly
    # as a ``FleetBroadcastError`` carrying the fleet report.
    try:
        fleet = FleetResult.model_validate(
            await broadcast(
                {"op": "evict_template", "path": key, "prefix": True},
                None,
                lambda: manager.delete_template_dir(key),
                publish_on_local_failure=True,
                pre_mutation_errors=(FileNotFoundError, ValueError),
            )
        )
    except FileNotFoundError as exc:
        raise NotFoundError(f"template directory {key!r} not found") from exc
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return {"path": key, "deleted": True, "fanout": fleet_fanout(fleet)}


@operation(
    summary="Render a template",
    tags=["templates"],
    errors=[BadRequestError, NotFoundError],
    request_model=TemplateRender,
)
async def render_template(
    content: str | None = None,
    template_id: str | None = None,
    kwargs: dict[str, Any] | None = None,
) -> dict:
    """Render an inline ``content`` OR a stored ``template_id`` with ``kwargs``.

    Exactly one of ``content``/``template_id`` is required. A missing stored
    template is a ``404``; broken client-supplied Jinja (a syntax error, a
    sandbox-blocked dunder traversal → ``SecurityError``, an undefined access) is
    author error → ``400``. Genuine storage failures raise other types (``500``).
    """
    kwargs = kwargs or {}
    if content is None and template_id is None:
        raise BadRequestError("one of 'content' or 'template_id' is required")
    if content is not None and template_id is not None:
        raise BadRequestError("provide either 'content' or 'template_id', not both")
    if content is not None and not isinstance(content, str):
        raise BadRequestError("'content' must be a string")
    if not isinstance(kwargs, dict):
        raise BadRequestError("'kwargs' must be a JSON object")
    if template_id is not None:
        template_id = _safe_key(template_id)
    try:
        rendered = await tai42_app.storage.resource_manager.render_by_id_or_content(
            content=content,
            template_id=template_id,
            kwargs=kwargs,
        )
    except TemplateNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except TemplateError as exc:
        raise BadRequestError(f"template error: {exc}") from exc
    return {"rendered": rendered}


@operation(summary="Clear the template render cache", tags=["templates"])
async def clear_templates_cache() -> dict:
    """Drop every compiled template from the render cache, fleet-wide.

    The manual escape hatch that pairs with the automatic per-key eviction: it
    broadcasts a ``clear_template_cache`` op so every worker cold-starts its compiled
    cache, not only the one that served this call. The response embeds the per-worker
    fleet report.
    """
    manager = tai42_app.storage.resource_manager

    async def _apply() -> None:
        manager.clear_cache()

    fleet = FleetResult.model_validate(
        await broadcast({"op": "clear_template_cache"}, None, _apply),
    )
    return {"cleared": True, "fanout": fleet_fanout(fleet)}
