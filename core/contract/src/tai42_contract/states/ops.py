"""The shared op vocabulary for a state record document — the op/path grammar, its
validation, and the pure query helpers every door and consumer speaks.

This is the dependency-free half of the state store: the op-name constants, the
resource caps, ``validate_op``/``validate_path``/``validate_guard`` (the loud
structural validators), and the pure read helpers ``value_at_path``, ``json_equal``
and ``keyed_op_match_counts``. It carries NO apply/fold engine — that persisted-store
machinery lives in the skeleton, importing this vocabulary so the wire grammar the
platform validates and the grammar a consumer builds against can never drift.

A **path** is a list of segments addressing a location in the record's JSON document:

- ``str``  — an object key (``["quest", "city"]``)
- ``int``  — an existing list index (``["vetoes", 0, "scope"]``)
- ``"-"``  — the JSON-Pointer append position: a NEW item at the end of the list
  (``["vetoes", "-"]`` appends the op's value; ``["vetoes", "-", "scope"]`` appends a
  fresh item and sets ``scope`` inside it)

An **op** is a dict:

- ``{"op": "set", "path": [...], "value": ...}`` — create-or-replace at the path.
  Missing intermediates are created (an object for a key segment, a LIST when the
  next segment is an index or ``"-"``); a ``set`` is what "add", "change", and
  "replace" all compile to.
- ``{"op": "remove", "path": [...]}`` — delete the key / list item at the path.
  Removing an ABSENT path is a NO-OP (idempotent erase); removing THROUGH the
  wrong container type is loud.
- ``{"op": "set_by_key", "path": [...], "key_field": "id", "value": {...}}`` —
  atomic keyed UPSERT into the LIST at ``path``. The item carries its OWN identity:
  the match key is ``value[key_field]``. The FIRST item that is a JSON object whose
  ``key_field`` JSON-equals that key is REPLACED; if none matches the item is
  APPENDED. An absent list is created (``[]``) then appended. This is the
  lost-update-safe alternative to reading a list, editing it, and setting the WHOLE
  list back.
- ``{"op": "remove_by_key", "path": [...], "key_field": "id", "key": ...}`` —
  atomic keyed REMOVE from the LIST at ``path``: drop EVERY item that is a JSON
  object whose ``key_field`` JSON-equals ``key``. ``key`` is a single scalar OR a
  list of scalars (the MongoDB ``$pull`` + ``$in`` idiom — "remove every item whose
  key is in this set" in ONE op); a list drops every item matching ANY listed key in
  a single pass. Zero matches is a quiet no-op; an absent list (or absent
  intermediate) is a no-op too. An EMPTY ``key`` list is a well-defined NO-OP (a jq
  ``[… | select(…)]`` that legitimately selected nothing lands as a traced no-op, it
  never raises).
- ``{"op": "set_by_key", "path": [...], "key_field": "id", "value": [{...}, …]}`` —
  the LIST form of the upsert: ``value`` may ALSO be a LIST of complete item objects
  (each carrying ``key_field``), applied in payload order, each doing the exact
  single-item semantics above (first key-match REPLACED, else APPENDED). One op,
  many upserts. The single-object form and the list form share the same
  first-match-replace-else-append. An EMPTY list is a no-op.
- ``{"op": "merge_by_key", "path": [...], "key_field": "id", "value": [{...}, …]}`` —
  atomic keyed partial-field UPDATE. ``value`` is a LIST of PARTIAL item objects
  (each carrying ``key_field``); for each partial, in payload order, the FIRST stored
  item whose ``key_field`` JSON-equals the partial's key is SHALLOW-MERGED with it
  (``{**existing, **partial}``, TOP-LEVEL only — a partial's nested object REPLACES
  the existing one wholesale, never deep-merges). NULL-valued partial fields are
  DROPPED before merging, so ``null`` NEVER deletes a field — a merge only ever
  WRITES fields; deleting fields by key is ``unset_by_key``'s job. A partial that
  matches NOTHING is SKIPPED (never an upsert, never an error). An absent list /
  absent intermediate, and an EMPTY ``value`` list, are no-ops. Unlike ``set_by_key``
  this NEVER inserts — it is the "patch these existing items" intent.
- ``{"op": "unset_by_key", "path": [...], "key_field": "id", "value": [{...}, …]}`` —
  atomic keyed FIELD REMOVAL, the deleting dual of ``merge_by_key`` (the MongoDB
  ``$unset`` intent). ``value`` is a LIST of ENTRIES — each EXACTLY
  ``{key_field: <key>, "fields": [<name>, …]}``: the addressed item's identity plus
  the TOP-LEVEL field names to remove from it (an entry is the removal's envelope,
  never a partial item — stray keys are refused loudly). For each entry, in payload
  order, the FIRST stored item whose ``key_field`` JSON-equals the entry's key has
  the named fields DELETED; a field already absent on the item is an idempotent
  per-field no-op (the plain ``remove``'s absent-path posture). ``key_field`` itself
  may never be listed — an item keeps its identity — and a ``key_field`` NAMED
  ``"fields"`` is refused (the envelope reserves that name); duplicate field names
  in one entry, and duplicate entry keys, are refused loudly. An entry that matches
  NOTHING is SKIPPED (never an insert, never an error); an absent list / absent
  intermediate, an EMPTY ``value`` list, and an entry with an EMPTY ``fields`` list
  are no-ops. Like ``merge_by_key`` this NEVER inserts — it is the "clear these
  fields on these existing items" intent.
- ``{"op": "set_by_key_each", "path": [...], "key_field": "id", "value": {...}}`` —
  the keyed FAN-OUT of the upsert. ``value`` is a JSON OBJECT mapping fan-out keys to
  LISTS of complete item objects (the ``{kind: [items…]}`` classifier-envelope shape);
  for each ``(key, items)`` entry the op applies the exact ``set_by_key`` list
  semantics into the LIST at ``path + [key]``. One op, one atomic apply, instead of
  one fill per fan-out key. A fan-out key must be a non-empty string and never ``"-"``
  (it becomes a path segment); a key mapping to ``[]`` contributes nothing, and a
  ``value`` that is ``{}`` — or whose lists are ALL empty — is a well-defined NO-OP.

The keyed ops address the LIST (never a new item) — ``set_by_key_each`` the OBJECT
holding the fanned lists — so ``"-"`` (append) is refused anywhere in a keyed path:
the op decides insert-vs-replace by key, not position.
A keyed ``key`` scalar (and every keyed item's ``value[key_field]``) is ``str`` or
``int`` ONLY — floats, booleans, ``null``, and containers make fragile match keys and
are refused loudly; a ``remove_by_key`` list ``key`` and the ``set_by_key`` /
``merge_by_key`` / ``unset_by_key`` payload lists hold those same scalar keys and
refuse DUPLICATE keys loudly (``json_equal`` dedup, the ``wait_for``
explicit-refusal idiom). An object item MISSING ``key_field`` never matches (like a
non-object item — never an error).

All structural errors (an index past the end, a key segment into a list, a keyed op
over a non-list, …) raise :class:`~tai42_contract.states.errors.InvalidPathError`
LOUDLY.
"""

from __future__ import annotations

from typing import Any, cast

from tai42_contract.states.errors import InvalidPathError

# The JSON-Pointer append token — a NEW item at the end of a list.
APPEND = "-"

OPS = frozenset({"set", "remove", "set_by_key", "remove_by_key", "merge_by_key", "unset_by_key", "set_by_key_each"})

# The keyed list ops — an atomic upsert/merge/remove BY KEY into the list a path
# addresses, under the store's record row lock (no whole-list read-modify-set, so
# no lost-update race). Distinguished from the plain ops throughout: they carry a
# ``key_field`` and address the LIST rather than one member. Membership here
# auto-propagates the shared machinery: the "-"-in-path refusal and the
# same-list/same-key path-overlap composability rules.
KEYED_OPS = frozenset({"set_by_key", "remove_by_key", "merge_by_key", "unset_by_key", "set_by_key_each"})

# Resource caps on the authed write surface. 128 segments is far beyond any real
# document path — past it the only effect is pathological nesting (json
# serialization of the merged doc recurses per level and would eventually hit
# RecursionError → 500). 1000 ops per batch bounds one write's work the same way.
MAX_PATH_SEGMENTS = 128
MAX_OPS_PER_BATCH = 1000
# A keyed list payload (a remove_by_key key list, OR a set_by_key / merge_by_key
# item list) is jq-computed and therefore unbounded in principle; past this cap the
# only effect is quadratic validation/apply work on the authed write surface (the
# duplicate-key scan is O(n^2)). Far beyond any real "these keys/items" set.
MAX_REMOVE_KEYS = 200


def validate_path(path: Any, *, where: str = "op") -> list[str | int]:
    """Return ``path`` when it is a non-empty list of valid segments, else raise loudly.

    Valid segments: non-empty ``str`` keys (``"-"`` is the append token), or
    non-negative ``int`` list indices. ``bool`` is rejected explicitly (it is an
    ``int`` subclass and would silently address index 0/1).
    """
    if not isinstance(path, list) or not path:
        raise InvalidPathError(f"{where}: path must be a non-empty list of segments, got {path!r}")
    segments = cast("list[Any]", path)
    if len(segments) > MAX_PATH_SEGMENTS:
        raise InvalidPathError(f"{where}: path has {len(segments)} segments — the cap is {MAX_PATH_SEGMENTS}")
    for seg in segments:
        if isinstance(seg, bool) or not isinstance(seg, str | int):
            raise InvalidPathError(f"{where}: path segment {seg!r} must be a string key or an integer index")
        if isinstance(seg, int) and seg < 0:
            raise InvalidPathError(f"{where}: negative list index {seg} is not supported")
        if isinstance(seg, str) and not seg:
            raise InvalidPathError(f"{where}: empty-string path segment")
    return segments


def _validate_key_value(key: Any, *, where: str) -> None:
    """A keyed op's match key is ``str`` or ``int`` ONLY, else raise loudly.

    ``bool`` is an ``int`` subclass and would silently match ``0``/``1`` (the
    bool-trap idiom from :func:`validate_path`); floats are a foot-gun as
    identities (rounding/serialization drift — note ``json_equal(1.0, 1)`` IS
    true, so a float-keyed item can still be matched by an int key on the read
    side); ``null`` and containers are not identities. All refused.
    """
    if isinstance(key, bool) or not isinstance(key, str | int):
        raise InvalidPathError(f"{where}: a keyed op's key must be a string or an integer, got {key!r}")


def _validate_remove_key(key: Any, *, where: str) -> None:
    """``remove_by_key``'s ``key`` — a single scalar (str/int) OR a list of scalars
    (the ``$pull`` + ``$in`` "remove any of these keys" idiom), else raise loudly.

    A list holds only scalars — a nested container or a duplicate key are refused
    loudly (the explicit-refusal style of :func:`validate_path` and the ``wait_for``
    uniqueness check). Duplicates are compared with :func:`json_equal` for consistency
    with the match semantics, not a plain ``set`` (str/int keys never collide across
    types, but the strict compare documents the intent). An EMPTY list is ACCEPTED as
    a well-defined NO-OP (aligned with the ``set_by_key`` / ``merge_by_key`` list
    forms): a jq ``[… | select(…)]`` that legitimately selected nothing must land as a
    traced no-op op, not raise — an empty selection is not a malformed one.
    """
    if not isinstance(key, list):
        _validate_key_value(key, where=where)
        return
    keys = cast("list[Any]", key)
    if len(keys) > MAX_REMOVE_KEYS:
        raise InvalidPathError(f"{where}: {len(keys)} keys in the remove list — the cap is {MAX_REMOVE_KEYS}")
    seen: list[Any] = []
    for i, k in enumerate(keys):
        _validate_key_value(k, where=f"{where}[{i}]")
        if any(json_equal(k, prev) for prev in seen):
            raise InvalidPathError(f"{where}[{i}]: duplicate key {k!r} in the remove list; keys must be unique")
        seen.append(k)


def _validate_keyed_item_list(value: Any, key_field: str, *, where: str, kind: str) -> None:
    """The LIST-form payload for ``set_by_key`` / ``merge_by_key``: each entry a JSON
    object CARRYING ``key_field`` whose value is a ``str``/``int`` key, and the keys
    UNIQUE across the list (``json_equal`` dedup, the ``_validate_remove_key`` pattern).
    ``value`` is a list the caller already established (an ``isinstance`` narrows to an
    untyped list, so it arrives as ``Any``).

    An EMPTY list is ACCEPTED as a well-defined NO-OP. Refuses loudly, per entry, a
    non-object entry, a missing ``key_field``, a bad key type, and a duplicate key —
    duplicates would make the payload's own apply order (first-match) ambiguous, so
    they are refused the same way the remove list refuses them. The cap bounds the
    O(n^2) dedup scan on the authed write surface.
    """
    entries = cast("list[Any]", value)
    if len(entries) > MAX_REMOVE_KEYS:
        raise InvalidPathError(f"{where}: {len(entries)} items in the {kind} list — the cap is {MAX_REMOVE_KEYS}")
    seen: list[Any] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise InvalidPathError(f"{where}[{i}]: a {kind} entry must be a JSON object, got {entry!r}")
        obj = cast("dict[str, Any]", entry)
        if key_field not in obj:
            raise InvalidPathError(
                f"{where}[{i}]: a {kind} entry must carry its key_field {key_field!r} (the item's own identity)"
            )
        k = obj[key_field]
        _validate_key_value(k, where=f"{where}[{i}]: value[{key_field!r}]")
        if any(json_equal(k, prev) for prev in seen):
            raise InvalidPathError(f"{where}[{i}]: duplicate key {k!r} in the {kind} list; keys must be unique")
        seen.append(k)


def _validate_unset_entries(value: Any, key_field: str, *, where: str) -> None:
    """``unset_by_key``'s payload: a LIST of removal ENTRIES, each EXACTLY the envelope
    ``{key_field: <key>, "fields": [<name>, …]}``.

    Per entry: a JSON object carrying ``key_field`` (a ``str``/``int`` key, unique
    across entries — the ``_validate_keyed_item_list`` dedup) plus a ``fields`` LIST of
    non-empty string field names, themselves unique within the entry. ``key_field``
    itself may never be listed (an item keeps its identity — removing it would orphan
    the item for every later keyed op). Stray entry keys are refused loudly — an entry
    is the removal's envelope, never a partial item, so a merge payload pasted into an
    unset fails instead of silently clearing wrong fields. An EMPTY ``value`` list, and
    an entry's EMPTY ``fields`` list, are ACCEPTED as well-defined no-ops (a jq
    selection that cleared nothing must land as a traced no-op, never raise). The
    shared cap bounds both the entry list and each ``fields`` list. ``value`` is a list
    the caller already established (an ``isinstance`` narrows to an untyped list)."""
    entries = cast("list[Any]", value)
    if len(entries) > MAX_REMOVE_KEYS:
        raise InvalidPathError(f"{where}: {len(entries)} entries in the unset list — the cap is {MAX_REMOVE_KEYS}")
    seen: list[Any] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise InvalidPathError(f"{where}[{i}]: an unset_by_key entry must be a JSON object, got {entry!r}")
        obj = cast("dict[str, Any]", entry)
        if key_field not in obj:
            raise InvalidPathError(
                f"{where}[{i}]: an unset_by_key entry must carry its key_field {key_field!r} (the item's own identity)"
            )
        k = obj[key_field]
        _validate_key_value(k, where=f"{where}[{i}]: value[{key_field!r}]")
        if any(json_equal(k, prev) for prev in seen):
            raise InvalidPathError(f"{where}[{i}]: duplicate key {k!r} in the unset list; keys must be unique")
        seen.append(k)
        raw_fields = obj.get("fields")
        if not isinstance(raw_fields, list):
            raise InvalidPathError(
                f"{where}[{i}]: an unset_by_key entry requires a 'fields' list naming the fields to "
                f"remove, got {raw_fields!r}"
            )
        fields = cast("list[Any]", raw_fields)
        if len(fields) > MAX_REMOVE_KEYS:
            raise InvalidPathError(
                f"{where}[{i}]: {len(fields)} field names in one entry — the cap is {MAX_REMOVE_KEYS}"
            )
        for j, name in enumerate(fields):
            if not isinstance(name, str) or not name:
                raise InvalidPathError(
                    f"{where}[{i}]: fields[{j}]: a field name must be a non-empty string, got {name!r}"
                )
            if name == key_field:
                raise InvalidPathError(
                    f"{where}[{i}]: fields[{j}]: refusing to unset the key_field {key_field!r} — "
                    "an item keeps its identity"
                )
            if name in fields[:j]:
                raise InvalidPathError(
                    f"{where}[{i}]: fields[{j}]: duplicate field {name!r} in the entry; field names must be unique"
                )
        extra = set(obj) - {key_field, "fields"}
        if extra:
            raise InvalidPathError(
                f"{where}[{i}]: an unset_by_key entry carries unknown keys {sorted(extra)} — an entry is "
                f"{{{key_field!r}: <key>, 'fields': [<name>, …]}}, never a partial item"
            )


def _validate_fanout_value(value: dict[Any, Any], key_field: str, *, where: str) -> None:
    """``set_by_key_each``'s payload: a JSON object mapping fan-out keys to item LISTS.

    Each fan-out key becomes a path segment (the upsert lands at ``path + [key]``), so
    it must be a non-empty string and never ``"-"`` — the same refusals a keyed path
    itself makes. Each key's list is validated exactly like a ``set_by_key`` item list
    (objects carrying ``key_field``, unique keys, the shared size cap); the SAME item
    key under two fan-out keys is two different items in two independent lists, never
    a duplicate. An empty list — and an empty object — is a well-defined NO-OP. The
    fan-out key count shares the list-payload cap, bounding one op's total work."""
    if len(value) > MAX_REMOVE_KEYS:
        raise InvalidPathError(f"{where}: {len(value)} fan-out keys — the cap is {MAX_REMOVE_KEYS}")
    for name, items in value.items():
        if not isinstance(name, str) or not name or name == APPEND:
            raise InvalidPathError(
                f"{where}: fan-out key {name!r} must be a non-empty string key (never '-') — it becomes a path segment"
            )
        if not isinstance(items, list):
            raise InvalidPathError(
                f"{where}[{name!r}]: a fan-out key must map to a JSON array of item objects, got {items!r}"
            )
        item_list = cast("list[Any]", items)
        _validate_keyed_item_list(item_list, key_field, where=f"{where}[{name!r}]", kind="set_by_key_each")


def validate_op(op: Any, *, where: str = "op") -> dict[str, Any]:
    """Return ``op`` when it is a well-formed operation dict, else raise loudly.

    An op MAY carry an optional compare-and-set ``guard`` (see :func:`validate_guard`):
    the store applies the op only when the record's current value at the guard path
    JSON-equals the guard's ``expected`` value, else it guarded-skips that op.

    The keyed ops (:data:`KEYED_OPS`) carry a non-empty string ``key_field`` naming a
    single top-level field of the list's item objects, address the LIST (so ``"-"`` is
    refused), and identify items by a ``str``/``int`` key:

    - ``set_by_key`` carries a ``value`` that is EITHER a single JSON object (the
      classic one-item upsert) OR a LIST of JSON objects (many upserts, one op) — each
      object's own ``key_field`` is its key.
    - ``merge_by_key`` carries a ``value`` that is a LIST of PARTIAL JSON objects (each
      carrying ``key_field``) — a partial-field update of matched items.
    - ``unset_by_key`` carries a ``value`` that is a LIST of removal ENTRIES
      ``{key_field: <key>, "fields": [<name>, …]}`` — a field removal on matched items
      (see :func:`_validate_unset_entries`; ``key_field`` may never be listed, and a
      ``key_field`` named ``"fields"`` is refused — the envelope reserves that name).
    - ``remove_by_key`` carries a ``key`` that is a bare scalar OR a list of scalars
      (remove-any-of).
    - ``set_by_key_each`` carries a ``value`` that is a JSON OBJECT mapping fan-out
      keys (non-empty strings, never ``"-"``) to LISTS of complete item objects — the
      keyed fan-out of the upsert (see :func:`_validate_fanout_value`).

    Every LIST payload (``remove_by_key``'s list ``key``, ``set_by_key`` /
    ``merge_by_key``'s item lists) refuses DUPLICATE keys loudly and accepts an EMPTY
    list as a no-op. Each kind refuses the OTHER kind's payload key (a stray ``key`` on
    a ``set_by_key``/``merge_by_key``, a stray ``value`` on a ``remove_by_key``) and
    the plain ops refuse ``key_field``/``key`` entirely — per-kind, never a shared flat
    allowlist.
    """
    if not isinstance(op, dict):
        raise InvalidPathError(f"{where}: an op must be a JSON object, got {op!r}")
    data = cast("dict[str, Any]", op)
    kind = data.get("op")
    if kind not in OPS:
        raise InvalidPathError(f"{where}: unknown op {kind!r} (supported: {sorted(OPS)})")
    validate_path(data.get("path"), where=where)

    if kind == "set":
        if "value" not in data:
            raise InvalidPathError(f"{where}: a set op requires a 'value'")
        allowed = {"op", "path", "value", "guard"}
    elif kind == "remove":
        if any(seg == APPEND for seg in data["path"]):
            raise InvalidPathError(
                f"{where}: '-' (append) has no meaning in a remove path — it addresses no existing item"
            )
        if "value" in data:
            # A stray value on a remove is probably a mis-built set — refused loudly,
            # consistent with the unknown-keys refusal below, never silently ignored.
            raise InvalidPathError(f"{where}: a remove op takes no 'value'")
        allowed = {"op", "path", "guard"}
    else:
        # A keyed op addresses the LIST — '-' (append) has no meaning; the op decides
        # insert-vs-replace by key, never by position.
        if any(seg == APPEND for seg in data["path"]):
            raise InvalidPathError(
                f"{where}: '-' (append) has no meaning in a {kind} path — it addresses the list, not a new item"
            )
        key_field = data.get("key_field")
        if not isinstance(key_field, str) or not key_field:
            raise InvalidPathError(f"{where}: a {kind} op requires a non-empty string 'key_field'")
        if kind == "set_by_key":
            if "key" in data:
                raise InvalidPathError(f"{where}: a set_by_key op takes no 'key' — its match key is value[key_field]")
            if "value" not in data:
                raise InvalidPathError(f"{where}: a set_by_key op requires a 'value' (the item object to upsert)")
            value = data["value"]
            if isinstance(value, list):
                # LIST form: many complete items, each doing the single-item
                # first-match-replace-else-append.
                _validate_keyed_item_list(value, key_field, where=f"{where}: value", kind="set_by_key")
            elif isinstance(value, dict):
                obj = cast("dict[str, Any]", value)
                if key_field not in obj:
                    raise InvalidPathError(
                        f"{where}: a set_by_key 'value' must carry its key_field {key_field!r} "
                        "(the item's own identity)"
                    )
                _validate_key_value(obj[key_field], where=f"{where}: value[{key_field!r}]")
            else:
                raise InvalidPathError(
                    f"{where}: a set_by_key 'value' must be a JSON object or a JSON array of objects, got {value!r}"
                )
            allowed = {"op", "path", "key_field", "value", "guard"}
        elif kind == "set_by_key_each":
            if "key" in data:
                raise InvalidPathError(
                    f"{where}: a set_by_key_each op takes no 'key' — each item's match key is its own key_field"
                )
            if "value" not in data:
                raise InvalidPathError(
                    f"{where}: a set_by_key_each op requires a 'value' (the object mapping fan-out keys to item lists)"
                )
            value = data["value"]
            if not isinstance(value, dict):
                raise InvalidPathError(
                    f"{where}: a set_by_key_each 'value' must be a JSON object mapping fan-out keys to arrays "
                    f"of item objects, got {value!r}"
                )
            _validate_fanout_value(cast("dict[str, Any]", value), key_field, where=f"{where}: value")
            allowed = {"op", "path", "key_field", "value", "guard"}
        elif kind == "merge_by_key":
            if "key" in data:
                raise InvalidPathError(f"{where}: a merge_by_key op takes no 'key' — each partial carries its own key")
            if "value" not in data:
                raise InvalidPathError(
                    f"{where}: a merge_by_key op requires a 'value' (the list of partial items to merge)"
                )
            value = data["value"]
            if not isinstance(value, list):
                raise InvalidPathError(
                    f"{where}: a merge_by_key 'value' must be a JSON array of partial item objects, got {value!r}"
                )
            _validate_keyed_item_list(value, key_field, where=f"{where}: value", kind="merge_by_key")
            allowed = {"op", "path", "key_field", "value", "guard"}
        elif kind == "unset_by_key":
            if "key" in data:
                raise InvalidPathError(f"{where}: an unset_by_key op takes no 'key' — each entry carries its own key")
            if key_field == "fields":
                raise InvalidPathError(
                    f"{where}: an unset_by_key op cannot use key_field 'fields' — the entry envelope reserves that name"
                )
            if "value" not in data:
                raise InvalidPathError(
                    f"{where}: an unset_by_key op requires a 'value' (the list of {{key, fields}} entries)"
                )
            value = data["value"]
            if not isinstance(value, list):
                raise InvalidPathError(
                    f"{where}: an unset_by_key 'value' must be a JSON array of "
                    f"{{{key_field!r}, 'fields'}} entries, got {value!r}"
                )
            _validate_unset_entries(value, key_field, where=f"{where}: value")
            allowed = {"op", "path", "key_field", "value", "guard"}
        else:  # remove_by_key
            if "value" in data:
                raise InvalidPathError(f"{where}: a remove_by_key op takes no 'value' — it removes by 'key'")
            if "key" not in data:
                raise InvalidPathError(f"{where}: a remove_by_key op requires a 'key' (the value to remove by)")
            _validate_remove_key(data["key"], where=f"{where}: key")
            allowed = {"op", "path", "key_field", "key", "guard"}

    if data.get("guard") is not None:
        validate_guard(data["guard"], where=where)
    extra = set(data) - allowed
    if extra:
        raise InvalidPathError(f"{where}: op carries unknown keys {sorted(extra)}")
    return data


def validate_guard(guard: Any, *, where: str = "op") -> dict[str, Any]:
    """Return ``guard`` when it is a well-formed compare-and-set guard, else raise loudly.

    A guard is exactly ``{"path": [seg…], "expected": <JSON value>}``: the store reads
    the record's CURRENT value at ``path`` (a missing path reads as ``null``) and applies
    the op only when it JSON-equals ``expected``. ``path`` is a non-empty segment list
    addressing EXISTING data — ``"-"`` (append) has no meaning reading a value and is
    refused. ``expected`` MUST be present, and ``null`` is a legitimate value (it means
    "expect the field absent/null").
    """
    if not isinstance(guard, dict):
        raise InvalidPathError(f"{where}: guard must be a JSON object, got {guard!r}")
    data = cast("dict[str, Any]", guard)
    if "expected" not in data:
        raise InvalidPathError(f"{where}: a guard requires an 'expected' value (null is valid)")
    validate_path(data.get("path"), where=f"{where} guard")
    if any(seg == APPEND for seg in data["path"]):
        raise InvalidPathError(f"{where}: '-' (append) has no meaning in a guard path — it reads existing data")
    extra = set(data) - {"path", "expected"}
    if extra:
        raise InvalidPathError(f"{where}: guard carries unknown keys {sorted(extra)}")
    return data


def value_at_path(doc: Any, path: list[str | int]) -> Any:
    """The value at ``path`` in ``doc``, or ``None`` when any segment is absent or
    addresses through the wrong container type — a guard reads a missing path as null."""
    node: Any = doc
    for seg in path:
        if isinstance(seg, str):
            if not isinstance(node, dict):
                return None
            obj = cast("dict[str, Any]", node)
            if seg not in obj:
                return None
            node = obj[seg]
        else:
            if not isinstance(node, list):
                return None
            arr = cast("list[Any]", node)
            if seg >= len(arr):
                return None
            node = arr[seg]
    return node


def json_equal(a: Any, b: Any) -> bool:
    """STRICT JSON-value equality: booleans match only booleans (never ``1``/``0``),
    objects compare key-set + values, arrays compare length + order; strings, numbers,
    and null compare by value. Distinct from Python ``==`` only where a ``bool`` would
    otherwise coerce to an ``int``."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        da = cast("dict[str, Any]", a)
        db = cast("dict[str, Any]", b)
        return da.keys() == db.keys() and all(json_equal(da[k], db[k]) for k in da)
    if isinstance(a, list) and isinstance(b, list):
        la = cast("list[Any]", a)
        lb = cast("list[Any]", b)
        return len(la) == len(lb) and all(json_equal(x, y) for x, y in zip(la, lb, strict=True))
    return a == b


def _item_matches(item: Any, key_field: str, key: Any) -> bool:
    """Whether a list ``item`` is a JSON object carrying ``key_field`` that JSON-equals
    ``key``. A non-object item, or an object MISSING ``key_field``, never matches (never
    an error) — the keyed ops skip past foreign items rather than refusing the list."""
    if not isinstance(item, dict):
        return False
    obj = cast("dict[str, Any]", item)
    return key_field in obj and json_equal(obj[key_field], key)


def keyed_op_match_counts(result_doc: Any, op: dict[str, Any]) -> dict[str, int] | None:
    """The observability shape for ONE keyed list op: ``{"supplied": S, "matched": M}``
    read against ``result_doc`` (the document AFTER the op applied — the committed
    result the state-write trace already carries). ``None`` for a non-keyed op.

    ``supplied`` is how many keys the op carried (a single-object ``set_by_key`` or a
    scalar ``remove_by_key`` is ``1``; the list forms are ``len``). ``matched`` is how
    many of those keys ACHIEVED THEIR INTENT, judged by the result:

    - ``merge_by_key`` / ``unset_by_key`` / ``set_by_key``: intent = the item is
      PRESENT in the result. merge and unset never insert, so ``supplied - matched``
      is exactly the silently-SKIPPED partials/entries — a key that raced away or was
      typo'd (the diagnostic this exists for). set upserts always land, so
      ``matched == supplied`` there (no silent-skip mode).
    - ``set_by_key_each``: the fan-out counts across ALL fanned lists — supplied is
      every item under every fan-out key, matched reads each item's key in the result
      list at ``path + [key]``; upserts always land, so ``matched == supplied``.
    - ``remove_by_key``: intent = the key is ABSENT from the result (removed, or
      already gone); ``matched < supplied`` would mean a requested key still stands.

    A pure read helper — a PARALLEL to the apply engine rather than a change to its
    signature, so the store's and deltas-door's existing apply callers are untouched.
    Reads the COMMITTED result, so a later op in the SAME batch that re-touches this
    list+key can shift the count (pathological — a single writer rarely merges/sets then
    removes the same key)."""
    kind = op.get("op")
    if kind not in KEYED_OPS:
        return None
    key_field: str = op["key_field"]
    if kind == "set_by_key_each":
        # The fan-out counts across ALL fanned lists: supplied = every item in every
        # fan-out key's list; matched = the item's key present in the result list at
        # path + [key]. Upserts always land, so matched == supplied (like set_by_key).
        container = value_at_path(result_doc, op["path"])
        obj = cast("dict[str, Any]", container) if isinstance(container, dict) else {}
        fanout: dict[str, Any] = op["value"]
        supplied = 0
        matched = 0
        for name, items in fanout.items():
            fanned = obj.get(name)
            present_fanned = cast("list[Any]", fanned) if isinstance(fanned, list) else []
            item_list: list[Any] = items
            supplied += len(item_list)
            matched += sum(
                1 for e in item_list if any(_item_matches(item, key_field, e[key_field]) for item in present_fanned)
            )
        return {"supplied": supplied, "matched": matched}
    result_list = value_at_path(result_doc, op["path"])
    present = cast("list[Any]", result_list) if isinstance(result_list, list) else []

    def _in_result(k: Any) -> bool:
        return any(_item_matches(item, key_field, k) for item in present)

    if kind == "remove_by_key":
        raw_key = op["key"]
        keys: list[Any] = cast("list[Any]", raw_key) if isinstance(raw_key, list) else [raw_key]
        return {"supplied": len(keys), "matched": sum(1 for k in keys if not _in_result(k))}
    value = op["value"]
    entries: list[Any] = cast("list[Any]", value) if isinstance(value, list) else [value]
    return {"supplied": len(entries), "matched": sum(1 for e in entries if _in_result(e[key_field]))}
