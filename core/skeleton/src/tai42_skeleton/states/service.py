"""The one validate + apply layer over the subject-keyed record store — the platform
half of the state feature.

Holds a :class:`~tai42_skeleton.states.store.PostgresStatesStore`; every door refuses
loudly (:class:`~tai42_contract.states.errors.StatesNotConfiguredError`, 501) while the
``states`` component's database is unbound. The service owns subject validation (the
``person`` kind against the identity store), the effective-schema composer, the template
lifecycle, and the WRITE-PROVENANCE CHOKEPOINT: it completes a consumer's
:class:`~tai42_contract.states.WriteOrigin` into a
:class:`~tai42_contract.states.CompletedOrigin` — stamping ``door``/``actor``/``turn_id``
from the ambient :class:`~tai42_contract.states.StateContext` (or ``api`` + the request
principal with none) — so the audit ledger is never optional or forgeable.

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
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote

if TYPE_CHECKING:
    from tai42_skeleton.states.seeds import StateTemplateSeedRegistry

import jsonschema
import referencing.exceptions
from jsonschema import Draft202012Validator
from psycopg import AsyncConnection
from tai42_contract.states.errors import (
    AttachConflictError,
    DeclarationInUseError,
    InvalidPathError,
    NonAdditiveRedeclareError,
    SchemaValidationError,
    StateNotFoundError,
    StatesNotConfiguredError,
    SubjectFoldError,
    SubjectRefusedError,
    TemplateExistsError,
    TemplateInUseError,
    TemplateValidationError,
    ValueValidationError,
)
from tai42_contract.states.models import (
    MAX_RETENTION_DAYS,
    PERSON_KIND,
    ApplyResult,
    AttachBody,
    AttachReconcileContext,
    AttachReconciler,
    AttachValidator,
    CompletedOrigin,
    ConsumerLister,
    ConsumerRow,
    StateContext,
    StateDeclaration,
    StateRecord,
    StateSubject,
    StateTemplateDocument,
    TemplateJqApplyResult,
    TemplateJqResult,
    WriteEntry,
    WriteOrigin,
    WritesPage,
)
from tai42_kit.utils.data.jq_util import run_jq_first

from tai42_skeleton.states.context import current_state_context, state_context
from tai42_skeleton.states.db import states_store_configured
from tai42_skeleton.states.paths import validate_op
from tai42_skeleton.states.store import (
    PostgresStatesStore,
    make_cursor,
    store_settings_default_retention,
    store_settings_retention,
)
from tai42_skeleton.states.templates import (
    StateTemplate,
    TemplateReconcile,
    compose_effective_schema,
    regime_for,
    template_jq_prelude,
    validate_template,
)

logger = logging.getLogger(__name__)

_DEFAULT_PAGE = 200
_MAX_PAGE = 500


# --------------------------------------------------------------------------- #
# Consumer-owned registries (per-app, reset each start() by the server)          #
# --------------------------------------------------------------------------- #
class StatesAttachValidatorRegistry:
    """The process-wide attach-validator registry — the body behind
    ``app.states.register_attach_validator``. A consumer registers a data-dependent
    validator when its module loads; the attach doors consult every registered validator
    before any write. Reset each ``start()`` so a reload re-registers cleanly."""

    def __init__(self) -> None:
        self._validators: list[AttachValidator] = []

    def register(self, validator: AttachValidator) -> None:
        self._validators.append(validator)

    def all(self) -> list[AttachValidator]:
        return list(self._validators)

    def reset(self) -> None:
        self._validators.clear()


class StatesAttachReconcilerRegistry:
    """The process-wide attach-reconciler registry — the body behind
    ``app.states.register_attach_reconciler``. A consumer registers a pre-write reconciler
    when its module loads; the attach doors run every registered reconciler after the
    validators and before the write. Reset each ``start()`` so a reload re-registers
    cleanly."""

    def __init__(self) -> None:
        self._reconcilers: list[AttachReconciler] = []

    def register(self, reconciler: AttachReconciler) -> None:
        self._reconcilers.append(reconciler)

    def all(self) -> list[AttachReconciler]:
        return list(self._reconcilers)

    def reset(self) -> None:
        self._reconcilers.clear()


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
            for raw in unquote(ref[2:]).split("/"):
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
    property-subtree edit or root-keyword edit registers as narrowing."""
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


class _AttachReconcileRecords:
    """The narrow record door an attach reconciler reads and writes through — the
    :class:`~tai42_contract.states.AttachReconcileRecords` handle bound to one state and the
    attach's transaction ``conn``. Every call runs on that transaction: ``merge`` and the
    keyed ``apply`` write on it (so a reconciler's resolution commits with the attach or
    rolls back with a refusal),
    and ``read``/``list_subjects`` read on it too, so a reconciler sees its own in-flight
    merges. Writes are completed and audited through the service chokepoint exactly like any
    facet write."""

    def __init__(self, service: StatesService, state: str, conn: AsyncConnection[Any]) -> None:
        self._service = service
        self._state = state
        self._conn = conn

    async def read(self, subject: StateSubject) -> StateRecord | None:
        return await self._service.read(self._state, subject, conn=self._conn)

    async def list_subjects(
        self, *, kind: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        return await self._service.list_subjects(self._state, kind=kind, limit=limit, cursor=cursor, conn=self._conn)

    async def merge(self, subject: StateSubject, patch: dict[str, Any], *, origin: WriteOrigin) -> StateRecord:
        """Shallow top-level merge ``patch`` into ``subject`` on the attach transaction."""
        if not isinstance(patch, dict):
            raise ValueValidationError("a merge patch must be a JSON object")
        ops = [{"op": "set", "path": [k], "value": v} for k, v in patch.items()]
        result = await self._service.apply(self._state, subject, ops, op_id=None, origin=origin, conn=self._conn)
        data = result.data if result.data is not None else {}
        seq = result.seq if result.seq is not None else 0.0
        return StateRecord(state=self._state, subject=subject, data=data, seq=seq, canonical_subject=subject)

    async def apply(self, subject: StateSubject, ops: list[dict[str, Any]], *, origin: WriteOrigin) -> ApplyResult:
        """Apply an op batch (the same keyed ops as an update-purpose program) to ``subject``
        on the attach transaction — so a record under a ``composing`` write regime can be
        closed with a keyed op that ``merge``'s whole-path set would refuse."""
        return await self._service.apply(self._state, subject, ops, op_id=None, origin=origin, conn=self._conn)


def _record_subtree(data: dict[str, Any], path: list[str]) -> dict[str, Any]:
    """The record document at an attachment's ``path`` — the subtree a template's jq programs
    operate over (``.`` at the seam). An absent or non-object node reads as ``{}``."""
    node: Any = data
    for seg in path:
        if not isinstance(node, dict):
            return {}
        node = node.get(seg)
    return node if isinstance(node, dict) else {}


def _rebase_op(op: Any, path: list[str]) -> dict[str, Any]:
    """One template-relative op with its ``path`` rebased under the attachment ``path``, so an
    update program (like a fill) authors its ops in its own coordinates. A malformed op is a
    loud refusal."""
    if not isinstance(op, dict) or not isinstance(op.get("path"), list):
        raise ValueValidationError(f"an update-program op must be an object carrying a list path, got {op!r}")
    return {**op, "path": [*path, *op["path"]]}


# One record page and the orphan-listing cap keep the reconcile refusal message and its read
# loop bounded on a state with many subjects.
_RECONCILE_PAGE = 200
_RECONCILE_LIST_CAP = 20
# Every reconcile close write carries this generic origin — a consumer name, never a template.
_RECONCILE_ORIGIN = WriteOrigin(consumer="attach-reconcile", meta={"origin": "reconcile"})


def _reconcile_refusal(context: AttachReconcileContext, orphans: list[tuple[StateSubject, dict[str, Any]]]) -> str:
    shown = orphans[:_RECONCILE_LIST_CAP]
    listed = "; ".join(
        f"[{subject.kind}:{subject.key}] {item.get('label', item.get('id'))} (id {item.get('id')})"
        for subject, item in shown
    )
    more = len(orphans) - len(shown)
    if more > 0:
        listed = f"{listed}; … and {more} more"
    return (
        f"re-attaching template {context.template.name!r} on state {context.state!r} would orphan "
        f"{len(orphans)} open record item(s) the new declarations no longer cover: {listed}. "
        'Re-attach with options {"orphans": "close", "resolution": "<not-done resolution>"} to close them.'
    )


def _reconcile_orphans_extra(orphans: list[tuple[StateSubject, dict[str, Any]]]) -> dict[str, Any]:
    """The reconcile refusal's STRUCTURED payload: the orphaned records (subject key/kind +
    the orphan item's id/label) so a UI keys its resolve step on the data, not the prose.
    ``reconcile`` flags the one refusal that has the close-with-resolution follow-up."""
    return {
        "reconcile": True,
        "orphans": [
            {
                "subject": subject.key,
                "kind": subject.kind,
                "id": item.get("id"),
                "label": item.get("label", item.get("id")),
            }
            for subject, item in orphans
        ],
    }


class StatesService:
    """The one validate + apply layer. Holds a store and the consumer-owned registries;
    every method refuses loudly while the feature is off."""

    _TEMPLATE_CACHE_MAX = 256

    def __init__(
        self,
        store: PostgresStatesStore | None = None,
        *,
        attach_validators: StatesAttachValidatorRegistry | None = None,
        attach_reconcilers: StatesAttachReconcilerRegistry | None = None,
        consumer_listers: StatesConsumerListerRegistry | None = None,
        seeds: StateTemplateSeedRegistry | None = None,
    ) -> None:
        from tai42_skeleton.states.seeds import StateTemplateSeedRegistry

        self._store = store or PostgresStatesStore()
        self._attach_validators = attach_validators or StatesAttachValidatorRegistry()
        self._attach_reconcilers = attach_reconcilers or StatesAttachReconcilerRegistry()
        self._consumer_listers = consumer_listers or StatesConsumerListerRegistry()
        self._seeds = seeds or StateTemplateSeedRegistry()
        self._template_cache: OrderedDict[tuple[str, Any], StateTemplate] = OrderedDict()
        # The platform's own template-document reconciler: it settles a state's open records
        # against a declarations edit through the template's ``reconcile`` contract. A no-op
        # for a first attach or a template that declares no ``reconcile``, so it is always on.
        self._attach_reconcilers.register(self._reconcile_template_records)

    # -- gate + template cache ---------------------------------------------------

    @staticmethod
    def _ensure_available() -> None:
        if not states_store_configured():
            raise StatesNotConfiguredError(
                "the states feature is off: bind the 'states' component's database (TAI_DB_BINDING_STATES / the "
                "default database) to enable it"
            )

    def _validated_template(self, row: dict[str, Any]) -> StateTemplate:
        """Validate a template row into a :class:`StateTemplate`, memoized on ``(name,
        updated_at)`` — an unchanged row is served from a bounded LRU."""
        key = (row["name"], row["updated_at"])
        cached = self._template_cache.get(key)
        if cached is not None:
            self._template_cache.move_to_end(key)
            return cached
        template = validate_template(row["body"])
        self._template_cache[key] = template
        self._template_cache.move_to_end(key)
        if len(self._template_cache) > self._TEMPLATE_CACHE_MAX:
            self._template_cache.popitem(last=False)
        return template

    # -- subject validation ------------------------------------------------------

    async def validate_subject(self, decl: StateDeclaration, subject: StateSubject) -> None:
        """Refuse a subject that a state's declaration does not admit: an undeclared
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

    # -- write-provenance chokepoint --------------------------------------

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
            regimes = self._compose_regimes(await self._load_state_attachments(decl.name))
            out.append(decl.model_copy(update={"regimes": regimes}))
        return out

    async def get_declaration(self, name: str) -> StateDeclaration | None:
        self._ensure_available()
        row = await self._store.get_declaration(name)
        if row is None:
            return None
        decl = _row_to_declaration(row)
        regimes = self._compose_regimes(await self._load_state_attachments(decl.name))
        return decl.model_copy(update={"regimes": regimes})

    async def put_declaration(self, decl: StateDeclaration) -> StateDeclaration:
        """Create or plain re-declare a state.

        With records present, a re-declare accepts only ADDITIVE schema changes; a removal
        or change of an existing property is refused while records exist
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
                    f"state {decl.name!r} has records: removing or changing a field is refused while records "
                    f"exist — erase them first"
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
        """Delete a state with its records, attachments and aliases; refuses while a registered
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

    # -- records -----------------------------------------------------------------

    async def read(
        self, state: str, subject: StateSubject, *, conn: AsyncConnection[Any] | None = None
    ) -> StateRecord | None:
        """The record for ``subject`` (resolving a fold), or ``None`` when none exists. An
        unknown person or a target mismatch is a refusal, never an empty document. With
        ``conn`` the read joins the caller's transaction (a attach reconciler reading its own
        in-flight merges)."""
        self._ensure_available()
        decl = await self._require_declaration_decl(state)
        await self.validate_subject(decl, subject)
        view = await self._store.read_record_view(state, subject, conn=conn)
        if view is None:
            return None
        return StateRecord(
            state=state,
            subject=subject,
            data=view["data"],
            seq=view["seq"],
            canonical_subject=view["canonical_subject"],
            folded_from=view["folded_from"],
        )

    async def replace(
        self, state: str, subject: StateSubject, data: dict[str, Any], *, origin: WriteOrigin
    ) -> StateRecord:
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
    ) -> StateRecord:
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
            return StateRecord(state=state, subject=subject, data={}, seq=0.0, canonical_subject=subject)
        return view

    async def apply(
        self,
        state: str,
        subject: StateSubject,
        ops: list[dict[str, Any]],
        *,
        op_id: str | None,
        origin: WriteOrigin,
        conn: AsyncConnection[Any] | None = None,
    ) -> ApplyResult:
        """Apply an op batch to ``subject``'s document under the effective schema. Refuses a
        composing-path shape violation before the ledger insert, stamps ``_trace`` under a
        traced attach, and records one write row. A replayed ``op_id`` returns
        ``applied=False``; guarded ops land in ``skipped``. With ``conn`` the write joins
        the caller's transaction (a attach reconciler's resolution)."""
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
            conn=conn,
        )
        return ApplyResult(
            applied=applied,
            data=data,
            seq=seq,
            skipped=[{"op": op.get("op"), "path": op.get("path"), "reason": "guard"} for op in skipped],
        )

    # -- template_jq -------------------------------------------------------------

    async def _resolve_template_jq(
        self, state: str, name: str
    ) -> tuple[StateTemplate, list[str], dict[str, Any], dict[str, Any], str]:
        """Resolve a ``template_jq`` program ``name`` across ``state``'s attachments to
        ``(template, path, parameters, declarations, program_name)``. An UNQUALIFIED name
        resolves to the one attachment whose template declares it — a name two attached
        templates both declare is a loud :class:`ValueValidationError` (the caller qualifies
        it). A QUALIFIED ``<template>.<name>`` resolves to that attachment's template — a
        template not attached on the state is a loud :class:`StateNotFoundError`. An unknown
        program is a :class:`StateNotFoundError`."""
        attachments = await self._load_state_attachments(state)
        if "." in name:
            template_name, program_name = name.split(".", 1)
            for template, path, parameters, declarations in attachments:
                if template.name == template_name:
                    if program_name not in template.template_jq:
                        raise StateNotFoundError(
                            f"template {template_name!r} attached on state {state!r} declares no "
                            f"template_jq {program_name!r}"
                        )
                    return template, path, parameters, declarations, program_name
            raise StateNotFoundError(f"template {template_name!r} is not attached on state {state!r}")
        matches = [
            (template, path, parameters, declarations)
            for template, path, parameters, declarations in attachments
            if name in template.template_jq
        ]
        if not matches:
            raise StateNotFoundError(f"no template_jq {name!r} on any template attached on state {state!r}")
        if len(matches) > 1:
            templates = ", ".join(sorted(t.name for t, _p, _pa, _d in matches))
            raise ValueValidationError(
                f"template_jq {name!r} is declared by more than one template attached on state {state!r} "
                f"({templates}); qualify it as <template>.{name}"
            )
        template, path, parameters, declarations = matches[0]
        return template, path, parameters, declarations, name

    async def eval_template_jq(
        self,
        state: str,
        subject: StateSubject,
        name: str,
        args: dict[str, Any],
        *,
        conn: AsyncConnection[Any] | None = None,
    ) -> TemplateJqResult:
        """Evaluate an ``input``-purpose ``template_jq`` program ``name`` over ``subject``'s
        record and return its value. ``args`` supplies the program's declared ``params`` as
        the single ``$params`` object (every declared key must be present — value may be
        null — and an undeclared key is a loud refusal); the jq runs over the record's
        attached subtree with the attachment's ``$parameters``/``$declarations`` bound and the
        sibling ``tjq_<name>`` input-program prelude. Read-only. An ``update``-purpose name is
        a :class:`ValueValidationError`."""
        self._ensure_available()
        decl = await self._require_declaration_decl(state)
        await self.validate_subject(decl, subject)
        template, path, parameters, declarations, program_name = await self._resolve_template_jq(state, name)
        program = template.template_jq[program_name]
        if program.purpose != "input":
            raise ValueValidationError(
                f"template_jq {program_name!r} on template {template.name!r} has purpose {program.purpose!r}; "
                f"eval needs an 'input'-purpose program (apply an 'update' one instead)"
            )
        missing = sorted(set(program.params) - set(args))
        unknown = sorted(set(args) - set(program.params))
        if missing or unknown:
            raise ValueValidationError(
                f"template_jq {program_name!r} on template {template.name!r} takes params {program.params}"
                + (f", missing {missing}" if missing else "")
                + (f", got unknown {unknown}" if unknown else "")
            )
        record = await self._store.read_record_view(state, subject, conn=conn)
        subtree = _record_subtree(record["data"], path) if record is not None else {}
        variables: dict[str, Any] = {"parameters": parameters, "declarations": declarations, "params": dict(args)}
        try:
            value = await run_jq_first(program.jq, subtree, prelude=template_jq_prelude(template), variables=variables)
        except Exception as exc:
            raise ValueValidationError(
                f"template_jq {program_name!r} on template {template.name!r} failed to evaluate: {exc}"
            ) from exc
        return TemplateJqResult(name=name, value=value)

    async def apply_template_jq(
        self,
        state: str,
        subject: StateSubject,
        name: str,
        input: Any,
        *,
        op_id: str | None,
        origin: WriteOrigin,
        conn: AsyncConnection[Any] | None = None,
    ) -> TemplateJqApplyResult:
        """Apply an ``update``-purpose ``template_jq`` program ``name`` to ``subject``. Its jq
        runs over ``{record, input}`` (the record's attached subtree and the adapter's
        ``input``) with the attachment's ``$parameters``/``$declarations`` bound and the
        sibling ``tjq_<name>`` input-program prelude, returning a template-relative op batch
        rebased under the attachment path and applied through the SAME ``apply`` chokepoint as
        a delta — so regimes, the composing-shape guard, retention, trace stamping and
        ``op_id`` idempotency all hold identically. When the program DECLARES ``params`` they
        are the contract for its ``.input`` object: ``input`` must be an object carrying
        exactly those keys (a value may be null) — a missing or undeclared key is a loud
        :class:`ValueValidationError`; a program that declares none accepts any ``input``. An
        ``input``-purpose name, or a jq that does not return an op batch, is a
        :class:`ValueValidationError`."""
        self._ensure_available()
        decl = await self._require_declaration_decl(state)
        await self.validate_subject(decl, subject)
        template, path, parameters, declarations, program_name = await self._resolve_template_jq(state, name)
        program = template.template_jq[program_name]
        if program.purpose != "update":
            raise ValueValidationError(
                f"template_jq {program_name!r} on template {template.name!r} has purpose {program.purpose!r}; "
                f"apply needs an 'update'-purpose program (eval an 'input' one instead)"
            )
        if program.params:
            if not isinstance(input, dict):
                raise ValueValidationError(
                    f"template_jq {program_name!r} on template {template.name!r} declares params "
                    f"{program.params}, so its input must be an object, got {type(input).__name__}"
                )
            missing = sorted(set(program.params) - set(input))
            unknown = sorted(set(input) - set(program.params))
            if missing or unknown:
                raise ValueValidationError(
                    f"template_jq {program_name!r} on template {template.name!r} declares params {program.params}"
                    + (f", input missing {missing}" if missing else "")
                    + (f", input has undeclared {unknown}" if unknown else "")
                )
        record = await self._store.read_record_view(state, subject, conn=conn)
        subtree = _record_subtree(record["data"], path) if record is not None else {}
        variables: dict[str, Any] = {"parameters": parameters, "declarations": declarations}
        try:
            result = await run_jq_first(
                program.jq,
                {"record": subtree, "input": input},
                prelude=template_jq_prelude(template),
                variables=variables,
            )
        except Exception as exc:
            raise ValueValidationError(
                f"template_jq {program_name!r} on template {template.name!r} failed to evaluate: {exc}"
            ) from exc
        if not isinstance(result, list):
            raise ValueValidationError(
                f"template_jq {program_name!r} on template {template.name!r} is an update program, so its jq must "
                f"return an op batch (a list of ops), got {type(result).__name__}"
            )
        ops = [_rebase_op(op, path) for op in result]
        applied = await self.apply(state, subject, ops, op_id=op_id, origin=origin, conn=conn)
        return TemplateJqApplyResult(
            name=name, applied=applied.applied, data=applied.data, seq=applied.seq, skipped=applied.skipped
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
        self,
        state: str,
        *,
        kind: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        conn: AsyncConnection[Any] | None = None,
    ) -> dict[str, Any]:
        """One keyset page of a state's subjects, ordered by the full subject identity
        ``(target_kind, target_name, kind, key)``. With ``conn`` the read joins the caller's
        transaction (a attach reconciler paging its own in-flight merges)."""
        self._ensure_available()
        page = _page_limit(limit)
        if await self._store.get_declaration(state) is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        rows = await self._store.list_subjects(state, kind=kind, limit=page, cursor=cursor, conn=conn)
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

    # -- backup restore ----------------------------------------------------------

    async def restore_records(self, state: str, rows: Sequence[dict[str, Any]], *, origin: WriteOrigin) -> None:
        """Restore record rows for ``state`` under the completed origin, validating each
        document against the effective schema. The backup section's own record-restore
        path (off the ``AppStates`` protocol).

        EVERY row's subject is validated (declared kind, non-empty key, and — for kind
        ``person`` — a known person of the row's target) through :meth:`validate_subject`
        BEFORE any write; a refusal names the offending row index and its subject and
        nothing is written, so a restore never lands records under an undeclared kind or an
        unknown person."""
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
                    f"restore row {index}: malformed subject "
                    f"{row.get('target_kind')!r}/{row.get('target_name')!r}/"
                    f"{row.get('subject_kind')!r}/{row.get('subject_key')!r}: {exc}"
                ) from exc
            try:
                await self.validate_subject(decl, subject)
            except SubjectRefusedError as exc:
                raise SubjectRefusedError(f"restore row {index}: {exc}") from exc
        completed = self._complete_origin(origin)
        await self._store.restore_records(state, row_list, origin=completed, validate_doc=_validate_document)

    async def restore_aliases(self, state: str, rows: Sequence[dict[str, Any]], *, origin: WriteOrigin) -> None:
        """Restore subject-alias rows for ``state`` verbatim (identity, not a write) — the
        backup section's restore path, off the ``AppStates`` protocol."""
        self._ensure_available()
        await self._store.restore_aliases(state, list(rows))

    # -- templates -----------------------------------------------------------------

    async def list_templates(self) -> list[StateTemplateDocument]:
        self._ensure_available()
        return [StateTemplateDocument.model_validate(row["body"]) for row in await self._store.list_templates()]

    async def list_templates_catalog(self) -> list[dict[str, Any]]:
        """The template-catalog projection the ``GET /api/state-templates`` list serves: each
        stored document plus ``attached_to`` (the number of states the template is attached on)
        and ``shipped_default`` (true when the template carries a seed ``shipped_hash`` — an
        unedited shipped default). The attach counts are one query over every template."""
        self._ensure_available()
        counts = await self._store.attached_template_counts()
        catalog: list[dict[str, Any]] = []
        for row in await self._store.list_templates():
            document = StateTemplateDocument.model_validate(row["body"]).model_dump()
            document["attached_to"] = counts.get(row["name"], 0)
            document["shipped_default"] = row["shipped_hash"] is not None
            catalog.append(document)
        return catalog

    async def get_template(self, name: str) -> StateTemplateDocument | None:
        self._ensure_available()
        row = await self._store.get_template(name)
        if row is None:
            return None
        self._validated_template(row)  # loud on a corrupt stored body
        return StateTemplateDocument.model_validate(row["body"])

    async def put_template(self, doc: StateTemplateDocument, *, replace: bool) -> StateTemplateDocument:
        """Store a template document, running every registered attach validator over each live
        attach before the write (a raise leaves the stored document untouched); overwriting
        an existing name without ``replace`` raises :class:`TemplateExistsError`."""
        self._ensure_available()
        # ``exclude_none`` drops an unset ``declarations`` (None) so the deep validator
        # sees the same absent-key shape ``to_document`` emits, never a null section.
        body = doc.model_dump(by_alias=True, exclude_none=True)
        template = validate_template(body)
        existing = await self._store.get_template(template.name)
        if existing is not None and not replace:
            raise TemplateExistsError(
                f"template {template.name!r} already exists — upload with replace=true to overwrite it"
            )
        template_doc = StateTemplateDocument.model_validate(template.to_document())
        attachment_rows = await self._store.list_attachments_of_template(template.name)
        for row in attachment_rows:
            attachment_declarations = dict(row["declarations"] or {})
            resolved = self._effective_parameters(template, dict(row["parameters"] or {}))
            try:
                await self._validate_attach_values(template, resolved, attachment_declarations)
                effective = compose_effective_schema(
                    (await self._require_declaration(row["state"]))["schema"],
                    [
                        (m, p, pa)
                        for m, p, pa, _d in await self._load_state_attachments(
                            row["state"], override={template.name: template}
                        )
                    ],
                )
                await self._run_attach_validators(template_doc, attachment_declarations, effective)
            except TemplateValidationError as exc:
                raise TemplateInUseError(
                    f"template {template.name!r} cannot be replaced: its attach on state {row['state']!r} no longer "
                    f"validates: {exc}"
                ) from exc
        await self._store.upsert_template(template.name, template.to_document(), None)
        for row in attachment_rows:
            resolved = self._effective_parameters(template, dict(row["parameters"] or {}))
            base_schema = (await self._require_declaration(row["state"]))["schema"]
            effective = await self._compose_effective(row["state"], base_schema)
            await self._store.update_attachment_parameters(
                row["state"], template.name, resolved, effective_schema=effective
            )
        return template_doc

    async def delete_template(self, name: str) -> None:
        """Delete a template document; refused while it is attached."""
        self._ensure_available()
        if await self._store.get_template(name) is None:
            raise StateNotFoundError(f"no template {name!r}")
        attachments = await self._store.list_attachments_of_template(name)
        if attachments:
            states = ", ".join(sorted(m["state"] for m in attachments))
            raise TemplateInUseError(f"template {name!r} is attached on state(s) {states} — detach it first")
        await self._store.delete_template(name)

    # -- attachments ------------------------------------------------------------------

    async def list_attachments(self, state: str | None = None, *, template: str | None = None) -> list[dict[str, Any]]:
        self._ensure_available()
        if state is not None and template is not None:
            row = await self._store.get_attachment(state, template)
            rows = [] if row is None else [row]
        elif state is not None:
            rows = await self._store.list_attachments_for_state(state)
        elif template is not None:
            rows = await self._store.list_attachments_of_template(template)
        else:
            rows = await self._store.list_all_attachments()
        return [
            {
                "state": r["state"],
                "template": r["template"],
                "path": list(r["path"]),
                "parameters": dict(r["parameters"] or {}),
                "declarations": dict(r["declarations"] or {}),
            }
            for r in rows
        ]

    async def attach(self, state: str, template_name: str, body: AttachBody, *, skip_reconcilers: bool = False) -> None:
        """Attach a template on a state: validate path/parameters/declarations (+ check), run
        every registered attach validator over the composed effective schema, run every
        registered reconciler, then store the resolved parameters and the recomposed
        effective schema in one transaction (nothing derived is materialized). The
        reconcilers and the attach write share ONE transaction, so a reconciler's record
        writes commit with the attach or roll back together with a refusal. ``body.options``
        is a per-operation directive passed to the reconcilers, never stored.
        ``skip_reconcilers`` (backup restore only) runs the validators but not the
        reconcilers — a restored attachment is a snapshot, not a re-attach."""
        self._ensure_available()
        path = list(body.path)
        parameters = dict(body.parameters or {})
        declarations = dict(body.declarations or {})
        options = dict(body.options or {})
        decl = await self._require_declaration(state)
        template = await self._get_template_or_raise(template_name)
        self._validate_attach_path(path)
        if await self._store.get_attachment(state, template_name) is not None:
            raise AttachConflictError(
                f"template {template_name!r} is already attached on state {state!r} — detach it to change "
                f"path/parameters"
            )
        await self._validate_attach_values(template, parameters, declarations)
        resolved = self._effective_parameters(template, parameters)
        existing = await self._load_state_attachments(state)
        effective = compose_effective_schema(
            decl["schema"], [*[(m, p, pa) for m, p, pa, _d in existing], (template, list(path), resolved)]
        )
        _validate_schema(effective)
        template_doc = StateTemplateDocument.model_validate(template.to_document())
        await self._run_attach_validators(template_doc, declarations, effective)
        reconcilers = [] if skip_reconcilers else self._attach_reconcilers.all()
        if reconcilers:
            async with self._store.begin() as conn:
                await self._run_attach_reconcilers(
                    reconcilers,
                    state,
                    template_doc,
                    "attach",
                    previous_declarations=None,
                    new_declarations=declarations,
                    options=options,
                    conn=conn,
                )
                await self._store.upsert_attachment(
                    state, template_name, path, resolved, declarations, effective_schema=effective, conn=conn
                )
        else:
            await self._store.upsert_attachment(
                state, template_name, path, resolved, declarations, effective_schema=effective
            )

    async def update_attachment_declarations(
        self,
        state: str,
        template_name: str,
        declarations: dict[str, Any],
        *,
        options: dict[str, Any] | None = None,
        skip_reconcilers: bool = False,
    ) -> None:
        """Rewrite an attachment's declarations, re-running every registered attach validator and
        reconciler and recomposing the effective schema before the write. The reconcilers
        and the write share ONE transaction. ``options`` is a per-operation directive
        passed to the reconcilers, never stored. ``skip_reconcilers`` (backup restore only)
        runs the validators but not the reconcilers."""
        self._ensure_available()
        declarations = dict(declarations or {})
        options = dict(options or {})
        row = await self._store.get_attachment(state, template_name)
        if row is None:
            raise StateNotFoundError(f"template {template_name!r} is not attached on state {state!r}")
        template = await self._get_template_or_raise(template_name)
        await self._validate_attach_values(template, dict(row["parameters"] or {}), declarations)
        effective = await self._compose_effective(state, (await self._require_declaration(state))["schema"])
        template_doc = StateTemplateDocument.model_validate(template.to_document())
        await self._run_attach_validators(template_doc, declarations, effective)
        reconcilers = [] if skip_reconcilers else self._attach_reconcilers.all()
        if reconcilers:
            async with self._store.begin() as conn:
                await self._run_attach_reconcilers(
                    reconcilers,
                    state,
                    template_doc,
                    "update_declarations",
                    previous_declarations=dict(row["declarations"] or {}),
                    new_declarations=declarations,
                    options=options,
                    conn=conn,
                )
                await self._store.update_attachment_declarations(
                    state, template_name, declarations, effective_schema=effective, conn=conn
                )
        else:
            await self._store.update_attachment_declarations(
                state, template_name, declarations, effective_schema=effective
            )

    async def detach(self, state: str, template_name: str) -> None:
        """Remove an attachment and recompose the state's effective schema (nothing else)."""
        self._ensure_available()
        if await self._store.get_attachment(state, template_name) is None:
            raise StateNotFoundError(f"template {template_name!r} is not attached on state {state!r}")
        decl = await self._require_declaration(state)
        remaining = [
            (m, p, pa) for m, p, pa, _d in await self._load_state_attachments(state) if m.name != template_name
        ]
        effective = compose_effective_schema(decl["schema"], remaining)
        await self._store.delete_attachment(state, template_name, effective_schema=effective)

    async def effective_schema_for(self, state: str) -> dict[str, Any]:
        """The stored effective schema (base + every attachment's fragment) for a declared
        state — the schema every document validation reads."""
        self._ensure_available()
        return (await self._require_declaration(state))["effective_schema"]

    async def served_declaration(self, name: str) -> dict[str, Any]:
        """The full served declaration read: ``schema`` (base), ``effective_schema``,
        ``subject_kinds``, ``default_subject_kind``, ``retention_days`` (``None`` when the
        state keeps records forever), ``attachments[]``, ``regimes[]`` (the absolute regime paths
        every attachment declares) and ``updated_at`` (the ISO timestamp of the last write) — the
        one read a consumer's bind-time checks and the Studio's fields view consume. Carries
        the same fields the list read dumps, so an edit form round-trips a declaration
        (``retention_days`` included) without dropping any."""
        self._ensure_available()
        decl = await self._require_declaration(name)
        attachments = await self._load_state_attachments(name)
        regimes = self._compose_regimes(attachments)
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
            "attachments": [
                {"template": m.name, "path": list(p), "parameters": dict(pa), "declarations": dict(d)}
                for m, p, pa, d in attachments
            ],
            "regimes": regimes,
            "updated_at": updated_at,
        }

    def regime_for_path(self, template: StateTemplate, relative_path: list[Any]) -> str:
        """The regime governing ``relative_path`` in ``template`` — exposed for a consumer's
        bind-time single-writer check."""
        return regime_for(template, relative_path)

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

    # -- attach validators --------------------------------------------------------

    def register_attach_validator(self, validator: AttachValidator) -> None:
        self._attach_validators.register(validator)

    async def _run_attach_validators(
        self, template_doc: StateTemplateDocument, declarations: dict[str, Any], effective: dict[str, Any]
    ) -> None:
        """Run every registered attach validator with the template document, the attach's
        declaration values, and the state's effective schema — BEFORE any write. A validator
        raises loudly (a :class:`TemplateValidationError`) to refuse the door."""
        for validator in self._attach_validators.all():
            await validator(template_doc, declarations, effective)

    # -- attach reconcilers -------------------------------------------------------

    def register_attach_reconciler(self, reconciler: AttachReconciler) -> None:
        self._attach_reconcilers.register(reconciler)

    async def _run_attach_reconcilers(
        self,
        reconcilers: list[AttachReconciler],
        state: str,
        template_doc: StateTemplateDocument,
        operation: Literal["attach", "update_declarations"],
        *,
        previous_declarations: dict[str, Any] | None,
        new_declarations: dict[str, Any],
        options: dict[str, Any],
        conn: AsyncConnection[Any],
    ) -> None:
        """Run each attach reconciler AFTER the validators and BEFORE the write, each with a
        :class:`AttachReconcileContext` whose record door writes on the caller's transaction
        ``conn`` — so a reconciler's writes commit with the attach or roll back together with
        a refusal. A reconciler raises (a :class:`TemplateValidationError`, named with the
        template and state) to refuse the attach, or writes resolutions through the record door
        and returns so the attach commits with them. Any other exception propagates with the
        template and state named — never swallowed."""
        context = AttachReconcileContext(
            state=state,
            template=template_doc,
            operation=operation,
            previous_declarations=previous_declarations,
            new_declarations=new_declarations,
            options=options,
            records=_AttachReconcileRecords(self, state, conn),
        )
        for reconciler in reconcilers:
            try:
                await reconciler(context)
            except TemplateValidationError:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"attach reconciler for template {template_doc.name!r} on state {state!r} failed: {exc}"
                ) from exc

    # -- the built-in template-document reconciler ---------------------------------

    async def _reconcile_template_records(self, context: AttachReconcileContext) -> None:
        """The platform's own attach reconciler (always registered). On a declarations edit of
        a template that declares ``reconcile``, it settles the state's open records against the
        new declarations through the template's ``reconcile`` contract — a no-op on a first
        attach (no previous declarations) or a template without ``reconcile``. No template concept
        enters this body: the template's own jq decides what a declarations edit orphans and how
        to close it."""
        if context.previous_declarations is None:
            return
        template = validate_template(context.template.model_dump(by_alias=True, exclude_none=True))
        if template.reconcile is None:
            return
        path = await self._reconcile_attach_path(context.state, context.template.name)
        await self._run_reconcile(context, template.reconcile, path)

    async def _reconcile_attach_path(self, state: str, template_name: str) -> list[str]:
        """The path at which ``template_name`` is attached on ``state`` — the subtree the
        template's records live under. The attachment exists at reconcile time (a re-attach /
        declarations edit)."""
        for template, attach_path, _params, _decls in await self._load_state_attachments(state):
            if template.name == template_name:
                return list(attach_path)
        raise RuntimeError(f"reconcile: template {template_name!r} is not attached on state {state!r}")

    async def _run_reconcile(
        self, context: AttachReconcileContext, reconcile: TemplateReconcile, path: list[str]
    ) -> None:
        previous = context.previous_declarations or {}
        new = context.new_declarations
        orphans: list[tuple[StateSubject, dict[str, Any]]] = []
        cursor: str | None = None
        while True:
            page = await context.records.list_subjects(limit=_RECONCILE_PAGE, cursor=cursor)
            for entry in page["subjects"]:
                subject = StateSubject(**entry["subject"])
                view = await context.records.read(subject)
                if view is None:
                    continue
                for item in await self._reconcile_orphans(
                    reconcile, _record_subtree(view.data, path), previous=previous, new=new
                ):
                    orphans.append((subject, item))
            cursor = page.get("next_cursor")
            if cursor is None:
                break

        if not orphans:
            return

        directive = context.options.get("orphans")
        if directive is None:
            raise TemplateValidationError(_reconcile_refusal(context, orphans), extra=_reconcile_orphans_extra(orphans))
        if directive != "close":
            raise TemplateValidationError(
                f"re-attaching template {context.template.name!r} on state {context.state!r}: unknown reconcile "
                f'directive options.orphans={directive!r}; the only directive is "close"'
            )
        resolution = context.options.get("resolution")
        await self._reconcile_guard_resolution(context, reconcile, new, resolution)
        for subject, item in orphans:
            current = await context.records.read(subject)
            subtree = _record_subtree(current.data, path) if current is not None else {}
            ops = await self._run_reconcile_jq(
                "close", reconcile.close, {"data": subtree, "id": item["id"], "resolution": resolution}
            )
            if not isinstance(ops, list):
                raise TemplateValidationError(f"reconcile close must return a list of ops, got {type(ops).__name__}")
            await context.records.apply(subject, [_rebase_op(op, path) for op in ops], origin=_RECONCILE_ORIGIN)

    async def _reconcile_orphans(
        self, reconcile: TemplateReconcile, subtree: dict[str, Any], *, previous: dict[str, Any], new: dict[str, Any]
    ) -> list[dict[str, Any]]:
        result = await self._run_reconcile_jq(
            "view", reconcile.view, {"previous": previous, "new": new, "data": subtree}
        )
        if not isinstance(result, list):
            raise TemplateValidationError(
                f"reconcile view must return a list of {{id, label}}, got {type(result).__name__}"
            )
        return result

    async def _reconcile_guard_resolution(
        self, context: AttachReconcileContext, reconcile: TemplateReconcile, new: dict[str, Any], resolution: Any
    ) -> None:
        if not isinstance(resolution, str) or not resolution.strip():
            raise TemplateValidationError(
                f"re-attaching template {context.template.name!r} on state {context.state!r}: "
                'options.orphans="close" needs options.resolution naming a not-done resolution'
            )
        declared = await self._run_reconcile_jq("resolutions", reconcile.resolutions, {"new": new})
        names = declared if isinstance(declared, list) else []
        if resolution not in names:
            raise TemplateValidationError(
                f"re-attaching template {context.template.name!r} on state {context.state!r}: resolution "
                f"{resolution!r} is not a not-done resolution the new declarations declare "
                f"(declared: {sorted(str(n) for n in names)})"
            )

    @staticmethod
    async def _run_reconcile_jq(label: str, expr: str, payload: Any) -> Any:
        """One reconcile jq program over its input payload — loud on an evaluation failure,
        carrying the program's own ``error(...)`` message out."""
        try:
            return await run_jq_first(expr, payload)
        except Exception as exc:
            raise TemplateValidationError(f"reconcile {label} failed to evaluate: {exc}") from exc

    # -- seeds -------------------------------------------------------------------

    def register_template_seed(self, doc: StateTemplateDocument) -> None:
        self._seeds.register(doc)

    async def apply_template_seeds(self) -> None:
        """Create each shipped template seed that is absent from the store (a no-op while the
        feature is off)."""
        if not states_store_configured():
            return
        from tai42_skeleton.states.seeds import apply_template_seeds

        await apply_template_seeds(self._store, seeds=self._seeds.seeds())

    # -- template/attach helpers ----------------------------------------------------

    async def _require_declaration(self, state: str) -> dict[str, Any]:
        decl = await self._store.get_declaration(state)
        if decl is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        return decl

    async def _require_declaration_decl(self, state: str) -> StateDeclaration:
        return _row_to_declaration(await self._require_declaration(state))

    async def _get_template_or_raise(self, name: str) -> StateTemplate:
        row = await self._store.get_template(name)
        if row is None:
            raise StateNotFoundError(f"no template {name!r}")
        return self._validated_template(row)

    async def _load_state_attachments(
        self, state: str, *, override: dict[str, StateTemplate] | None = None
    ) -> list[tuple[StateTemplate, list[str], dict[str, Any], dict[str, Any]]]:
        """Every attachment on the state as ``(template, path, parameters, declarations)``.
        ``override`` supplies a not-yet-stored template body (a template replace composes
        against the candidate)."""
        override = override or {}
        out: list[tuple[StateTemplate, list[str], dict[str, Any], dict[str, Any]]] = []
        for row in await self._store.list_attachments_for_state(state):
            template = override.get(row["template"]) or await self._get_template_or_raise(row["template"])
            out.append((template, list(row["path"]), dict(row["parameters"] or {}), dict(row["declarations"] or {})))
        return out

    async def _compose_effective(self, state: str, base_schema: dict[str, Any]) -> dict[str, Any]:
        """The effective schema for ``base_schema`` over the state's CURRENT attachments."""
        attachments = await self._load_state_attachments(state)
        return compose_effective_schema(base_schema, [(m, p, pa) for m, p, pa, _d in attachments])

    @staticmethod
    def _compose_regimes(
        attachments: list[tuple[StateTemplate, list[str], dict[str, Any], dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """The absolute write-regime rules over already-loaded ``attachments``: each attached
        template's regime paths prefixed by the attach path. The ONE composition every
        declaration read (``get_declaration``/``list_declarations``) and
        ``served_declaration`` share, so a served regime is identical across doors."""
        regimes: list[dict[str, Any]] = []
        for template, base_path, _params, _decls in attachments:
            for rule in template.regimes:
                regimes.append({"path": [*base_path, *rule.path], "regime": rule.regime})
        return regimes

    def _validate_attach_path(self, path: Any) -> None:
        if not isinstance(path, list):
            raise AttachConflictError("attach path must be a list of object keys")
        for seg in path:
            if not isinstance(seg, str) or not seg:
                raise AttachConflictError(
                    f"attach path segment {seg!r} must be a non-empty object key (an attach never sits on a list index)"
                )

    @staticmethod
    def _effective_parameters(template: StateTemplate, parameters: dict[str, Any]) -> dict[str, Any]:
        """The parameter map an attach PERSISTS: the template's defaults overlaid by the
        client's supplied values."""
        return {**template.defaults(), **dict(parameters or {})}

    async def _validate_attach_values(
        self, template: StateTemplate, parameters: dict[str, Any], declarations: dict[str, Any]
    ) -> None:
        """Validate an attach's effective parameter values against each parameter's schema
        (every no-default parameter supplied) and its declarations against the template's
        declarations schema and optional ``check`` predicate. Loud on the first failure.

        The ``check`` runs over the declarations with the attach's EFFECTIVE parameters
        (template defaults overlaid by supplied values — the map the runtime sees) bound as
        the named jq variable ``$parameters``, so a check may constrain a declaration
        against a parameter (e.g. against a parameter-declared enum) at the earliest point
        both are known."""
        effective = self._effective_parameters(template, parameters)
        for name, value in effective.items():
            param = template.parameters.get(name)
            if param is None:
                raise TemplateValidationError(
                    f"attach supplies unknown parameter {name!r} for template {template.name!r}"
                )
            try:
                Draft202012Validator(param.schema).validate(value)
            except jsonschema.ValidationError as exc:
                raise TemplateValidationError(f"attach parameter {name!r} is invalid: {exc.message}") from exc
        for name, param in template.parameters.items():
            if not param.has_default and name not in effective:
                raise TemplateValidationError(
                    f"attach of template {template.name!r} must supply parameter {name!r} (it has no default)"
                )
        if template.declarations is None:
            if declarations:
                raise TemplateValidationError(
                    f"template {template.name!r} declares no declarations section, so none may be supplied"
                )
            return
        try:
            Draft202012Validator(template.declarations.schema).validate(declarations)
        except jsonschema.ValidationError as exc:
            raise TemplateValidationError(
                f"attach declarations are invalid under template {template.name!r}: {exc.message}"
            ) from exc
        if template.declarations.check is not None:
            try:
                result = await run_jq_first(
                    template.declarations.check, declarations, variables={"parameters": effective}
                )
            except Exception as exc:
                raise TemplateValidationError(
                    f"template {template.name!r} declarations check failed to evaluate: {exc}"
                ) from exc
            if result is not True:
                message = result if isinstance(result, str) else "the declarations violate the template's check rule"
                raise TemplateValidationError(f"attach declarations rejected by template {template.name!r}: {message}")


__all__ = [
    "StatesAttachReconcilerRegistry",
    "StatesAttachValidatorRegistry",
    "StatesConsumerListerRegistry",
    "StatesService",
    "current_state_context",
    "state_context",
]
