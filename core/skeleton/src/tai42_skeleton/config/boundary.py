"""The shared env-write boundary validator — the refusals every env/manifest writer crosses.

The refusals are enforced at the :class:`~tai42_skeleton.config.service.ConfigService` validation layer.

X-band refusal
    An env write may never carry a deployment / boot-identity key: the registry's
    ``excluded`` fields together with a central bare-read list. These keys are written by the
    launcher / deployment substrate and have no reload path, so a profile- or
    editor-carried value is at best an inert no-op and at worst relocates the readiness
    sentinel. The refusal reads the WRITER'S PAYLOAD keys, NEVER the post-carry
    effective env — a profile apply legitimately CARRIES the whole X band across
    ``replace_env``, so refusing the effective env would reject the applier's own
    carry.

Deployment-owned refusal (CHANGE-aware)
    A key the deployment env sets (:mod:`tai42_skeleton.config.deployment_env`) keeps the
    deployment's value at every boot and reload, so a stored value for it never takes effect.
    An env write may not SET such a key to a value other than its stored one; the deployment
    changes it in its own environment and restarts. Removing a stored copy, and carrying one
    unchanged, are allowed.

Key-material refusal (CHANGE-aware)
    An env write may never CHANGE a ``key_material`` field (a KEK, a signing/HMAC key) to a
    new value. These are ``hot``-class — not X-band — so the X refusal misses them, yet
    setting one to a new value through a profile / the env editor would silently invalidate
    every secret the old key secured; rotation runs through its own controlled path. Unlike
    X-band keys, key material CAN be legitimately PRESENT in the editable store (a stack that
    provisions a KEK), so — unlike the X refusal — this one compares the payload's value to
    the current stored value and refuses only a CHANGE: an unchanged carry (a read-modify-
    write round-trip, or a profile snapshotted from the stored env) is allowed.

Dangling ``!ENV`` refusal
    ``pyaml_env`` resolves a marker whose var is absent to the literal ``"N/A"``
    silently (``raise_if_na=False``). A change that drops a key a manifest
    ``!ENV ${VAR}`` marker references (with no ``:default``) would therefore run
    on a phantom value, so it is caught pre-persist and refused, naming the var
    and the manifest json-pointer — names only, never the marker's value.

Unset connector-env refusal
    An ``oauth`` connector in the manifest ``connectors`` list names a
    ``client_id_env`` and a ``client_secret_env`` the OAuth client reads at
    connect time; unlike an ``!ENV`` marker these names live in the descriptor,
    not in a scanned marker, so the dangling-marker scan cannot see them. A change
    that leaves either var absent from the effective env is refused, naming the
    ``(VAR, json-pointer)`` pairs exactly like the marker refusal.

Colliding-spelling refusal
    An env may not set one name under two spellings (``TAI_LOG_LEVEL`` and ``tai_log_level``):
    the settings layer reads names folded, so which value a process reads is not fixed by the
    env. Every writer judges the env it would store, and the stored-env apply judges the env it
    applies.

Unrecyclable-change refusal
    A ``recycle``-class key takes effect only when its worker is respawned, so a change
    to one is carried by a recycle. A change no recycle can carry is refused, naming the
    keys: a bus-reaching key, which reaches the worker bus itself.
    :func:`refuse_unrecyclable_env_change` diffs the stored env against the env the write
    would store, both without the keys the deployment sets, so a key carried unchanged and a
    stored copy of a deployment key, removed or carried, are never refused.

The single-call authority :func:`refuse_unresolved_env` runs BOTH the dangling
``!ENV`` and the unset connector-env refusals — every config-pipeline writer and
the offline lint cross it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from tai42_kit.settings import SettingsFieldRef, fold_env_name, registered_settings, settings_fields_set_by
from tai42_kit.utils.data.env_markers import scan_env_marker_refs

from tai42_skeleton.config.deployment_env import DeploymentOwnership
from tai42_skeleton.config.recycle_policy import (
    X_CLASSIFIED_DEPLOYMENT_BARE_READS,
    _refuse_unrecyclable,
    _replace_diff_keys,
)
from tai42_skeleton.connectors.manifest_env import connector_client_env_refs

if TYPE_CHECKING:
    from tai42_kit.settings import ReloadClass, SettingsFieldInfo

# Boot-identity + manifest-path bare reads that NO settings class in the
# registered-settings inventory (``registered_settings()``) marks as an ``excluded``
# field. Each is a real env var the launcher /
# runtime reads with no reload path, so a profile-carried value is X-refused at the
# boundary. ``TAI_MANIFEST_PATH`` is redundant with the registry half
# (``CoreSettings.manifest_path`` is field-excluded) — kept as belt-and-braces.
X_BAND_EXTRA: frozenset[str] = frozenset(
    {
        "TAI_MANIFEST_PATH",
        "TAI_TRANSPORT",
        "TAI_STATELESS_HTTP",
        "TAI_METRICS_RUN_ID",
        "TAI_RUN_MODE",
        "TAI_SERVE_WORKERS",
        "PROMETHEUS_RUNTIME",
    }
)


# The reload classes from the least to the most restrictive.
_RELOAD_CLASS_RANK: dict[ReloadClass, int] = {"hot": 0, "recycle": 1, "excluded": 2}


class EnvKeyClasses:
    """The classes of env key names, by every registered settings field each name sets.

    An env name sets the fields it is an accepted name of (a nested-group reference's JSON
    name included) and every field the settings layer reads under it: each field of a nested
    group under the group's JSON name, and the field a delimited name names
    (:func:`~tai42_kit.settings.settings_fields_set_by`). Names compare folded
    (:func:`~tai42_kit.settings.fold_env_name`), as the settings layer matches them. Read once
    from the live registry at construction, so it reflects only the settings classes IMPORTED
    into the process: a caller that needs an externally-installed provider's groups imports
    their settings modules first.
    """

    def __init__(self) -> None:
        """Index every registered field by its folded accepted names and by its reference."""
        self._named: dict[str, list[SettingsFieldInfo]] = {}
        self._by_ref: dict[SettingsFieldRef, SettingsFieldInfo] = {}
        for info in registered_settings():
            for field in info.fields:
                self._by_ref[SettingsFieldRef(info.qualname, field.name)] = field
                for name in field.accepted_env_vars:
                    self._named.setdefault(fold_env_name(name), []).append(field)

    def fields_set_by(self, key: str) -> list[SettingsFieldInfo]:
        """The registered fields ``key`` names or sets; empty when it reaches none."""
        named = self._named.get(fold_env_name(key), [])
        return [*named, *(self._by_ref[ref] for ref in settings_fields_set_by(key))]

    def sets_a_field(self, key: str) -> bool:
        """Whether ``key`` names or sets any registered field."""
        return bool(self.fields_set_by(key))

    def reload_class(self, key: str) -> ReloadClass:
        """The most restrictive ``reload_class`` of the fields ``key`` sets, as a change to it reaches them all.

        ``hot`` for a key that sets no registered field.
        """
        classes: list[ReloadClass] = [field.reload_class for field in self.fields_set_by(key)]
        return max(classes, key=_RELOAD_CLASS_RANK.__getitem__) if classes else "hot"

    def x_band(self, key: str) -> bool:
        """Whether ``key`` is X band: it sets an ``excluded`` field, or is a bare read or an extra.

        The deployment bare reads and the boot-identity extras compare folded, as the excluded
        fields' names do.
        """
        if fold_env_name(key) in X_CLASSIFIED_DEPLOYMENT_BARE_READS | X_BAND_EXTRA:
            return True
        return any(field.reload_class == "excluded" for field in self.fields_set_by(key))

    def key_material(self, key: str) -> bool:
        """Whether ``key`` sets a ``key_material`` field (the kit ``KeyMaterial`` type)."""
        return any(field.key_material for field in self.fields_set_by(key))


def counting_diff_keys(
    stored: Mapping[str, str], proposed: Mapping[str, str], ownership: DeploymentOwnership
) -> set[str]:
    """The env key names a whole-env write from ``stored`` to ``proposed`` changes in the env a reload produces.

    Both sides are taken without the keys ``ownership`` names
    (:meth:`~tai42_skeleton.config.deployment_env.DeploymentOwnership.counting`): their stored
    copies are never applied, so adding, removing or changing one changes nothing a reload
    produces. A write that sets an owned key is refused before this diff
    (:func:`refuse_deployment_owned`). Names only.
    """
    return _replace_diff_keys(ownership.counting(stored), ownership.counting(proposed))


def refuse_unrecyclable_env_change(
    stored: Mapping[str, str], proposed: Mapping[str, str], ownership: DeploymentOwnership, *, change: str
) -> tuple[list[str], list[str]]:
    """Refuse an env change no recycle can carry; return its ``(hot, recycle)`` keys.

    ``proposed`` is the WHOLE env the write would store. The diff is the change the reload
    really produces (:func:`counting_diff_keys`): a key the deployment sets is never part of
    it, so removing or carrying a stored copy of one is no change. The diff keys are
    classified by the fields each sets in the live registry (:class:`EnvKeyClasses`): a
    bus-reaching key is refused, a :class:`ValueError` naming ``change`` and the keys.
    Otherwise the changed ``hot``-class keys (a key that sets no registered field counts as
    ``hot``) and the changed ``recycle``-class keys are returned, sorted. Names only.
    """
    diff_keys = counting_diff_keys(stored, proposed, ownership)
    classes = EnvKeyClasses()
    hot = sorted(key for key in diff_keys if classes.reload_class(key) == "hot")
    recycle = sorted(key for key in diff_keys if classes.reload_class(key) == "recycle")
    _refuse_unrecyclable(diff_keys, change=change)
    return hot, recycle


def refuse_x_band(written_keys: Iterable[str]) -> None:
    """Refuse an env write whose PAYLOAD carries any X-band key.

    ``written_keys`` is the writer's declared payload — the keys it intends to
    write — NEVER the post-carry effective env. A key is X band by :meth:`EnvKeyClasses.x_band`.
    Raises :class:`ValueError` naming every offender (names only) — the operations layer maps it
    to a 400.
    """
    classes = EnvKeyClasses()
    offenders = sorted({key for key in written_keys if classes.x_band(key)})
    if offenders:
        raise ValueError(
            "Refusing an env write carrying deployment / boot-identity (X-band) keys "
            f"no profile or editor may set: {', '.join(offenders)}."
        )


def refuse_key_material(written: Mapping[str, str], current: Mapping[str, str]) -> None:
    """Refuse an env write that CHANGES a key-material field to a new value.

    Key material (a KEK, a signing/HMAC key) is rotated through its own controlled path —
    re-encrypt / re-sign what the old key secured — never set to a new value through a profile
    or the env editor, where a careless value silently invalidates every secret the old key
    protected. But merely CARRYING an unchanged key-material value is harmless — and
    unavoidable for a read-modify-write round-trip or a profile snapshotted from the stored
    env — so the refusal is CHANGE-AWARE: a key-material key is refused only when its written
    value DIFFERS from its ``current`` value (a new value, or a newly introduced key), never
    when it is carried unchanged. A key is key material by :meth:`EnvKeyClasses.key_material`.

    ``current`` is the current stored env — the REAL values ``GET /api/config/env`` returns
    and the editor reads back — so a round-trip that re-sends the same value compares equal
    and is allowed. (A client that sent a MASKED placeholder for a key-material key would look
    "changed" and be refused; but every read surface here returns real values, so the
    read-modify-write contract these editors use holds.) Raises :class:`ValueError` naming
    every offender and the rotation path (names only) — the operations layer maps it to 400.
    """
    classes = EnvKeyClasses()
    offenders = sorted(key for key, value in written.items() if classes.key_material(key) and value != current.get(key))
    if offenders:
        raise ValueError(
            "Refusing an env write that CHANGES key-material keys — rotate these out of band "
            f"through their own key-rotation path, never through a profile: {', '.join(offenders)}."
        )


def refuse_colliding_spellings(env: Mapping[str, str], *, source: str) -> None:
    """Refuse an env that sets one env name under more than one spelling.

    The settings layer reads env names folded (:func:`~tai42_kit.settings.fold_env_name`), so of
    two spellings of one name only one is read, and which one depends on the process: two
    workers of one fleet could each run on a different value. ``env`` is the whole env that would
    be stored or applied; an empty value is absent. ``source`` names it in the refusal. Raises
    :class:`ValueError` naming every spelling of each such name (names only) — the operations
    layer maps it to a 400.
    """
    spellings: dict[str, list[str]] = {}
    for key, value in env.items():
        if value != "":
            spellings.setdefault(fold_env_name(key), []).append(key)
    collisions = sorted(sorted(keys) for keys in spellings.values() if len(keys) > 1)
    if collisions:
        named = "; ".join(" and ".join(keys) for keys in collisions)
        raise ValueError(
            f"Refusing {source} that sets one env name under several spellings: {named}. Env names are "
            "read case-insensitively, so keep one spelling of each name."
        )


def refuse_deployment_owned(
    written: Mapping[str, str], current: Mapping[str, str], ownership: DeploymentOwnership
) -> None:
    """Refuse an env write that SETS a key the deployment sets to a value other than its stored one.

    The deployment's value wins over a stored copy at every boot and reload, so such a write
    would never take effect. ``written`` is the writer's payload (an empty value removes its
    key); ``current`` is the current stored env; ``ownership`` is the fleet's
    (:func:`~tai42_skeleton.config.deployment_env.resolve_deployment_ownership`). Removing a stored
    copy and carrying one unchanged are allowed. Raises :class:`ValueError` naming every offender (names only) — the
    operations layer maps it to a 400.
    """
    offenders = ownership.changed_writes(written, current)
    if offenders:
        raise ValueError(
            f"Refusing an env write setting keys set by the deployment: {', '.join(offenders)}. The deployment's "
            "value wins over a stored one, so change them in the deployment's environment and restart."
        )


# Per-database admin (migrator) identity, both-or-neither per NAME. Mirrors the kit db
# registry's env layout (``db/registry.py`` ``TAI_DATABASE_<NAME>_PG_ADMIN_USER`` /
# ``_PG_ADMIN_PASSWORD``): matched on the folded name, as the settings layer reads it; group 1 is
# the (uppercased) database name, group 2 the half.
_ADMIN_PAIR_RE = re.compile(r"^TAI_DATABASE_(.+)_PG_ADMIN_(USER|PASSWORD)$")


def refuse_incomplete_admin_pair(effective_env: Mapping[str, str]) -> None:
    """Refuse a half-set per-database admin (migrator) identity.

    ``TAI_DATABASE_<NAME>_PG_ADMIN_USER`` / ``_PG_ADMIN_PASSWORD`` are both-or-neither per
    database name: both set is the admin identity, neither set migrates the runtime identity,
    exactly one set silently pairs one admin field with a runtime one. The kit db registry
    raises :class:`~tai42_kit.db.registry.AdminIdentityIncompleteError` only at migration
    time (``admin_database_settings``), so a profile / env write is caught FIRST at the
    validate step, naming the incomplete pair. Checked against the RESULTING effective env (a
    key present with a non-empty value counts as set — the store never persists empties, so
    this matches what a reload materializes and the registry then loads). Raises
    :class:`ValueError` naming both vars per incomplete database — the ops layer maps it to a
    400.
    """
    halves: dict[str, set[str]] = {}
    for key, value in effective_env.items():
        match = _ADMIN_PAIR_RE.match(fold_env_name(key))
        if match and value != "":
            halves.setdefault(match.group(1), set()).add(match.group(2))
    incomplete = sorted(name for name, present in halves.items() if len(present) == 1)
    if incomplete:
        pairs = "; ".join(
            f"database {name!r}: set BOTH TAI_DATABASE_{name}_PG_ADMIN_USER and "
            f"TAI_DATABASE_{name}_PG_ADMIN_PASSWORD, or neither"
            for name in incomplete
        )
        raise ValueError(
            "Refusing an env write with a half-set admin (migrator) identity — the admin "
            f"user/password are both-or-neither per database: {pairs}."
        )


def refuse_dangling_env_markers(preserved_manifest: Mapping[str, Any], effective_env: Mapping[str, str]) -> None:
    """Refuse a change that leaves a manifest ``!ENV`` marker dangling.

    Delegates the scan to the shared kit authority
    (:func:`~tai42_kit.utils.data.env_markers.scan_env_marker_refs`): a ref DANGLES iff
    it carries no ``:default`` (``ref.required``) AND its var is absent from
    *effective_env* — ``${VAR:default}`` and a bare ``!ENV VAR`` never dangle. Raises
    :class:`ValueError` naming each ``(VAR, json-pointer)`` pair (names only, never the
    marker value) — the operations layer maps it to a 400.
    """
    dangling = [
        f"{ref.var} (at {ref.pointer})"
        for ref in scan_env_marker_refs(preserved_manifest)
        if ref.required and ref.var not in effective_env
    ]
    if dangling:
        raise ValueError(
            "Refusing a change that leaves manifest !ENV markers dangling — these "
            'references resolve to no env var and would silently run on "N/A": ' + ", ".join(dangling) + "."
        )


def refuse_unset_connector_env(preserved_manifest: Mapping[str, Any], effective_env: Mapping[str, str]) -> None:
    """Refuse a change that leaves an ``oauth`` connector's client-credential env unset.

    Each ``connectors[*]`` entry with ``kind == "oauth"`` names a ``client_id_env`` and
    a ``client_secret_env`` the OAuth client reads STRAIGHT from ``os.environ`` at connect
    time. These names live in the descriptor, not in a scanned ``!ENV`` marker, so
    :func:`refuse_dangling_env_markers` cannot see them. *effective_env* is the FULL
    process environment the connector will see at connect time (never a narrowed profile
    band — a profile that omits a deployment-supplied cred does not remove it from the
    process env), so both must be present in it, else :class:`ValueError` naming each
    ``(VAR, json-pointer)`` pair (names only, never a value) — the operations layer maps
    it to a 400.
    """
    unset = [
        f"{ref.var} (at /connectors/{ref.index}/{ref.field})"
        for ref in connector_client_env_refs(preserved_manifest)
        if ref.var not in effective_env
    ]
    if unset:
        raise ValueError(
            "Refusing a change that leaves an oauth connector's client-credential env unset — "
            "these variables the connector needs at connect time resolve to no env var: " + ", ".join(unset) + "."
        )


def refuse_unresolved_env(
    preserved_manifest: Mapping[str, Any],
    effective_env: Mapping[str, str],
    *,
    connector_env: Mapping[str, str] | None = None,
) -> None:
    """The single env-resolution authority: run BOTH the dangling ``!ENV`` refusal and the unset connector-env refusal.

    Markers resolve against *effective_env* — the modeled post-change band. Connector
    client-credential env resolves against *connector_env* (defaulting to *effective_env*):
    the FULL process environment the OAuth client reads at connect time. A whole-env REPLACE
    (a settings-profile apply) models a NARROWED band for markers — a dropped stored key must
    still dangle — but the connector reads the live ``os.environ`` a profile does not own, so
    the replace path passes the wider *connector_env* to keep a deployment-supplied cred from
    reading as unset.

    Every former :func:`refuse_dangling_env_markers` caller (the config pipeline's four
    validate seams and the offline ``tai manifest validate`` / ``tai config lint`` door)
    crosses this one function, so a manifest write is refused whenever it would leave EITHER
    a marker or an oauth connector's credential env unresolved.
    """
    refuse_dangling_env_markers(preserved_manifest, effective_env)
    refuse_unset_connector_env(preserved_manifest, effective_env if connector_env is None else connector_env)
