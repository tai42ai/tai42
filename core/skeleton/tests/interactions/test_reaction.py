"""The mid-form reaction chokepoint ``react``: open-check, type caps, handler identity/deadline,
form-update validation, and statelessness — the one seam every reaction door reaches."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tai42_contract.interactions import AnswerFormat, InteractionRequest

from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions import reaction as reaction_module
from tai42_skeleton.interactions.reaction import (
    FormReactionClosedError,
    FormReactionHandlerError,
    FormReactionRequestError,
    SubmittedCheckRejectedError,
    _declared_slots,
    _is_option_bearing,
    _normalize_event,
    _types_only_schema,
    _validate_update,
    _validate_update_display,
    _validate_update_errors,
    _validate_update_options,
    _validate_update_values,
    enforce_submitted_check,
    react,
)
from tai42_skeleton.interactions.settings import InteractionsSettings

_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "slot": {"type": "string", "enum": ["9am", "10am"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "n": {"type": "integer"},
    },
}


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    settings = InteractionsSettings()
    monkeypatch.setattr(reaction_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(reaction_module, "interactions_settings", lambda: settings)
    store = InteractionStore(settings.key_prefix)
    return SimpleNamespace(settings=settings, store=store, fake=fake_redis, monkeypatch=monkeypatch)


def _reacting(
    store: InteractionStore,
    *,
    iid: str = "r1",
    reactions: dict | None = None,
    reaction_tool: str | None = "react_tool",
    schema: dict | None = None,
    pages: list[dict] | None = None,
) -> InteractionRequest:
    now = datetime.now(UTC)
    future = now + timedelta(hours=1)
    payload: dict = {"schema": schema or _SCHEMA}
    if pages is not None:
        payload["pages"] = pages
    if reactions is not None:
        payload["reactions"] = reactions
    overrides: dict = {}
    if reaction_tool is not None:
        overrides = {
            "mode": "async",
            "continuation_tool": "resume",
            "continuation_identity": "svc-key",
            "expiry_at": future,
            "reaction_tool": reaction_tool,
        }
    return InteractionRequest(
        interaction_id=iid,
        group_id="rg",
        question="?",
        answer_format=AnswerFormat.FORM,
        format_payload=payload,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=future,
        **overrides,
    )


def _stub_handler(monkeypatch, *, returns=None, captured=None):
    async def _run(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return {} if returns is None else returns

    monkeypatch.setattr(reaction_module, "_run_reaction", _run)


# --- open check -----------------------------------------------------------------------


async def test_react_on_missing_interaction_is_closed(wired):
    with pytest.raises(FormReactionClosedError):
        await react("missing", {"kind": "submitted"}, {})


async def test_react_on_static_form_is_closed(wired):
    # A non-reacting (static) form carries no reaction_tool: the chokepoint refuses it.
    await wired.store.add(wired.fake, _reacting(wired.store, reaction_tool=None), idle_ttl=86400)
    with pytest.raises(FormReactionClosedError, match="not a reacting form"):
        await react("r1", {"kind": "submitted"}, {})


async def test_react_on_answered_interaction_is_closed(wired, monkeypatch):
    from tai42_contract.interactions import InteractionResponse

    await wired.store.add(wired.fake, _reacting(wired.store, reactions={"field_changed": ["name"]}), idle_ttl=86400)
    await wired.store.record_answer(
        wired.fake,
        InteractionResponse(interaction_id="r1", answer={}, answered_by="x", answered_at=datetime.now(UTC)),
        "rg",
        reply_ttl=60,
        continuation_due_ttl=60,
        continuation_first_attempt_at_ms=int(datetime.now(UTC).timestamp() * 1000),
    )
    _stub_handler(monkeypatch)
    with pytest.raises(FormReactionClosedError):
        await react("r1", {"kind": "field_changed", "field": "name"}, {"name": "x"})


# --- event + partial-value validation -------------------------------------------------


async def test_event_naming_undeclared_trigger_is_a_request_error(wired, monkeypatch):
    await wired.store.add(wired.fake, _reacting(wired.store, reactions={"field_changed": ["name"]}), idle_ttl=86400)
    _stub_handler(monkeypatch)
    with pytest.raises(FormReactionRequestError, match="not a declared field trigger"):
        await react("r1", {"kind": "field_changed", "field": "slot"}, {"name": "x"})


async def test_partial_values_are_type_checked(wired, monkeypatch):
    await wired.store.add(wired.fake, _reacting(wired.store, reactions={"field_changed": ["name"]}), idle_ttl=86400)
    _stub_handler(monkeypatch)
    with pytest.raises(FormReactionRequestError, match="do not fit the schema"):
        await react("r1", {"kind": "field_changed", "field": "name"}, {"n": "not-an-int"})


async def test_partial_values_allow_a_subset_and_a_reaction_fed_choice(wired, monkeypatch):
    # Partial (not every field) is fine, and a choice field's membership is NOT enforced here.
    await wired.store.add(
        wired.fake,
        _reacting(wired.store, reactions={"field_changed": ["slot"], "choices": ["slot"], "submitted": True}),
        idle_ttl=86400,
    )
    _stub_handler(monkeypatch)
    assert await react("r1", {"kind": "field_changed", "field": "slot"}, {"slot": "not-in-enum"}) == {}


# --- identity + args passed -----------------------------------------------------------


async def test_handler_receives_stored_identity_and_normalized_event(wired, monkeypatch):
    captured: dict = {}
    await wired.store.add(wired.fake, _reacting(wired.store, reactions={"field_changed": ["name"]}), idle_ttl=86400)
    _stub_handler(monkeypatch, returns={"values": {"name": "Al"}}, captured=captured)
    update = await react("r1", {"kind": "field_changed", "field": "name", "extra": "dropped"}, {"name": "x"})
    assert update == {"values": {"name": "Al"}}
    assert captured["identity"] == "svc-key"  # the STORED continuation identity
    assert captured["reaction_tool"] == "react_tool"
    assert captured["event"] == {"kind": "field_changed", "field": "name"}  # normalized, extra key dropped
    assert captured["values"] == {"name": "x"}


# --- update validation ----------------------------------------------------------------


async def test_update_naming_undeclared_field_is_a_handler_error(wired, monkeypatch):
    await wired.store.add(wired.fake, _reacting(wired.store, reactions={"field_changed": ["name"]}), idle_ttl=86400)
    _stub_handler(monkeypatch, returns={"values": {"ghost": "x"}})
    with pytest.raises(FormReactionHandlerError, match="undeclared fields"):
        await react("r1", {"kind": "field_changed", "field": "name"}, {"name": "x"})


async def test_update_options_require_an_option_bearing_field(wired, monkeypatch):
    await wired.store.add(wired.fake, _reacting(wired.store, reactions={"field_changed": ["name"]}), idle_ttl=86400)
    _stub_handler(monkeypatch, returns={"options": {"name": [{"value": "x"}]}})
    with pytest.raises(FormReactionHandlerError, match="string-enum or array-of-strings"):
        await react("r1", {"kind": "field_changed", "field": "name"}, {"name": "x"})


async def test_update_options_on_a_choice_field_pass(wired, monkeypatch):
    await wired.store.add(
        wired.fake,
        _reacting(wired.store, reactions={"field_changed": ["name"], "choices": ["slot"], "submitted": True}),
        idle_ttl=86400,
    )
    _stub_handler(monkeypatch, returns={"options": {"slot": [{"value": "11am", "label": "11 am"}]}})
    assert await react("r1", {"kind": "field_changed", "field": "name"}, {"name": "x"}) == {
        "options": {"slot": [{"value": "11am", "label": "11 am"}]}
    }


async def test_update_display_requires_a_declared_slot(wired, monkeypatch):
    pages = [{"title": "P", "fields": ["name", "slot", "tags", "n"], "display": [{"kind": "body", "slot": "total"}]}]
    await wired.store.add(
        wired.fake,
        _reacting(wired.store, reactions={"field_changed": ["name"]}, pages=pages),
        idle_ttl=86400,
    )
    _stub_handler(monkeypatch, returns={"display": {"ghost": "x"}})
    with pytest.raises(FormReactionHandlerError, match="undeclared slots"):
        await react("r1", {"kind": "field_changed", "field": "name"}, {"name": "x"})
    _stub_handler(monkeypatch, returns={"display": {"total": "$5"}})
    assert await react("r1", {"kind": "field_changed", "field": "name"}, {"name": "x"}) == {"display": {"total": "$5"}}


# --- statelessness --------------------------------------------------------------------


async def test_react_never_touches_the_durable_record(wired, monkeypatch):
    await wired.store.add(wired.fake, _reacting(wired.store, reactions={"field_changed": ["name"]}), idle_ttl=86400)
    _stub_handler(monkeypatch, returns={"values": {"name": "Al"}})
    await react("r1", {"kind": "field_changed", "field": "name"}, {"name": "x"})
    state = await wired.store.get_state(wired.fake, "r1")
    assert state is not None
    assert state.status == "pending"  # never resolved
    assert state.response is None  # no answer recorded


# --- the handler run: deadline + raise surface loudly ---------------------------------


@asynccontextmanager
async def _fake_bind(identity, *, bound_fingerprint=""):
    yield


async def _run_with_tool(monkeypatch, run_tool, *, deadline):
    monkeypatch.setattr("tai42_skeleton.authz.execution.bind_execution_identity", _fake_bind)
    monkeypatch.setattr(reaction_module, "tai42_app", SimpleNamespace(tools=SimpleNamespace(run_tool=run_tool)))
    return await reaction_module._run_reaction(
        reaction_tool="react_tool",
        identity="svc-key",
        fingerprint=None,
        state_ctx=None,
        asked_by=[],
        interaction_id="r1",
        event={"kind": "submitted"},
        values={},
        deadline=deadline,
    )


async def test_handler_deadline_surfaces_loudly(monkeypatch):
    async def _slow(tool, args, continues_chain=None):
        await asyncio.sleep(1)

    with pytest.raises(FormReactionHandlerError, match="deadline"):
        await _run_with_tool(monkeypatch, _slow, deadline=0.01)


async def test_handler_raise_surfaces_loudly(monkeypatch):
    async def _boom(tool, args, continues_chain=None):
        raise RuntimeError("handler blew up")

    with pytest.raises(FormReactionHandlerError, match="failed"):
        await _run_with_tool(monkeypatch, _boom, deadline=5)


async def test_handler_returns_the_tool_result(monkeypatch):
    async def _ok(tool, args, continues_chain=None):
        return {"values": {"name": "Al"}}

    assert await _run_with_tool(monkeypatch, _ok, deadline=5) == {"values": {"name": "Al"}}


# --- pure helpers ---------------------------------------------------------------------


def test_types_only_schema_strips_enum_and_required():
    out = _types_only_schema({"type": "object", "required": ["slot"], "properties": _SCHEMA["properties"]})
    assert out["required"] == []
    assert out["additionalProperties"] is False
    assert "enum" not in out["properties"]["slot"]


def test_normalize_event_rejects_unknown_kind():
    from tai42_contract.interactions import FormReactions

    with pytest.raises(FormReactionRequestError, match="unknown reaction event kind"):
        _normalize_event({"kind": "wiggled"}, FormReactions(field_changed=["name"]))


def test_validate_update_rejects_unknown_keys():
    with pytest.raises(FormReactionHandlerError, match="unknown keys"):
        _validate_update({"nope": 1}, _SCHEMA, {})


async def test_react_rejects_non_object_values(wired, monkeypatch):
    await wired.store.add(wired.fake, _reacting(wired.store, reactions={"field_changed": ["name"]}), idle_ttl=86400)
    _stub_handler(monkeypatch)
    with pytest.raises(FormReactionRequestError, match="values must be an object"):
        await react("r1", {"kind": "field_changed", "field": "name"}, ["not", "a", "dict"])


def test_is_option_bearing():
    assert _is_option_bearing({"type": "string", "enum": ["a"]}) is True
    assert _is_option_bearing({"type": "string"}) is False
    assert _is_option_bearing({"type": "array", "items": {"type": "string"}}) is True
    assert _is_option_bearing({"type": "integer"}) is False


def test_declared_slots():
    payload = {"pages": [{"display": [{"kind": "body", "slot": "total"}, {"kind": "heading", "text": "x"}]}]}
    assert _declared_slots(payload) == {"total"}
    assert _declared_slots({}) == set()


def test_normalize_event_page_and_submitted():
    from tai42_contract.interactions import FormReactions

    reactions = FormReactions(page_advanced=["Who"], submitted=True)
    assert _normalize_event({"kind": "page_advanced", "page": "Who"}, reactions) == {
        "kind": "page_advanced",
        "page": "Who",
    }
    assert _normalize_event({"kind": "submitted"}, reactions) == {"kind": "submitted"}
    with pytest.raises(FormReactionRequestError, match="not a declared page trigger"):
        _normalize_event({"kind": "page_advanced", "page": "Nope"}, reactions)
    with pytest.raises(FormReactionRequestError, match="object with a 'kind'"):
        _normalize_event("nope", reactions)


def test_normalize_event_submitted_not_a_trigger():
    from tai42_contract.interactions import FormReactions

    with pytest.raises(FormReactionRequestError, match="submitted is not a declared"):
        _normalize_event({"kind": "submitted"}, FormReactions(field_changed=["name"]))


def test_validate_update_value_helpers_reject_malformed():
    with pytest.raises(FormReactionHandlerError, match="'values' must be an object"):
        _validate_update_values("x", _SCHEMA, {"name"})
    with pytest.raises(FormReactionHandlerError, match="do not fit the schema"):
        _validate_update_values({"n": "not-int"}, _SCHEMA, {"n"})
    with pytest.raises(FormReactionHandlerError, match="'options' must be an object"):
        _validate_update_options("x", _SCHEMA["properties"])
    with pytest.raises(FormReactionHandlerError, match="must be a non-empty list"):
        _validate_update_options({"slot": []}, _SCHEMA["properties"])
    with pytest.raises(FormReactionHandlerError, match="are malformed"):
        _validate_update_options({"slot": [{"nope": 1}]}, _SCHEMA["properties"])
    with pytest.raises(FormReactionHandlerError, match="'errors' must be an object"):
        _validate_update_errors("x", {"name"})
    with pytest.raises(FormReactionHandlerError, match="must be strings"):
        _validate_update_errors({"name": 5}, {"name"})
    with pytest.raises(FormReactionHandlerError, match="'display' must be an object"):
        _validate_update_display("x", {})


def test_validate_update_accepts_errors_and_options():
    update = {"errors": {"name": "too short"}, "options": {"slot": [{"value": "11am"}]}}
    assert _validate_update(update, _SCHEMA, {}) == update


# --- enforce_submitted_check: the server-side submit gate every answer door runs ------
#
# The door calls this AFTER the schema check and BEFORE the record. It runs the form's
# ``submitted`` event through the one ``react`` chokepoint (NO prior react call) and refuses
# the answer when the handler returns per-field errors. These pin that it (a) runs the handler
# on a clean submit, (b) raises carrying the handler's errors on a decline, (c) is a no-op
# that never runs the handler when the form declares no ``submitted`` trigger, and (d) lets a
# handler raise surface loudly.


_SUBMITTED_REACTIONS = {"submitted": True, "choices": ["slot"]}


async def test_enforce_submitted_clean_runs_the_handler_and_passes(wired, monkeypatch):
    captured: dict = {}
    request = _reacting(wired.store, reactions=_SUBMITTED_REACTIONS)
    await wired.store.add(wired.fake, request, idle_ttl=86400)
    _stub_handler(monkeypatch, returns={}, captured=captured)
    assert await enforce_submitted_check("r1", AnswerFormat.FORM, request.format_payload, {"slot": "9am"}) is None
    # The handler RAN, on the submitted event, with the answer as its partial values.
    assert captured["event"] == {"kind": "submitted"}
    assert captured["values"] == {"slot": "9am"}


async def test_enforce_submitted_errors_raise_rejected(wired, monkeypatch):
    captured: dict = {}
    request = _reacting(wired.store, reactions=_SUBMITTED_REACTIONS)
    await wired.store.add(wired.fake, request, idle_ttl=86400)
    _stub_handler(monkeypatch, returns={"errors": {"slot": "bad"}}, captured=captured)
    with pytest.raises(SubmittedCheckRejectedError) as exc:
        await enforce_submitted_check("r1", AnswerFormat.FORM, request.format_payload, {"slot": "9am"})
    assert exc.value.errors == {"slot": "bad"}  # the per-field errors ride the raise for the door
    assert captured["event"] == {"kind": "submitted"}  # the handler RAN and DECLINED (did not raise)


async def test_enforce_submitted_without_the_trigger_is_a_noop(wired, monkeypatch):
    captured: dict = {}
    # A FORM whose reactions do NOT declare ``submitted``: the gate returns without running react.
    request = _reacting(wired.store, reactions={"field_changed": ["name"]})
    await wired.store.add(wired.fake, request, idle_ttl=86400)
    _stub_handler(monkeypatch, returns={"errors": {"slot": "bad"}}, captured=captured)
    assert await enforce_submitted_check("r1", AnswerFormat.FORM, request.format_payload, {"slot": "9am"}) is None
    # A FORM carrying NO reactions block at all is equally a no-op.
    assert await enforce_submitted_check("r1", AnswerFormat.FORM, {"schema": _SCHEMA}, {"slot": "9am"}) is None
    assert captured == {}  # the handler was never reached in either case


async def test_enforce_submitted_handler_raise_surfaces_loudly(wired, monkeypatch):
    request = _reacting(wired.store, reactions=_SUBMITTED_REACTIONS)
    await wired.store.add(wired.fake, request, idle_ttl=86400)

    async def _boom(**kwargs):
        raise FormReactionHandlerError("handler blew up")

    monkeypatch.setattr(reaction_module, "_run_reaction", _boom)
    with pytest.raises(FormReactionHandlerError, match="blew up"):
        await enforce_submitted_check("r1", AnswerFormat.FORM, request.format_payload, {"slot": "9am"})
