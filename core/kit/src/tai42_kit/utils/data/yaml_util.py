"""Comment-preserving YAML utilities for manifest loading and writing.

Manifests are hand-authored AND machine-edited, so writes must keep comments,
key ordering, and formatting intact. The helpers here use a ruamel.yaml
round-trip (``rt``) pipeline for exactly that, while keeping ``!ENV`` tags alive
as ``"!ENV <value>"`` marker strings so a resolved value never bakes to disk.
"""

from io import StringIO

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.constructor import RoundTripConstructor
from ruamel.yaml.nodes import ScalarNode
from ruamel.yaml.representer import RoundTripRepresenter

# ---------------------------------------------------------------------------
# !ENV marker convention
# ---------------------------------------------------------------------------

_ENV_TAG = "!ENV"
_ENV_MARKER_PREFIX = "!ENV "


def _construct_env_marker(constructor: RoundTripConstructor, node: ScalarNode) -> str:
    """Turn an ``!ENV`` tagged scalar into a ``"!ENV <value>"`` marker string."""
    value = constructor.construct_scalar(node)
    return f"!ENV {value}"


def _represent_env_marker(representer: RoundTripRepresenter, data: str) -> ScalarNode:
    """Emit a ``"!ENV <value>"`` marker string as a genuine ``!ENV`` YAML tag.

    A string of the form ``"!ENV <value>"`` becomes an ``!ENV``-tagged scalar
    (``key: !ENV <value>``) so it reloads through :func:`load_manifest` as the
    same marker. Every other string uses the default round-trip representation.
    """
    if data.startswith(_ENV_MARKER_PREFIX):
        return representer.represent_scalar(_ENV_TAG, data[len(_ENV_MARKER_PREFIX) :])
    return RoundTripRepresenter.represent_str(representer, data)


class _ManifestConstructor(RoundTripConstructor):
    """Round-trip constructor that reads ``!ENV`` tags as marker strings."""


class _ManifestRepresenter(RoundTripRepresenter):
    """Round-trip representer that writes ``!ENV`` marker strings as tags."""


_ManifestConstructor.add_constructor(_ENV_TAG, _construct_env_marker)
_ManifestRepresenter.add_representer(str, _represent_env_marker)


def _make_yaml() -> YAML:
    """Build a round-trip ``YAML`` engine configured to the manifest style."""
    yaml = YAML(typ="rt")
    yaml.Constructor = _ManifestConstructor
    yaml.Representer = _ManifestRepresenter
    # Two-space block sequences indented under their parent key (``  - item``),
    # matching the hand-authored manifest style so round-trip diffs stay minimal.
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.preserve_quotes = True
    return yaml


_yaml = _make_yaml()


# ---------------------------------------------------------------------------
# Load / dump
# ---------------------------------------------------------------------------


def load_manifest(text: str) -> CommentedMap:
    """Parse manifest YAML into a round-trip document.

    Comments, key ordering, and formatting are preserved for a later round trip,
    and every ``!ENV <expr>`` node becomes a ``"!ENV <expr>"`` marker string.

    Args:
        text: The manifest YAML source.

    Returns:
        The round-trip mapping. An empty document yields an empty mapping.

    Raises:
        TypeError: If the top-level document is not a mapping.
    """
    data = _yaml.load(text)
    if data is None:
        return CommentedMap()
    if not isinstance(data, CommentedMap):
        raise TypeError(f"Manifest must be a mapping, got {type(data).__name__}.")
    return data


def dump_manifest(document: CommentedMap) -> str:
    """Serialize a round-trip manifest document back to a YAML string.

    Comments, ordering, and formatting are preserved, and ``"!ENV <expr>"`` marker
    strings are emitted as genuine ``!ENV`` tags (the inverse of
    :func:`load_manifest`).
    """
    stream = StringIO()
    _yaml.dump(document, stream)
    return stream.getvalue()


# ---------------------------------------------------------------------------
# Merge + dump helper
# ---------------------------------------------------------------------------


def merge_and_dump_manifest(defaults: dict, current: CommentedMap, manifest: dict) -> str:
    """Merge into the round-trip *current* document in place and dump it.

    ``defaults`` backfills only keys missing from *current*; ``manifest`` values
    win. The resulting top-level mapping is value-identical to the shallow merge
    ``{**defaults, **current, **manifest}``, but the updates are applied in place
    on *current* so untouched keys keep their comments and ordering.

    Args:
        defaults: Base/template manifest values (backfill missing keys only).
        current: The round-trip document to update in place.
        manifest: New values to apply on top (these win).

    Returns:
        The dumped YAML string with ``!ENV`` tags preserved.
    """
    for key, value in defaults.items():
        if key not in current:
            current[key] = value
    for key, value in manifest.items():
        current[key] = value
    return dump_manifest(current)
