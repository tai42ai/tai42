"""Storage operations — the deployment content store (``/api/storage*``).

A thin skin over the registered :class:`~tai42_contract.storage.Storage` provider.
Storage is dead by default (the skeleton ships no provider); a backend registers
one as a manifest-loaded plugin. Every operation reads the provider through the
concrete storage facet (``instance.app.storage.provider``) and reports honestly
when none is installed — a loud ``501`` (``NotSupportedError``) rather than a
fabricated default (except ``storage_info``, which answers ``present: false``).

Every id/path-carrying input must be a relative path with no ``..`` segment — an
absolute path (leading ``/``) or a ``..`` segment is rejected with a ``400``: the
flagship provider is filesystem-backed, where both are real traversal vectors. The
guard runs INSIDE the operation, so it defends the MCP tool edge and the CLI as
well as the HTTP route; every operation that passes a path to the provider also maps
the provider's boundary ``ValueError`` to a ``400`` so a provider-reported violation
is a loud ``400``, never a ``500``.

``list_resources`` is the op behind ``GET /api/storage/resources``; the write/delete ops
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
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    NotSupportedError,
    operation,
)
from tai42_skeleton.operations._broadcast import broadcast
from tai42_skeleton.operations.response_models_group_b import (
    DirDeleted,
    ResourceDeleted,
    ResourceList,
    ResourceStat,
    ResourceStored,
    StorageInfo,
)

_NO_PROVIDER_MESSAGE = "storage needs a storage-provider plugin"
_UNSAFE_ID_MESSAGE = "must be a relative path with no '..' segment"


class StorageUpload(BaseModel):
    """A storage upload: exactly one of ``content_text`` or ``content_base64`` supplies the content for ``id``.

    ``content_text`` is stored verbatim; ``content_base64`` is decoded to bytes.
    An existing id is overwritten — provider passthrough semantics.
    """

    id: str
    content_text: str | None = None
    content_base64: str | None = None


def _provider() -> Storage | None:
    """The registered storage provider, or ``None`` while dead by default."""
    return instance.app.storage.provider


def _require_provider() -> Storage:
    """The registered provider, or a loud ``501`` when none is installed."""
    provider = _provider()
    if provider is None:
        raise NotSupportedError(_NO_PROVIDER_MESSAGE)
    return provider


def _is_unsafe_path(value: str) -> bool:
    """Whether an id/path input is unsafe to pass to the provider.

    Unsafe means absolute (a leading ``/``) or carrying a ``..`` segment. A safe
    input is a relative path with no ``..`` segment.
    """
    return value.startswith("/") or ".." in value.split("/")


def _reject_unsafe(kind: str, value: str) -> None:
    if _is_unsafe_path(value):
        raise BadRequestError(f"{kind} {value!r} {_UNSAFE_ID_MESSAGE}")


async def _write_then_evict(resource_id: str, write: Callable[[], Awaitable[None]]) -> None:
    """Apply one store write to ``resource_id`` here and on every worker, dropping its render state.

    The write runs as this worker's local apply followed by the template eviction of the
    id; a failed write raises before anything is evicted or broadcast.
    """
    manager = instance.app.storage.resource_manager

    async def _apply() -> None:
        await write()
        manager.evict_compiled(resource_id)

    await broadcast({"op": "evict_template", "path": resource_id}, None, _apply)


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


@operation(summary="Get the storage provider identity", tags=["storage"], response_model=StorageInfo)
async def storage_info() -> dict:
    """Report the registered provider's identity, or ``present: false`` when none is installed.

    A ``200``, so the UI renders the empty state without an error.
    """
    provider = _provider()
    if provider is None:
        return {"present": False, "provider": None, "module": None}
    return {"present": True, "provider": type(provider).__name__, "module": type(provider).__module__}


@operation(
    summary="List storage resources",
    tags=["storage"],
    errors=[NotSupportedError],
    response_model=ResourceList,
)
async def list_resources() -> dict:
    """List the sorted resource ids from the active storage provider."""
    provider = _require_provider()
    return {"resources": sorted(await provider.list())}


@operation(
    summary="Stat a storage resource",
    tags=["storage"],
    errors=[BadRequestError, NotSupportedError],
    response_model=ResourceStat,
)
async def stat_resource(resource_id: str) -> dict:
    """Return the resource's inferred content type."""
    _reject_unsafe("resource id", resource_id)
    provider = _require_provider()
    try:
        stat = await provider.stat(resource_id)
    except ValueError as exc:
        # A provider-reported boundary violation is a client error (400), never a 500.
        raise BadRequestError(str(exc)) from exc
    return {"id": resource_id, "content_type": stat.content_type}


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
    _reject_unsafe("resource id", resource_id)

    if (content_text is None) == (content_base64 is None):
        raise BadRequestError("exactly one of 'content_text' or 'content_base64' is required")
    if content_text is not None and not isinstance(content_text, str):
        raise BadRequestError("'content_text' must be a string")
    if content_base64 is not None and not isinstance(content_base64, str):
        raise BadRequestError("'content_base64' must be a base64 string")

    write = _upload_write(_require_provider(), resource_id, content_text, content_base64)
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
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    response_model=ResourceDeleted,
)
async def delete_resource(resource_id: str) -> dict:
    """Remove one object from the store; its render state is evicted fleet-wide."""
    _reject_unsafe("resource id", resource_id)
    provider = _require_provider()
    try:
        await _write_then_evict(resource_id, lambda: provider.delete(resource_id))
    except FileNotFoundError as exc:
        raise NotFoundError(f"resource {resource_id!r} not found") from exc
    except ValueError as exc:
        # A provider-reported boundary violation is a client error (400), never a 500.
        raise BadRequestError(str(exc)) from exc
    return {"id": resource_id, "deleted": True}


@operation(
    summary="Delete a storage directory",
    tags=["storage"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    response_model=DirDeleted,
)
async def delete_dir(dir_path: str) -> dict:
    """Remove a directory subtree from the store; the render state under it is evicted fleet-wide.

    This worker evicts whatever the outcome. A failure before deletion begins — a missing
    directory (``404``) or a rejected path (``400``) — broadcasts nothing. A failure once
    deletion may have begun leaves the store partially changed, so the eviction still fans
    out before the error propagates (a ``FleetBroadcastError`` carrying the fleet report).
    """
    _reject_unsafe("directory path", dir_path)
    provider = _require_provider()
    manager = instance.app.storage.resource_manager

    async def _apply() -> None:
        try:
            await provider.delete_dir(dir_path)
        finally:
            # A partial delete must never leave content under the directory served stale.
            manager.evict_dir(dir_path)

    try:
        await broadcast(
            {"op": "evict_template", "path": dir_path, "prefix": True},
            None,
            _apply,
            publish_on_local_failure=True,
            pre_mutation_errors=(FileNotFoundError, ValueError),
        )
    except FileNotFoundError as exc:
        raise NotFoundError(f"directory {dir_path!r} not found") from exc
    except ValueError as exc:
        # ``assert_not_root`` raises ``ValueError`` for a root-resolving dir path
        # (``"."`` / ``"/"`` / ``"a/.."``) — a client error, surfaced as 400.
        raise BadRequestError(str(exc)) from exc
    return {"dir": dir_path, "deleted": True}
