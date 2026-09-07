"""The one validate + apply layer over the subject-keyed record store — the platform
half of the state feature.

Holds a :class:`~tai42_skeleton.states.store.PostgresStatesStore`; every door refuses
loudly (:class:`~tai42_contract.states.errors.StatesNotConfiguredError`, 501) while the
``states`` component's database is unbound. The service owns subject validation (the
``person`` kind against the identity store), the effective-schema composer, the module
lifecycle, and the WRITE-PROVENANCE CHOKEPOINT: it completes a consumer's
:class:`~tai42_contract.states.WriteOrigin` into a
:class:`~tai42_contract.states.CompletedOrigin` — stamping ``door``/``actor``/``turn_id``
from the ambient :class:`~tai42_contract.states.StateContext` (or ``api`` + the request
principal with none) — so the audit ledger is never optional or forgeable (D-6).

The ambient state-context carrier the write chokepoint reads lives in the kit
(``tai42_kit.utils.state_context``) so backend workers can deposit it without importing
the skeleton; :mod:`tai42_skeleton.states.context` re-exports it, and this module imports
``state_context``/``current_state_context`` from there.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tai42_skeleton.states.seeds import StateModuleSeedRegistry

import jsonschema
import referencing.exceptions
from jsonschema import Draft202012Validator
from tai42_contract.states.errors import (
    DeclarationInUseError,
    InvalidPathError,
    MigrationConversionError,
    ModuleExistsError,
    ModuleInUseError,
    ModuleValidationError,
    MountConflictError,
    NarrowingRequiresConfirmationError,
    NonAdditiveRedeclareError,
    SchemaValidationError,
    StateNotFoundError,
    StatesNotConfiguredError,
    SubjectFoldError,
    SubjectRefusedError,
    ValueValidationError,
)
from tai42_contract.states.models import (
    MAX_RETENTION_DAYS,
    PERSON_KIND,
    ApplyResult,
    CompletedOrigin,
    ConsumerLister,
    ConsumerRow,
    MountBody,
    MountValidator,
    RecordView,
    StateContext,
    StateDeclaration,
    StateModuleDocument,
    StateSubject,
    WriteEntry,
    WriteOrigin,
    WritesPage,
)
from tai42_kit.utils.data.jq_util import run_jq_first

from tai42_skeleton.states.context import current_state_context, state_context
from tai42_skeleton.states.db import states_store_configured
from tai42_skeleton.states.modules import StateModule, compose_effective_schema, regime_for, validate_module
from tai42_skeleton.states.paths import APPEND, validate_op, validate_path
from tai42_skeleton.states.paths import apply_ops as apply_path_ops
from tai42_skeleton.states.store import (
    PostgresStatesStore,
    make_cursor,
    store_settings_default_retention,
    store_settings_retention,
)

logger = logging.getLogger(__name__)

_PREVIEW_EXAMPLE_CAP = 10
_DEFAULT_PAGE = 200
_MAX_PAGE = 500


# --------------------------------------------------------------------------- #
# Consumer-owned registries (per-app, reset each start() by the server)          #
# --------------------------------------------------------------------------- #
class StatesMountValidatorRegistry:
    """The process-wide mount-validator registry — the body behind
    ``app.states.register_mount_validator``. A consumer registers a data-dependent
    validator when its module loads; the mount doors consult every registered validator
    before any write. Reset each ``start()`` so a reload re-registers cleanly."""

    def __init__(self) -> None:
        self._validators: list[MountValidator] = []

    def register(self, validator: MountValidator) -> None:
        self._validators.append(validator)

    def all(self) -> list[MountValidator]:
        return list(self._validators)

    def reset(self) -> None:
        self._validators.clear()


class StatesConsumerListerRegistry:
    """The process-wide consumer-lister registry — the body behind
    ``app.states.register_consumer_lister``. A duplicate kind within one load raises
    loudly. Reset each ``start()`` so a reload re-registers cleanly."""

    def __init__(self) -> None:
        self._listers: dict[str, ConsumerLister] = {}

    def register(self, kind: str, lister: ConsumerLister) -> None:
        if kind in self._listers:
            raise ValueError(f"states consumer lister for kind {kind!r} is already registered")
        self._listers[kind] = lister

    def all(self) -> dict[str, ConsumerLister]:
        return dict(self._listers)

    def reset(self) -> None:
        self._listers.clear()


# --------------------------------------------------------------------------- #
# Pure schema validators                                                      #
# --------------------------------------------------------------------------- #
def _canonical(value: Any) -> str:
    """A byte-stable canonical form for comparing two field schemas for equality."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _validate_schema(schema: Any) -> None:
    """Accept any VALID JSON Schema (draft 2020-12) that is object-rooted with ≥1
    property; refuse everything else loudly. Nesting to any depth is the point — the
    record document is validated WHOLE against this schema on every write."""
    if not isinstance(schema, dict):
        raise SchemaValidationError("schema must be a JSON object")
    if schema.get("type") != "object":
        raise SchemaValidationError('schema must declare "type": "object"')
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        raise SchemaValidationError("schema must declare at least one property")
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise SchemaValidationError(f"schema is not a valid JSON Schema (draft 2020-12): {exc.message}") from exc
    _validate_refs(schema)


def _validate_refs(schema: dict[str, Any]) -> None:
    """Refuse ``$ref``s ``check_schema`` cannot vouch for (SYNTAX-only): a remote ref, a
    dangling local one, and ``$dynamicRef`` are all declare-time refusals. Local support:
    ``#`` (root), ``#/json/pointer`` (resolved against the document), and ``#anchor``."""
    if _uses_key(schema, "$dynamicRef"):
        raise SchemaValidationError("$dynamicRef is not supported — use $ref with root-level $defs")
    for ref in _iter_refs(schema):
        if not ref.startswith("#"):
            raise SchemaValidationError(f"remote $ref {ref!r} is not supported — inline the schema or use $defs")
        if ref == "#":
            continue
        if ref.startswith("#/"):
            node: Any = schema
            for raw in ref[2:].split("/"):
                token = raw.replace("~1", "/").replace("~0", "~")
                if isinstance(node, dict) and token in node:
                    node = node[token]
                elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
                    node = node[int(token)]
                else:
                    raise SchemaValidationError(f"$ref {ref!r} does not resolve — {token!r} is missing")
            continue
        anchor = ref[1:]
        if not _anchor_exists(schema, anchor):
            raise SchemaValidationError(f"$ref {ref!r} does not resolve — no $anchor {anchor!r} in the schema")


def _iter_refs(node: Any):
    """Yield every ``$ref`` string value anywhere in the schema document."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref
        for value in node.values():
            yield from _iter_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_refs(item)


def _uses_key(node: Any, key: str) -> bool:
    """Whether ``key`` appears as a dict key anywhere in the schema document."""
    if isinstance(node, dict):
        return key in node or any(_uses_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(_uses_key(item, key) for item in node)
    return False


def _anchor_exists(node: Any, anchor: str) -> bool:
    if isinstance(node, dict):
        if node.get("$anchor") == anchor:
            return True
        return any(_anchor_exists(v, anchor) for v in node.values())
    if isinstance(node, list):
        return any(_anchor_exists(item, anchor) for item in node)
    return False


def _validate_document(schema: dict[str, Any], doc: dict[str, Any]) -> None:
    """Validate the FULL record document against the effective schema; the error names the
    offending JSON path. Loud on the first failure."""
    try:
        Draft202012Validator(schema).validate(doc)
    except jsonschema.ValidationError as exc:
        raise ValueValidationError(f"record invalid under the state schema at {exc.json_path}: {exc.message}") from exc
    except referencing.exceptions.Unresolvable as exc:
        raise ValueValidationError(f"the state schema carries an unresolvable $ref: {exc}") from exc


def _is_narrowing(old_schema: dict[str, Any], new_schema: dict[str, Any]) -> bool:
    """Whether ``new_schema`` removes or changes any top-level property of ``old_schema`` —
    OR changes any ROOT keyword outside ``properties``. Deliberately conservative: any
    property-subtree edit or root-keyword edit registers as narrowing and takes the guarded
    migrate door."""
    old_props = old_schema.get("properties", {}) if isinstance(old_schema, dict) else {}
    new_props = new_schema.get("properties", {})
    for fname, fschema in old_props.items():
        if fname not in new_props or _canonical(new_props[fname]) != _canonical(fschema):
            return True
    old_root = {k: v for k, v in old_schema.items() if k != "properties"} if isinstance(old_schema, dict) else {}
    new_root = {k: v for k, v in new_schema.items() if k != "properties"}
    return _canonical(old_root) != _canonical(new_root)


def _page_limit(limit: Any) -> int:
    """The clamped page size for the listing/search doors — ``None`` takes the default; a
    non-positive or non-integer limit is a loud client error; anything above the hard cap
    is clamped."""
    if limit is None:
        return _DEFAULT_PAGE
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueValidationError(f"limit must be a positive integer, got {limit!r}")
    return min(limit, _MAX_PAGE)


def _subject_from_row(state: str, row: dict[str, Any]) -> dict[str, Any]:
    """A store subject row → the listing dict ``{subject: {…}, updated_at}``."""
    return {
        "subject": {
            "target_kind": row["target_kind"],
            "target_name": row["target_name"],
            "kind": row["subject_kind"],
            "key": row["subject_key"],
        },
        "updated_at": row["updated_at"],
    }


def _row_to_declaration(row: dict[str, Any]) -> StateDeclaration:
    return StateDeclaration(
        name=row["name"],
        description=row.get("description") or "",
        schema=row["schema"],
        subject_kinds=list(row["subject_kinds"]),
        default_subject_kind=row["default_subject_kind"],
        retention_days=row.get("retention_days"),
        effective_schema=row.get("effective_schema"),
        updated_at=row.get("updated_at"),
    )


class StatesService:
    """The one validate + apply layer. Holds a store and the consumer-owned registries;
    every method refuses loudly while the feature is off."""

    _MODULE_CACHE_MAX = 256

    def __init__(
        self,
        store: PostgresStatesStore | None = None,
        *,
        mount_validators: StatesMountValidatorRegistry | None = None,
        consumer_listers: StatesConsumerListerRegistry | None = None,
        seeds: StateModuleSeedRegistry | None = None,
    ) -> None:
        from tai42_skeleton.states.seeds import StateModuleSeedRegistry

        self._store = store or PostgresStatesStore()
        self._mount_validators = mount_validators or StatesMountValidatorRegistry()
        self._consumer_listers = consumer_listers or StatesConsumerListerRegistry()
        self._seeds = seeds or StateModuleSeedRegistry()
        self._module_cache: OrderedDict[tuple[str, Any], StateModule] = OrderedDict()

    # -- gate + module cache -----------------------------------------------------

    @staticmethod
    def _ensure_available() -> None:
        if not states_store_configured():
            raise StatesNotConfiguredError(
                "the states feature is off: bind the 'states' component's database (TAI_DB_BINDING_STATES / the "
                "default database) to enable it"
            )

    def _validated_module(self, row: dict[str, Any]) -> StateModule:
        """Validate a module row into a :class:`StateModule`, memoized on ``(name,
        updated_at)`` — an unchanged row is served from a bounded LRU."""
        key = (row["name"], row["updated_at"])
        cached = self._module_cache.get(key)
        if cached is not None:
            self._module_cache.move_to_end(key)
            return cached
        module = validate_module(row["body"])
        self._module_cache[key] = module
        self._module_cache.move_to_end(key)
        if len(self._module_cache) > self._MODULE_CACHE_MAX:
            self._module_cache.popitem(last=False)
        return module

    # -- subject validation ------------------------------------------------------

    async def validate_subject(self, decl: StateDeclaration, subject: StateSubject) -> None:
        """Refuse a subject that a state's declaration does not admit (D-1): an undeclared
        kind, or — for kind ``person`` — an unknown person or a person whose target does
        not match the subject's. The ``ConversationPersonStore`` is constructed LAZILY and
        ONLY on the ``person`` branch (its constructor raises 501 without the redis
        conversations backend), so no state of another kind is gated on redis."""
        if subject.kind not in decl.subject_kinds:
            raise SubjectRefusedError(
                f"subject kind {subject.kind!r} is not declared by state {decl.name!r} "
                f"(declared kinds: {sorted(decl.subject_kinds)})"
            )
        if subject.kind != PERSON_KIND:
            return
        from tai42_skeleton.conversations.persons import ConversationPersonStore
        from tai42_skeleton.conversations.settings import ConversationsSettings

        person = await ConversationPersonStore(ConversationsSettings()).get_by_id(subject.key)
        if person is None:
            raise SubjectRefusedError(
                f"subject key {subject.key!r} of kind 'person' names no person in the identity store"
            )
        if person.target_kind != subject.target_kind or person.target_name != subject.target_name:
            raise SubjectRefusedError(
                f"person {subject.key!r} belongs to target {person.target_kind}/{person.target_name}, "
                f"not the subject's {subject.target_kind}/{subject.target_name}"
            )

    # -- write-provenance chokepoint (D-6) --------------------------------------

    def _complete_origin(self, origin: WriteOrigin) -> CompletedOrigin:
        """Complete a consumer's :class:`WriteOrigin` into a :class:`CompletedOrigin`:
        ``door``/``actor``/``turn_id``/``inbound_id`` from the ambient context, or ``api``
        + the request principal with none. The consumer's ``door``/``actor``/``turn_id``
        cannot be supplied (absent from :class:`WriteOrigin`, ``extra='forbid'``), so the
        ledger can never be forged."""
        ctx = current_state_context()
        if ctx is not None:
            return CompletedOrigin(
                consumer=origin.consumer,
                meta=origin.meta,
                run_id=origin.run_id,
                op_id=origin.op_id,
                door=ctx.door,
                actor=ctx.actor,
                turn_id=ctx.turn_id,
                inbound_id=ctx.inbound_id,
            )
        from tai42_skeleton.access_control.user import request_identity

        actor, _restricted = request_identity()
        return CompletedOrigin(
            consumer=origin.consumer,
            meta=origin.meta,
            run_id=origin.run_id,
            op_id=origin.op_id,
            door="api",
            actor=actor,
            turn_id=None,
            inbound_id=None,
        )

    def context(self) -> StateContext | None:
        return current_state_context()

    # -- declarations ------------------------------------------------------------

    async def list_declarations(self) -> list[StateDeclaration]:
        self._ensure_available()
        out: list[StateDeclaration] = []
        for row in await self._store.list_declarations():
            decl = _row_to_declaration(row)
            regimes = self._compose_regimes(await self._load_state_mounts(decl.name))
            out.append(decl.model_copy(update={"regimes": regimes}))
        return out

    async def get_declaration(self, name: str) -> StateDeclaration | None:
        self._ensure_available()
        row = await self._store.get_declaration(name)
        if row is None:
            return None
        decl = _row_to_declaration(row)
        regimes = self._compose_regimes(await self._load_state_mounts(decl.name))
        return decl.model_copy(update={"regimes": regimes})

    async def put_declaration(self, decl: StateDeclaration) -> StateDeclaration:
        """Create or plain re-declare a state.

        With records present, a re-declare accepts only ADDITIVE schema changes; a removal
        or change of an existing property is refused pointing at the guarded migrate door
        (:class:`NonAdditiveRedeclareError`), and removing a subject kind still present in
        records raises :class:`DeclarationInUseError`. ``retention_days`` is metadata, not
        schema, so changing it alone is never gated."""
        self._ensure_available()
        if decl.effective_schema is not None:
            raise ValueError("effective_schema is computed by the platform")
        if decl.regimes is not None:
            raise ValueError("regimes are computed by the platform")
        if decl.updated_at is not None:
            raise ValueError("updated_at is set by the platform")
        _validate_schema(decl.schema_)
        effective_schema = await self._compose_effective(decl.name, decl.schema_)

        def decide(existing: dict[str, Any] | None, per_kind: dict[str, int]) -> None:
            if existing is None:
                return
            total = sum(per_kind.values())
            if total > 0 and _is_narrowing(existing["schema"], decl.schema_):
                raise NonAdditiveRedeclareError(
                    f"state {decl.name!r} has records: removing or changing a field goes through the guarded "
                    f"migrate door, not a plain re-declare"
                )
            removed = set(existing["subject_kinds"]) - set(decl.subject_kinds)
            in_use = sorted(k for k in removed if per_kind.get(k, 0) > 0)
            if in_use:
                raise DeclarationInUseError(
                    f"state {decl.name!r} still has records under subject kind(s) {in_use}; erase them before "
                    f"removing the kind(s)"
                )

        await self._store.upsert_declaration_guarded(
            decl.name,
            decl.description,
            decl.schema_,
            decl.subject_kinds,
            decl.default_subject_kind,
            decl.retention_days,
            effective_schema=effective_schema,
            decide=decide,
        )
        return decl

    async def delete_declaration(self, name: str) -> None:
        """Delete a state with its records, mounts and aliases; refuses while a registered
        consumer still binds it (:class:`DeclarationInUseError`)."""
        self._ensure_available()
        if await self._store.get_declaration(name) is None:
            raise StateNotFoundError(f"no state declared as {name!r}")
        consumers = await self.consumers(name)
        binders = [c for c in consumers if c.unavailable is None]
        if binders:
            names = ", ".join(sorted(f"{c.kind}:{c.name}" for c in binders if c.name))
            raise DeclarationInUseError(
                f"state {name!r} is still bound by {names or 'a consumer'} — remove the binding(s) first"
            )
        await self._store.delete_declaration(name)

    async def stats(self, name: str) -> dict[str, Any]:
        """``{records, per_field, per_kind, consumers}`` for the listing."""
        self._ensure_available()
        decl = await self._store.get_declaration(name)
        if decl is None:
            raise StateNotFoundError(f"no state declared as {name!r}")
        records, per_field, per_kind = await self._store.field_stats(name)
        props = decl["schema"].get("properties", {})
        consumers = await self.consumers(name)
        return {
            "records": records,
            "per_field": {f: per_field.get(f, 0) for f in props},
            "per_kind": per_kind,
            "consumers": len([c for c in consumers if c.unavailable is None]),
        }

    async def migrate(
        self,
        name: str,
        new_schema: dict[str, Any],
        *,
        origin: WriteOrigin,
        transform_expr: str | None = None,
        confirm_drop: bool = False,
        resolutions: list[dict[str, Any]] | None = None,
    ) -> None:
        """The guarded schema-change door: additive changes pass through; a narrowing needs
        exactly one of ``transform_expr`` / ``confirm_drop`` / ``resolutions``. The
        conversion + schema replace happen in ONE transaction; a failing rule or a
        still-invalid record aborts the WHOLE change. Records one ``state_writes`` row per
        converted subject under the completed ``origin``, in the same transaction."""
        self._ensure_available()
        completed = self._complete_origin(origin)
        _validate_schema(new_schema)
        checked = None if resolutions is None else _validate_resolutions(resolutions)
        new_effective_schema = await self._compose_effective(name, new_schema)

        def decide(old_schema: dict[str, Any]):
            doors = sum([transform_expr is not None, bool(confirm_drop), checked is not None])
            if not _is_narrowing(old_schema, new_schema):
                if doors:
                    raise NarrowingRequiresConfirmationError(
                        "the change is additive — no conversion runs, so transform_expr/confirm_drop/resolutions "
                        "must not be supplied"
                    )
                return _no_conversion
            if doors != 1:
                raise NarrowingRequiresConfirmationError(
                    "a narrowing change requires EXACTLY ONE of transform_expr, confirm_drop, or resolutions"
                )
            if transform_expr is not None:
                return _transform_converter(transform_expr, new_effective_schema)
            if checked is not None:
                return _resolutions_converter(checked, new_effective_schema)
            return _drop_converter(old_schema, new_schema, new_effective_schema)

        await self._store.migrate(name, new_schema, new_effective_schema, decide=decide, origin=completed)

    async def preview_migrate(self, name: str, new_schema: dict[str, Any]) -> dict[str, Any]:
        """A read-only dry run of ``new_schema`` against the state's CURRENT records: how
        many fit, how many don't, which json paths fail, and up to ten misfit examples."""
        self._ensure_available()
        _validate_schema(new_schema)
        decl = await self._store.get_declaration(name)
        if decl is None:
            raise StateNotFoundError(f"no state declared as {name!r}")
        new_effective_schema = await self._compose_effective(name, new_schema)
        records = await self._store.all_records(name)
        validator = Draft202012Validator(new_effective_schema)
        fits = 0
        misfits = 0
        misfit_fields: dict[str, int] = {}
        examples: list[dict[str, Any]] = []
        for subject in sorted(records, key=lambda s: (s[2], s[3], s[0], s[1])):
            try:
                errors = sorted(validator.iter_errors(records[subject]), key=lambda e: e.json_path)
            except referencing.exceptions.Unresolvable as exc:
                raise SchemaValidationError(f"the candidate schema carries an unresolvable $ref: {exc}") from exc
            if not errors:
                fits += 1
                continue
            misfits += 1
            for path in sorted({err.json_path for err in errors}):
                misfit_fields[path] = misfit_fields.get(path, 0) + 1
            if len(examples) < _PREVIEW_EXAMPLE_CAP:
                examples.append(
                    {
                        "subject": {
                            "target_kind": subject[0],
                            "target_name": subject[1],
                            "kind": subject[2],
                            "key": subject[3],
                        },
                        "errors": [{"path": e.json_path, "message": e.message} for e in errors],
                    }
                )
        return {
            "records": len(records),
            "fits": fits,
            "misfits": misfits,
            "misfit_fields": misfit_fields,
            "examples": examples,
        }

    # -- records -----------------------------------------------------------------

    async def read(self, state: str, subject: StateSubject) -> RecordView | None:
        """The record for ``subject`` (resolving a fold), or ``None`` when none exists. An
        unknown person or a target mismatch is a refusal, never an empty document."""
        self._ensure_available()
        decl = await self._require_declaration_decl(state)
        await self.validate_subject(decl, subject)
        view = await self._store.read_record_view(state, subject)
        if view is None:
            return None
        return RecordView(
            state=state,
            subject=subject,
            data=view["data"],
            seq=view["seq"],
            canonical_subject=view["canonical_subject"],
            folded_from=view["folded_from"],
        )

    async def replace(
        self, state: str, subject: StateSubject, data: dict[str, Any], *, origin: WriteOrigin
    ) -> RecordView:
        """Replace ``subject``'s whole document with ``data`` and return the new record."""
        self._ensure_available()
        if not isinstance(data, dict):
            raise ValueValidationError("a record document must be a JSON object")
        decl = await self._require_declaration_decl(state)
        await self.validate_subject(decl, subject)
        completed = self._complete_origin(origin)
        await self._store.replace(state, subject, data, origin=completed, validate_doc=_validate_document)
        view = await self.read(state, subject)
        assert view is not None  # a record was just written
        return view

    async def merge(
        self, state: str, subject: StateSubject, patch: dict[str, Any], *, origin: WriteOrigin
    ) -> RecordView:
        """Shallow top-level merge ``patch`` into ``subject``'s document — one ``set`` op
        per top-level key, applied atomically under the record lock — and return the new
        record."""
        self._ensure_available()
        if not isinstance(patch, dict):
            raise ValueValidationError("a merge patch must be a JSON object")
        ops = [{"op": "set", "path": [k], "value": v} for k, v in patch.items()]
        await self.apply(state, subject, ops, op_id=None, origin=origin)
        view = await self.read(state, subject)
        if view is None:
            # An empty patch touched nothing and no record exists — represent the still-empty
            # document rather than inventing a write.
            return RecordView(state=state, subject=subject, data={}, seq=0.0, canonical_subject=subject)
        return view

    async def apply(
        self,
        state: str,
        subject: StateSubject,
        ops: list[dict[str, Any]],
        *,
        op_id: str | None,
        origin: WriteOrigin,
    ) -> ApplyResult:
        """Apply an op batch to ``subject``'s document under the effective schema. Refuses a
        composing-path shape violation before the ledger insert, stamps ``_trace`` under a
        traced mount, and records one write row. A replayed ``op_id`` returns
        ``applied=False``; guarded ops land in ``skipped``."""
        self._ensure_available()
        if not isinstance(ops, list):
            raise InvalidPathError("ops must be a list of operations")
        if not ops:
            return ApplyResult(applied=False, data=None, seq=None, skipped=[])
        for i, op in enumerate(ops):
            validate_op(op, where=f"ops[{i}]")
        decl = await self._require_declaration_decl(state)
        await self.validate_subject(decl, subject)
        completed = self._complete_origin(origin)
        applied, data, seq, skipped = await self._store.apply_ops(
            state,
            subject,
            ops,
            op_id=op_id,
            origin=completed,
            validate_doc=_validate_document,
            retention_days=store_settings_retention(),
        )
        return ApplyResult(
            applied=applied,
            data=data,
            seq=seq,
            skipped=[{"op": op.get("op"), "path": op.get("path"), "reason": "guard"} for op in skipped],
        )

    async def erase(self, state: str, subject: StateSubject, *, origin: WriteOrigin) -> None:
        """Erase ``subject``'s record, recording the write."""
        self._ensure_available()
        decl = await self._require_declaration_decl(state)
        await self.validate_subject(decl, subject)
        completed = self._complete_origin(origin)
        await self._store.erase_subject(state, subject, origin=completed)

    async def fold(
        self, state: str, subject: StateSubject, into: StateSubject, mode: str, *, origin: WriteOrigin
    ) -> dict[str, Any]:
        """Fold ``subject`` into ``into`` (``switch`` drops, ``merge`` combines; survivor
        wins) and return the fold report."""
        self._ensure_available()
        if mode not in ("switch", "merge"):
            raise SubjectFoldError(f"unknown fold mode {mode!r} (supported: merge, switch)")
        decl = await self._require_declaration_decl(state)
        await self.validate_subject(decl, subject)
        await self.validate_subject(decl, into)
        completed = self._complete_origin(origin)
        return await self._store.fold_subject(
            state, subject, into, mode, origin=completed, validate_doc=_validate_document
        )

    async def list_subjects(
        self, state: str, *, kind: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        """One keyset page of a state's subjects, ordered by the full subject identity
        ``(target_kind, target_name, kind, key)``."""
        self._ensure_available()
        page = _page_limit(limit)
        if await self._store.get_declaration(state) is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        rows = await self._store.list_subjects(state, kind=kind, limit=page, cursor=cursor)
        next_cursor = (
            make_cursor(
                rows[-1]["target_kind"], rows[-1]["target_name"], rows[-1]["subject_kind"], rows[-1]["subject_key"]
            )
            if len(rows) == page
            else None
        )
        return {"subjects": [_subject_from_row(state, r) for r in rows], "next_cursor": next_cursor}

    async def search(
        self, state: str, filters: dict[str, Any], *, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        """Content search — the subjects whose record data CONTAINS ``filters`` (a JSONB
        containment document, matched with ``data @> filters``). A non-object or empty
        ``filters`` is a loud client error."""
        self._ensure_available()
        page = _page_limit(limit)
        if not isinstance(filters, dict) or not filters:
            raise ValueValidationError("search needs a non-empty filters object (a JSONB containment document)")
        if await self._store.get_declaration(state) is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        rows = await self._store.search_records(state, filters, limit=page, cursor=cursor)
        next_cursor = (
            make_cursor(
                rows[-1]["target_kind"], rows[-1]["target_name"], rows[-1]["subject_kind"], rows[-1]["subject_key"]
            )
            if len(rows) == page
            else None
        )
        return {"matches": [_subject_from_row(state, r) for r in rows], "next_cursor": next_cursor}

    async def writes(
        self, state: str, subject: StateSubject, *, limit: int | None = None, cursor: str | None = None
    ) -> WritesPage:
        """One keyset page of ``subject``'s audit trail, newest first — the ``items`` (each
        a write with its completed origin and touched paths) and the ``next_cursor`` the
        next call pages from (the last row's id when the page is full, else ``None``)."""
        self._ensure_available()
        page = _page_limit(limit)
        if cursor is not None:
            try:
                int(cursor)
            except (TypeError, ValueError):
                raise ValueValidationError(f"writes cursor must be a row id (an integer), got {cursor!r}") from None
        rows = await self._store.writes(state, subject, limit=page, cursor=cursor)
        items = [
            WriteEntry(
                seq=row["seq"] if row["seq"] is not None else 0.0,
                at=row["at"],
                origin=CompletedOrigin(
                    consumer=row["consumer"],
                    meta=row["meta"],
                    run_id=row["run_id"],
                    op_id=row["op_id"],
                    door=row["door"],
                    actor=row["actor"],
                    turn_id=row["turn_id"],
                ),
                paths=[list(p) for p in (row["paths"] or [])],
            )
            for row in rows
        ]
        next_cursor = str(rows[-1]["id"]) if len(rows) == page else None
        return WritesPage(items=items, next_cursor=next_cursor)

    async def prune_expired(self) -> dict[str, int]:
        """The explicit retention sweep — delete every record past its state's effective
        retention. A misconfigured global default is refused loudly before any delete."""
        self._ensure_available()
        default = store_settings_default_retention()
        if default is not None and (isinstance(default, bool) or default < 1 or default > MAX_RETENTION_DAYS):
            raise ValueValidationError(
                f"STATES_DEFAULT_RETENTION_DAYS must be a positive integer ≤ {MAX_RETENTION_DAYS} or unset, "
                f"got {default!r}"
            )
        counts = await self._store.prune_expired(default)
        if counts:
            logger.info(
                "states retention prune: deleted %d record(s) across %d state(s)", sum(counts.values()), len(counts)
            )
        return counts

    # -- bulk import -------------------------------------------------------------

    async def import_records(self, state: str, rows: Sequence[dict[str, Any]], *, origin: WriteOrigin) -> None:
        """Import record rows for ``state`` under the completed origin, validating each
        document against the effective schema.

        EVERY row's subject is validated (declared kind, non-empty key, and — for kind
        ``person`` — a known person of the row's target) through :meth:`validate_subject`
        BEFORE any write; a refusal names the offending row index and its subject and
        nothing is written, so a backup restore or a state transfer never lands records
        under an undeclared kind or an unknown person (§4.10 transfer step 2)."""
        self._ensure_available()
        from pydantic import ValidationError

        decl = await self._require_declaration_decl(state)
        row_list = list(rows)
        for index, row in enumerate(row_list):
            try:
                subject = StateSubject(
                    target_kind=row["target_kind"],
                    target_name=row["target_name"],
                    kind=row["subject_kind"],
                    key=row["subject_key"],
                )
            except ValidationError as exc:
                raise SubjectRefusedError(
                    f"import row {index}: malformed subject "
                    f"{row.get('target_kind')!r}/{row.get('target_name')!r}/"
                    f"{row.get('subject_kind')!r}/{row.get('subject_key')!r}: {exc}"
                ) from exc
            try:
                await self.validate_subject(decl, subject)
            except SubjectRefusedError as exc:
                raise SubjectRefusedError(f"import row {index}: {exc}") from exc
        completed = self._complete_origin(origin)
        await self._store.import_records(state, row_list, origin=completed, validate_doc=_validate_document)

    async def import_aliases(self, state: str, rows: Sequence[dict[str, Any]], *, origin: WriteOrigin) -> None:
        """Import subject-alias rows for ``state`` verbatim (identity, not a write)."""
        self._ensure_available()
        await self._store.import_aliases(state, list(rows))

    async def import_applied_ops(self, rows: Sequence[dict[str, Any]]) -> None:
        """Import applied-op ledger rows verbatim (the idempotency keys carry no subject)."""
        self._ensure_available()
        await self._store.import_applied_ops(list(rows))

    # -- modules -----------------------------------------------------------------

    async def list_modules(self) -> list[StateModuleDocument]:
        self._ensure_available()
        return [StateModuleDocument.model_validate(row["body"]) for row in await self._store.list_modules()]

    async def list_modules_catalog(self) -> list[dict[str, Any]]:
        """The module-catalog projection the ``GET /api/state-modules`` list serves: each
        stored document plus ``mounted_on`` (the number of states the module is mounted on)
        and ``shipped_default`` (true when the module carries a seed ``shipped_hash`` — an
        unedited shipped default). The mount counts are one query over every module."""
        self._ensure_available()
        counts = await self._store.mounted_module_counts()
        catalog: list[dict[str, Any]] = []
        for row in await self._store.list_modules():
            document = StateModuleDocument.model_validate(row["body"]).model_dump()
            document["mounted_on"] = counts.get(row["name"], 0)
            document["shipped_default"] = row["shipped_hash"] is not None
            catalog.append(document)
        return catalog

    async def get_module(self, name: str) -> StateModuleDocument | None:
        self._ensure_available()
        row = await self._store.get_module(name)
        if row is None:
            return None
        self._validated_module(row)  # loud on a corrupt stored body
        return StateModuleDocument.model_validate(row["body"])

    async def put_module(self, doc: StateModuleDocument, *, replace: bool) -> StateModuleDocument:
        """Store a module document, running every registered mount validator over each live
        mount before the write (a raise leaves the stored document untouched); overwriting
        an existing name without ``replace`` raises :class:`ModuleExistsError`."""
        self._ensure_available()
        # ``exclude_none`` drops an unset ``declarations`` (None) so the deep validator
        # sees the same absent-key shape ``to_document`` emits, never a null section.
        body = doc.model_dump(by_alias=True, exclude_none=True)
        module = validate_module(body)
        existing = await self._store.get_module(module.name)
        if existing is not None and not replace:
            raise ModuleExistsError(f"module {module.name!r} already exists — upload with replace=true to overwrite it")
        module_doc = StateModuleDocument.model_validate(module.to_document())
        mount_rows = await self._store.list_mounts_of_module(module.name)
        for row in mount_rows:
            mount_declarations = dict(row["declarations"] or {})
            resolved = self._effective_parameters(module, dict(row["parameters"] or {}))
            try:
                await self._validate_mount_values(module, resolved, mount_declarations)
                effective = compose_effective_schema(
                    (await self._require_declaration(row["state"]))["schema"],
                    [
                        (m, p, pa)
                        for m, p, pa, _d in await self._load_state_mounts(row["state"], override={module.name: module})
                    ],
                )
                await self._run_mount_validators(module_doc, mount_declarations, effective)
            except ModuleValidationError as exc:
                raise ModuleInUseError(
                    f"module {module.name!r} cannot be replaced: its mount on state {row['state']!r} no longer "
                    f"validates: {exc}"
                ) from exc
        await self._store.upsert_module(module.name, module.to_document(), None)
        for row in mount_rows:
            resolved = self._effective_parameters(module, dict(row["parameters"] or {}))
            base_schema = (await self._require_declaration(row["state"]))["schema"]
            effective = await self._compose_effective(row["state"], base_schema)
            await self._store.update_mount_parameters(row["state"], module.name, resolved, effective_schema=effective)
        return module_doc

    async def delete_module(self, name: str) -> None:
        """Delete a module document; refused while it is mounted."""
        self._ensure_available()
        if await self._store.get_module(name) is None:
            raise StateNotFoundError(f"no module {name!r}")
        mounts = await self._store.list_mounts_of_module(name)
        if mounts:
            states = ", ".join(sorted(m["state"] for m in mounts))
            raise ModuleInUseError(f"module {name!r} is mounted on state(s) {states} — unmount it first")
        await self._store.delete_module(name)

    # -- mounts ------------------------------------------------------------------

    async def list_mounts(self, state: str | None = None, *, module: str | None = None) -> list[dict[str, Any]]:
        self._ensure_available()
        if state is not None and module is not None:
            row = await self._store.get_mount(state, module)
            rows = [] if row is None else [row]
        elif state is not None:
            rows = await self._store.list_mounts_for_state(state)
        elif module is not None:
            rows = await self._store.list_mounts_of_module(module)
        else:
            rows = await self._store.list_all_mounts()
        return [
            {
                "state": r["state"],
                "module": r["module"],
                "path": list(r["path"]),
                "parameters": dict(r["parameters"] or {}),
                "declarations": dict(r["declarations"] or {}),
            }
            for r in rows
        ]

    async def mount(self, state: str, module_name: str, body: MountBody) -> None:
        """Mount a module on a state: validate path/parameters/declarations (+ check), run
        every registered mount validator over the composed effective schema, then store the
        resolved parameters and the recomposed effective schema in one transaction (D-5 —
        nothing derived is materialized)."""
        self._ensure_available()
        path = list(body.path)
        parameters = dict(body.parameters or {})
        declarations = dict(body.declarations or {})
        decl = await self._require_declaration(state)
        module = await self._get_module_or_raise(module_name)
        self._validate_mount_path(path)
        if await self._store.get_mount(state, module_name) is not None:
            raise MountConflictError(
                f"module {module_name!r} is already mounted on state {state!r} — unmount it to change path/parameters"
            )
        await self._validate_mount_values(module, parameters, declarations)
        resolved = self._effective_parameters(module, parameters)
        existing = await self._load_state_mounts(state)
        effective = compose_effective_schema(
            decl["schema"], [*[(m, p, pa) for m, p, pa, _d in existing], (module, list(path), resolved)]
        )
        _validate_schema(effective)
        module_doc = StateModuleDocument.model_validate(module.to_document())
        await self._run_mount_validators(module_doc, declarations, effective)
        await self._store.upsert_mount(state, module_name, path, resolved, declarations, effective_schema=effective)

    async def update_mount_declarations(self, state: str, module_name: str, declarations: dict[str, Any]) -> None:
        """Rewrite a mount's declarations only, re-running every registered mount validator
        and recomposing the effective schema before the write."""
        self._ensure_available()
        declarations = dict(declarations or {})
        row = await self._store.get_mount(state, module_name)
        if row is None:
            raise StateNotFoundError(f"module {module_name!r} is not mounted on state {state!r}")
        module = await self._get_module_or_raise(module_name)
        await self._validate_mount_values(module, dict(row["parameters"] or {}), declarations)
        effective = await self._compose_effective(state, (await self._require_declaration(state))["schema"])
        module_doc = StateModuleDocument.model_validate(module.to_document())
        await self._run_mount_validators(module_doc, declarations, effective)
        await self._store.update_mount_declarations(state, module_name, declarations, effective_schema=effective)

    async def unmount(self, state: str, module_name: str) -> None:
        """Remove a mount and recompose the state's effective schema (D-5 — nothing else)."""
        self._ensure_available()
        if await self._store.get_mount(state, module_name) is None:
            raise StateNotFoundError(f"module {module_name!r} is not mounted on state {state!r}")
        decl = await self._require_declaration(state)
        remaining = [(m, p, pa) for m, p, pa, _d in await self._load_state_mounts(state) if m.name != module_name]
        effective = compose_effective_schema(decl["schema"], remaining)
        await self._store.delete_mount(state, module_name, effective_schema=effective)

    async def effective_schema_for(self, state: str) -> dict[str, Any]:
        """The stored effective schema (base + every mount's fragment) for a declared
        state — the schema every document validation reads."""
        self._ensure_available()
        return (await self._require_declaration(state))["effective_schema"]

    async def served_declaration(self, name: str) -> dict[str, Any]:
        """The full served declaration read: ``schema`` (base), ``effective_schema``,
        ``subject_kinds``, ``default_subject_kind``, ``retention_days`` (``None`` when the
        state keeps records forever), ``mounts[]``, ``regimes[]`` (the absolute regime paths
        every mount declares) and ``updated_at`` (the ISO timestamp of the last write) — the
        one read a consumer's bind-time checks and the Studio's fields view consume. Carries
        the same fields the list read dumps, so an edit form round-trips a declaration
        (``retention_days`` included) without dropping any."""
        self._ensure_available()
        decl = await self._require_declaration(name)
        mounts = await self._load_state_mounts(name)
        regimes = self._compose_regimes(mounts)
        # Serialize ``updated_at`` through the same model dump the list read uses, so both
        # reads render the timestamp identically (pydantic's ISO ``…Z``), never two formats.
        updated_at = _row_to_declaration(decl).model_dump(mode="json")["updated_at"]
        return {
            "name": decl["name"],
            "description": decl.get("description") or "",
            "schema": decl["schema"],
            "effective_schema": decl["effective_schema"],
            "subject_kinds": list(decl["subject_kinds"]),
            "default_subject_kind": decl["default_subject_kind"],
            "retention_days": decl["retention_days"],
            "mounts": [
                {"module": m.name, "path": list(p), "parameters": dict(pa), "declarations": dict(d)}
                for m, p, pa, d in mounts
            ],
            "regimes": regimes,
            "updated_at": updated_at,
        }

    def regime_for_path(self, module: StateModule, relative_path: list[Any]) -> str:
        """The regime governing ``relative_path`` in ``module`` — exposed for a consumer's
        bind-time single-writer check."""
        return regime_for(module, relative_path)

    # -- consumers ---------------------------------------------------------------

    def register_consumer_lister(self, kind: str, lister: ConsumerLister) -> None:
        self._consumer_listers.register(kind, lister)

    async def consumers(self, state: str) -> list[ConsumerRow]:
        """Everything that binds ``state`` — the union of every registered consumer
        lister."""
        self._ensure_available()
        rows: list[ConsumerRow] = []
        for lister in self._consumer_listers.all().values():
            rows.extend(await lister(state))
        return rows

    # -- mount validators --------------------------------------------------------

    def register_mount_validator(self, validator: MountValidator) -> None:
        self._mount_validators.register(validator)

    async def _run_mount_validators(
        self, module_doc: StateModuleDocument, declarations: dict[str, Any], effective: dict[str, Any]
    ) -> None:
        """Run every registered mount validator with the module document, the mount's
        declaration values, and the state's effective schema — BEFORE any write. A validator
        raises loudly (a :class:`ModuleValidationError`) to refuse the door."""
        for validator in self._mount_validators.all():
            await validator(module_doc, declarations, effective)

    # -- seeds -------------------------------------------------------------------

    def register_module_seed(self, doc: StateModuleDocument) -> None:
        self._seeds.register(doc)

    def register_retired_module_name(self, name: str) -> None:
        self._seeds.register_retired(name)

    async def apply_module_seeds(self) -> None:
        """Reconcile the shipped module seeds against the store (a no-op while the feature
        is off)."""
        if not states_store_configured():
            return
        from tai42_skeleton.states.seeds import apply_module_seeds

        await apply_module_seeds(self._store, seeds=self._seeds.seeds(), retired=self._seeds.retired())

    # -- module/mount helpers ----------------------------------------------------

    async def _require_declaration(self, state: str) -> dict[str, Any]:
        decl = await self._store.get_declaration(state)
        if decl is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        return decl

    async def _require_declaration_decl(self, state: str) -> StateDeclaration:
        return _row_to_declaration(await self._require_declaration(state))

    async def _get_module_or_raise(self, name: str) -> StateModule:
        row = await self._store.get_module(name)
        if row is None:
            raise StateNotFoundError(f"no module {name!r}")
        return self._validated_module(row)

    async def _load_state_mounts(
        self, state: str, *, override: dict[str, StateModule] | None = None
    ) -> list[tuple[StateModule, list[str], dict[str, Any], dict[str, Any]]]:
        """Every mount on the state as ``(module, path, parameters, declarations)``.
        ``override`` supplies a not-yet-stored module body (a module replace composes
        against the candidate)."""
        override = override or {}
        out: list[tuple[StateModule, list[str], dict[str, Any], dict[str, Any]]] = []
        for row in await self._store.list_mounts_for_state(state):
            module = override.get(row["module"]) or await self._get_module_or_raise(row["module"])
            out.append((module, list(row["path"]), dict(row["parameters"] or {}), dict(row["declarations"] or {})))
        return out

    async def _compose_effective(self, state: str, base_schema: dict[str, Any]) -> dict[str, Any]:
        """The effective schema for ``base_schema`` over the state's CURRENT mounts."""
        mounts = await self._load_state_mounts(state)
        return compose_effective_schema(base_schema, [(m, p, pa) for m, p, pa, _d in mounts])

    @staticmethod
    def _compose_regimes(
        mounts: list[tuple[StateModule, list[str], dict[str, Any], dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """The absolute write-regime rules over already-loaded ``mounts``: each mounted
        module's regime paths prefixed by the mount path. The ONE composition every
        declaration read (``get_declaration``/``list_declarations``) and
        ``served_declaration`` share, so a served regime is identical across doors."""
        regimes: list[dict[str, Any]] = []
        for module, base_path, _params, _decls in mounts:
            for rule in module.regimes:
                regimes.append({"path": [*base_path, *rule.path], "regime": rule.regime})
        return regimes

    def _validate_mount_path(self, path: Any) -> None:
        if not isinstance(path, list):
            raise MountConflictError("mount path must be a list of object keys")
        for seg in path:
            if not isinstance(seg, str) or not seg:
                raise MountConflictError(
                    f"mount path segment {seg!r} must be a non-empty object key (a mount never sits on a list index)"
                )

    @staticmethod
    def _effective_parameters(module: StateModule, parameters: dict[str, Any]) -> dict[str, Any]:
        """The parameter map a mount PERSISTS: the module's defaults overlaid by the
        client's supplied values."""
        return {**module.defaults(), **dict(parameters or {})}

    async def _validate_mount_values(
        self, module: StateModule, parameters: dict[str, Any], declarations: dict[str, Any]
    ) -> None:
        """Validate a mount's parameter values against each parameter's schema (every
        no-default parameter supplied) and its declarations against the module's
        declarations schema and optional ``check`` predicate. Loud on the first failure."""
        for name, value in parameters.items():
            param = module.parameters.get(name)
            if param is None:
                raise ModuleValidationError(f"mount supplies unknown parameter {name!r} for module {module.name!r}")
            try:
                Draft202012Validator(param.schema).validate(value)
            except jsonschema.ValidationError as exc:
                raise ModuleValidationError(f"mount parameter {name!r} is invalid: {exc.message}") from exc
        for name, param in module.parameters.items():
            if not param.has_default and name not in parameters:
                raise ModuleValidationError(
                    f"mount of module {module.name!r} must supply parameter {name!r} (it has no default)"
                )
        if module.declarations is None:
            if declarations:
                raise ModuleValidationError(
                    f"module {module.name!r} declares no declarations section, so none may be supplied"
                )
            return
        try:
            Draft202012Validator(module.declarations.schema).validate(declarations)
        except jsonschema.ValidationError as exc:
            raise ModuleValidationError(
                f"mount declarations are invalid under module {module.name!r}: {exc.message}"
            ) from exc
        if module.declarations.check is not None:
            try:
                result = await run_jq_first(module.declarations.check, declarations)
            except Exception as exc:
                raise ModuleValidationError(
                    f"module {module.name!r} declarations check failed to evaluate: {exc}"
                ) from exc
            if result is not True:
                message = result if isinstance(result, str) else "the declarations violate the module's check rule"
                raise ModuleValidationError(f"mount declarations rejected by module {module.name!r}: {message}")


# --------------------------------------------------------------------------- #
# Migrate converters                                                            #
# --------------------------------------------------------------------------- #
async def _no_conversion(records: dict[Any, Any]) -> dict[Any, Any]:
    """Additive migrate: no record is rewritten."""
    return {}


def _transform_converter(transform_expr: str, new_effective_schema: dict[str, Any]):
    """Build the async converter that runs ``transform_expr`` over every record, validating
    each result WHOLE against the new EFFECTIVE schema; any failure aborts the migrate."""

    async def convert(records: dict[Any, Any]) -> dict[Any, Any]:
        converted: dict[Any, Any] = {}
        for subject, data in records.items():
            try:
                result = await run_jq_first(transform_expr, data)
            except Exception as exc:
                raise MigrationConversionError(f"transform_expr failed on subject {subject!r}: {exc}") from exc
            if not isinstance(result, dict):
                raise MigrationConversionError(f"transform_expr produced a non-object for subject {subject!r}")
            try:
                _validate_document(new_effective_schema, result)
            except ValueValidationError as exc:
                raise MigrationConversionError(
                    f"converted record for subject {subject!r} is invalid under the new schema: {exc}"
                ) from exc
            converted[subject] = result
        return converted

    return convert


def _validate_resolutions(resolutions: Any) -> list[dict[str, Any]]:
    """Shape-check a migrate ``resolutions`` list at the door, loudly. Each is
    ``{"path": [seg…], "action": "drop"}`` or ``{"path": [seg…], "action": "default",
    "value": …}``; ``"-"`` is refused (a resolution addresses existing data)."""
    if not isinstance(resolutions, list):
        raise InvalidPathError("resolutions must be a list")
    out: list[dict[str, Any]] = []
    for i, r in enumerate(resolutions):
        where = f"resolutions[{i}]"
        if not isinstance(r, dict):
            raise InvalidPathError(f"{where}: a resolution must be a JSON object, got {r!r}")
        action = r.get("action")
        if action not in ("drop", "default"):
            raise InvalidPathError(f"{where}: unknown action {action!r} (supported: ['default', 'drop'])")
        validate_path(r.get("path"), where=where)
        if any(seg == APPEND for seg in r["path"]):
            raise InvalidPathError(
                f"{where}: '-' (append) has no meaning in a resolution path — it addresses existing data"
            )
        if action == "default" and "value" not in r:
            raise InvalidPathError(f"{where}: a default resolution requires a 'value'")
        if action == "drop" and "value" in r:
            raise InvalidPathError(f"{where}: a drop resolution takes no 'value'")
        extra = set(r) - {"path", "action", "value"}
        if extra:
            raise InvalidPathError(f"{where}: resolution carries unknown keys {sorted(extra)}")
        out.append(r)
    return out


def _resolutions_converter(resolutions: list[dict[str, Any]], new_effective_schema: dict[str, Any]):
    """Build the async converter for the declarative resolutions door: a record already
    valid under the new schema is untouched; an invalid one gets every resolution applied
    and is re-validated (still invalid ⇒ abort)."""
    ops = [
        {"op": "remove", "path": r["path"]}
        if r["action"] == "drop"
        else {"op": "set", "path": r["path"], "value": r["value"]}
        for r in resolutions
    ]

    async def convert(records: dict[Any, Any]) -> dict[Any, Any]:
        converted: dict[Any, Any] = {}
        for subject, data in records.items():
            try:
                _validate_document(new_effective_schema, data)
                continue
            except ValueValidationError:
                pass
            try:
                fixed = apply_path_ops(data, ops)
            except InvalidPathError as exc:
                raise MigrationConversionError(f"resolutions failed on subject {subject!r}: {exc}") from exc
            try:
                _validate_document(new_effective_schema, fixed)
            except ValueValidationError as exc:
                raise MigrationConversionError(
                    f"subject {subject!r} is still invalid after the resolutions ({exc}); use transform_expr "
                    "to reshape it instead"
                ) from exc
            converted[subject] = fixed
        return converted

    return convert


def _drop_converter(old_schema: dict[str, Any], new_schema: dict[str, Any], new_effective_schema: dict[str, Any]):
    """Build the async converter that drops removed/changed top-level fields from every
    record — validating the RESULT whole against the new EFFECTIVE schema."""
    old_props = old_schema.get("properties", {}) if isinstance(old_schema, dict) else {}
    new_props = new_schema.get("properties", {})
    kept = {name for name, fschema in old_props.items() if _canonical(new_props.get(name)) == _canonical(fschema)}

    async def convert(records: dict[Any, Any]) -> dict[Any, Any]:
        converted: dict[Any, Any] = {}
        for subject, data in records.items():
            dropped = {k: v for k, v in data.items() if k in kept}
            try:
                _validate_document(new_effective_schema, dropped)
            except ValueValidationError as exc:
                raise MigrationConversionError(
                    f"dropping the changed fields leaves subject {subject!r} invalid under the new schema "
                    f"({exc}); use transform_expr to reshape the records instead"
                ) from exc
            converted[subject] = dropped
        return converted

    return convert


__all__ = [
    "StatesConsumerListerRegistry",
    "StatesMountValidatorRegistry",
    "StatesService",
    "current_state_context",
    "state_context",
]
