"""The shared env-write boundary validator — two refusals every env/manifest
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

Dangling ``!ENV`` refusal
    ``pyaml_env`` resolves a marker whose var is absent to the literal ``"N/A"``
    silently (``raise_if_na=False``). A change that drops a key a manifest
    ``!ENV ${VAR}`` marker references (with no ``:default``) would therefore run
    on a phantom value, so it is caught pre-persist and refused, naming the var
    and the manifest json-pointer — names only, never the marker's value.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from tai42_kit.settings import registered_settings

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

# ``pyaml_env``'s own marker grammar (``parse_config`` with the default ``:``
# separator): group 1 is the env var name, group 2 an optional ``:default`` suffix.
# A ref with no default (group 2 empty) resolves to ``"N/A"`` when the var is absent.
_ENV_REF = re.compile(r"\$\{([^}{:]+)(:[^}]+)?\}")

_ENV_MARKER_PREFIX = "!ENV "


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


def refuse_dangling_env_markers(preserved_manifest: Mapping[str, Any], effective_env: Mapping[str, str]) -> None:
    """Refuse a change that leaves a manifest ``!ENV`` marker dangling.

    Walks the PRESERVED document's scalar leaves; a leaf beginning ``"!ENV "`` is a
    marker whose expression is scanned for ``${VAR}`` refs. A ref DANGLES iff it
    carries no ``:default`` AND its var is absent from *effective_env* —
    ``${VAR:default}`` and a bare ``!ENV VAR`` never dangle. Raises
    :class:`ValueError` naming each ``(VAR, json-pointer)`` pair (names only, never
    the marker value) — the operations layer maps it to a 400."""
    dangling: list[str] = []
    for pointer, leaf in _scalar_leaves(preserved_manifest, ""):
        if not leaf.startswith(_ENV_MARKER_PREFIX):
            continue
        expression = leaf[len(_ENV_MARKER_PREFIX) :]
        for var, default in _ENV_REF.findall(expression):
            if not default and var not in effective_env:
                dangling.append(f"{var} (at {pointer})")
    if dangling:
        raise ValueError(
            "Refusing a change that leaves manifest !ENV markers dangling — these "
            'references resolve to no env var and would silently run on "N/A": ' + ", ".join(dangling) + "."
        )


def _scalar_leaves(node: Any, pointer: str) -> Iterator[tuple[str, str]]:
    """Yield ``(json-pointer, value)`` for every string scalar leaf of *node*,
    descending mappings and sequences. RFC 6901 pointers (``~`` → ``~0``, ``/`` →
    ``~1`` in map keys)."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from _scalar_leaves(value, f"{pointer}/{_escape(str(key))}")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _scalar_leaves(value, f"{pointer}/{index}")
    elif isinstance(node, str):
        yield pointer, node


def _escape(token: str) -> str:
    """RFC 6901 json-pointer token escaping."""
    return token.replace("~", "~0").replace("/", "~1")
