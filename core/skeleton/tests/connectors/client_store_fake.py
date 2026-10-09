"""An in-memory stand-in for the connector client store.

It seals and opens credentials with the real helpers and chooses a flow's client with the real
:func:`select_for_flow`, so only the SQL is replaced; the SQL itself is exercised on a real
Postgres in ``test_store_client_store_integration.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

from tai42_skeleton.connectors.oauth.client import ClientAuthMethod
from tai42_skeleton.connectors.oauth.discovery import same_issuer
from tai42_skeleton.connectors.oauth.registration import RegisteredClient
from tai42_skeleton.connectors.store.client_store import (
    RestoreClientRowResult,
    StoredClient,
    StoredClientRow,
    open_row,
    seal_credentials,
    select_for_flow,
)


class FakeClientStore:
    """Rows in a list, newest last; ``calls`` records every method called, in order."""

    def __init__(self) -> None:
        self.rows: list[StoredClientRow] = []
        self.calls: list[str] = []

    def _newest_first(self, provider_id: str) -> list[StoredClientRow]:
        return [row for row in reversed(self.rows) if row.provider_id == provider_id]

    def add_row(
        self,
        *,
        provider_id: str,
        issuer: str,
        client_id: str,
        source: str,
        client_secret: str | None = "stored-secret",
        auth_method: ClientAuthMethod = "client_secret_post",
        redirect_uri: str | None = None,
        secret_expires_at: datetime | None = None,
        dead: bool = False,
    ) -> StoredClientRow:
        row_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        row = StoredClientRow(
            client_row_id=row_id,
            provider_id=provider_id,
            issuer=issuer,
            client_id=client_id,
            source=source,  # type: ignore[arg-type]
            redirect_uri=redirect_uri,
            secret_expires_at=secret_expires_at,
            dead=dead,
            encrypted_blob=seal_credentials(row_id, client_secret, auth_method),
            created_at=now,
            updated_at=now,
        )
        self.rows.append(row)
        return row

    async def find_for_flow(self, provider_id: str, issuer: str, redirect_uri: str) -> StoredClient | None:
        self.calls.append("find_for_flow")
        row = select_for_flow(
            self._newest_first(provider_id), issuer=issuer, redirect_uri=redirect_uri, now=datetime.now(UTC)
        )
        return None if row is None else open_row(row)

    async def get(self, provider_id: str, issuer: str, client_id: str) -> StoredClient | None:
        self.calls.append("get")
        for row in self._newest_first(provider_id):
            if row.client_id == client_id and same_issuer(row.issuer, issuer):
                return open_row(row)
        return None

    async def manual_issuer(self, provider_id: str) -> str | None:
        self.calls.append("manual_issuer")
        for row in self._newest_first(provider_id):
            if row.source == "manual":
                return row.issuer
        return None

    async def put_manual(
        self,
        provider_id: str,
        issuer: str,
        client_id: str,
        client_secret: str | None,
        auth_method: ClientAuthMethod,
    ) -> StoredClient:
        self.calls.append("put_manual")
        self.rows = [row for row in self.rows if not (row.provider_id == provider_id and row.source == "manual")]
        row = self.add_row(
            provider_id=provider_id,
            issuer=issuer,
            client_id=client_id,
            source="manual",
            client_secret=client_secret,
            auth_method=auth_method,
        )
        return open_row(row)

    async def add_dynamic(self, provider_id: str, issuer: str, registered: RegisteredClient) -> StoredClient:
        self.calls.append("add_dynamic")
        row = self.add_row(
            provider_id=provider_id,
            issuer=issuer,
            client_id=registered.client_id,
            source="dynamic",
            client_secret=registered.client_secret,
            auth_method=registered.auth_method,
            redirect_uri=registered.redirect_uri,
            secret_expires_at=registered.secret_expires_at,
        )
        return open_row(row)

    async def mark_dead(self, client_row_id: str) -> bool:
        self.calls.append("mark_dead")
        for index, row in enumerate(self.rows):
            if row.client_row_id == client_row_id and row.source == "dynamic":
                self.rows[index] = replace(row, dead=True)
                return True
        return False

    async def delete_for_provider(self, provider_id: str) -> int:
        self.calls.append("delete_for_provider")
        before = len(self.rows)
        self.rows = [row for row in self.rows if row.provider_id != provider_id]
        return before - len(self.rows)

    async def has_rows(self, provider_id: str) -> bool:
        self.calls.append("has_rows")
        return any(row.provider_id == provider_id for row in self.rows)

    async def enumerate_rows(self) -> list[StoredClientRow]:
        self.calls.append("enumerate_rows")
        return sorted(self.rows, key=lambda row: row.client_row_id)

    async def get_row(self, client_row_id: str) -> StoredClientRow | None:
        self.calls.append("get_row")
        return next((row for row in self.rows if row.client_row_id == client_row_id), None)

    async def rewrite_blob(self, client_row_id: str, new_blob: bytes, *, expected_blob: bytes) -> bool:
        self.calls.append("rewrite_blob")
        for index, row in enumerate(self.rows):
            if row.client_row_id == client_row_id and row.encrypted_blob == expected_blob:
                self.rows[index] = replace(row, encrypted_blob=new_blob)
                return True
        return False

    async def restore(self, rows: Sequence[StoredClientRow], *, overwrite: bool) -> list[RestoreClientRowResult]:
        self.calls.append("restore")
        results: list[RestoreClientRowResult] = []
        for incoming in rows:

            def conflicts(row: StoredClientRow, incoming: StoredClientRow = incoming) -> bool:
                return (
                    row.client_row_id == incoming.client_row_id
                    or (row.provider_id, row.issuer, row.client_id)
                    == (incoming.provider_id, incoming.issuer, incoming.client_id)
                    or (
                        incoming.source == "manual"
                        and row.provider_id == incoming.provider_id
                        and row.source == "manual"
                    )
                )

            clash = any(conflicts(row) for row in self.rows)
            if clash and not overwrite:
                results.append(RestoreClientRowResult(incoming.client_row_id, "skipped_existing"))
                continue
            self.rows = [row for row in self.rows if not conflicts(row)]
            self.rows.append(incoming)
            results.append(RestoreClientRowResult(incoming.client_row_id, "replaced" if clash else "created"))
        return results

    def row(self, client_row_id: str) -> StoredClientRow:
        return next(row for row in self.rows if row.client_row_id == client_row_id)
