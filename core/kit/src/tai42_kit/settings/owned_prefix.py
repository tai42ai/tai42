"""Owned settings prefixes: every env name under an owned prefix must name a registered setting.

The settings layer reads only declared fields, so an env name nothing declares is
otherwise ignored without a word. A settings group that sets
``TaiBaseSettings.env_prefix_owned`` claims its ``env_prefix``: any env name under it
that no registered group accepts is refused, which makes a mistyped, renamed or
removed setting loud.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Final

from tai42_kit.settings.registry import _registered_infos


class UnknownOwnedSettingError(ValueError):
    """An env name under an owned settings prefix names no registered setting."""


# The variable the container runtime sets in every Kubernetes pod.
_KUBERNETES_POD_MARKER: Final = "KUBERNETES_SERVICE_HOST"

# The suffixes Kubernetes appends to a Service's upper-cased name in its injected
# link variables: ``<NAME>_SERVICE_HOST``, ``<NAME>_SERVICE_PORT[_<PORTNAME>]``,
# ``<NAME>_PORT`` and ``<NAME>_PORT_<n>_<PROTO>[_PROTO|_PORT|_ADDR]``.
KUBERNETES_SERVICE_LINK_SUFFIX_RE: Final = re.compile(
    r"^(SERVICE_HOST|SERVICE_PORT(_[A-Z0-9_]+)?|PORT(_[0-9]+_(TCP|UDP|SCTP)(_(PROTO|PORT|ADDR))?)?)$"
)


def owned_env_prefixes() -> frozenset[str]:
    """Every env prefix a registered group declares owned."""
    return frozenset(info.env_prefix.upper() for info in _registered_infos() if info.env_prefix_owned)


def _accepted_env_names() -> frozenset[str]:
    """The upper-cased env names every registered group (any prefix) accepts."""
    return frozenset(name.upper() for info in _registered_infos() for f in info.fields for name in f.accepted_env_vars)


def kubernetes_service_link_names(environ: Mapping[str, str]) -> frozenset[str]:
    """The ``<NAME>`` of every Service whose link family is present in ``environ``.

    Empty unless ``KUBERNETES_SERVICE_HOST`` is in ``environ``. Otherwise each ``<NAME>``
    for which BOTH ``<NAME>_SERVICE_HOST`` and ``<NAME>_PORT`` are in ``environ`` (keys
    compared upper-cased).
    """
    keys = {key.upper() for key in environ}
    if _KUBERNETES_POD_MARKER not in keys:
        return frozenset()
    host_suffix = "_SERVICE_HOST"
    return frozenset(
        key[: -len(host_suffix)]
        for key in keys
        if key.endswith(host_suffix) and len(key) > len(host_suffix) and f"{key[: -len(host_suffix)]}_PORT" in keys
    )


def is_kubernetes_service_link(key: str, names: frozenset[str]) -> bool:
    """True when the upper-cased ``key`` is ``<NAME>_<suffix>`` for a ``<NAME>`` in ``names``.

    ``<suffix>`` must match ``KUBERNETES_SERVICE_LINK_SUFFIX_RE``.
    """
    upper = key.upper()
    return any(
        upper.startswith(f"{name}_") and KUBERNETES_SERVICE_LINK_SUFFIX_RE.match(upper[len(name) + 1 :]) is not None
        for name in names
    )


def unknown_owned_env_keys(
    keys: Iterable[str], *, service_link_env: Mapping[str, str] | None = None
) -> list[tuple[str, str]]:
    """``(key, prefix)`` for each key under an owned prefix that no registered group accepts, sorted by key.

    Comparison is upper-cased; the accepted set is the union of ``accepted_env_vars`` of every
    registered group (any prefix), so an alias declared elsewhere is accepted. A key under
    several owned prefixes is reported with the longest. With ``service_link_env`` (the
    process environment), an unaccepted key for which
    ``is_kubernetes_service_link(key, kubernetes_service_link_names(service_link_env))`` holds
    is not reported: the container runtime injects a Service's link family into every pod of
    its namespace, and a Service named like an owned prefix yields names under it. A lone
    ``<NAME>_PORT`` or ``<NAME>_SERVICE_HOST``, or any key outside a Kubernetes pod, is
    reported like any unknown name.
    """
    prefixes = sorted(owned_env_prefixes(), key=len, reverse=True)
    if not prefixes:
        return []
    accepted = _accepted_env_names()
    link_names = kubernetes_service_link_names(service_link_env) if service_link_env is not None else frozenset()
    found: set[tuple[str, str]] = set()
    for key in keys:
        upper = key.upper()
        prefix = next((p for p in prefixes if upper.startswith(p)), None)
        if prefix is None or upper in accepted:
            continue
        if link_names and is_kubernetes_service_link(upper, link_names):
            continue
        found.add((key, prefix))
    return sorted(found)


def refuse_unknown_owned_env(
    keys: Iterable[str], *, source: str, service_link_env: Mapping[str, str] | None = None
) -> None:
    """Raise ``UnknownOwnedSettingError`` naming every unknown key; no-op when none.

    ``source`` names where the keys were read (it leads the message).
    """
    found = unknown_owned_env_keys(keys, service_link_env=service_link_env)
    if not found:
        return
    names = ", ".join(f"{key} (prefix {prefix})" for key, prefix in found)
    raise UnknownOwnedSettingError(
        f"{source}: unknown setting(s) under an owned env prefix: {names}. An owned prefix accepts only the "
        "env names of its registered settings groups; check the name against the settings reference."
    )
