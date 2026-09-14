"""Composing-shape refusal and the ``_trace`` stamping the write path applies before its ops."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tai42_contract.states.errors import RegimeViolationError

from tai42_skeleton.states.paths import APPEND, KEYED_OPS
from tai42_skeleton.states.templates import path_overlaps


def _iso_now() -> str:
    """An ISO-8601 UTC timestamp for a ``_trace`` stamp."""
    return datetime.now(UTC).isoformat()


def _abs_regime_paths(attachment_rows: list[dict[str, Any]]) -> list[tuple[list[Any], str, str]]:
    """The absolute regime paths every attach on a state declares, from the stored template
    bodies: for each attach ``(template at base_path)`` and each of the template's regime
    rules, ``base_path + rule.path`` (wildcards preserved), its regime, and the template
    name — the input the composing shape refusal walks. Reads the stored body directly
    (validated at store time), so the hot write path never re-validates a template."""
    out: list[tuple[list[Any], str, str]] = []
    for row in attachment_rows:
        base_path = list(row["path"] or [])
        body = row["body"] or {}
        for rule in body.get("regimes", []) or []:
            out.append(([*base_path, *rule.get("path", [])], rule.get("regime"), body.get("name", row["template"])))
    return out


def _traced_paths(attachment_rows: list[dict[str, Any]]) -> tuple[tuple[str | int, ...], ...]:
    """The attach paths whose template traces (``trace.enabled``) — the prefixes under which
    a write stamps ``_trace``."""
    paths: list[tuple[str | int, ...]] = []
    for row in attachment_rows:
        body = row["body"] or {}
        if bool((body.get("trace") or {}).get("enabled")):
            paths.append(tuple(row["path"] or []))
    return tuple(paths)


def _refuse_composing_shape(ops: list[dict[str, Any]], regime_paths: list[tuple[list[Any], str, str]]) -> None:
    """Refuse a write whose SHAPE violates a ``composing`` path: a whole-path
    ``set``/``remove`` over a composing path admits only a keyed op or an append ``set``
    (path ending ``"-"``). Anything else raises :class:`RegimeViolationError` naming the
    path — BEFORE the ledger insert, so a refused batch consumes no op-id."""
    for op in ops:
        path = op.get("path")
        if not isinstance(path, list):
            continue
        kind = op.get("op")
        append_set = kind == "set" and bool(path) and path[-1] == APPEND
        if kind in KEYED_OPS or append_set:
            continue
        if kind not in ("set", "remove"):
            continue
        for abs_pattern, regime, template_name in regime_paths:
            if regime == "composing" and path_overlaps(path, abs_pattern):
                raise RegimeViolationError(
                    f"composing path {abs_pattern} of template {template_name!r} admits only keyed ops or an append "
                    f"set (path ending '-'); this write uses {kind!r} at {path}"
                )


def _under_traced_path(path: list[Any], traced_paths: tuple[tuple[str | int, ...], ...]) -> bool:
    """Whether ``path`` lies at or under any tracing attach path (the attach path is a
    prefix of the op path — the op writes into the attached subtree)."""
    for attach_path in traced_paths:
        if len(attach_path) <= len(path) and all(attach_path[i] == path[i] for i in range(len(attach_path))):
            return True
    return False


def _stamp_items(value: Any, stamp: dict[str, Any]) -> None:
    """Stamp ``_trace`` into ``value`` when it is an object, or into each object item when
    it is a list; non-objects are untouched."""
    if isinstance(value, dict):
        value["_trace"] = dict(stamp)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                item["_trace"] = dict(stamp)


def stamp_trace(
    ops: list[dict[str, Any]], traced_paths: tuple[tuple[str | int, ...], ...], stamp: dict[str, Any]
) -> None:
    """Stamp ``_trace`` into every object an op WRITES, for each op whose absolute path
    lies under a tracing attach path. Mutates the ops in place before the apply, so the
    effective schema's ``_trace`` property admits the stamped field. Keyed ops carry an
    item object or a list of them; ``set_by_key_each`` carries a ``{key: [items…]}``
    fan-out; a ``set`` (including a ``"-"`` append) carries the object value. Non-object
    values — scalars, arrays of scalars, remove keys — are untouched."""
    for op in ops:
        path = op.get("path")
        if not isinstance(path, list) or not _under_traced_path(path, traced_paths):
            continue
        kind = op.get("op")
        if kind == "set_by_key_each":
            value = op.get("value")
            if isinstance(value, dict):
                for items in value.values():
                    _stamp_items(items, stamp)
        elif kind in ("set_by_key", "merge_by_key"):
            _stamp_items(op.get("value"), stamp)
        elif kind == "set":
            value = op.get("value")
            if isinstance(value, dict):
                value["_trace"] = dict(stamp)
