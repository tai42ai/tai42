"""Recycle refusal policy: the keys no env change may carry, and the recycle orchestrator's constants.

Two kinds of key are refused by every env writer, on every deployment:

* The bus-reaching keys (:func:`bus_reaching_keys`): every env name the settings layer
  reads for the bus Redis URL or the bus namespace. The orchestrator's census rides the bus
  itself, so a bus recycle is intrinsically unobservable: the scan opens the OLD bus while
  replacements register only on the NEW bus after resync. ``TAI_DEFAULT_REDIS_URL`` reaches the
  bus via ``BusSettings``' default-URL fallback and ``TAI_BUS_REDIS`` sets the bus Redis group
  as one JSON value, so both join ``TAI_BUS_REDIS_URL``; ``TAI_BUS_NAMESPACE`` prefixes every bus
  key, the census included, so a recycled worker under a new namespace registers in a census the
  scan never reads. A name matches under any spelling the settings layer reads
  (:func:`~tai42_kit.settings.fold_env_name`).
* The deployment's own infrastructure reads (:data:`X_CLASSIFIED_DEPLOYMENT_BARE_READS`), folded
  into the boundary validator's X band.

A ``recycle``-class change is carried by a worker recycle: the recycled worker exits gracefully
and the deployment's process supervisor starts it again under the new env.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_kit.settings import fold_env_name

from tai42_skeleton.app.bus import WorkerKind
from tai42_skeleton.app.bus_settings import fleet_env_names

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


def bus_reaching_keys(keys: Iterable[str]) -> list[str]:
    """The keys of ``keys`` that reach the bus, refused on every deployment, sorted.

    A key reaches the bus when the settings layer reads it, under any spelling, for a value that
    locates the bus and its census (:func:`~tai42_skeleton.app.bus_settings.fleet_env_names`).
    """
    names = fleet_env_names()
    return sorted({key for key in keys if fold_env_name(key) in names})


# Deployment-infrastructure bare reads. X-classified: no profile may carry them — a carried
# value would relocate the readiness sentinel. The boundary validator folds this set into its
# X-band refusal, enforced at EVERY env writer.
X_CLASSIFIED_DEPLOYMENT_BARE_READS: frozenset[str] = frozenset({"TAI_READY_SENTINEL_PATH"})

# The worker kinds the recycle orchestrator censuses as recycle targets.
CENSUS_TARGET_KINDS: tuple[WorkerKind, ...] = (WorkerKind.backend, WorkerKind.serve)


def _replace_diff_keys(stored: Mapping[str, str], proposed: Mapping[str, str]) -> set[str]:
    """The env key NAMES a whole-env replace changes: added, removed, or value-changed.

    Names only — the caller classifies them; a diff never carries a value off this seam.
    """
    added = {key for key in proposed if key not in stored}
    removed = {key for key in stored if key not in proposed}
    changed = {key for key in proposed if key in stored and stored[key] != proposed[key]}
    return added | removed | changed


def _refuse_unrecyclable(diff_keys: set[str], *, change: str) -> None:
    """Refuse an env change whose diff carries a bus-reaching key.

    A bus URL and the bus namespace locate the worker bus and its census, which no recycle can
    carry. The refusal is a loud ``ValueError`` the operations layer maps to a 400, naming
    ``change`` (what is being applied, e.g. ``"this settings profile"``) and the keys. Names only.
    """
    bus = bus_reaching_keys(diff_keys)
    if bus:
        raise ValueError(
            f"Refusing to apply {change}: it changes bus-reaching key(s): {', '.join(bus)}. A bus URL and the "
            "bus namespace locate the worker bus and its census, which no recycle can carry. Set them in the "
            "deployment's environment and restart the deployment."
        )


def _recycle_step_timeout() -> float:
    """The per-step recycle budget — the same drain budget a retire uses.

    A recycled worker's replacement gets the app's declared drain window to boot and rejoin the census.
    """
    from tai42_skeleton.app import instance

    return instance.build_app().drain_budgets.budget()
