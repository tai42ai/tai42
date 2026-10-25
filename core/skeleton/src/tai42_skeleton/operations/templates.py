"""Templates operations — the Studio templates surface (``/api/*template*``).

A thin skin over the app's resource manager (``tai42_app.storage.resource_manager``):

* ``list_templates`` — the stored template ids/paths, without unlisted prefixes.
* ``get_template`` — one stored object as a template entry, with its inferred input schema.
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

**Keys:** every template key is a canonical store key, checked by the shared key guard
(``checked_store_key``) INSIDE the operation, so the MCP tool and CLI edges get the same
``400`` as the HTTP route.
"""

from __future__ import annotations

from jinja2 import TemplateError
from pydantic import BaseModel
from tai42_contract.app import tai42_app
from tai42_contract.storage import StoragePathConflictError
from tai42_contract.template import TemplatedText

from tai42_skeleton.app.bus import FleetResult
from tai42_skeleton.app.bus.ops import ClearTemplateCache, EvictTemplate
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    NotSupportedError,
    OperationFailedError,
    operation,
)
from tai42_skeleton.operations._broadcast import broadcast, fleet_fanout
from tai42_skeleton.operations.response_models_group_b import (
    CacheClearResult,
    RenderedTemplate,
    StringListResponse,
    TemplateDeleteResult,
    TemplateEntry,
    TemplateUploadResult,
)
from tai42_skeleton.operations.store_keys import checked_store_key
from tai42_skeleton.storage.entries import read_stored_entry
from tai42_skeleton.storage.listing import listed_keys
from tai42_skeleton.template import TemplateNotFoundError


class TemplateFetch(BaseModel):
    """Read a stored object as a template entry by id."""

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
    """Render the one authored ``text`` — inline ``content`` OR a stored ``id`` — on demand.

    ``text`` carries its own render ``kwargs``.
    """

    text: TemplatedText


@operation(summary="List templates", tags=["templates"], response_model=StringListResponse)
async def list_templates() -> list[str]:
    """List the stored template ids/paths from the active storage provider, without unlisted prefixes."""
    return listed_keys(await tai42_app.storage.resource_manager.list_resources())


@operation(
    summary="Read a stored object as a template entry with its inferred input schema",
    tags=["templates"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=TemplateFetch,
    response_model=TemplateEntry,
)
async def get_template(template_id: str) -> dict:
    """Return one stored object as a template entry: the stored entry plus its inferred input schema.

    The entry is the shared stored-entry reader's (``text``, ``document`` or ``binary``).
    A text entry carries the schema inferred from its content; when the inference meets
    broken Jinja the entry is still served (``200``) with ``schema: null`` and the
    ``schema_error`` beside it. A document's extracted text and a binary object are not
    templates: their ``schema`` is ``null``.

    A missing object is a ``404``; a non-canonical key or a refused text or document
    (over the size cap, not UTF-8, an extractor's failure) a ``400``; no provider, or no
    ``files`` extra, a ``501``. When type inference cannot describe a template, the schema
    degrades to variable names carrying an ``x-tai42-inference: partial`` marker.
    """
    # A field-specific 400 for a blank/absent id, ahead of the lexical path guard, so
    # the message names ``template_id`` rather than the guard's generic ``path``.
    if not isinstance(template_id, str) or not template_id:
        raise BadRequestError("template_id must be a non-empty string")
    entry = await read_stored_entry(template_id)
    if entry["kind"] != "text":
        return {**entry, "schema": None}
    try:
        schema = await tai42_app.storage.resource_manager.get_template_schema(content=entry["content"])
    except TemplateError as exc:
        return {**entry, "schema": None, "schema_error": f"template error: {exc}"}
    return {**entry, "schema": schema, "schema_error": None}


@operation(
    summary="Upload a template",
    tags=["templates"],
    destructive=True,
    errors=[BadRequestError, ConflictError],
    request_model=TemplateUpload,
    response_model=TemplateUploadResult,
)
async def upload_template(path: str, content: str) -> dict:
    """Write ``content`` to the template store under ``path`` (create or overwrite).

    The write target is guarded against a root escape before it reaches the store.

    A template id cannot also name a directory that still holds templates: an
    upload whose ``path`` collides with such a directory is refused with a ``409``
    naming the templates in the way, so the caller deletes them first.

    The store write is a single durable act, but the compiled template is held in a
    per-worker cache, so the write is followed by a fleet ``evict_template`` broadcast:
    this worker's local apply persists and evicts, and every sibling drops the stale
    compilation on dispatch. The response embeds the per-worker fleet report so a
    caller has deterministic proof the eviction propagated; an unconfirmed worker is
    logged loudly, matching the reload broadcast.
    """
    key = checked_store_key(path)
    if not isinstance(content, str):
        raise BadRequestError("content must be a string")
    manager = tai42_app.storage.resource_manager
    try:
        fleet = FleetResult.model_validate(
            await broadcast(
                EvictTemplate(op="evict_template", path=key),
                None,
                lambda: manager.upload_template(path=key, content=content),
            )
        )
    except StoragePathConflictError as exc:
        raise ConflictError(
            f"cannot upload template {key!r}: templates exist under that path: "
            f"{exc.conflicts_summary()}; delete them first"
        ) from exc
    return {"path": key, "uploaded": True, "fanout": fleet_fanout(fleet)}


@operation(
    summary="Delete a template",
    tags=["templates"],
    destructive=True,
    errors=[BadRequestError, ConflictError, OperationFailedError],
    request_model=TemplateDelete,
    response_model=TemplateDeleteResult,
)
async def delete_template(path: str) -> dict:
    """Delete the stored template at ``path``.

    Idempotent: an already-absent path is a no-op success (the store treats a
    missing template as a no-op rather than raising), so this returns ``200``. A
    template a stored object still names is refused with a ``409`` naming its holders;
    a holder that cannot be read fails the delete with a ``500``.

    The store delete is followed by a fleet ``evict_template`` broadcast so every
    worker drops the stale compilation, not only the one that served this call; the
    response embeds the per-worker fleet report.
    """
    key = checked_store_key(path)
    manager = tai42_app.storage.resource_manager
    fleet = FleetResult.model_validate(
        await broadcast(
            EvictTemplate(op="evict_template", path=key),
            None,
            lambda: manager.delete_template(key),
        )
    )
    return {"path": key, "deleted": True, "fanout": fleet_fanout(fleet)}


@operation(
    summary="Delete a template directory",
    tags=["templates"],
    destructive=True,
    errors=[BadRequestError, ConflictError, NotFoundError, OperationFailedError],
    request_model=TemplateDirDelete,
    response_model=TemplateDeleteResult,
)
async def delete_template_dir(path: str) -> dict:
    """Delete every stored template under the directory ``path``.

    Unlike the idempotent single-template delete, a directory matching nothing is a loud
    ``404`` (the provider's ``FileNotFoundError``). The key guard refuses every spelling
    of the template root (``.``, ``a/..``); a provider reporting a root violation as
    ``ValueError`` maps to ``400`` too. A directory holding a template a stored object
    names is refused with a ``409`` naming every holder, deleting nothing.
    """
    key = checked_store_key(path)
    manager = tai42_app.storage.resource_manager
    # The store delete is this worker's local apply; the fleet ``evict_template`` op
    # carries the directory key (``prefix`` marks the prefix semantics) so every sibling
    # drops every stale compilation under it.
    #
    # Failure splits by phase. A pre-mutation failure deleted nothing — a missing
    # directory (``FileNotFoundError`` → 404), a rejected path (``ValueError`` → 400),
    # a held template (``ConflictError`` → 409) or an unreadable holder
    # (``OperationFailedError`` → 500) — so it aborts before any broadcast, siblings untouched, and its typed outcome is
    # mapped here unchanged. A failure once destructive deletion may have begun leaves
    # the store partially mutated, so the eviction MUST still fan out (it is idempotent —
    # a spurious evict costs one re-render) before the original error propagates loudly
    # as a ``FleetBroadcastError`` carrying the fleet report.
    try:
        fleet = FleetResult.model_validate(
            await broadcast(
                EvictTemplate(op="evict_template", path=key, prefix=True),
                None,
                lambda: manager.delete_template_dir(key),
                publish_on_local_failure=True,
                pre_mutation_errors=(FileNotFoundError, ValueError, ConflictError, OperationFailedError),
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
    response_model=RenderedTemplate,
)
async def render_template(text: TemplatedText) -> dict:
    """Render the one authored ``text`` — inline ``content`` OR a stored ``id`` — with its ``kwargs``.

    ``text`` carries EXACTLY ONE source: its own type refuses neither and both, so a
    caller supplying the wrong shape is a loud ``400`` at the edge. A stored ``id`` that
    cannot be fetched is a ``404`` naming that id; broken client-supplied Jinja (a syntax
    error, a sandbox-blocked dunder traversal → ``SecurityError``, an undefined access)
    is author error → ``400``, as is a stored ``id`` naming an object that is not UTF-8
    text. Genuine storage failures raise other types (``500``).
    """
    # A stored id is a caller-supplied logical key: run it through the containment guard
    # (which maps an escape to a loud 400) before it reaches the store.
    if text.id is not None:
        text = TemplatedText(id=checked_store_key(text.id), kwargs=text.kwargs)
    try:
        rendered = await tai42_app.storage.resource_manager.render_templated_text(text)
    except TemplateNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except TemplateError as exc:
        raise BadRequestError(f"template error: {exc}") from exc
    return {"rendered": rendered}


@operation(summary="Clear the template render cache", tags=["templates"], response_model=CacheClearResult)
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
        await broadcast(ClearTemplateCache(op="clear_template_cache"), None, _apply),
    )
    return {"cleared": True, "fanout": fleet_fanout(fleet)}
