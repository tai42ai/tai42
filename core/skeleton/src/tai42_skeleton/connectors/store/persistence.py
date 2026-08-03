"""Single canonical decrypt-and-parse path for ConnectionRecord blobs
(``store.get`` -> ``crypto.decrypt`` -> ``model_validate_json``), so every caller
shares one error-handling contract.

:func:`load_record` raises :class:`ConnectionNotFoundError` on a missing blob and
re-raises (after logging ERROR) on a decrypt failure; :func:`load_record_or_none`
returns ``None`` on either. A broken ``CONNECTORS_KEK`` is neither — not a property of
one blob — so both let it propagate (unlogged, unswallowed).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from tai42_contract.connectors.errors import MalformedConnectionIdError
from tai42_contract.connectors.models import ConnectionRecord

from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.settings import connector_engine_config
from tai42_skeleton.connectors.store import token_store

logger = logging.getLogger(__name__)


def session_expires_at_for(record: ConnectionRecord) -> datetime:
    """Effective session expiry (Redis cache TTL and ``session_expires_at`` column),
    capped by ``CONNECTORS_MAX_SESSION_TTL``. Resets on every write, so a regularly-used
    connection behaves like an inactivity window.
    """
    return datetime.now(UTC) + connector_engine_config().max_session_ttl


class ConnectionNotFoundError(KeyError):
    """Missing connection; routers surface as 404. Defined here so the persistence
    helper has no upward dependency on the service module."""


async def load_record_with_blob(
    connection_id: str,
    *,
    include_expired: bool = False,
) -> tuple[ConnectionRecord, bytes]:
    """Decrypt + parse, returning the record AND the raw ciphertext it loaded from.
    Raises :class:`ConnectionNotFoundError` on missing.

    The blob is the compare-and-set handle for a refresh write-back: the resolver passes
    it to ``store.put(expected_blob=...)`` so the store commits only if no peer rotated
    the record meanwhile.

    ``include_expired`` is forwarded to ``store.get``: ``False`` for serving reads (expired
    reads as missing), ``True`` ONLY by disconnect's cleanup so an expired connection stays
    loadable-to-purge.

    A malformed ``connection_id`` surfaces as the same :class:`ConnectionNotFoundError` an
    absent id does — no oracle for the id's shape. Decrypt failures log at ERROR and
    re-raise. A missing/malformed ``CONNECTORS_KEK`` is excluded (it raises for EVERY blob),
    so it propagates unlogged rather than blaming one blob for a deployment fault.
    """
    try:
        blob = await token_store().get(connection_id, include_expired=include_expired)
    except MalformedConnectionIdError as exc:
        raise ConnectionNotFoundError(connection_id) from exc
    if blob is None:
        raise ConnectionNotFoundError(connection_id)
    try:
        plain = crypto.decrypt(blob, connection_id=connection_id)
    except crypto.ConnectorEncryptionConfigError:
        raise
    except Exception:
        logger.error(
            "connectors: connection blob %r failed to decrypt — wrong CONNECTORS_KEK for this blob or a corrupted blob",
            connection_id,
            exc_info=True,
        )
        raise
    return ConnectionRecord.model_validate_json(plain), blob


async def load_record(connection_id: str, *, include_expired: bool = False) -> ConnectionRecord:
    """Decrypt + parse. Raises :class:`ConnectionNotFoundError` on missing.

    Thin projection of :func:`load_record_with_blob` for read paths that don't need the
    compare-and-set handle.
    """
    record, _ = await load_record_with_blob(connection_id, include_expired=include_expired)
    return record


async def load_record_or_none(connection_id: str) -> ConnectionRecord | None:
    """Return ``None`` on missing OR unreadable, raise on a broken KEK config.

    For list-projection paths where one bad row must not poison the whole response.
    A per-blob decrypt failure or shape drift skips the one row (logged WARNING); a
    missing/malformed ``CONNECTORS_KEK`` raises for EVERY row, so it propagates rather
    than passing a deployment fault off as empty data. A malformed ``connection_id`` keys
    no record and returns ``None``, indistinguishable from a genuine miss.
    """
    try:
        blob = await token_store().get(connection_id)
    except MalformedConnectionIdError:
        return None
    if blob is None:
        return None
    try:
        plain = crypto.decrypt(blob, connection_id=connection_id)
        return ConnectionRecord.model_validate_json(plain)
    except crypto.ConnectorEncryptionConfigError:
        raise
    except Exception:
        logger.warning(
            "connectors: skipping unreadable connection %r",
            connection_id,
            exc_info=True,
        )
        return None
