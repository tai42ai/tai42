"""Comment-preserving YAML utilities for manifest loading and writing.

Manifests are hand-authored AND machine-edited, so writes must keep comments,
key ordering, and formatting intact. The helpers here use a ruamel.yaml
round-trip (``rt``) pipeline for exactly that, while keeping ``!ENV`` tags alive
as ``"!ENV <value>"`` marker strings so a resolved value never bakes to disk.
"""

from io import StringIO

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedBase, CommentedMap, merge_attrib
from ruamel.yaml.constructor import RoundTripConstructor
from ruamel.yaml.nodes import ScalarNode
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.tokens import CommentToken

from tai42_kit.utils.data.env_markers import ENV_MARKER_PREFIX

# ---------------------------------------------------------------------------
# !ENV marker convention
# ---------------------------------------------------------------------------

_ENV_TAG = "!ENV"


def _construct_env_marker(constructor: RoundTripConstructor, node: ScalarNode) -> str:
    """Turn an ``!ENV`` tagged scalar into a ``"!ENV <value>"`` marker string."""
    value = constructor.construct_scalar(node)
    return f"{ENV_MARKER_PREFIX}{value}"


def _represent_env_marker(representer: RoundTripRepresenter, data: str) -> ScalarNode:
    """Emit a ``"!ENV <value>"`` marker string as a genuine ``!ENV`` YAML tag.

    A string of the form ``"!ENV <value>"`` becomes an ``!ENV``-tagged scalar
    (``key: !ENV <value>``) so it reloads through :func:`load_manifest` as the
    same marker. Every other string uses the default round-trip representation.
    """
    if data.startswith(ENV_MARKER_PREFIX):
        return representer.represent_scalar(_ENV_TAG, data[len(ENV_MARKER_PREFIX) :])
    return RoundTripRepresenter.represent_str(representer, data)


class _ManifestConstructor(RoundTripConstructor):
    """Round-trip constructor that reads ``!ENV`` tags as marker strings."""


class _ManifestRepresenter(RoundTripRepresenter):
    """Round-trip representer that writes ``!ENV`` marker strings as tags."""


_ManifestConstructor.add_constructor(_ENV_TAG, _construct_env_marker)
_ManifestRepresenter.add_representer(str, _represent_env_marker)


def _make_yaml() -> YAML:
    """Build a round-trip ``YAML`` engine configured to the manifest style.

    An engine keeps its reader, scanner, parser and emitter state between the calls it
    serves, so every load and dump builds its own: concurrent threads never share one.
    """
    yaml = YAML(typ="rt")
    yaml.Constructor = _ManifestConstructor
    yaml.Representer = _ManifestRepresenter
    # Two-space block sequences indented under their parent key (``  - item``),
    # matching the hand-authored manifest style so round-trip diffs stay minimal.
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.preserve_quotes = True
    return yaml


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
    data = _make_yaml().load(text)
    if data is None:
        return CommentedMap()
    if not isinstance(data, CommentedMap):
        raise TypeError(f"Manifest must be a mapping, got {type(data).__name__}.")
    return data


def _comment_tokens(slot: object) -> list[CommentToken]:
    """The comment tokens held in one comment slot, however deeply its lists nest."""
    if isinstance(slot, CommentToken):
        return [slot]
    if isinstance(slot, list):
        return [token for item in slot for token in _comment_tokens(item)]
    return []


def _reset_written_marks(document: CommentedMap) -> None:
    """Clear the written mark on every comment token in *document*.

    The emitter skips a comment token marked as written, and the mark stays on the token
    after the dump. Before it emits, the representer clears the marks it passes on to
    the emitter, but not the comment a mapping key holds for its value (a blank line
    between a key and its block sequence). When that comment and the sequence's own one
    are separate tokens, a later dump leaves the key's comment out and puts the sequence
    on the key's line as invalid YAML. Clearing every mark first makes each dump write
    every comment once.

    The walk descends through plain dicts and lists too (a caller may wrap round-trip
    nodes in them) and through the source mappings a ``<<`` merge key reads from, which
    a deep copy holds apart from the rest of the document.
    """
    seen: set[int] = set()
    pending: list[object] = [document]
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, CommentedBase):
            comments = node.ca
            for token in _comment_tokens([comments.comment, list(comments.items.values()), comments.end]):
                token.reset()
        if isinstance(node, dict):
            pending.extend(node.values())
            pending.extend(getattr(node, merge_attrib, []))
        elif isinstance(node, list):
            pending.extend(node)


def dump_manifest(document: CommentedMap) -> str:
    """Serialize a round-trip manifest document back to a YAML string.

    Comments, ordering, and formatting are preserved, and ``"!ENV <expr>"`` marker
    strings are emitted as genuine ``!ENV`` tags (the inverse of
    :func:`load_manifest`). The same document dumps to the same text every time.
    """
    _reset_written_marks(document)
    stream = StringIO()
    _make_yaml().dump(document, stream)
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
