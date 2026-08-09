"""The single authority for the ``!ENV ${VAR[:default]}`` marker convention.

A manifest value written ``!ENV ${VAR}`` (or ``!ENV ${VAR:default}``) is a
marker string — :func:`~tai42_kit.utils.data.yaml_util.load_manifest` keeps it
as ``"!ENV <expr>"`` rather than baking in a resolved value, and ``pyaml_env``
materializes it from the process environment at parse time. A ref that carries
NO ``:default`` resolves to the literal ``"N/A"`` when its var is absent
(``raise_if_na=False``), so a bare ``${VAR}`` is a REQUIRED var: absent means a
silent phantom value, not an error.

This module is the one place the marker grammar and the scalar-leaf walk are
defined: a consumer that must scan a parsed config for marker refs — the
env-write boundary's dangling-marker refusal, the manifest readers' expanded-read
guard, the mcp env-refs projection — imports them from here rather than
re-deriving the grammar, so there is a single authority. It REUSES
:data:`~tai42_kit.utils.data.yaml_util._ENV_MARKER_PREFIX` rather than
re-declaring the prefix.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from tai42_kit.utils.data.yaml_util import _ENV_MARKER_PREFIX

# ``pyaml_env``'s marker grammar (``parse_config`` with the default ``:``
# separator): group 1 is the env var name, group 2 an optional ``:default``
# suffix. A ref with no default (group 2 empty) resolves to ``"N/A"`` when the
# var is absent.
ENV_REF = re.compile(r"\$\{([^}{:]+)(:[^}]+)?\}")


@dataclass(frozen=True)
class EnvMarkerRef:
    """One ``${VAR[:default]}`` reference found inside an ``!ENV`` marker leaf.

    ``default`` is ``None`` for a bare ref (no ``:default``) — a REQUIRED var
    that resolves to ``"N/A"`` when absent; otherwise it is the literal default
    text with the leading ``:`` stripped. ``pointer`` is the RFC 6901 json-pointer
    of the scalar leaf the ref was found in.
    """

    var: str
    default: str | None
    pointer: str

    @property
    def required(self) -> bool:
        """True iff the ref carries no ``:default`` — an absent var silently
        resolves to ``"N/A"`` rather than erroring, so the var must be present."""
        return self.default is None


def scalar_leaves(node: Any, pointer: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(json-pointer, value)`` for every string scalar leaf of *node*,
    descending mappings and sequences. RFC 6901 pointers (``~`` → ``~0``, ``/`` →
    ``~1`` in map keys)."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from scalar_leaves(value, f"{pointer}/{_escape(str(key))}")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from scalar_leaves(value, f"{pointer}/{index}")
    elif isinstance(node, str):
        yield pointer, node


def scan_env_marker_refs(config: Any) -> list[EnvMarkerRef]:
    """Walk *config*'s scalar leaves and return every ``${VAR[:default]}`` ref
    carried in an ``!ENV`` marker string, in document order.

    A leaf is a marker iff it begins with the ``!ENV `` prefix; the text past the
    prefix is scanned with :data:`ENV_REF`. A non-marker leaf, or a marker whose
    expression carries no ``${...}`` ref, contributes nothing.
    """
    refs: list[EnvMarkerRef] = []
    for pointer, leaf in scalar_leaves(config):
        if not leaf.startswith(_ENV_MARKER_PREFIX):
            continue
        expression = leaf[len(_ENV_MARKER_PREFIX) :]
        for var, default in ENV_REF.findall(expression):
            refs.append(EnvMarkerRef(var=var, default=default[1:] if default else None, pointer=pointer))
    return refs


def _escape(token: str) -> str:
    """RFC 6901 json-pointer token escaping."""
    return token.replace("~", "~0").replace("/", "~1")


__all__ = [
    "ENV_REF",
    "EnvMarkerRef",
    "scalar_leaves",
    "scan_env_marker_refs",
]
