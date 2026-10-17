"""The cycles of references a JSON Schema instance check would walk forever."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, NamedTuple

from jsonschema import SchemaError
from jsonschema.protocols import Validator

from tai42_kit.utils.data.json_schema_util.plain_json import _Place, _steps

# A subschema as an instance check evaluates it: by identity, with the class evaluating it, and
# whether a ``$ref`` in it hides its siblings. In one resource each reference resolves alike however
# the instance check reaches it, so nothing else decides what the subschema evaluates.
_NodeKey = tuple[int, type[Validator], bool]


class _Edge(NamedTuple):
    # A subschema evaluated against the same instance as the one holding it.
    target: _NodeKey
    # The reference keyword and value it is reached through, ``None`` for an applicator.
    keyword: str | None
    ref: Any
    # The place of the subschema holding the reference.
    place: _Place


def _refuse_in_place_cycles(edges: dict[_NodeKey, list[_Edge]]) -> None:
    # Depth-first from every subschema over the edges between subschemas evaluated against the
    # same instance; an edge back to a subschema still being walked closes a cycle, refused at the
    # last reference on it (every such cycle runs through one: a schema holding itself is refused
    # before this walk). Each subschema is walked once, so the pass is linear in the edges.
    finished: set[_NodeKey] = set()
    for start in edges:
        if start in finished:
            continue
        walking: dict[_NodeKey, int] = {start: 0}
        path: list[_Edge] = []
        pending: list[tuple[_NodeKey, Iterator[_Edge]]] = [(start, iter(edges[start]))]
        while pending:
            key, remaining = pending[-1]
            edge = next(remaining, None)
            if edge is None:
                pending.pop()
                del walking[key]
                finished.add(key)
                if path:
                    path.pop()
                continue
            if edge.target in finished:
                continue
            if edge.target in walking:
                cycle = [*path[walking[edge.target] :], edge]
                closing = next(step for step in reversed(cycle) if step.keyword is not None)
                message = (
                    f"{closing.keyword} {closing.ref!r} forms a cycle of references that never descends "
                    "into the instance, so checking an instance that follows it would never end"
                )
                raise SchemaError(message, path=[*_steps(closing.place), str(closing.keyword)])
            walking[edge.target] = len(path) + 1
            path.append(edge)
            pending.append((edge.target, iter(edges[edge.target])))
