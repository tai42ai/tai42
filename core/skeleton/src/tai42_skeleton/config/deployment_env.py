"""The deployment env: the environment the process was started with, before any stored key is applied.

A setting whose value arrives in the deployment env belongs to the deployment. Its value wins over
a stored copy at every boot and reload, and no env write may set it: the deployment changes it in
its own environment and restarts. Every other setting belongs to the stored env.

The deployment env is captured ONCE per process, by the cold-boot env bridge just before its
stored-env apply (``_apply_stored_env_at_boot``, which every serving and backend entry point
crosses). Until that apply nothing has written a stored key into ``os.environ``, so the capture
holds the container env and the launcher's own stamps. A forked child inherits the capture together with the
env it describes; a process started fresh captures its own at its first apply.

A settings field set under several env names is owned under all of them once the deployment
sets any one: its accepted names, the JSON name of a nested group it is in, and its delimited
names (:class:`tai42_kit.settings.EnvNameIndex`). Every name compares folded, as the settings
layer reads env names; a name no reader explains is owned under every spelling too, as the other
case-insensitive readers of the env read it. The names are resolved through the env readers of
the process: every registered settings group and every declared reader
(:func:`tai42_kit.settings.declare_env_reader`, FastMCP's settings among them). A reader
described after an apply (a plugin's group, imported once the boot env bridge ran) has the
stored keys it makes owned withdrawn at its description
(:func:`tai42_skeleton.app.applied_env._withdraw_newly_owned_env`).

A name counts as set by the deployment only when its value is non-empty: the settings layer reads
an empty value as absent (:func:`tai42_kit.settings.present_env_value`), so an empty container
value leaves the setting to the stored env.

Ownership holds across the fleet. The stored env is one for every worker, so a key the deployment
sets on any worker belongs to the deployment on every worker. Each bus worker publishes on its
presence row the names its deployment env sets (:func:`published_deployment_keys`) and the digest
of a description of its env readers (:func:`published_env_readers`), names and shapes only, never
values; the description itself is kept on the bus under that digest. Every judge of ownership
resolves the union of every census row's names, in any state and freshness, through one index of
every row's env readers and its own (:func:`fleet_deployment_ownership`), so a name only one
worker's readers read (a plugin installed on one worker, a worker on another image) is owned the
same on every worker. Every successful census read records the fleet's names and readers as last
read (:func:`known_fleet_env`). The boot env bridge and every operation that
judges a save, a reload or a read resolve the fleet's ownership from one census read
(:func:`resolve_deployment_ownership`). The boot env bridge applies the stored env by this
process's own deployment env, reads the census on the bus that env configures, and withdraws a
stored copy of a key any worker's deployment sets before anything is bound or built on it; a
withdraw that takes away a value locating that bus (its Redis URL or namespace) refuses the boot.
When the bus cannot answer, the operation judges by the fleet's names and readers as this process
last read them (at boot, its own deployment env) and logs a WARNING. A worker joining the
bus, receiving a join announcement or reconnecting reads the census strictly and fails that step
when it cannot. It withdraws the applied stored keys the fleet owns and, when it withdrew any,
rebuilds its serving generation through a config reload; a withdraw that takes away a value
locating the bus it is a member of ends the process with an ERROR instead. On joining the bus a worker publishes a
``deployment_keys_changed`` fleet op, on which every other worker does the same and reports the
recycle-class keys it withdrew but still runs on; the joining worker rolls a recycle over exactly
those workers, one at a time, and recycles itself last when its own withdraw left a recycle-class
key. With no bus the fleet is this process. A worker the deployment did not give a value runs that
setting's default.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections import OrderedDict
from typing import TYPE_CHECKING, NamedTuple, Protocol

from tai42_kit.settings import (
    EnvNameIndex,
    EnvReader,
    encode_env_readers,
    env_readers,
    fold_env_name,
    on_env_reader_described,
    present_env_value,
)

from tai42_skeleton.app.boot_rules import ConfigInvariantError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from tai42_kit.settings import SettingsFieldRef

    from tai42_skeleton.app.bus import WorkerRow


class PublishedEnvReaders(NamedTuple):
    """This process's env readers as its presence row publishes them."""

    # The SHA-256 hex digest of ``body``: what the presence row carries.
    digest: str
    # The canonical JSON of the readers (:func:`~tai42_kit.settings.encode_env_readers`),
    # kept on the bus under ``digest``.
    body: str
    readers: tuple[EnvReader, ...]


class FleetEnv(NamedTuple):
    """The fleet's deployment names and env readers, as one census read found them."""

    keys: frozenset[str]
    # Each census row's reader description, by digest.
    readers: Mapping[str, tuple[EnvReader, ...]]


# The process env as the cold-boot env bridge found it; ``None`` until then.
_captured: dict[str, str] | None = None

# The names this process's deployment env sets, sorted, as its presence row publishes them;
# ``None`` until the capture. One immutable value, replaced whole, because the bus heartbeat
# thread reads it.
_published: tuple[str, ...] | None = None

# This process's env readers as its presence row publishes them; ``None`` until the capture.
# One immutable value, replaced whole, because the bus heartbeat thread reads it.
_published_readers: PublishedEnvReaders | None = None

# Reader descriptions by digest: this process's own and those of the rows the last census read found.
_reader_bodies: dict[str, tuple[EnvReader, ...]] = {}

# Ownership indexes by the digests of the readers they hold, the most recently used last.
_indexes: OrderedDict[frozenset[str], EnvNameIndex] = OrderedDict()
_INDEXES_KEPT = 8

# The fleet as the most recent successful census read in this process found it; empty until the first read.
_fleet_known: FleetEnv = FleetEnv(frozenset(), {})

logger = logging.getLogger(__name__)


def capture_deployment_env() -> None:
    """Record the current ``os.environ`` as the deployment env, once per process; later calls do nothing."""
    global _captured, _published
    if _captured is None:
        _captured = dict(os.environ)
        _published = tuple(sorted(deployment_env_keys()))
        _describe_env_readers()


def _describe_env_readers() -> None:
    """Describe this process's env readers now, as its presence row publishes them."""
    global _published_readers
    readers = env_readers()
    body = encode_env_readers(readers)
    published = PublishedEnvReaders(hashlib.sha256(body.encode()).hexdigest(), body, readers)
    _reader_bodies[published.digest] = readers
    _published_readers = published


def _republish_on_description(_reader: EnvReader) -> None:
    """Describe the env readers again when a reader is described after the capture."""
    if _captured is not None:
        _describe_env_readers()


on_env_reader_described(_republish_on_description)


def published_deployment_keys() -> tuple[str, ...]:
    """The names this process's deployment env sets, sorted: what its presence row publishes to the fleet.

    Reads one cached value, safe from any thread. Raises when the deployment env is not captured.
    """
    if _published is None:
        _require_captured()
        raise RuntimeError("the deployment env is captured but its names were never recorded")
    return _published


def published_env_readers() -> PublishedEnvReaders:
    """This process's env readers, their digest and their body: what its presence row publishes to the fleet.

    Every registered settings group and every declared reader, described again whenever one is
    described after the capture. Reads one cached value, safe from any thread. Raises when the
    deployment env is not captured.
    """
    if _published_readers is None:
        _require_captured()
        raise RuntimeError("the deployment env is captured but its env readers were never described")
    return _published_readers


def _require_captured() -> dict[str, str]:
    """The captured deployment env; raises when the cold-boot env bridge has not captured it yet."""
    if _captured is None:
        raise RuntimeError(
            "the deployment env is not captured: the boot env bridge has not run in this process "
            "(it captures the deployment env before it applies the stored env)"
        )
    return _captured


def deployment_env_value(key: str) -> str | None:
    """The value ``key`` had in the deployment env, empty included; ``None`` when the deployment did not carry it."""
    return _require_captured().get(key)


def deployment_env_keys() -> frozenset[str]:
    """The names the deployment env sets to a non-empty value."""
    captured = _require_captured()
    return frozenset(key for key in captured if present_env_value(captured, key) is not None)


def _ownership_index(fleet_readers: Mapping[str, Sequence[EnvReader]]) -> EnvNameIndex:
    """One index of this process's env readers and ``fleet_readers``, kept by the digests it holds."""
    own = published_env_readers()
    bodies: dict[str, Sequence[EnvReader]] = {**fleet_readers, own.digest: own.readers}
    key = frozenset(bodies)
    index = _indexes.get(key)
    if index is None:
        index = EnvNameIndex.of(reader for digest in sorted(bodies) for reader in bodies[digest])
        _indexes[key] = index
        while len(_indexes) > _INDEXES_KEPT:
            _indexes.popitem(last=False)
    _indexes.move_to_end(key)
    return index


class DeploymentOwnership:
    """The keys the deployment owns, resolved once through one index of env readers.

    A key is owned when this process's deployment env sets it to a non-empty value or another
    worker publishes it (``fleet_keys``), under any spelling of the name: every name compares
    folded (:func:`~tai42_kit.settings.fold_env_name`), as the settings layer and the other
    case-insensitive readers read env names. A key is owned too when it sets a field an owned key
    sets, as the index resolves both (:meth:`~tai42_kit.settings.EnvNameIndex.fields_set_by`): a
    field's accepted names, the JSON name of a nested group it is in, and its delimited names all
    set it, so the deployment setting any one of them owns every other. The index holds this
    process's env readers and every reader description in ``fleet_readers`` (by digest), so every
    worker given the same fleet resolves every name the same. Without ``fleet_keys`` and
    ``fleet_readers`` this is the view of this process alone, as the boot env bridge applies the
    stored env by it before it reads the census; every operation that judges a save or an applied
    env resolves the fleet's view with :func:`resolve_deployment_ownership`.
    """

    def __init__(
        self, fleet_keys: Iterable[str] = (), fleet_readers: Mapping[str, Sequence[EnvReader]] | None = None
    ) -> None:
        """Resolve the owned keys from the captured deployment env, ``fleet_keys`` and the readers' index."""
        self._keys = deployment_env_keys() | frozenset(fleet_keys)
        self._folded = frozenset(fold_env_name(key) for key in self._keys)
        self._index = _ownership_index(fleet_readers or {})
        self._fields = frozenset(field for key in self._keys for field in self._index.fields_set_by(key))

    def fields_set_by(self, key: str) -> frozenset[SettingsFieldRef]:
        """Every field of the ownership's env readers the env name ``key`` sets; empty when it sets none."""
        return self._index.fields_set_by(key)

    def owns(self, key: str) -> bool:
        """Whether ``key`` is set by the deployment."""
        return fold_env_name(key) in self._folded or not self._fields.isdisjoint(self._index.fields_set_by(key))

    def owned(self, keys: Iterable[str]) -> list[str]:
        """The keys of ``keys`` the deployment owns, sorted."""
        return sorted({key for key in keys if self.owns(key)})

    def counting(self, env: Mapping[str, str]) -> dict[str, str]:
        """The entries of the stored env ``env`` that count: every key the deployment does not own.

        A stored copy of an owned key is never applied, so it is not part of the env a reload
        produces.
        """
        return {key: value for key, value in env.items() if not self.owns(key)}

    def changed_writes(self, written: Mapping[str, str], stored: Mapping[str, str]) -> list[str]:
        """The owned keys ``written`` sets to a value other than their ``stored`` value, sorted.

        An empty written value removes its key and is never counted; neither is a value equal to the
        stored one.
        """
        return sorted(
            key for key, value in written.items() if value != "" and value != stored.get(key) and self.owns(key)
        )


class _Census(Protocol):
    """The census and env-reader reads of a worker bus."""

    async def census(self) -> list[WorkerRow]: ...

    async def env_readers(self, digests: Sequence[str]) -> dict[str, tuple[EnvReader, ...]]: ...


async def fleet_deployment_ownership(bus: _Census) -> DeploymentOwnership:
    """The keys the deployment owns across the fleet: every census row's names, through every row's env readers.

    Every row counts, whatever its state or freshness: a worker that is leaving, returning or late
    on its beat still runs on the env it was started with. A row's reader description is fetched
    from the bus once per digest and kept while the most recent read's census names it; a read
    judges by the descriptions its own census names. A row whose description the bus does not
    hold raises a :class:`~tai42_skeleton.app.boot_rules.ConfigInvariantError` naming the
    worker and the digest. The read records the fleet's names and readers as last read
    (:func:`known_fleet_env`). A failed census read raises.
    """
    global _fleet_known
    rows = await bus.census()
    digests = {row.env_readers for row in rows}
    # The bodies this read judges by are taken from the cache before the fetch awaits, so a
    # concurrent read that forgets them meanwhile does not take them from this one.
    readers = {digest: _reader_bodies[digest] for digest in digests if digest in _reader_bodies}
    unseen = sorted(digests - readers.keys())
    if unseen:
        fetched = await bus.env_readers(unseen)
        readers.update(fetched)
        _reader_bodies.update(fetched)
    for row in rows:
        if row.env_readers not in readers:
            raise ConfigInvariantError(
                f"worker {row.name} publishes env readers {row.env_readers}, and the bus holds no "
                "description under that digest"
            )
    own = published_env_readers().digest
    for digest in [digest for digest in _reader_bodies if digest not in digests and digest != own]:
        del _reader_bodies[digest]
    fleet = FleetEnv(keys=frozenset(key for row in rows for key in row.deployment_keys), readers=readers)
    _fleet_known = fleet
    return DeploymentOwnership(*fleet)


def known_fleet_env() -> FleetEnv:
    """The fleet's names and env readers, as the most recent successful census read found them.

    Empty before the first read in this process. Reads no bus.
    """
    return _fleet_known


async def resolve_deployment_ownership(bus: _Census, op_name: str) -> DeploymentOwnership:
    """The fleet's ownership for the operation ``op_name``, read from one census read.

    When the bus cannot answer (one of the bus's transport errors), the operation judges by the
    fleet's names and readers as this process last read them (:func:`known_fleet_env`; before any
    read, this process's own deployment env) and logs a WARNING naming the operation. Any other
    error raises.
    """
    # Imported here: the bus package imports this module at load.
    from tai42_skeleton.app.bus.models import TRANSPORT_ERRORS

    try:
        return await fleet_deployment_ownership(bus)
    except TRANSPORT_ERRORS:
        known = known_fleet_env()
        logger.warning(
            "deployment ownership for %r: the census could not be read — judged by the fleet's names "
            "as last read (%d names, %d reader descriptions)",
            op_name,
            len(known.keys),
            len(known.readers),
            exc_info=True,
        )
        return DeploymentOwnership(*known)


def reset_deployment_env() -> None:
    """Forget the captured deployment env, what this process publishes and the fleet as last read.

    The next capture records them again.

    For tests, which run many boots in one process; a deployed process captures exactly once.
    """
    global _captured, _published, _published_readers, _fleet_known
    _captured = None
    _published = None
    _published_readers = None
    _reader_bodies.clear()
    _indexes.clear()
    _fleet_known = FleetEnv(frozenset(), {})
