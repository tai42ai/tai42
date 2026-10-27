"""``TAI_BUS_*`` config for the internal worker bus.

The worker bus is app-owned INTERNAL infrastructure — the fan-out primitive that
updates every worker (HTTP server and backend runtime alike) — NOT a plugin: none
of these settings name a registrable or swappable surface, and no manifest field
selects a bus implementation. There is exactly one bus.

Settings are de-mixed the same way the other Redis-backed features are: the Redis
connection is a field composed from the kit connection shape (not a base the group
extends), so the group declares only its own fields. Connection values read from
the ``TAI_BUS_REDIS_*`` env; feature values from ``TAI_BUS_*``.

``TAI_BUS_REDIS_URL`` unset means the bus is OFF — the single-worker/file-mode
process runs on :meth:`WorkerBus.local`, and the boot rules name this var when they
refuse a deployment that requires a bus (multi-worker, a registered backend, or a
non-file ``TAI_CONFIG_MODE``).

Every bus key is under one root, ``{namespace}:v{major}``: the namespace
(``TAI_BUS_NAMESPACE``, default ``tai``) and the major version of the running skeleton.
The root prefixes the control channel, every ephemeral reply channel, the per-worker
presence keys and their presence index, the slot-claim keys, the generation counters
and the env-reader descriptions. Redis pub/sub is server-global (it is NOT scoped by
the numeric db), so two stacks sharing one Redis MUST diverge by namespace or they
cross-deliver each other's fleet ops. Workers of two skeleton majors on one Redis and
namespace are two fleets: neither reads the other's presence rows nor receives the
other's ops, so a rolling replacement across majors runs both side by side until the
older workers are gone. The major is a fact of the installed code, not a setting.
"""

from __future__ import annotations

from typing import ClassVar

from packaging.version import Version
from pydantic import Field, PrivateAttr
from pydantic_settings import SettingsConfigDict
from tai42_contract.access_control.identity import ReadinessTarget
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import ReloadClass, TaiBaseSettings, registered_settings, settings_cache

from tai42_skeleton import __version__


def skeleton_major() -> int:
    """The major version of the running skeleton: the part of the bus key root that is not the namespace."""
    return Version(__version__).major


class BusRedisSettings(RedisConnectionSettings):
    """Redis connection for the worker bus, composed from the kit connection shape.

    Connection values come from the ``TAI_BUS_REDIS_*`` env (``TAI_BUS_REDIS_URL``
    …); with no ``redis_url`` the bus is off and the process runs on
    :meth:`WorkerBus.local`.
    """

    model_config = SettingsConfigDict(env_prefix="TAI_BUS_")

    # Bus/broker URL reaches its pooled connection through the bus, torn down and
    # rebuilt on a process recycle — never live-swappable in-process.
    reload_class: ClassVar[ReloadClass] = "recycle"


class BusSettings(TaiBaseSettings):
    """Worker-bus settings, read from the ``TAI_BUS_`` environment."""

    model_config = SettingsConfigDict(env_prefix="TAI_BUS_", frozen=True)

    # Bus infrastructure converges only through a process recycle (boot resync
    # applies the env before bus init on respawn), never an in-process flip.
    reload_class: ClassVar[ReloadClass] = "recycle"

    # Infra: the redis connection is composed from the kit (a field, not a base),
    # so the group declares no connection fields of its own.
    redis: BusRedisSettings = Field(default_factory=BusRedisSettings)

    # With the running skeleton major, prefixes every bus key (:attr:`key_root`) so
    # co-tenant stacks on one server-global pub/sub Redis do not cross-deliver. It
    # locates the census the recycle roll reads, so no config change or profile may
    # change it (``bus_reaching_keys``).
    namespace: str = Field(
        default="tai",
        description=(
            "Prefixes every worker-bus key, followed by the running skeleton's major version: workers of "
            "different majors on one Redis and namespace are separate fleets. Set it in the deployment's "
            "environment: a config change or settings profile that changes it is refused."
        ),
    )

    # Short liveness deadline: reaching it only ends the brief ack wait — the
    # collector stops waiting on the fast ``received`` acks and budgets the rest of
    # its wait toward the apply/report cut. The missing/departed presence re-check
    # and verdict assignment happen once, in the finalize pass at the report cut
    # (apply deadline) — not here.
    ack_timeout: float = Field(default=2.0, gt=0)

    # Long apply deadline — the report cut. Never below the retire's drain floor
    # (three drains of the declared drain budget): the boot rules refuse a lower
    # value at boot, on every reload and on every env change, so a worker draining
    # real work answers inside it and ``timed_out`` names a worker whose reload is
    # stuck past its drain budget. The default budgets a worst-case reload under the
    # platform defaults: three default drains (3 x 10 s), the fork quiesce (30 s),
    # the credential refresh and the MCP re-probes (3 s each), the post-apply
    # ``on_fleet_op_applied`` hook (the pool turnover, 5 s + 3 s) and the rebuild
    # (about 2 s) — 76 s, with headroom for a loaded host. A worker that acked but
    # exceeds this is re-checked against presence: expired ⇒ departed,
    # alive ⇒ timed_out.
    apply_timeout: float = Field(default=90.0, gt=0)

    # Presence-key TTL; a subscriber's heartbeat thread refreshes it at a third of this,
    # so a killed or stopped process, or one whose heartbeat thread died, leaves the
    # census within one TTL instead of blocking every op. A config reload's rebuild
    # blocks the serving loop, not the heartbeat thread, so it never lapses the row.
    heartbeat_ttl: float = Field(default=15.0, gt=0)

    # The skeleton major these settings were built under, read by looking up
    # :func:`skeleton_major` each time a value is built and fixed for its life like every
    # other value of this frozen group.
    _major: int = PrivateAttr(default_factory=lambda: skeleton_major())

    @property
    def enabled(self) -> bool:
        """True when a Redis URL is configured; False means the bus is off."""
        return bool(self.redis.redis_url)

    @property
    def fleet(self) -> tuple[str | None, str]:
        """The values that locate the fleet's census: the bus Redis URL and the namespace.

        Two settings with the same ``fleet`` reach the same census, whatever their heartbeat,
        timeouts or connection knobs; ``(None, namespace)`` is the busless process. Every env
        name that sets one of these values is in :func:`fleet_env_names`. The skeleton major,
        the other part of :attr:`key_root`, is one value for every setting of one process.
        """
        return (self.redis.redis_url, self.namespace)

    @property
    def key_root(self) -> str:
        """The root of every bus key and channel: ``{namespace}:v{major}``.

        ``major`` is the running skeleton's major version, so workers of two majors on one
        Redis and namespace share no key: each major is its own fleet.
        """
        return f"{self.namespace}:v{self._major}"

    @property
    def channel(self) -> str:
        """The single control channel every subscriber consumes."""
        return f"{self.key_root}:bus:control"

    @property
    def reply_prefix(self) -> str:
        """Prefix for the per-dispatch ephemeral reply channels."""
        return f"{self.key_root}:bus:reply:"

    @property
    def presence_prefix(self) -> str:
        """Prefix for the per-name presence keys the census reads."""
        return f"{self.key_root}:bus:presence:"

    @property
    def presence_index(self) -> str:
        """The set of live worker names the census reads to find the presence keys.

        A subscriber SADDs its own name here alongside every presence write and SREMs
        it on a deliberate stop; the census SMEMBERS this set, then GET/PTTLs each
        member's presence key. The set has no TTL and is self-healing: a member whose
        presence key has expired is SREMed by the census on read.
        """
        return f"{self.key_root}:bus:presence-index"

    def presence_key(self, name: str) -> str:
        """The presence key for one worker slot name."""
        return f"{self.presence_prefix}{name}"

    @property
    def slot_prefix(self) -> str:
        """Prefix for the per-name slot-claim keys (``SET NX`` at claim time)."""
        return f"{self.key_root}:bus:slot:"

    def slot_key(self, name: str) -> str:
        """The claim key a worker wins to hold the slot ``name``."""
        return f"{self.slot_prefix}{name}"

    @property
    def gen_prefix(self) -> str:
        """Prefix for the per-name generation counters (``INCR`` at claim time)."""
        return f"{self.key_root}:bus:gen:"

    def gen_key(self, name: str) -> str:
        """The monotonic generation counter for the slot ``name``."""
        return f"{self.gen_prefix}{name}"

    def env_readers_key(self, digest: str) -> str:
        """The key holding the env-reader description a presence row names by ``digest``."""
        return f"{self.key_root}:env-readers:{digest}"


@settings_cache
def bus_settings() -> BusSettings:
    """Return the process-wide :class:`BusSettings`, cached after first load."""
    return BusSettings()


# The settings fields whose values make up :attr:`BusSettings.fleet`, by their group.
_FLEET_FIELDS: dict[type[TaiBaseSettings], frozenset[str]] = {
    BusSettings: frozenset({"redis", "namespace"}),
    BusRedisSettings: frozenset({"redis_url"}),
}


def fleet_env_names() -> frozenset[str]:
    """Every env name the settings layer reads for a value of :attr:`BusSettings.fleet`, upper-cased.

    Read from the settings registry: each field's accepted env names plus its
    default-namespace fallback. Raises ``RuntimeError`` when a bus settings group, or one of
    those fields, is not registered.
    """
    infos = {info.qualname: info for info in registered_settings()}
    names: set[str] = set()
    for cls, fields in _FLEET_FIELDS.items():
        info = infos.get(f"{cls.__module__}.{cls.__qualname__}")
        if info is None:
            raise RuntimeError(f"the bus settings group {cls.__qualname__} is not registered")
        found = [field for field in info.fields if field.name in fields]
        if {field.name for field in found} != fields:
            raise RuntimeError(f"the bus settings group {cls.__qualname__} does not register all of {sorted(fields)}")
        for field in found:
            names.update(name.upper() for name in field.accepted_env_vars)
            if field.default_namespace_var is not None:
                names.add(field.default_namespace_var.upper())
    return frozenset(names)


def readiness_targets() -> list[ReadinessTarget]:
    """The worker-bus Redis, when the bus is enabled."""
    bus = bus_settings()
    return [ReadinessTarget("bus", RedisClient, bus.redis)] if bus.enabled else []
