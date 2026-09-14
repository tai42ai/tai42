"""Connection lifecycle — start_connect, complete_connect, reconnect,
disconnect, patch_sub_services.

Single-namespace: a connection is keyed by its uuid4 ``connection_id`` alone
(globally unique). Every record read-modify-write runs under
``connection_lock`` so concurrent writers serialise on a lock-on-write basis.
The lock is best-effort (a Redis outage lets the body run unlocked), so each
read-modify-write additionally persists via compare-and-set on the ciphertext
it loaded: a writer that lost the race raises
:class:`ConcurrentConnectionUpdateError` instead of clobbering the peer's record
(e.g. a concurrently-rotated refresh token).

The lifecycle operations reach their external collaborators through this package
namespace (``get_provider``, ``token_store``, ``load_record``,
``load_record_with_blob``, ``connection_lock``, ``clear_refresh_cooldown``,
``ConfigService``, and the ``state`` / ``manifest_writer`` modules): the operation
modules read them as attributes here at call time, so overriding one on this package
object reaches every operation. They are bound before the operation modules are
imported so the attributes exist as those modules bind against this package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_contract.connectors.service import AliasInUseError as AliasInUseError

from tai42_skeleton.config.service import ConfigService as ConfigService
from tai42_skeleton.connectors.oauth import state as state
from tai42_skeleton.connectors.providers.registry import get_provider as get_provider
from tai42_skeleton.connectors.runtime.locks import clear_refresh_cooldown as clear_refresh_cooldown
from tai42_skeleton.connectors.runtime.locks import connection_lock as connection_lock
from tai42_skeleton.connectors.service import manifest_writer as manifest_writer
from tai42_skeleton.connectors.store import token_store as token_store
from tai42_skeleton.connectors.store.persistence import ConnectionNotFoundError as ConnectionNotFoundError
from tai42_skeleton.connectors.store.persistence import load_record as load_record
from tai42_skeleton.connectors.store.persistence import load_record_with_blob as load_record_with_blob

from .complete import complete_connect
from .disconnect import disconnect
from .patch import patch_sub_services
from .persist import ConcurrentConnectionUpdateError
from .start import start_connect, start_reconnect

# Re-exported so callers import the lifecycle errors + operations from one place.
__all__ = [
    "AliasInUseError",
    "ConcurrentConnectionUpdateError",
    "ConnectionNotFoundError",
    "complete_connect",
    "disconnect",
    "patch_sub_services",
    "start_connect",
    "start_reconnect",
]


# -- Protocol conformance -------------------------------------------------

if TYPE_CHECKING:
    # This package IS the ConnectionService implementation (free functions, no
    # implementing class). Bind those functions as staticmethods so pyright checks
    # them structurally against the contract Protocol: a signature drift (e.g. a
    # missing kw-only ``origin``) fails the ``_CONFORMS`` assignment below at
    # type-check time.
    from tai42_contract.connectors.service import ConnectionService as _ConnectionServiceProtocol

    class _ModuleConnectionService:
        start_connect = staticmethod(start_connect)
        start_reconnect = staticmethod(start_reconnect)
        complete_connect = staticmethod(complete_connect)
        disconnect = staticmethod(disconnect)
        patch_sub_services = staticmethod(patch_sub_services)

    _CONFORMS: _ConnectionServiceProtocol = _ModuleConnectionService()
