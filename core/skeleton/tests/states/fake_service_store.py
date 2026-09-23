"""Shared in-memory fakes for the states-service unit tests.

An in-memory stand-in for :class:`PostgresStatesStore` covering the methods the service
drives, plus the declaration/subject/template helpers and the fake resource manager the
by-id save/attach paths render through — enough to pin the service's validate+apply logic
without a live Postgres.
"""

from __future__ import annotations

import copy
import inspect
from contextlib import asynccontextmanager
from typing import Any

from tai42_contract.conversations import ConversationTargetKind
from tai42_contract.states.errors import StateNotFoundError
from tai42_contract.states.models import StateDeclaration, StateSubject, StateTemplateDocument, WriteOrigin
from tai42_contract.template import TemplatedText

from tai42_skeleton.states.store import _iso_now, _traced_paths, stamp_trace

_ORIGIN = WriteOrigin(consumer="c")

# The stored resources a by-id declarations ``check`` names, keyed by id → its jq body,
# plus the by-id state/template ``schema`` bodies (rendered text is parsed as JSON).
_CHECK_RESOURCES = {
    "capped-check": 'if .count <= $parameters.limit then true else "count exceeds the attach limit" end',
    "stored-state-schema": '{"type": "object", "properties": {"n": {"type": "integer"}}}',
    "stored-fragment-schema": '{"type": "object", "properties": {"y": {"type": "integer"}}}',
    "not-json-schema": "this is not JSON",
}


class FakeStatesStore:
    """An in-memory stand-in for :class:`PostgresStatesStore` covering the methods the
    service drives — enough to pin the service's validate+apply logic."""

    def __init__(self) -> None:
        self.declarations: dict[str, dict[str, Any]] = {}
        self.templates: dict[str, dict[str, Any]] = {}
        self.attachments: dict[tuple[str, str], dict[str, Any]] = {}
        self.records: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self.write_rows: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
        self.applied_origins: list[Any] = []
        self.upsert_attach_calls = 0
        self.update_decl_calls = 0
        self.upsert_template_calls = 0
        self.op_ledger_stale = 0
        self.prune_ops_days: int | None = None

    @asynccontextmanager
    async def begin(self):
        """A transaction boundary: snapshot the tables on enter, restore them on any
        exception (a real rollback), so a reconcile + attach write commit or roll back as
        one — the atomicity the real store gives through a shared connection."""
        snapshot = (copy.deepcopy(self.records), copy.deepcopy(self.attachments), copy.deepcopy(self.declarations))
        try:
            yield object()
        except BaseException:
            self.records, self.attachments, self.declarations = snapshot
            raise

    # declarations
    async def get_declaration(self, name):
        return self.declarations.get(name)

    async def list_declarations(self):
        return list(self.declarations.values())

    async def upsert_declaration_guarded(
        self,
        name,
        description,
        schema,
        subject_kinds,
        default_subject_kind,
        retention_days,
        *,
        effective_schema,
        decide,
    ):
        existing = self.declarations.get(name)
        per_kind: dict[str, int] = {}
        for state, _tk, _tn, sk, _key in self.records:
            if state == name:
                per_kind[sk] = per_kind.get(sk, 0) + 1
        outcome = decide(existing, per_kind)
        if inspect.isawaitable(outcome):
            await outcome
        self.declarations[name] = {
            "name": name,
            "description": description,
            "schema": schema,
            "effective_schema": effective_schema,
            "subject_kinds": list(subject_kinds),
            "default_subject_kind": default_subject_kind,
            "retention_days": retention_days,
            "updated_at": 1,
        }

    async def delete_declaration(self, name):
        return self.declarations.pop(name, None) is not None

    async def field_stats(self, state):
        per_kind: dict[str, int] = {}
        for s, _tk, _tn, sk, _key in self.records:
            if s == state:
                per_kind[sk] = per_kind.get(sk, 0) + 1
        return sum(per_kind.values()), {}, per_kind

    # templates
    async def get_template(self, name):
        return self.templates.get(name)

    async def list_templates(self):
        return list(self.templates.values())

    async def attached_template_counts(self):
        counts: dict[str, int] = {}
        for _s, template in self.attachments:
            counts[template] = counts.get(template, 0) + 1
        return counts

    async def writes(self, state, subject, *, limit, cursor):
        rows = list(
            self.write_rows.get((state, subject.target_kind, subject.target_name, subject.kind, subject.key), [])
        )
        start = 0 if cursor is None else next((i for i, r in enumerate(rows) if r["id"] < int(cursor)), len(rows))
        return rows[start : start + limit]

    async def upsert_template(self, name, body, shipped_hash):
        self.upsert_template_calls += 1
        self.templates[name] = {"name": name, "body": body, "shipped_hash": shipped_hash, "updated_at": 1}

    async def delete_template(self, name):
        return self.templates.pop(name, None) is not None

    # attachments
    async def get_attachment(self, state, template):
        return self.attachments.get((state, template))

    async def list_attachments_for_state(self, state):
        return [v for (s, _m), v in self.attachments.items() if s == state]

    async def list_attachments_of_template(self, template):
        return [v for (_s, m), v in self.attachments.items() if m == template]

    async def list_all_attachments(self):
        return list(self.attachments.values())

    async def upsert_attachment(self, state, template, path, parameters, declarations, *, effective_schema, conn=None):
        self.upsert_attach_calls += 1
        self.attachments[(state, template)] = {
            "state": state,
            "template": template,
            "path": path,
            "parameters": parameters,
            "declarations": declarations,
            "updated_at": 1,
        }
        self.declarations[state]["effective_schema"] = effective_schema

    async def update_attachment_declarations(self, state, template, declarations, *, effective_schema, conn=None):
        self.update_decl_calls += 1
        self.attachments[(state, template)]["declarations"] = declarations
        self.declarations[state]["effective_schema"] = effective_schema
        return True

    async def update_attachment_parameters(self, state, template, parameters, *, effective_schema):
        self.attachments[(state, template)]["parameters"] = parameters
        self.declarations[state]["effective_schema"] = effective_schema
        return True

    async def delete_attachment(self, state, template, *, effective_schema):
        self.attachments.pop((state, template), None)
        self.declarations[state]["effective_schema"] = effective_schema
        return True

    # records
    async def read_record_view(self, state, subject, *, conn=None):
        row = self.records.get((state, subject.target_kind, subject.target_name, subject.kind, subject.key))
        if row is None:
            return None
        return {"data": row, "seq": 1.0, "canonical_subject": subject, "folded_from": []}

    async def apply_ops(self, state, subject, ops, *, op_id, origin, validate_doc, validate_subject_in_txn, conn=None):
        decl = self.declarations.get(state)
        if decl is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        # The real store validates the subject inside the write txn from the locked declaration's
        # subject_kinds; mirror that so the service's admission refusals still fire end to end.
        await validate_subject_in_txn(list(decl["subject_kinds"]))
        self.applied_origins.append(origin)
        # Mirror the store's chokepoint so the service-level provenance test is end to
        # end: compose the state's traced paths from its attachments + templates and stamp
        # ``_trace`` from the COMPLETED origin (the stamping mechanics themselves are pinned
        # in test_store.py). A state with no traced attach leaves the ops untouched.
        attach_rows = [
            {"template": m["template"], "path": m["path"], "body": self.templates[m["template"]]["body"]}
            for (s, _template), m in self.attachments.items()
            if s == state and m["template"] in self.templates
        ]
        traced = _traced_paths(attach_rows)
        if traced:
            stamp = {
                "meta": origin.meta,
                "run": origin.run_id,
                "turn": origin.turn_id,
                "inbound": origin.inbound_id,
                "at": _iso_now(),
            }
            stamp_trace(ops, traced, stamp)
        doc: dict[str, Any] = {}
        for op in ops:
            if op["op"] == "set":
                doc[op["path"][0]] = op["value"]
        self.records[(state, subject.target_kind, subject.target_name, subject.kind, subject.key)] = doc
        return (True, doc, 1.0, [])

    async def restore_records(self, state, rows, *, origin, validate_doc):
        decl = self.declarations.get(state)
        if decl is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        self.applied_origins.append(origin)
        for row in rows:
            validate_doc(decl["effective_schema"], row["data"])
            key = (state, row["target_kind"], row["target_name"], row["subject_kind"], row["subject_key"])
            self.records[key] = row["data"]

    async def restore_aliases(self, state, rows):
        self.restored_aliases = list(rows)

    async def replace(self, state, subject, data, *, origin, validate_doc):
        decl = self.declarations.get(state)
        if decl is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        validate_doc(decl["effective_schema"], data)
        self.applied_origins.append(origin)
        self.records[(state, subject.target_kind, subject.target_name, subject.kind, subject.key)] = data
        return data, 1.0

    async def erase_subject(self, state, subject, *, origin):
        self.applied_origins.append(origin)
        self.records.pop((state, subject.target_kind, subject.target_name, subject.kind, subject.key), None)

    async def fold_subject(self, state, subject, into, mode, *, origin, validate_doc):
        self.applied_origins.append(origin)
        return {
            "mode": mode,
            "from": {"kind": subject.kind, "key": subject.key},
            "into": {"kind": into.kind, "key": into.key},
            "already": False,
            "flattened": 0,
        }

    async def list_subjects(self, state, *, kind, limit, cursor, conn=None):
        rows = [
            {"target_kind": tk, "target_name": tn, "subject_kind": sk, "subject_key": key, "updated_at": 1.0}
            for (s, tk, tn, sk, key) in self.records
            if s == state and (kind is None or sk == kind)
        ]
        return rows[:limit]

    async def search_records(self, state, containment, *, limit, cursor):
        rows = [
            {"target_kind": tk, "target_name": tn, "subject_kind": sk, "subject_key": key, "updated_at": 1.0}
            for (s, tk, tn, sk, key), data in self.records.items()
            if s == state and all(data.get(k) == v for k, v in containment.items())
        ]
        return rows[:limit]

    async def prune_expired(self, default):
        self.prune_default = default
        return {"alerts": 2} if self.records else {}

    async def prune_ops(self, retention_days):
        self.prune_ops_days = retention_days
        return self.op_ledger_stale


_STATE = StateDeclaration(
    name="alerts",
    schema={"type": "object", "properties": {"n": {"type": "integer"}}},
    subject_kinds=["thread"],
    default_subject_kind="thread",
)


def _subject(kind="thread", key="t1", tk: ConversationTargetKind = "agent", tn="a") -> StateSubject:
    return StateSubject(target_kind=tk, target_name=tn, kind=kind, key=key)


def _template_doc(name="tpl", **over) -> StateTemplateDocument:
    body = {
        "kind": "state-template",
        "name": name,
        "schema": {"type": "object", "properties": {"y": {"type": "integer"}}},
    }
    body.update(over)
    return StateTemplateDocument.model_validate(body)


class _FakeResourceManager:
    """Renders a declarations check or a schema body: inline ``content`` verbatim, or a stored
    ``id`` from ``_CHECK_RESOURCES`` — an unmapped id is the loud not-found the real manager
    raises."""

    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        if text.id is not None:
            from tai42_skeleton.template.resource_manager import TemplateNotFoundError

            if text.id not in _CHECK_RESOURCES:
                raise TemplateNotFoundError(f"no stored resource {text.id!r}")
            return _CHECK_RESOURCES[text.id]
        assert text.content is not None
        return text.content


class _FakeStorage:
    resource_manager = _FakeResourceManager()


class _FakeApp:
    storage = _FakeStorage()
