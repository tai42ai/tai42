"""The client-credential env names a raw manifest's oauth connectors reference.

An ``oauth`` connector descriptor names the env variables its OAuth client reads at
connect time (``client_id_env``, ``client_secret_env``). Every config reader that needs
those names reads them here, from the raw manifest dict (marker leaves allowed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

_CLIENT_ENV_FIELDS: tuple[Literal["client_id_env", "client_secret_env"], ...] = ("client_id_env", "client_secret_env")


@dataclass(frozen=True)
class ConnectorEnvRef:
    """One client-credential env name of the connector at ``connectors[index]``."""

    index: int
    field: Literal["client_id_env", "client_secret_env"]
    var: str

    @property
    def secret(self) -> bool:
        """True for the client secret's name, whose value the platform masks."""
        return self.field == "client_secret_env"


def connector_client_env_refs(manifest: Mapping[str, Any]) -> list[ConnectorEnvRef]:
    """Client-credential env names the raw manifest's oauth connectors reference.

    Reads the raw dict (marker leaves allowed); only ``kind == "oauth"`` entries; only
    non-empty str values; a missing or malformed ``connectors`` section contributes nothing.
    """
    connectors = manifest.get("connectors")
    if not isinstance(connectors, list):
        return []
    refs: list[ConnectorEnvRef] = []
    for index, connector in enumerate(connectors):
        if not isinstance(connector, dict) or connector.get("kind") != "oauth":
            continue
        for field_name in _CLIENT_ENV_FIELDS:
            var = connector.get(field_name)
            if isinstance(var, str) and var:
                refs.append(ConnectorEnvRef(index=index, field=field_name, var=var))
    return refs
