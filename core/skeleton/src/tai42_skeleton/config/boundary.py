"""The shared env-write boundary validator — three refusals every env/manifest
writer crosses at the :class:`~tai42_skeleton.config.service.ConfigService`
validation layer.

X-band refusal
    An env write may never carry a deployment / boot-identity key: the registry's
    ``excluded`` fields together with a central bare-read list. These keys are written by the
    launcher / deployment substrate and have no reload path, so a profile- or
    editor-carried value is at best an inert no-op and at worst spoofs shape
    detection (a self-exit on an unsupervised host) or relocates the readiness
    sentinel. The refusal reads the WRITER'S PAYLOAD keys, NEVER the post-carry
    effective env — a profile apply legitimately CARRIES the whole X band across
    ``replace_env``, so refusing the effective env would reject the applier's own
    carry.

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
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from tai42_kit.settings import registered_settings
from tai42_kit.utils.data.env_markers import scan_env_marker_refs

from tai42_skeleton.config.recycle_policy import X_CLASSIFIED_DEPLOYMENT_BARE_READS

# Boot-identity + manifest-path bare reads that NO settings class registers as an
# ``excluded`` field (SETTINGS_INVENTORY §K). Each is a real env var the launcher /
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
        "PROMETHEUS_RUNTIME",
    }
)


def excluded_env_var_names() -> frozenset[str]:
    """Half (a): every registered field whose resolved ``reload_class`` is
    ``excluded`` and that carries a non-empty ``env_var``.

    Sourced from :func:`~tai42_kit.settings.registered_settings`, so it reflects
    only the settings classes IMPORTED into the process — a caller that needs the
    full set (e.g. the K8s provider's ``TAI_K8S_*`` group) must boot / import the
    settings modules first."""
    return frozenset(
        field.env_var
        for info in registered_settings()
        for field in info.fields
        if field.reload_class == "excluded" and field.env_var
    )


def registered_env_var_names() -> frozenset[str]:
    """Every registered field's non-empty ``env_var``, across ALL reload classes.

    Reflects only IMPORTED settings classes. Used to keep a GENERATED secret key from
    SHADOWING any registered settings env var (C9c) — not just the X band, which covers
    only the ``excluded`` subset."""
    return frozenset(field.env_var for info in registered_settings() for field in info.fields if field.env_var)


def reload_class_by_env_var() -> dict[str, str]:
    """Map each registered field's ``env_var`` to its resolved ``reload_class``.

    Reflects only IMPORTED settings classes (the running kit + skeleton + manifest
    plugin modules), so a caller diffing env keys against it (the profile diff / apply
    recycle classification) sees exactly the live registry's classes. A key absent from
    the map has no registered field and is treated as the ``hot`` default by callers."""
    mapping: dict[str, str] = {}
    for info in registered_settings():
        for field in info.fields:
            if field.env_var:
                mapping[field.env_var] = field.reload_class
    return mapping


def x_band_env_keys() -> frozenset[str]:
    """The full X band: the registry's ``excluded`` fields, the deployment
    bare-reads, and the boot-identity extras, unioned. The single source of truth
    for X refusal."""
    return excluded_env_var_names() | X_CLASSIFIED_DEPLOYMENT_BARE_READS | X_BAND_EXTRA


def refuse_x_band(written_keys: Iterable[str]) -> None:
    """Refuse an env write whose PAYLOAD carries any X-band key.

    ``written_keys`` is the writer's declared payload — the keys it intends to
    write — NEVER the post-carry effective env. Raises :class:`ValueError` naming
    every offender (names only) — the operations layer maps it to a 400."""
    offenders = sorted(set(written_keys) & x_band_env_keys())
    if offenders:
        raise ValueError(
            "Refusing an env write carrying deployment / boot-identity (X-band) keys "
            f"no profile or editor may set: {', '.join(offenders)}."
        )


def key_material_env_keys() -> frozenset[str]:
    """Every registered field flagged ``key_material`` (the kit ``KeyMaterial`` type)
    that carries a non-empty ``env_var``. Reflects only IMPORTED settings classes, so a
    key-material field only enters the set once its settings module is loaded."""
    return frozenset(
        field.env_var for info in registered_settings() for field in info.fields if field.key_material and field.env_var
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
    when it is carried unchanged.

    ``current`` is the current stored env — the REAL values ``GET /api/config/env`` returns
    and the editor reads back — so a round-trip that re-sends the same value compares equal
    and is allowed. (A client that sent a MASKED placeholder for a key-material key would look
    "changed" and be refused; but every read surface here returns real values, so the
    read-modify-write contract these editors use holds.) Raises :class:`ValueError` naming
    every offender and the rotation path (names only) — the operations layer maps it to 400."""
    key_material = key_material_env_keys()
    offenders = sorted(key for key, value in written.items() if key in key_material and value != current.get(key))
    if offenders:
        raise ValueError(
            "Refusing an env write that CHANGES key-material keys — rotate these out of band "
            f"through their own key-rotation path, never through a profile: {', '.join(offenders)}."
        )


# Per-database admin (migrator) identity, both-or-neither per NAME. Mirrors the kit db
# registry's env layout (``db/registry.py`` ``TAI_DATABASE_<NAME>_PG_ADMIN_USER`` /
# ``_PG_ADMIN_PASSWORD``): group 1 is the (uppercased) database name, group 2 the half.
_ADMIN_PAIR_RE = re.compile(r"^TAI_DATABASE_(.+)_PG_ADMIN_(USER|PASSWORD)$")


def refuse_incomplete_admin_pair(effective_env: Mapping[str, str]) -> None:
    """Refuse a half-set per-database admin (migrator) identity (README D8/D14).

    ``TAI_DATABASE_<NAME>_PG_ADMIN_USER`` / ``_PG_ADMIN_PASSWORD`` are both-or-neither per
    database name: both set is the admin identity, neither set migrates the runtime identity,
    exactly one set silently pairs one admin field with a runtime one. The kit db registry
    raises :class:`~tai42_kit.db.registry.AdminIdentityIncompleteError` only at migration
    time (``admin_database_settings``), so a profile / env write is caught FIRST at the
    validate step, naming the incomplete pair. Checked against the RESULTING effective env (a
    key present with a non-empty value counts as set — the store never persists empties, so
    this matches what a reload materializes and the registry then loads). Raises
    :class:`ValueError` naming both vars per incomplete database — the ops layer maps it to a
    400."""
    halves: dict[str, set[str]] = {}
    for key, value in effective_env.items():
        match = _ADMIN_PAIR_RE.match(key)
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
    marker value) — the operations layer maps it to a 400."""
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
