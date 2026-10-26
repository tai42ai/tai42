"""Response models for the connectors operations: the start-connect outcome and the KEK re-encrypt sweep."""

from __future__ import annotations

from pydantic import BaseModel, RootModel
from tai42_contract.connectors.models import StartConnectNoAuthResponse, StartConnectResponse


class StartConnectOutcome(RootModel[StartConnectResponse | StartConnectNoAuthResponse]):
    """A Connect start.

    Either an OAuth authorize URL (``StartConnectResponse``) or an immediate no-auth connection
    (``StartConnectNoAuthResponse``).
    """


class ConnectorReencryptResult(BaseModel):
    """The KEK re-encrypt sweep's outcome, for the connection tokens and the stored client credentials.

    ``scanned`` connection blobs split into ``reencrypted`` (rewritten under the current KEK), ``skipped``
    (already under the current key), and ``failed`` (no configured key could open them, or compare-and-set
    contention was not resolved); ``failed_connection_ids`` names each failed connection. The ``clients_*``
    counts split the stored OAuth client blobs the same way, ``failed_client_row_ids`` naming each failed
    client row. ``cas_retries`` counts the compare-and-set retries concurrent writers forced in both tables.
    """

    scanned: int
    reencrypted: int
    skipped: int
    failed: int
    failed_connection_ids: list[str]
    clients_scanned: int
    clients_reencrypted: int
    clients_skipped: int
    clients_failed: int
    failed_client_row_ids: list[str]
    cas_retries: int
