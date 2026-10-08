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
re-deriving the grammar, so there is a single authority. The marker prefix is
defined here and the YAML loader imports it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

# The prefix that makes a string scalar an ``!ENV`` marker: the tag and exactly one space.
ENV_MARKER_PREFIX: Final = "!ENV "

# ``pyaml_env``'s marker grammar (``parse_config`` with the default ``:``
# separator): group 1 is the env var name, group 2 an optional ``:default``
# suffix. A ref with no default (group 2 empty) resolves to ``"N/A"`` when the
# var is absent.
ENV_REF = re.compile(r"\$\{([^}{:]+)(:[^}]+)?\}")


@dataclass(frozen=True)
class EnvRef:
    """The one ``${VAR[:default]}`` reference of a single-reference marker; ``default`` is ``None`` when bare."""

    var: str
    default: str | None


def is_env_marker(leaf: object) -> bool:
    """True iff ``leaf`` is a string beginning with :data:`ENV_MARKER_PREFIX`."""
    return isinstance(leaf, str) and leaf.startswith(ENV_MARKER_PREFIX)


def format_env_marker(var: str, default: str | None = None) -> str:
    """The marker ``!ENV ${var}``, or ``!ENV ${var:default}`` when ``default`` is given.

    Raises ``ValueError`` when :func:`parse_env_marker` would not read the marker back as
    ``(var, default)``: a name is non-empty and carries no ``{``, ``}`` or ``:``; a default is
    non-empty and carries no ``}``.
    """
    suffix = "" if default is None else f":{default}"
    marker = f"{ENV_MARKER_PREFIX}${{{var}{suffix}}}"
    if parse_env_marker(marker) != EnvRef(var=var, default=default):
        raise ValueError(
            f"var {var!r} with default {default!r} cannot be written as an !ENV marker: a name is non-empty "
            "without '{', '}' or ':', a default is non-empty without '}'"
        )
    return marker


def parse_env_marker(leaf: str) -> EnvRef | None:
    """The reference of ``leaf`` iff it is exactly the prefix plus ONE ``${VAR[:default]}``, else ``None``."""
    if not leaf.startswith(ENV_MARKER_PREFIX):
        return None
    match = ENV_REF.fullmatch(leaf[len(ENV_MARKER_PREFIX) :])
    if match is None:
        return None
    default = match.group(2)
    return EnvRef(var=match.group(1), default=default[1:] if default is not None else None)


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
        """True iff the ref carries no ``:default``.

        An absent var silently resolves to ``"N/A"`` rather than erroring, so the var must be
        present.
        """
        return self.default is None


def scalar_leaves(node: Any, pointer: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(json-pointer, value)`` for every string scalar leaf of *node*.

    Descends mappings and sequences. Pointers are RFC 6901 (``~`` → ``~0``, ``/`` → ``~1`` in
    map keys).
    """
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from scalar_leaves(value, f"{pointer}/{escape_json_pointer_token(str(key))}")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from scalar_leaves(value, f"{pointer}/{index}")
    elif isinstance(node, str):
        yield pointer, node


def scan_env_marker_refs(config: Any) -> list[EnvMarkerRef]:
    """Walk *config*'s scalar leaves and return every ``${VAR[:default]}`` ref, in document order.

    Each ref is carried in an ``!ENV`` marker string.

    A leaf is a marker iff it begins with the ``!ENV `` prefix; the text past the
    prefix is scanned with :data:`ENV_REF`. A non-marker leaf, or a marker whose
    expression carries no ``${...}`` ref, contributes nothing.
    """
    refs: list[EnvMarkerRef] = []
    for pointer, leaf in scalar_leaves(config):
        if not leaf.startswith(ENV_MARKER_PREFIX):
            continue
        expression = leaf[len(ENV_MARKER_PREFIX) :]
        for var, default in ENV_REF.findall(expression):
            refs.append(EnvMarkerRef(var=var, default=default[1:] if default else None, pointer=pointer))
    return refs


def escape_json_pointer_token(token: str) -> str:
    """RFC 6901 json-pointer token escaping (``~`` → ``~0``, ``/`` → ``~1``)."""
    return token.replace("~", "~0").replace("/", "~1")


__all__ = [
    "ENV_MARKER_PREFIX",
    "ENV_REF",
    "EnvMarkerRef",
    "EnvRef",
    "escape_json_pointer_token",
    "format_env_marker",
    "is_env_marker",
    "parse_env_marker",
    "scalar_leaves",
    "scan_env_marker_refs",
]
