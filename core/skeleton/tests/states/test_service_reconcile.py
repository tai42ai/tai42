"""The states service's attach-reconciler seam: reconcilers run after the validators and
before the write, share the attach transaction (commit or roll back together), read their own
in-flight merges, and can close a composing record a whole-path merge cannot — driven against
the in-memory ``FakeStatesStore`` and the fake-pg-backed real store."""

from __future__ import annotations

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import RegimeViolationError, TemplateValidationError
from tai42_contract.states.models import AttachBody, WriteOrigin

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService
from tai42_skeleton.states.templates import validate_template

from .fake_service_store import _ORIGIN, _STATE, FakeStatesStore, _FakeApp, _subject, _template_doc


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    with tai42_app.bound(_FakeApp()):
        yield StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


async def _attach_template(svc: StatesService, *, declarations: dict, options: dict | None = None) -> None:
    decl_template = _template_doc(
        "m", declarations={"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}}
    )
    await svc.put_template(decl_template, replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"], declarations=declarations, options=options or {}))


async def _noop() -> None:
    return None


async def test_attach_runs_reconciler_with_a_first_attach_context(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    seen = []
    svc.register_attach_reconciler(lambda ctx: seen.append(ctx) or _noop())
    await _attach_template(svc, declarations={"n": 1}, options={"on_orphan": "close"})
    (ctx,) = seen
    assert ctx.state == "alerts"
    assert ctx.template.name == "m"
    assert ctx.operation == "attach"
    assert ctx.previous_declarations is None
    assert ctx.new_declarations == {"n": 1}
    assert ctx.options == {"on_orphan": "close"}


async def test_update_declarations_runs_reconciler_with_previous_and_options(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await _attach_template(svc, declarations={"n": 1})
    seen = []
    svc.register_attach_reconciler(lambda ctx: seen.append(ctx) or _noop())
    await svc.update_attachment_declarations("alerts", "m", {"n": 2}, options={"on_orphan": "refuse"})
    (ctx,) = seen
    assert ctx.operation == "update_declarations"
    assert ctx.previous_declarations == {"n": 1}
    assert ctx.new_declarations == {"n": 2}
    assert ctx.options == {"on_orphan": "refuse"}
    # options are a per-operation directive passed to the reconciler, never stored.
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert "options" not in store.attachments[("alerts", "m")]


async def test_raising_reconciler_refuses_the_attach_and_writes_nothing(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)

    async def _refuse(ctx):
        raise TemplateValidationError("record t1 points at a value the new declarations drop")

    svc.register_attach_reconciler(_refuse)
    with pytest.raises(TemplateValidationError, match="points at a value"):
        await _attach_template(svc, declarations={"n": 1})
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert ("alerts", "m") not in store.attachments


async def test_reconciler_writes_are_visible_after_the_attach_commits(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.replace("alerts", _subject(key="open1"), {"n": 1}, origin=_ORIGIN)

    async def _close_open(ctx):
        page = await ctx.records.list_subjects()
        for sub in page["subjects"]:
            subject = _subject(kind=sub["subject"]["kind"], key=sub["subject"]["key"])
            await ctx.records.merge(subject, {"closed": True}, origin=WriteOrigin(consumer="reconciler"))

    svc.register_attach_reconciler(_close_open)
    await _attach_template(svc, declarations={"n": 1})
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert ("alerts", "m") in store.attachments
    view = await svc.read("alerts", _subject(key="open1"))
    assert view is not None
    assert view.data["closed"] is True


async def test_reconciler_merge_then_raise_rolls_back_the_record_and_the_attach(svc: StatesService) -> None:
    # The reconcile + attach write share ONE transaction: a reconciler that writes a record
    # and then refuses leaves NEITHER the record write NOR the attach — atomic all-or-nothing.
    await svc.put_declaration(_STATE)
    await svc.replace("alerts", _subject(key="open1"), {"n": 1}, origin=_ORIGIN)

    async def _write_then_refuse(ctx):
        await ctx.records.merge(_subject(key="open1"), {"closed": True}, origin=WriteOrigin(consumer="reconciler"))
        raise TemplateValidationError("record open1 points at a value the new declarations drop")

    svc.register_attach_reconciler(_write_then_refuse)
    with pytest.raises(TemplateValidationError, match="points at a value"):
        await _attach_template(svc, declarations={"n": 1})
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    assert ("alerts", "m") not in store.attachments
    view = await svc.read("alerts", _subject(key="open1"))
    assert view is not None
    assert "closed" not in view.data


async def test_failed_attach_write_rolls_back_a_reconciler_write(
    svc: StatesService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A merge succeeds inside the reconciler, then the attach write itself fails: the shared
    # transaction rolls the reconciler's record write back too.
    await svc.put_declaration(_STATE)
    await svc.replace("alerts", _subject(key="open1"), {"n": 1}, origin=_ORIGIN)

    async def _close_open(ctx):
        await ctx.records.merge(_subject(key="open1"), {"closed": True}, origin=WriteOrigin(consumer="reconciler"))

    svc.register_attach_reconciler(_close_open)

    async def _boom(*args, **kwargs):
        raise RuntimeError("attach write failed")

    monkeypatch.setattr(svc._store, "upsert_attachment", _boom)
    with pytest.raises(RuntimeError, match="attach write failed"):
        await _attach_template(svc, declarations={"n": 1})
    view = await svc.read("alerts", _subject(key="open1"))
    assert view is not None
    assert "closed" not in view.data


async def test_skip_reconcilers_runs_validators_but_not_reconcilers(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    calls: list[str] = []

    async def _validator(template_doc, declarations, effective):
        calls.append("validator")

    async def _reconciler(ctx):
        calls.append("reconciler")

    svc.register_attach_validator(_validator)
    svc.register_attach_reconciler(_reconciler)
    decl_template = _template_doc(
        "m", declarations={"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}}
    )
    await svc.put_template(decl_template, replace=False)
    await svc.attach("alerts", "m", AttachBody(path=["sub"], declarations={"n": 1}), skip_reconcilers=True)
    assert calls == ["validator"]


async def test_reconciler_reads_its_own_in_flight_merge(svc: StatesService) -> None:
    # The record door's read/list_subjects run on the attach transaction, so a reconciler
    # sees the merge it just wrote (read-your-writes) before the attach commits.
    await svc.put_declaration(_STATE)
    await svc.replace("alerts", _subject(key="open1"), {"n": 1}, origin=_ORIGIN)
    seen: list[object] = []

    async def _read_own_write(ctx):
        await ctx.records.merge(_subject(key="open1"), {"n": 9}, origin=WriteOrigin(consumer="reconciler"))
        view = await ctx.records.read(_subject(key="open1"))
        seen.append(None if view is None else view.data.get("n"))
        page = await ctx.records.list_subjects()
        seen.append(len(page["subjects"]))

    svc.register_attach_reconciler(_read_own_write)
    await _attach_template(svc, declarations={"n": 1})
    assert seen == [9, 1]


_COMPOSING_TEMPLATE = {
    "schema": {
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "closed": {"type": "boolean"}},
                },
            }
        },
    },
    "regimes": [{"path": ["entries"], "regime": "composing"}],
}


async def _setup_composing_ledger(store: object, monkeypatch: pytest.MonkeyPatch) -> StatesService:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    rsvc = StatesService(store=store)  # type: ignore[arg-type]
    await rsvc.put_declaration(_STATE)
    await rsvc.put_template(_template_doc("ctpl", **_COMPOSING_TEMPLATE), replace=False)
    await rsvc.attach("alerts", "ctpl", AttachBody(path=[], declarations={}))
    await rsvc.apply(
        "alerts",
        _subject(key="led1"),
        [{"op": "set_by_key", "path": ["entries"], "key_field": "id", "value": {"id": 1}}],
        op_id=None,
        origin=_ORIGIN,
    )
    return rsvc


async def test_reconciler_apply_closes_a_composing_record_that_merge_cannot(
    pg: object, store: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A record under a ``composing`` write regime cannot be closed with ``merge`` (a
    # whole-path set is a RegimeViolationError); a reconciler closes it with the keyed
    # ``apply`` — exactly what a template fill can write — and the attach commits it.
    rsvc = await _setup_composing_ledger(store, monkeypatch)
    subject = _subject(key="led1")

    async def _close(ctx):
        with pytest.raises(RegimeViolationError):
            await ctx.records.merge(subject, {"entries": [{"id": 1, "closed": True}]}, origin=_ORIGIN)
        await ctx.records.apply(
            subject,
            [{"op": "set_by_key", "path": ["entries"], "key_field": "id", "value": {"id": 1, "closed": True}}],
            origin=WriteOrigin(consumer="reconciler"),
        )

    rsvc.register_attach_reconciler(_close)
    await rsvc.update_attachment_declarations("alerts", "ctpl", {})
    view = await rsvc.read("alerts", subject)
    assert view is not None
    assert view.data["entries"] == [{"id": 1, "closed": True}]


async def test_reconciler_apply_on_a_composing_record_rolls_back_on_refuse(
    pg: object, store: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    rsvc = await _setup_composing_ledger(store, monkeypatch)
    subject = _subject(key="led1")

    async def _close_then_refuse(ctx):
        await ctx.records.apply(
            subject,
            [{"op": "set_by_key", "path": ["entries"], "key_field": "id", "value": {"id": 1, "closed": True}}],
            origin=WriteOrigin(consumer="reconciler"),
        )
        raise TemplateValidationError("refuse after the keyed write")

    rsvc.register_attach_reconciler(_close_then_refuse)
    with pytest.raises(TemplateValidationError, match="refuse after the keyed write"):
        await rsvc.update_attachment_declarations("alerts", "ctpl", {})
    view = await rsvc.read("alerts", subject)
    assert view is not None
    assert view.data["entries"] == [{"id": 1}]  # the keyed write rolled back with the refused attach


async def test_reconciler_runs_after_the_validator(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    order: list[str] = []

    async def _validator(template_doc, declarations, effective):
        order.append("validator")

    async def _reconciler(ctx):
        order.append("reconciler")

    svc.register_attach_validator(_validator)
    svc.register_attach_reconciler(_reconciler)
    await _attach_template(svc, declarations={"n": 1})
    assert order == ["validator", "reconciler"]


def test_regime_for_path(svc: StatesService) -> None:
    template = validate_template(
        {
            "kind": "state-template",
            "name": "m",
            "schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}}},
            "regimes": [{"path": ["items"], "regime": "composing"}],
        }
    )
    assert svc.regime_for_path(template, ["items"]) == "composing"
