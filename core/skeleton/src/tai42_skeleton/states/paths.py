"""The apply/fold engine for a state record document — the ONE ``apply`` used by
BOTH the persisted store and the in-run engine view, so the two can never drift.

The op/path *vocabulary* it applies — the op-name constants, the resource caps, the
loud validators (:func:`validate_op`/:func:`validate_path`/:func:`validate_guard`)
and the pure read helpers (:func:`value_at_path`, :func:`json_equal`,
:func:`keyed_op_match_counts`) — lives in the dependency-free
:mod:`tai42_contract.states.ops`; this module imports it (and re-exports it, so the
store and service reach the whole grammar through one import) and adds the mutating
apply here. The op catalog is documented on that contract module.

``apply_ops`` is pure: the input document is never mutated (copy-on-write along the
touched path only, so untouched subtrees are shared, not copied). All structural
errors (an index past the end, a key segment into a list, a keyed op over a non-list,
…) raise :class:`~tai42_contract.states.errors.InvalidPathError` LOUDLY.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.states.errors import InvalidPathError
from tai42_contract.states.ops import (
    APPEND,
    KEYED_OPS,
    MAX_OPS_PER_BATCH,
    MAX_PATH_SEGMENTS,
    MAX_REMOVE_KEYS,
    OPS,
    _item_matches,
    json_equal,
    keyed_op_match_counts,
    validate_guard,
    validate_op,
    validate_path,
    value_at_path,
)

# The op vocabulary (``APPEND``…``value_at_path``) is re-exported from the contract so
# a store/service caller reaches the whole grammar plus the apply engine
# (``apply_op``/``apply_ops``/``guard_passes``/``path_str``) through this one module.
__all__ = [
    "APPEND",
    "KEYED_OPS",
    "MAX_OPS_PER_BATCH",
    "MAX_PATH_SEGMENTS",
    "MAX_REMOVE_KEYS",
    "OPS",
    "apply_op",
    "apply_ops",
    "guard_passes",
    "json_equal",
    "keyed_op_match_counts",
    "path_str",
    "validate_guard",
    "validate_op",
    "validate_path",
    "value_at_path",
]


def guard_passes(doc: dict[str, Any], guard: dict[str, Any]) -> bool:
    """Whether ``doc`` satisfies a validated compare-and-set ``guard`` — the value at
    ``guard['path']`` (missing ⇒ null) JSON-equals ``guard['expected']``."""
    return json_equal(value_at_path(doc, guard["path"]), guard["expected"])


class _Absent:
    """Sentinel: the addressed location does not exist in the document."""


_ABSENT = _Absent()


def path_str(path: list[str | int]) -> str:
    """The human-readable ``/a/0/b`` form of a path — error messages and the
    path-overlap refusal both name paths this way."""
    return "/" + "/".join(str(s) for s in path)


# Internal alias for the apply walk.
_path_str = path_str


def _fresh_for(next_seg: str | int | None) -> dict[str, Any] | list[Any]:
    """The container a missing intermediate must be, decided by the NEXT segment:
    an index or ``"-"`` needs a list; a key (or no next segment) needs an object."""
    return [] if isinstance(next_seg, int) or next_seg == APPEND else {}


def _copy_level(node: Any) -> Any:
    """A one-level copy of a container (copy-on-write along the touched path)."""
    if isinstance(node, dict):
        return dict(node)
    if isinstance(node, list):
        return list(node)
    return node


def _step(node: Any, seg: str | int, nxt: str | int | None, *, create: bool, p: str) -> tuple[Any, str | int]:
    """Resolve one INTERMEDIATE segment against ``node`` (already copied at this
    level). ``nxt`` is the following segment (decides what a created intermediate
    must be). Returns ``(child, resolved_seg)``; ``child`` is ``_ABSENT`` when the
    location does not exist and ``create`` is off."""
    if isinstance(seg, str) and seg == APPEND:
        if not isinstance(node, list):
            raise InvalidPathError(f"path {p}: '-' (append) requires a list, found {type(node).__name__}")
        node.append(_fresh_for(nxt))
        return node[-1], len(node) - 1
    if isinstance(seg, str):
        if not isinstance(node, dict):
            raise InvalidPathError(f"path {p}: key {seg!r} requires an object, found {type(node).__name__}")
        if seg not in node:
            if not create:
                return _ABSENT, seg
            node[seg] = _fresh_for(nxt)
        return node[seg], seg
    if not isinstance(node, list):
        raise InvalidPathError(f"path {p}: index {seg} requires a list, found {type(node).__name__}")
    if seg >= len(node):
        if not create:
            return _ABSENT, seg
        raise InvalidPathError(
            f"path {p}: index {seg} is past the end of the list (len {len(node)}); use '-' to append"
        )
    return node[seg], seg


def _item_matches_any(item: Any, key_field: str, keys: list[Any]) -> bool:
    """The list-key form of :func:`_item_matches`: whether ``item`` is a JSON object
    carrying ``key_field`` that JSON-equals ANY key in ``keys`` (``remove_by_key``'s
    ``$pull`` + ``$in`` set-membership). Membership is a per-key :func:`json_equal`
    scan, not a hash lookup — ``json_equal`` is not hashable-safe across the int/float
    boundary, so the strict-ish match semantics carry over unchanged."""
    return isinstance(item, dict) and key_field in item and any(json_equal(item[key_field], k) for k in keys)


def _shallow_merge(existing: dict[str, Any], partial: dict[str, Any]) -> dict[str, Any]:
    """``merge_by_key``'s per-item merge: ``{**existing, **partial}`` with NULL-valued
    partial fields DROPPED first. TOP-LEVEL only — a partial's nested object/list
    REPLACES the existing value wholesale, it never deep-merges. ``null`` NEVER
    deletes (a dropped null leaves the existing field as-is) — a merge only ever
    WRITES fields; deleting fields by key is ``unset_by_key``'s job. ``existing``
    is always a dict (it matched via :func:`_item_matches`); ``partial`` was validated
    to carry a scalar ``key_field``, so the key survives the merge unchanged."""
    return {**existing, **{k: v for k, v in partial.items() if v is not None}}


def _is_empty_keyed_payload(op: dict[str, Any]) -> bool:
    """Whether a validated keyed op carries an EMPTY payload — an empty item/entry list
    (``set_by_key`` / ``merge_by_key`` / ``unset_by_key``), an empty key list
    (``remove_by_key``), or a
    fan-out object that is ``{}`` / all-empty-lists (``set_by_key_each``). Such an op
    is a well-defined NO-OP: :func:`apply_op` short-circuits it BEFORE the path walk,
    so it never creates intermediates, an empty ``[]``, or an empty container."""
    if op["op"] == "remove_by_key":
        key = op["key"]
        return isinstance(key, list) and not key
    value = op["value"]
    if op["op"] == "set_by_key_each":
        return all(not items for items in value.values())
    return isinstance(value, list) and not value


def _slot_at_last(node: Any, last: str | int, *, creating: bool, p: str) -> Any:
    """Resolve the value a keyed op addresses at its final segment ``last`` against the
    already-copied parent ``node`` — the current value, or ``_ABSENT`` when missing.
    The wrong PARENT container type (a key into a list, an index into an object) is a
    loud :class:`InvalidPathError`; whether the resolved VALUE has the right type is
    the caller's per-op check."""
    if isinstance(last, str):
        if not isinstance(node, dict):
            raise InvalidPathError(f"path {p}: key {last!r} requires an object, found {type(node).__name__}")
        return node.get(last, _ABSENT)
    if not isinstance(node, list):
        raise InvalidPathError(f"path {p}: index {last} requires a list, found {type(node).__name__}")
    if last >= len(node):
        if creating:
            raise InvalidPathError(
                f"path {p}: index {last} is past the end of the list (len {len(node)}); use '-' to append"
            )
        return _ABSENT
    return node[last]


def _list_at_last(node: Any, last: str | int, *, creating: bool, p: str, kind: str) -> Any:
    """Resolve the LIST a keyed op addresses at its final segment ``last`` against the
    already-copied parent ``node``. Returns the current list, or ``_ABSENT`` when it is
    missing (``set_by_key`` creates ``[]``; ``remove_by_key`` / ``merge_by_key`` /
    ``unset_by_key`` no-op).
    A present value that is not a list is a loud :class:`InvalidPathError`."""
    current = _slot_at_last(node, last, creating=creating, p=p)
    if current is not _ABSENT and not isinstance(current, list):
        raise InvalidPathError(f"path {p}: {kind} requires a list at the path, found {type(current).__name__}")
    return current


def _upsert_by_key(current: Any, items: list[dict[str, Any]], key_field: str) -> list[Any]:
    """The ``set_by_key`` upsert over one list: a NEW list from ``current`` (``_ABSENT``
    starts ``[]``) with each item applied in payload order — the FIRST item whose
    ``key_field`` JSON-equals the entry's key is REPLACED (whole item, in list order),
    else the entry is APPENDED. Shared verbatim by ``set_by_key`` (single and list
    forms) and each fanned list of ``set_by_key_each``."""
    new_list = list(current) if current is not _ABSENT else []
    for entry in items:
        match_key = entry[key_field]
        for idx, item in enumerate(new_list):
            if _item_matches(item, key_field, match_key):
                new_list[idx] = entry  # replace the FIRST match, in list order
                break
        else:
            new_list.append(entry)  # no match — upsert-APPEND
    return new_list


def apply_op(doc: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
    """Apply ONE validated op to ``doc``, returning a NEW document (input untouched)."""
    kind = op["op"]
    path: list[str | int] = op["path"]
    p = _path_str(path)
    # An empty keyed LIST payload is a well-defined no-op for ALL keyed ops — return
    # the doc unchanged (copy-on-write safe) WITHOUT walking, so an empty merge / set /
    # remove list neither creates intermediates nor leaves an empty [] at the path.
    if kind in KEYED_OPS and _is_empty_keyed_payload(op):
        return _copy_level(doc)
    # set, set_by_key, and set_by_key_each create missing intermediates; remove,
    # remove_by_key, merge_by_key, and unset_by_key (updates that never insert)
    # no-op through an absent one.
    creating = kind in ("set", "set_by_key", "set_by_key_each")

    root = _copy_level(doc)
    node = root
    # Walk to the PARENT of the final segment, copying each level we touch.
    for i, seg in enumerate(path[:-1]):
        child, resolved = _step(node, seg, path[i + 1], create=creating, p=p)
        if child is _ABSENT:
            # remove through an absent intermediate: the target is absent — no-op.
            return root
        copied = _copy_level(child)
        node[resolved] = copied
        node = copied

    last = path[-1]

    if kind in KEYED_OPS:
        key_field: str = op["key_field"]
        if kind == "set_by_key_each":
            # The fan-out addresses the OBJECT holding the fanned lists: an absent
            # container is created (the op is an upsert), a present non-object is
            # loud. Each (fan-out key, items) entry then does the exact set_by_key
            # upsert into the list at path + [key]; a key with NO items contributes
            # nothing (its list is not created — the empty-payload no-op, per key).
            container = _slot_at_last(node, last, creating=True, p=p)
            if container is not _ABSENT and not isinstance(container, dict):
                raise InvalidPathError(
                    f"path {p}: set_by_key_each requires an object at the path, found {type(container).__name__}"
                )
            new_container = dict(container) if container is not _ABSENT else {}
            for name, items in op["value"].items():
                if not items:
                    continue
                fanned = new_container.get(name, _ABSENT)
                if fanned is not _ABSENT and not isinstance(fanned, list):
                    raise InvalidPathError(
                        f"path {_path_str([*path, name])}: set_by_key_each requires a list at the fan-out key, "
                        f"found {type(fanned).__name__}"
                    )
                new_container[name] = _upsert_by_key(fanned, items, key_field)
            node[last] = new_container
            return root
        current = _list_at_last(node, last, creating=creating, p=p, kind=kind)
        if kind == "set_by_key":
            value = op["value"]
            # value is a single item OR a LIST of items; each does the same
            # first-match-replace-else-append, in payload order. (An empty list was
            # already short-circuited above as a no-op.)
            node[last] = _upsert_by_key(current, value if isinstance(value, list) else [value], key_field)
            return root
        if kind == "merge_by_key":
            # Partial-field UPDATE of matched items ONLY — merge NEVER inserts, so an
            # absent list is a quiet no-op (nothing to patch). Each partial (payload
            # order) shallow-merges into its FIRST key-match; a no-match partial is
            # skipped (never an upsert). Null partial fields are dropped by the merge.
            if current is _ABSENT:
                return root
            new_list = list(current)
            for partial in op["value"]:
                match_key = partial[key_field]
                for idx, item in enumerate(new_list):
                    if _item_matches(item, key_field, match_key):
                        new_list[idx] = _shallow_merge(item, partial)
                        break
                # no else — a partial matching nothing is skipped, never upserted
            node[last] = new_list
            return root
        if kind == "unset_by_key":
            # Field REMOVAL on matched items ONLY — unset NEVER inserts, so an absent
            # list is a quiet no-op (nothing to clear). Each entry (payload order)
            # deletes its named fields from its FIRST key-match; a field already
            # absent on the item is an idempotent per-field no-op. key_field can
            # never be among the names (validated), so the item keeps its identity.
            if current is _ABSENT:
                return root
            new_list = list(current)
            for entry in op["value"]:
                match_key = entry[key_field]
                for idx, item in enumerate(new_list):
                    if _item_matches(item, key_field, match_key):
                        new_list[idx] = {k: v for k, v in item.items() if k not in entry["fields"]}
                        break
                # no else — an entry matching nothing is skipped, never inserted
            node[last] = new_list
            return root
        # remove_by_key — drop EVERY match; an absent list is a quiet no-op. A scalar
        # key keeps the byte-identical fast path; a list drops any item matching ANY
        # listed key in the SAME single pass (set-semantics, no per-key re-scan).
        if current is _ABSENT:
            return root
        key = op["key"]
        if isinstance(key, list):
            node[last] = [item for item in current if not _item_matches_any(item, key_field, key)]
        else:
            node[last] = [item for item in current if not _item_matches(item, key_field, key)]
        return root

    if kind == "set":
        if isinstance(last, str) and last == APPEND:
            if not isinstance(node, list):
                raise InvalidPathError(f"path {p}: '-' (append) requires a list, found {type(node).__name__}")
            node.append(op["value"])
            return root
        if isinstance(last, str):
            if not isinstance(node, dict):
                raise InvalidPathError(f"path {p}: key {last!r} requires an object, found {type(node).__name__}")
            node[last] = op["value"]
            return root
        if not isinstance(node, list):
            raise InvalidPathError(f"path {p}: index {last} requires a list, found {type(node).__name__}")
        if last >= len(node):
            raise InvalidPathError(
                f"path {p}: index {last} is past the end of the list (len {len(node)}); use '-' to append"
            )
        node[last] = op["value"]
        return root

    # remove — an absent TARGET is a quiet no-op (idempotent erase); the wrong
    # container type on the walk or here stays loud.
    if isinstance(last, str):
        if not isinstance(node, dict):
            raise InvalidPathError(f"path {p}: key {last!r} requires an object, found {type(node).__name__}")
        node.pop(last, None)
        return root
    if not isinstance(node, list):
        raise InvalidPathError(f"path {p}: index {last} requires a list, found {type(node).__name__}")
    if last < len(node):
        node.pop(last)
    return root


def apply_ops(doc: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply every op in order to ``doc``, returning the NEW document. Pure; each op
    is validated before ANY is applied, so a malformed batch changes nothing."""
    if len(ops) > MAX_OPS_PER_BATCH:
        raise InvalidPathError(f"ops batch has {len(ops)} operations — the cap is {MAX_OPS_PER_BATCH}")
    for i, op in enumerate(ops):
        validate_op(op, where=f"ops[{i}]")
    out = doc
    for op in ops:
        out = apply_op(out, op)
    return out
