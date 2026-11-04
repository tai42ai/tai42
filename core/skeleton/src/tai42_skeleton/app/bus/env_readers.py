"""The env-reader descriptions the bus holds, each under the digest a presence row names it by.

A worker's presence write (:mod:`~tai42_skeleton.app.bus.life`) lands the description of its env
readers under its digest; a reader of the fleet's rows looks the descriptions up here by digest.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import decode_env_readers

from tai42_skeleton.config.deployment_env import published_env_readers

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tai42_kit.settings import EnvReader

    from tai42_skeleton.app.bus_settings import BusSettings

# The seam-package object this submodule reads ``client_ctx`` through at call time, so a
# ``monkeypatch.setattr`` on the ``tai42_skeleton.app.bus`` alias bites the pooled
# connection opened here. Captured as this package instance's own object.
_pkg = sys.modules["tai42_skeleton.app.bus"]


class WorkerBusEnvReadersMixin:
    """The env-reader description lookup of :class:`~tai42_skeleton.app.bus.WorkerBus`."""

    if TYPE_CHECKING:
        # Supplied by the composed WorkerBus (set in WorkerBus.__init__).
        _settings: BusSettings
        _local: bool

    async def env_readers(self, digests: Sequence[str]) -> dict[str, tuple[EnvReader, ...]]:
        """The env-reader descriptions the bus holds under ``digests``, by digest, read in one round trip.

        A digest the bus holds no description under is left out. The busless variant holds this
        process's own description only. On an outage it raises one of
        :data:`~tai42_skeleton.app.bus.models.TRANSPORT_ERRORS`; a value that is not a description
        raises.
        """
        if not digests:
            return {}
        if self._local:
            own = published_env_readers()
            return {own.digest: own.readers} if own.digest in digests else {}
        async with _pkg.client_ctx(RedisClient, self._settings.redis) as conn:
            bodies = await conn.mget([self._settings.env_readers_key(digest) for digest in digests])
        return {
            digest: decode_env_readers(body) for digest, body in zip(digests, bodies, strict=True) if body is not None
        }
