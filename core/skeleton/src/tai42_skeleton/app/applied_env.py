"""The stored env the process applied to ``os.environ``, which a serving epoch is built under.

The cold-boot env bridge applies the stored env before anything is bound; every epoch build
applies the proposed env it builds under (:func:`~tai42_skeleton.app.epoch.build_and_swap_epoch`)
and a discarded build restores the previous one (:func:`restore_env`). Every apply keeps the
value the deployment sets: a stored key the deployment owns is never applied, and a key that
becomes owned after it was applied — a worker's join names it, or a settings group described
later reads it — is withdrawn. This module holds the set of names the last apply set.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from tai42_kit.settings import on_env_reader_described, reset_all_settings

from tai42_skeleton.config.boundary import refuse_colliding_spellings, refuse_x_band
from tai42_skeleton.config.deployment_env import DeploymentOwnership, deployment_env_value, known_fleet_env

if TYPE_CHECKING:
    from tai42_kit.settings import EnvReader

logger = logging.getLogger(__name__)

# The stored env keys the last successful apply loaded into os.environ, so the next
# apply can undo a key the proposed env no longer applies rather than leaving its stale
# value in os.environ. Never holds a key the deployment sets.
_loaded_env_keys: set[str] = set()


def _apply_env(proposed: Mapping[str, str], ownership: DeploymentOwnership) -> None:
    """Apply the proposed stored env to ``os.environ``, keeping every value the deployment sets.

    ``ownership`` is resolved from the deployment env the cold-boot bridge captured before its
    apply (:func:`~tai42_skeleton.config.deployment_env.capture_deployment_env`). A proposed key
    ``ownership`` names is not applied: the deployment's value wins, and the ignored names are
    logged at WARNING (names only). The cold-boot bridge applies by this process's own ownership
    and withdraws by the fleet's once it has read the census on the bus the applied env
    configures (:func:`withdraw_fleet_owned_env`); every reload applies by the fleet's. Ownership
    is judged against the settings groups registered at the apply; a group registered later
    withdraws the applied keys it makes owned (:func:`_withdraw_newly_owned_env`). Removed-key
    reconciliation: a key a prior apply loaded that the proposed env no longer applies returns to
    its deployment value when the deployment env carried one, and is dropped otherwise. Unrelated process env (PATH, the
    launcher's boot identity) is untouched. A proposed env carrying an X-band key (deployment / boot
    identity, which no writer stores) is refused before anything is applied
    (:func:`~tai42_skeleton.config.boundary.refuse_x_band`, a ``ValueError`` naming the keys), and
    so is one that sets one name under two spellings
    (:func:`~tai42_skeleton.config.boundary.refuse_colliding_spellings`).
    """
    global _loaded_env_keys
    refuse_x_band(proposed)
    refuse_colliding_spellings(proposed, source="the stored env")
    ignored = ownership.owned(proposed)
    if ignored:
        logger.warning("stored env keys ignored, the deployment sets them and its value wins: %s", ", ".join(ignored))
    applied = {key: value for key, value in proposed.items() if not ownership.owns(key)}
    for key in _loaded_env_keys - set(applied):
        deployed = deployment_env_value(key)
        if deployed is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = deployed
    os.environ.update(applied)
    _loaded_env_keys = set(applied)


def _withdraw_owned_applied_keys(ownership: DeploymentOwnership) -> list[str]:
    """Withdraw every applied stored key ``ownership`` names; returns the withdrawn names, sorted.

    Each such key returns to its deployment value when the deployment env carried one and is
    dropped otherwise, and it leaves the applied set, so a later reload's reconciliation does
    not touch it again. An apply never leaves a key its ownership names applied; one is found here
    only once the cold-boot bridge's census read names a key its own-view apply applied,
    an env reader described after the apply, or a worker that joined the bus since, made it
    owned.
    """
    global _loaded_env_keys
    if not _loaded_env_keys:
        return []
    withdrawn = ownership.owned(_loaded_env_keys)
    for key in withdrawn:
        deployed = deployment_env_value(key)
        if deployed is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = deployed
    _loaded_env_keys = _loaded_env_keys - set(withdrawn)
    return withdrawn


def _withdraw_newly_owned_env(reader: EnvReader) -> None:
    """Withdraw the applied stored keys a newly described env reader makes deployment-owned.

    A reader described after a stored-env apply (a plugin's settings group, imported once the
    boot env bridge ran, or a declared reader) can name a field under several env names. When the
    deployment sets one of them, the field's other names are the deployment's too, and a stored
    copy the apply already put under one of those names must not count: it is withdrawn
    (:func:`_withdraw_owned_applied_keys`), the names are logged at WARNING (names only), and the
    settings caches are cleared so no value read under the withdrawn copy stays cached. Called at
    every description, before a registered group's respawn-class values are bound, and after the
    deployment env module described this process's readers again (it subscribes first, as this
    module imports it). The description reads no census: the other workers' names and readers
    are those the boot env bridge's census read or a later successful read recorded
    (:func:`~tai42_skeleton.config.deployment_env.known_fleet_env`). A group whose excluded fields
    make an applied stored key X band refuses the registration
    (:func:`~tai42_skeleton.config.boundary.refuse_x_band`), as the apply refuses one.
    """
    if not _loaded_env_keys:
        # Nothing applied yet (the reader was described before the boot env bridge ran).
        return
    refuse_x_band(_loaded_env_keys)
    withdrawn = _withdraw_owned_applied_keys(DeploymentOwnership(*known_fleet_env()))
    if not withdrawn:
        return
    logger.warning(
        "stored env keys ignored, env reader %s reads them for a field the deployment sets and its value wins: %s",
        reader.qualname,
        ", ".join(withdrawn),
    )
    reset_all_settings()


on_env_reader_described(_withdraw_newly_owned_env)


def withdraw_fleet_owned_env(ownership: DeploymentOwnership) -> list[str]:
    """Withdraw the applied stored keys the fleet's deployment owns; returns the withdrawn names, sorted.

    Run by the cold-boot bridge once it has read the census on the bus its applied env
    configures, when this worker joins the bus and when another worker announces its join, with
    the ownership just read from the census: a key another worker's deployment sets stops
    counting here too. Each withdrawn key returns to this process's deployment value or is dropped
    (:func:`_withdraw_owned_applied_keys`), the names are logged at WARNING (names only), and the
    settings caches are cleared so the next settings read resolves without them. The cold-boot
    bridge runs before anything is bound or built on the applied env; a join or a join
    announcement rebuilds what runs on a withdrawn value
    (:meth:`~tai42_skeleton.app.lifecycle.bus_subscription.BusSubscriptionMixin._converge_after_withdraw`).
    """
    withdrawn = _withdraw_owned_applied_keys(ownership)
    if withdrawn:
        logger.warning(
            "stored env keys ignored, a worker's deployment sets them and its value wins: %s", ", ".join(withdrawn)
        )
        reset_all_settings()
    return withdrawn


def apply_env_and_reset_settings(proposed: Mapping[str, str], ownership: DeploymentOwnership) -> None:
    """Apply ``proposed`` to ``os.environ`` (removed-key reconciled) and clear the settings accessor caches.

    So the next settings read resolves under it. The apply-then-reset pair the
    epoch build and the cold-boot env bridge both cross — one authority, so boot
    and reload resolve settings identically: a key the deployment sets keeps the
    deployment's value on both, and every other stored key is applied over the process env.
    """
    _apply_env(proposed, ownership)
    reset_all_settings()


def applied_env_keys() -> frozenset[str]:
    """The names the last env apply set in ``os.environ``.

    The stored env the boot bridge applied, or the proposed env an epoch build applied (a
    failed build restores the previous set).
    """
    return frozenset(_loaded_env_keys)


def restore_env(snapshot: Mapping[str, str], loaded: set[str]) -> None:
    """Restore ``os.environ`` to ``snapshot`` and the applied set to ``loaded`` when a build is discarded.

    The discard-only counterpart of ``_apply_env`` (never a with-block, which would restore on
    a swapped build too and defeat the stays-live contract). The restore is exact except for a
    restored stored key that a settings group the discarded build registered made
    deployment-owned: it is withdrawn (:func:`_withdraw_owned_applied_keys`) and named at WARNING.
    """
    global _loaded_env_keys
    os.environ.clear()
    os.environ.update(snapshot)
    _loaded_env_keys = set(loaded)
    withdrawn = _withdraw_owned_applied_keys(DeploymentOwnership(*known_fleet_env()))
    if withdrawn:
        logger.warning(
            "stored env keys ignored on the restored env, a settings group the discarded build "
            "registered reads them for a field the deployment sets and its value wins: %s",
            ", ".join(withdrawn),
        )


def clear_applied_env() -> None:
    """Forget the applied set, as a worker lifespan's teardown leaves the process for the next context."""
    global _loaded_env_keys
    _loaded_env_keys = set()
