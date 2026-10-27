"""Storage operations — the deployment content store (``/api/storage*``).

A thin skin over the registered :class:`~tai42_contract.storage.Storage` provider.
Storage is dead by default (the skeleton ships no provider); a backend registers
one as a manifest-loaded plugin. Every operation reads the provider through the
concrete storage facet (``instance.app.storage.provider``) and reports honestly
when none is installed — a loud ``501`` (``NotSupportedError``) rather than a
fabricated default (except ``storage_info``, which answers ``present: false``).

Every id or path input is a canonical store key, checked by the shared key guard
(``checked_store_key``) INSIDE the operation, so the MCP tool edge and the CLI get the
same ``400`` as the HTTP route. Every operation that passes a path to the provider also
maps the provider's boundary ``ValueError`` to a ``400``, never a ``500``.

``list_resources`` is the op behind ``GET /api/storage/resources``; it leaves out every
key under a prefix marked unlisted (the objects stay reachable by id). ``read_resource``
answers one object as a text, document or binary entry through the shared stored-entry
reader (``tai42_skeleton.storage.entries``). The write/delete ops
(``upload_resource``, ``delete_resource``, ``delete_dir``) mutate the store, so they
are ``destructive``.

**Fleet eviction:** the store these ops write is the one the template manager renders
from, and every worker holds per-id render state (compiled templates, ids storage
answered "not found" for). Each write is applied on this worker followed by its
template eviction, then broadcast as the ``evict_template`` fleet op (``prefix`` for a
directory delete), so every worker's next render sees the write. An unconfirmed worker
is logged loudly by the broadcast.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from urllib.parse import quote

from pydantic import BaseModel
from tai42_contract.storage import Storage, StoragePathConflictError

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus.ops import EvictTemplate
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    NotSupportedError,
    OperationFailedError,
    operation,
)
from tai42_skeleton.operations._broadcast import broadcast
from tai42_skeleton.operations.response_models.storage import (
    DirDeleted,
    ResourceDeleted,
    ResourceList,
    ResourceStat,
    ResourceStored,
    StorageInfo,
    StoredEntry,
)
from tai42_skeleton.operations.storage_provider import require_storage_provider, storage_provider
from tai42_skeleton.operations.store_keys import checked_store_key
from tai42_skeleton.storage.entries import read_stored_entry
from tai42_skeleton.storage.listing import listed_keys

_SURFACE_ROUTERS = (
    ("storage", "tai42_skeleton.routers.storage"),
    ("templates", "tai42_skeleton.routers.templates"),
)


class StorageUpload(BaseModel):
    """A storage upload: exactly one of ``content_text`` or ``content_base64`` supplies the content for ``id``.

    ``content_text`` is stored verbatim; ``content_base64`` is decoded to bytes.
    An existing id is overwritten — provider passthrough semantics.
    """

    id: str
    content_text: str | None = None
    content_base64: str | None = None


async def _write_then_evict(resource_id: str, write: Callable[[], Awaitable[None]]) -> None:
    """Apply one store write to ``resource_id`` here and on every worker, dropping its render state.

    The write runs as this worker's local apply followed by the template eviction of the
    id; a failed write raises before anything is evicted or broadcast.
    """
    manager = instance.app.storage.resource_manager

    async def _apply() -> None:
        await write()
        manager.evict_compiled(resource_id)

    await broadcast(EvictTemplate(op="evict_template", path=resource_id), None, _apply)


def _upload_write(
    provider: Storage, resource_id: str, content_text: str | None, content_base64: str | None
) -> Callable[[], Awaitable[None]]:
    """The store write for a validated upload: the text verbatim, else the decoded base64 bytes.

    Undecodable base64 is a ``400`` raised here, before anything is written.
    """
    if content_text is not None:
        return lambda: provider.upload(resource_id, content_text)
    if content_base64 is None:
        raise AssertionError
    try:
        # ``binascii.Error`` (bad padding / alphabet) subclasses ``ValueError``.
        data = base64.b64decode(content_base64, validate=True)
    except ValueError as exc:
        raise BadRequestError(f"'content_base64' is not valid base64: {exc}") from exc
    return lambda: provider.upload_bytes(resource_id, data)


def _content_disposition(filename: str) -> str:
    r"""A ``Content-Disposition: attachment`` header with an ASCII ``filename`` fallback and an RFC 8187 ``filename*``.

    Per RFC 6266 § 4.3 a recipient that understands ``filename*`` uses it and ignores
    ``filename``; older agents fall back to the ASCII ``filename``. The fallback is the
    control-stripped name with every non-ASCII character replaced by ``_`` and the
    quoted-string ``"``/``\\`` escaping kept; the ``filename*`` value percent-encodes
    every byte outside the RFC 8187 § 3.2.1 attr-char set (``urllib.parse.quote`` leaves
    the attr-char set literal via ``safe`` and percent-encodes the rest of the UTF-8 bytes).
    """
    sanitized = "".join(ch for ch in filename if ch >= " " and ch != "\x7f")
    ascii_fallback = "".join(ch if ord(ch) < 128 else "_" for ch in sanitized)
    ascii_fallback = ascii_fallback.replace("\\", "\\\\").replace('"', '\\"')
    ext_value = quote(sanitized, safe="!#$&+-.^_`|~")  # RFC 8187 § 3.2.1 attr-char set
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{ext_value}"


@operation(
    summary="Get the storage provider identity",
    tags=["storage", "templates"],
    response_model=StorageInfo,
)
async def storage_info() -> dict:
    """Report the registered provider's identity, or ``present: false`` when none is installed.

    A ``200``, so the UI renders the empty state without an error. ``surfaces`` names the
    management routers this deployment mounts (``storage``, ``templates``), so a caller
    never calls a door that is not served.
    """
    modules = instance.app.effective_router_modules()
    if modules is None:
        raise RuntimeError("storage_info needs a started deployment: no effective router set")
    surfaces = [surface for surface, module in _SURFACE_ROUTERS if module in modules]
    provider = storage_provider()
    if provider is None:
        return {"present": False, "provider": None, "module": None, "surfaces": surfaces}
    return {
        "present": True,
        "provider": type(provider).__name__,
        "module": type(provider).__module__,
        "surfaces": surfaces,
    }


@operation(
    summary="List storage resources",
    tags=["storage"],
    errors=[NotSupportedError],
    response_model=ResourceList,
)
async def list_resources() -> dict:
    """List the sorted resource ids from the active storage provider, without unlisted prefixes."""
    provider = require_storage_provider()
    return {"resources": listed_keys(sorted(await provider.list()))}


@operation(
    summary="Stat a storage resource",
    tags=["storage"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    response_model=ResourceStat,
)
async def stat_resource(resource_id: str) -> dict:
    """Return the resource's inferred content type; a missing object is a ``404``."""
    checked_store_key(resource_id)
    provider = require_storage_provider()
    try:
        stat = await provider.stat(resource_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"resource {resource_id!r} not found") from exc
    except ValueError as exc:
        # A provider-reported boundary violation is a client error (400), never a 500.
        raise BadRequestError(str(exc)) from exc
    return {"id": resource_id, "content_type": stat.content_type}


@operation(
    summary="Read a stored object as a text, document or binary entry",
    tags=["storage"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    response_model=StoredEntry,
)
async def read_resource(resource_id: str) -> dict:
    """Read one stored object as an entry: ``text`` or ``document`` with its ``content``, else ``binary``.

    The kind and the content are the shared content classifier's. A binary object is
    classified from its size and at most two detection windows, never read whole. A
    refused text or document (over the size cap, not UTF-8, an extractor's failure) is
    a ``400``; a missing object a ``404``; no provider, or no ``files`` extra, a ``501``.
    """
    return await read_stored_entry(resource_id)


@operation(
    summary="Upload a storage resource",
    tags=["storage"],
    destructive=True,
    errors=[BadRequestError, ConflictError, NotSupportedError],
    request_model=StorageUpload,
    response_model=ResourceStored,
)
async def upload_resource(
    resource_id: str,
    content_text: str | None = None,
    content_base64: str | None = None,
) -> dict:
    """Store text OR base64-decoded bytes under ``resource_id`` (overwrite on reuse).

    The type/shape validation the tool schema cannot express — a non-empty ``id``,
    exactly one content field, and each field's type — is enforced here so the MCP
    tool edge carries it too; the HTTP route's extractor passes the raw body through
    to the same checks. The stored id's render state is evicted fleet-wide.
    """
    if not isinstance(resource_id, str) or not resource_id:
        raise BadRequestError("body must contain a non-empty string 'id'")
    checked_store_key(resource_id)

    if (content_text is None) == (content_base64 is None):
        raise BadRequestError("exactly one of 'content_text' or 'content_base64' is required")
    if content_text is not None and not isinstance(content_text, str):
        raise BadRequestError("'content_text' must be a string")
    if content_base64 is not None and not isinstance(content_base64, str):
        raise BadRequestError("'content_base64' must be a base64 string")

    write = _upload_write(require_storage_provider(), resource_id, content_text, content_base64)
    try:
        await _write_then_evict(resource_id, write)
    except StoragePathConflictError as exc:
        # An id cannot be both a file and a directory that still holds objects; the
        # provider names the objects in the way. Surfaced as a 409, not a 500.
        raise ConflictError(
            f"cannot store {resource_id!r}: objects exist under that path: {exc.conflicts_summary()}; delete them first"
        ) from exc
    except ValueError as exc:
        # A provider-reported boundary/validation error (e.g. content a text-only
        # provider cannot store) is a client error, surfaced as 400 rather than 500.
        raise BadRequestError(str(exc)) from exc
    return {"id": resource_id, "stored": True}


@operation(
    summary="Delete a storage resource",
    tags=["storage"],
    errors=[BadRequestError, ConflictError, NotFoundError, NotSupportedError, OperationFailedError],
    response_model=ResourceDeleted,
)
async def delete_resource(resource_id: str) -> dict:
    """Remove one object from the store; its render state is evicted fleet-wide.

    An object a stored object still names is refused with a ``409`` naming its holders.
    """
    checked_store_key(resource_id)
    require_storage_provider()
    manager = instance.app.storage.resource_manager
    try:
        await _write_then_evict(resource_id, lambda: manager.delete_object(resource_id))
    except FileNotFoundError as exc:
        raise NotFoundError(f"resource {resource_id!r} not found") from exc
    except ValueError as exc:
        # A provider-reported boundary violation is a client error (400), never a 500.
        raise BadRequestError(str(exc)) from exc
    return {"id": resource_id, "deleted": True}


@operation(
    summary="Delete a storage directory",
    tags=["storage"],
    errors=[BadRequestError, ConflictError, NotFoundError, NotSupportedError, OperationFailedError],
    response_model=DirDeleted,
)
async def delete_dir(dir_path: str) -> dict:
    """Remove a directory subtree from the store; the render state under it is evicted fleet-wide.

    This worker evicts whatever the outcome once deletion begins. A failure before deletion
    begins — a missing directory (``404``), a rejected path (``400``), an object under it a
    stored object names (``409``) or a holder that cannot be read (``500``) — broadcasts
    nothing. A failure once deletion may have begun leaves the store partially changed, so
    the eviction still fans out before the error propagates (a ``FleetBroadcastError``
    carrying the fleet report).
    """
    checked_store_key(dir_path)
    require_storage_provider()
    manager = instance.app.storage.resource_manager

    try:
        await broadcast(
            EvictTemplate(op="evict_template", path=dir_path, prefix=True),
            None,
            lambda: manager.delete_template_dir(dir_path),
            publish_on_local_failure=True,
            pre_mutation_errors=(FileNotFoundError, ValueError, ConflictError, OperationFailedError),
        )
    except FileNotFoundError as exc:
        raise NotFoundError(f"directory {dir_path!r} not found") from exc
    except ValueError as exc:
        # A provider-reported boundary violation is a client error (400), never a 500.
        raise BadRequestError(str(exc)) from exc
    return {"dir": dir_path, "deleted": True}
