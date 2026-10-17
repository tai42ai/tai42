"""The anchor names a reference into a reference registry binds dynamically, read from the registry.

A ``$dynamicRef`` naming a ``$dynamicAnchor``, and a ``$ref`` naming one too (``referencing``
resolves either through the anchor, which walks the dynamic scope), resolves to the outermost
resource in the dynamic scope declaring a ``$dynamicAnchor`` of that name. A schema whose reference
reaches a subschema of a registry resource can so be re-entered at a ``$dynamicAnchor`` of its own
carrying a name that subschema, or any subschema reachable from it by reference, binds this way.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any, NamedTuple
from urllib.parse import unquote, urldefrag, urljoin

from referencing import Registry, Specification
from referencing.jsonschema import DynamicAnchor

_REFERENCE_KEYWORDS = ("$ref", "$dynamicRef")
_ID_KEYWORDS = ("$id", "id")


def names_a_dynamic_anchor(registry: Registry[Any], uri: str, fragment: str) -> bool:
    """Whether ``fragment`` of a reference into ``registry``'s resource ``uri`` names a ``$dynamicAnchor`` there.

    A fragment that is empty or a ``/…`` pointer names no anchor. An anchor name the resource does not
    declare raises ``referencing``'s ``NoSuchAnchor``.
    """
    if not fragment or fragment.startswith("/"):
        return False
    return isinstance(registry.anchor(uri, fragment).value, DynamicAnchor)


class DynamicBindings:
    """Per subschema of a registry's resources, the anchor names a reference reaching it binds dynamically."""

    def __init__(self, registry: Registry[Any]) -> None:
        """Read every resource of ``registry`` whole.

        A reference in any of them leading outside the registry raises ``LookupError`` here, since no
        lookup through the registry could follow it.
        """
        self._registry = registry
        self._bound: dict[tuple[str, int], frozenset[str]] = {}
        for uri in registry:
            self.bound(uri, "")

    def bound(self, uri: str, fragment: str) -> frozenset[str]:
        """Every anchor name the subschema ``fragment`` names in resource ``uri`` binds dynamically.

        The subschema is the one a reference ``<uri>#<fragment>`` resolves to: the resource itself for an
        empty fragment, the value a ``/…`` pointer names, or the subschema an anchor marks. It binds a
        name when a ``$ref``/``$dynamicRef`` anywhere inside it, or inside any subschema those
        references reach in turn, names a ``$dynamicAnchor`` of that name. A value that is no schema
        (the meta-schema's ``$vocabulary``) is read alike, so it binds only what references inside
        it name.
        """
        reached = self._reached(uri, fragment)
        key = (uri, id(reached.contents))
        if key not in self._bound:
            self._bound[key] = self._closure(uri, reached)
        return self._bound[key]

    def _reached(self, uri: str, fragment: str) -> _Reached:
        # The subschema ``<uri>#<fragment>`` resolves to, read as ``referencing`` reads it: a pointer
        # percent-decoded with ``~1`` and ``~0`` unescaped, and each object it passes moving the base
        # URI by its identifier and the specification by its ``$schema``.
        resource = self._registry[uri]
        here = _Reached(uri, Specification.detect(resource.contents), resource.contents)
        if not fragment.startswith("/"):
            contents = self._registry.resolver(uri).lookup(f"#{fragment}").contents
            return here._replace(contents=contents)
        for segment in unquote(fragment[1:]).split("/"):
            here = here.entered()
            node = here.contents
            if isinstance(node, Mapping):
                here = here._replace(contents=node[segment.replace("~1", "/").replace("~0", "~")])
            elif isinstance(node, Sequence) and not isinstance(node, str):
                here = here._replace(contents=node[int(segment)])
            else:
                raise TypeError(f"#{fragment} in {uri} passes {node!r}, which holds no {segment!r}")
        return here

    def _closure(self, uri: str, start: _Reached) -> frozenset[str]:
        # The names bound from ``start`` (in resource ``uri``) and from each subschema its references
        # reach in turn.
        names: set[str] = set()
        seen = {(uri, id(start.contents))}
        pending = [(uri, start)]
        while pending:
            current, reached = pending.pop()
            for keyword, ref, resolved in reached.references():
                target, fragment = urldefrag(resolved)
                if target not in self._registry:
                    raise LookupError(
                        f"{keyword} {ref!r} in {current} leads to {target!r}, which the registry does not hold"
                    )
                if names_a_dynamic_anchor(self._registry, target, fragment):
                    names.add(fragment)
                following = self._reached(target, fragment)
                if (target, id(following.contents)) not in seen:
                    seen.add((target, id(following.contents)))
                    pending.append((target, following))
        return frozenset(names)


class _Reached(NamedTuple):
    # A value inside a registry resource: the base URI references in it resolve against, the
    # specification reading it, and the value itself.
    base: str
    specification: Specification[Any]
    contents: Any

    def entered(self) -> _Reached:
        # This value as read inside itself: an object moves the base URI by the identifier it
        # carries and the specification by the ``$schema`` it names. A value under either keyword
        # that is no string is no identifier or dialect but a subschema (the meta-schemas describe
        # each keyword under ``properties``), so it moves neither.
        if not isinstance(self.contents, Mapping):
            return self
        contents = self.contents
        specification = self.specification
        if isinstance(contents.get("$schema"), str):
            specification = specification.detect(contents)
        # What a specification reads an identifier from: ``$id`` or ``id``, either ignored beside a
        # ``$ref`` in the drafts before 2019-09.
        naming = {key: contents[key] for key in _ID_KEYWORDS if isinstance(contents.get(key), str)}
        if "$ref" in contents:
            naming["$ref"] = contents["$ref"]
        identifier = specification.id_of(naming)
        base = self.base if identifier is None else urljoin(self.base, identifier)
        return _Reached(base, specification, contents)

    def references(self) -> Iterator[tuple[str, str, str]]:
        # Every reference anywhere inside this value, with the absolute URI it resolves to.
        pending: list[_Reached] = [self]
        while pending:
            here = pending.pop().entered()
            node = here.contents
            if isinstance(node, Mapping):
                for keyword in _REFERENCE_KEYWORDS:
                    ref = node.get(keyword)
                    if isinstance(ref, str):
                        yield keyword, ref, urljoin(here.base, ref)
                values = node.values()
            elif isinstance(node, Sequence) and not isinstance(node, str):
                values = node
            else:
                continue
            pending.extend(here._replace(contents=value) for value in values)
