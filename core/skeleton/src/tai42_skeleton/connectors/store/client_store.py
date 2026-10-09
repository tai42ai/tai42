"""The sealed OAuth client store: the clients of providers that name no env client.

Postgres only (table ``connector_oauth_clients``, defined in the skeleton baseline SQL). A row is a
MANUAL client an administrator registered by hand (at most one per provider) or a DYNAMIC client
the engine registered with an authorization server. Every row is bound to the issuer that issued
it and is never used with another; issuers are compared with the engine's one issuer comparison.
The store is read once per flow start and once per refresh, never on the token hot path, so it has
no cache.

The client secret and the token-endpoint authentication method are sealed in ``encrypted_blob``
with ``oauth-client:<client_row_id>`` as associated data, so a blob opens only in its own row. The
backup and key-rotation sweeps reach the table only through :meth:`ConnectorClientStore.enumerate_rows`,
:meth:`~ConnectorClientStore.restore`, :meth:`~ConnectorClientStore.get_row` and
:meth:`~ConnectorClientStore.rewrite_blob`.

Every method refuses with :class:`ConnectorStoreNotConfiguredError` before any SQL when the
skeleton's database is not configured; every backend error raises.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, LiteralString, cast, get_args

from tai42_contract.connectors.errors import ConnectorError
from tai42_contract.errors import ErrorKind
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient, read_connection
from tai42_kit.db import component_store_configured, component_store_settings

from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.oauth.client import CLIENT_AUTH_METHODS, ClientAuthMethod
from tai42_skeleton.connectors.oauth.discovery import same_issuer
from tai42_skeleton.db import SKELETON_COMPONENT, not_configured_message

if TYPE_CHECKING:
    from tai42_skeleton.connectors.oauth.registration import RegisteredClient

StoredClientSource = Literal["manual", "dynamic"]
RestoreClientOutcome = Literal["created", "replaced", "skipped_existing"]

_SOURCES: tuple[str, ...] = get_args(StoredClientSource)

_COLUMNS: LiteralString = (
    "client_row_id, provider_id, issuer, client_id, source, redirect_uri, secret_expires_at, dead, "
    "encrypted_blob, created_at, updated_at"
)


class ConnectorStoreNotConfiguredError(ConnectorError):
    """The connector client store was reached while the skeleton's database is not configured."""

    __tai_error_kind__ = ErrorKind.UNAVAILABLE


@dataclass(frozen=True)
class StoredClientRow:
    """One row of the client store as stored: every column, the blob still sealed."""

    client_row_id: str
    provider_id: str
    issuer: str
    client_id: str
    source: StoredClientSource
    redirect_uri: str | None
    secret_expires_at: datetime | None
    dead: bool
    encrypted_blob: bytes = field(repr=False)
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class StoredClient:
    """A stored client, opened: its identity, its state and its credentials."""

    client_row_id: str
    provider_id: str
    issuer: str
    client_id: str
    source: StoredClientSource
    redirect_uri: str | None
    secret_expires_at: datetime | None
    dead: bool
    client_secret: str | None = field(repr=False)
    auth_method: ClientAuthMethod


@dataclass(frozen=True)
class RestoreClientRowResult:
    """What :meth:`ConnectorClientStore.restore` did with one row."""

    client_row_id: str
    outcome: RestoreClientOutcome


def client_aad(client_row_id: str) -> str:
    """The associated data a client row's blob is sealed with: a namespace no connection id can take."""
    return f"oauth-client:{client_row_id}"


def seal_credentials(client_row_id: str, client_secret: str | None, auth_method: ClientAuthMethod) -> bytes:
    """Seal a client's secret and authentication method to the row ``client_row_id``."""
    plaintext = json.dumps({"client_secret": client_secret, "token_endpoint_auth_method": auth_method})
    return crypto.encrypt(plaintext.encode("utf-8"), aad=client_aad(client_row_id))


def open_row(row: StoredClientRow) -> StoredClient:
    """Open a row's sealed credentials; a blob that is not this row's, or malformed, raises."""
    payload = json.loads(crypto.decrypt(row.encrypted_blob, aad=client_aad(row.client_row_id)))
    secret = payload.get("client_secret") if isinstance(payload, dict) else None
    method = payload.get("token_endpoint_auth_method") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or (secret is not None and not isinstance(secret, str)):
        raise ConnectorError(f"client row {row.client_row_id} holds malformed credentials")
    if method not in CLIENT_AUTH_METHODS:
        raise ConnectorError(f"client row {row.client_row_id} holds an unknown authentication method {method!r}")
    return StoredClient(
        client_row_id=row.client_row_id,
        provider_id=row.provider_id,
        issuer=row.issuer,
        client_id=row.client_id,
        source=row.source,
        redirect_uri=row.redirect_uri,
        secret_expires_at=row.secret_expires_at,
        dead=row.dead,
        client_secret=secret,
        auth_method=cast("ClientAuthMethod", method),
    )


def select_for_flow(
    rows: Iterable[StoredClientRow], *, issuer: str, redirect_uri: str, now: datetime
) -> StoredClientRow | None:
    """The row a new flow uses among one provider's ``rows`` (newest first), or ``None``.

    The provider's manual row for ``issuer`` when it has one; else the newest dynamic row for
    ``issuer`` that is not dead, whose secret has not expired and that was registered with exactly
    ``redirect_uri``. Another issuer's rows are never chosen.
    """
    dynamic: StoredClientRow | None = None
    for row in rows:
        if not same_issuer(row.issuer, issuer):
            continue
        if row.source == "manual":
            return row
        usable = (
            not row.dead
            and (row.secret_expires_at is None or row.secret_expires_at > now)
            and row.redirect_uri == redirect_uri
        )
        if usable and dynamic is None:
            dynamic = row
    return dynamic


def _row(values: Sequence[Any]) -> StoredClientRow:
    (row_id, provider_id, issuer, client_id, source, redirect_uri, expires, dead, blob, created, updated) = values
    if source not in _SOURCES:
        raise ConnectorError(f"client row {row_id} has an unknown source {source!r}")
    return StoredClientRow(
        client_row_id=str(row_id),
        provider_id=provider_id,
        issuer=issuer,
        client_id=client_id,
        source=source,
        redirect_uri=redirect_uri,
        secret_expires_at=expires,
        dead=bool(dead),
        encrypted_blob=bytes(blob),
        created_at=created,
        updated_at=updated,
    )


class ConnectorClientStore:
    """The Postgres-backed store of manual and dynamic OAuth clients."""

    @staticmethod
    def _require_configured() -> None:
        if not component_store_configured(SKELETON_COMPONENT):
            raise ConnectorStoreNotConfiguredError(not_configured_message("connector client store"))

    @staticmethod
    def _pool() -> Any:
        return client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT))

    async def _select(self, where: LiteralString, params: Sequence[Any]) -> list[StoredClientRow]:
        self._require_configured()
        async with self._pool() as pool, read_connection(pool) as conn, conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM connector_oauth_clients WHERE {where} "  # noqa: S608 constant fragments
                "ORDER BY created_at DESC, client_row_id",
                params,
            )
            return [_row(values) for values in await cur.fetchall()]

    # -- the engine's reads -------------------------------------------------------------------

    async def find_for_flow(self, provider_id: str, issuer: str, redirect_uri: str) -> StoredClient | None:
        """The client a new flow of ``provider_id`` at ``issuer`` uses (see :func:`select_for_flow`), or ``None``."""
        rows = await self._select("provider_id = %s", (provider_id,))
        row = select_for_flow(rows, issuer=issuer, redirect_uri=redirect_uri, now=datetime.now(UTC))
        return None if row is None else open_row(row)

    async def get(self, provider_id: str, issuer: str, client_id: str) -> StoredClient | None:
        """The client a grant was issued to, dead rows included, or ``None``."""
        rows = await self._select("provider_id = %s AND client_id = %s", (provider_id, client_id))
        for row in rows:
            if same_issuer(row.issuer, issuer):
                return open_row(row)
        return None

    async def manual_issuer(self, provider_id: str) -> str | None:
        """The issuer of the provider's manual client, or ``None`` when it has none."""
        rows = await self._select("provider_id = %s AND source = 'manual'", (provider_id,))
        return rows[0].issuer if rows else None

    async def has_rows(self, provider_id: str) -> bool:
        """Whether any client row (live, expired or dead) is stored for ``provider_id``."""
        self._require_configured()
        async with self._pool() as pool, read_connection(pool) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT EXISTS (SELECT 1 FROM connector_oauth_clients WHERE provider_id = %s)", (provider_id,)
            )
            found = await cur.fetchone()
        return bool(found and found[0])

    async def sources(self, provider_ids: Sequence[str]) -> dict[str, StoredClientSource]:
        """For each of ``provider_ids`` that holds a usable stored client, which kind it holds.

        ``manual`` when the provider has a manual row, else ``dynamic`` when it has a dynamic row
        that is neither dead nor past its secret's expiry; a provider with neither is absent.
        """
        self._require_configured()
        async with self._pool() as pool, read_connection(pool) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT provider_id, bool_or(source = 'manual'), "
                "bool_or(source = 'dynamic' AND NOT dead AND (secret_expires_at IS NULL OR secret_expires_at > now())) "
                "FROM connector_oauth_clients WHERE provider_id = ANY(%s) GROUP BY provider_id",
                (list(provider_ids),),
            )
            grouped = await cur.fetchall()
        out: dict[str, StoredClientSource] = {}
        for provider_id, has_manual, has_dynamic in grouped:
            if has_manual:
                out[provider_id] = "manual"
            elif has_dynamic:
                out[provider_id] = "dynamic"
        return out

    # -- the engine's writes ------------------------------------------------------------------

    async def put_manual(
        self,
        provider_id: str,
        issuer: str,
        client_id: str,
        client_secret: str | None,
        auth_method: ClientAuthMethod,
    ) -> StoredClient:
        """Store the provider's manual client, replacing the one it holds, in one statement.

        The row takes a new ``client_row_id`` (its blob is sealed to it).
        """
        self._require_configured()
        if auth_method not in CLIENT_AUTH_METHODS:
            raise ValueError(f"unknown token endpoint authentication method {auth_method!r}")
        row_id = str(uuid.uuid4())
        blob = seal_credentials(row_id, client_secret, auth_method)
        async with self._pool() as pool, pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO connector_oauth_clients "
                "(client_row_id, provider_id, issuer, client_id, source, encrypted_blob) "
                "VALUES (%s, %s, %s, %s, 'manual', %s) "
                "ON CONFLICT (provider_id) WHERE source = 'manual' DO UPDATE "
                "SET client_row_id = EXCLUDED.client_row_id, issuer = EXCLUDED.issuer, "
                "    client_id = EXCLUDED.client_id, redirect_uri = NULL, secret_expires_at = NULL, "
                "    dead = false, encrypted_blob = EXCLUDED.encrypted_blob, created_at = now(), updated_at = now() "
                "RETURNING client_row_id, provider_id, issuer, client_id, source, redirect_uri, secret_expires_at, "
                "dead, encrypted_blob, created_at, updated_at",
                (uuid.UUID(row_id), provider_id, issuer, client_id, blob),
            )
            values = await cur.fetchone()
        if values is None:
            raise ConnectorError(f"storing the manual client of provider {provider_id!r} returned no row")
        return open_row(_row(values))

    async def add_dynamic(self, provider_id: str, issuer: str, registered: RegisteredClient) -> StoredClient:
        """Insert a dynamically registered client; dynamic rows are never overwritten."""
        self._require_configured()
        row_id = str(uuid.uuid4())
        blob = seal_credentials(row_id, registered.client_secret, registered.auth_method)
        async with self._pool() as pool, pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO connector_oauth_clients "
                "(client_row_id, provider_id, issuer, client_id, source, redirect_uri, secret_expires_at, "
                " encrypted_blob) "
                "VALUES (%s, %s, %s, %s, 'dynamic', %s, %s, %s) "
                "RETURNING client_row_id, provider_id, issuer, client_id, source, redirect_uri, secret_expires_at, "
                "dead, encrypted_blob, created_at, updated_at",
                (
                    uuid.UUID(row_id),
                    provider_id,
                    issuer,
                    registered.client_id,
                    registered.redirect_uri,
                    registered.secret_expires_at,
                    blob,
                ),
            )
            values = await cur.fetchone()
        if values is None:
            raise ConnectorError(f"storing a dynamic client of provider {provider_id!r} returned no row")
        return open_row(_row(values))

    async def mark_dead(self, client_row_id: str) -> bool:
        """Mark a dynamic client refused by its authorization server; a manual row is never marked.

        Returns whether a row was marked (``False``: no such dynamic row).
        """
        self._require_configured()
        async with self._pool() as pool, pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE connector_oauth_clients SET dead = true, updated_at = now() "
                "WHERE client_row_id = %s AND source = 'dynamic' RETURNING client_row_id",
                (uuid.UUID(client_row_id),),
            )
            marked = await cur.fetchone()
        return marked is not None

    async def delete_for_provider(self, provider_id: str) -> int:
        """Delete every client row of ``provider_id``; returns how many were deleted."""
        self._require_configured()
        async with self._pool() as pool, pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM connector_oauth_clients WHERE provider_id = %s", (provider_id,))
            return cur.rowcount

    # -- the sweeps (backup, key rotation) ----------------------------------------------------

    async def enumerate_rows(self) -> list[StoredClientRow]:
        """Every row as stored, dead and expired ones included, ordered by ``client_row_id``."""
        self._require_configured()
        async with self._pool() as pool, read_connection(pool) as conn, conn.cursor() as cur:
            await cur.execute(f"SELECT {_COLUMNS} FROM connector_oauth_clients ORDER BY client_row_id")  # noqa: S608 constant fragment
            return [_row(values) for values in await cur.fetchall()]

    async def get_row(self, client_row_id: str) -> StoredClientRow | None:
        """The row ``client_row_id`` as it stands now, or ``None``."""
        rows = await self._select("client_row_id = %s", (uuid.UUID(client_row_id),))
        return rows[0] if rows else None

    async def rewrite_blob(self, client_row_id: str, new_blob: bytes, *, expected_blob: bytes) -> bool:
        """Compare-and-set a row's sealed blob; ``False`` when the row changed or is gone."""
        self._require_configured()
        async with self._pool() as pool, pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE connector_oauth_clients SET encrypted_blob = %s, updated_at = now() "
                "WHERE client_row_id = %s AND encrypted_blob = %s RETURNING client_row_id",
                (bytes(new_blob), uuid.UUID(client_row_id), bytes(expected_blob)),
            )
            written = await cur.fetchone()
        return written is not None

    async def restore(self, rows: Sequence[StoredClientRow], *, overwrite: bool) -> list[RestoreClientRowResult]:
        """Write each row whole, its ``client_row_id`` kept (its blob is sealed to it), one savepoint per row.

        A row conflicts with the stored rows that share its natural key (``provider_id``,
        ``issuer``, ``client_id``) or its ``client_row_id``, and, for a manual row, with the
        provider's other manual row. Without ``overwrite`` a conflicting row is ``skipped_existing``
        and nothing is touched; with it the conflicting rows are replaced by the imported one
        (``replaced``). Any database error aborts the restore.
        """
        self._require_configured()
        results: list[RestoreClientRowResult] = []
        async with self._pool() as pool, pool.connection() as conn, conn.cursor() as cur:
            for row in rows:
                row_uuid = uuid.UUID(row.client_row_id)
                async with conn.transaction():
                    await cur.execute(
                        "SELECT client_row_id FROM connector_oauth_clients "
                        "WHERE client_row_id = %s OR (provider_id = %s AND issuer = %s AND client_id = %s) "
                        "OR (%s AND provider_id = %s AND source = 'manual')",
                        (row_uuid, row.provider_id, row.issuer, row.client_id, row.source == "manual", row.provider_id),
                    )
                    conflicts = [values[0] for values in await cur.fetchall()]
                    if conflicts and not overwrite:
                        results.append(RestoreClientRowResult(str(row_uuid), "skipped_existing"))
                        continue
                    if conflicts:
                        await cur.execute(
                            "DELETE FROM connector_oauth_clients WHERE client_row_id = ANY(%s)", (conflicts,)
                        )
                    await cur.execute(
                        f"INSERT INTO connector_oauth_clients ({_COLUMNS}) "  # noqa: S608 constant fragment
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            row_uuid,
                            row.provider_id,
                            row.issuer,
                            row.client_id,
                            row.source,
                            row.redirect_uri,
                            row.secret_expires_at,
                            row.dead,
                            row.encrypted_blob,
                            row.created_at,
                            row.updated_at,
                        ),
                    )
                results.append(RestoreClientRowResult(str(row_uuid), "replaced" if conflicts else "created"))
        return results


def active_client_store() -> ConnectorClientStore:
    """The engine's client store."""
    return ConnectorClientStore()
