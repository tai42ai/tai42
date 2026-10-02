"""The two mid-form reaction doors — the unauthenticated channel-callback sibling (under the
ticket) and the authenticated in-app sibling (under the answer door's correlation) — both route
every reaction through the ONE ``react`` chokepoint with the caps/correlation of the door they
sibling, adding nothing."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import AnswerFormat, InteractionRequest

from tai42_skeleton.interactions import reaction as reaction_module
from tai42_skeleton.operations import interactions as ops
from tai42_skeleton.operations.errors import ConflictError, ForbiddenError, UpstreamError
from tai42_skeleton.routers import interactions as router

from ._harness import _identity, _json, _seed_reacting_form, make_request

# The callback door reads ``fire_continuation_after_claim`` as its OWN module global; resolve the
# submodule through ``sys.modules`` (the package attribute ``callback`` is the route function, not
# the module) so a clean-record test can stub the detached continuation fire on it.
callback_module = sys.modules["tai42_skeleton.routers.interactions.callback"]


def _reacting_req(
    store, *, iid="r1", gid="rg", audience=None, reaction_tool: str | None = "react_tool"
) -> InteractionRequest:
    now = datetime.now(UTC)
    future = now + timedelta(hours=1)
    payload: dict = {"schema": {"type": "object", "properties": {"name": {"type": "string"}}}}
    overrides: dict = {}
    if reaction_tool is not None:
        payload["reactions"] = {"field_changed": ["name"]}
        overrides = {
            "mode": "async",
            "continuation_tool": "resume",
            "continuation_identity": "svc-key",
            "expiry_at": future,
            "reaction_tool": reaction_tool,
        }
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="Fill?",
        answer_format=AnswerFormat.FORM,
        format_payload=payload,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=future,
        audience=audience,
        **overrides,
    )


def _wire_reaction(wired, fake_client_ctx, *, run=None, raises=None):
    wired.monkeypatch.setattr(reaction_module, "client_ctx", fake_client_ctx)
    wired.monkeypatch.setattr(reaction_module, "interactions_settings", lambda: wired.settings)

    async def _run(**kwargs):
        if raises is not None:
            raise raises
        return {"values": {"name": "Al"}} if run is None else run

    wired.monkeypatch.setattr(reaction_module, "_run_reaction", _run)


def _body(event, values) -> bytes:
    return json.dumps({"event": event, "values": values}).encode()


# === the unauthenticated channel-callback reaction door =====================


async def test_callback_react_runs_the_chokepoint(wired, fake_client_ctx):
    _wire_reaction(wired, fake_client_ctx)
    await wired.store.add(wired.fake, _reacting_req(wired.store), idle_ttl=86400, ticket="TKT", ticket_ttl=60)
    body = _body({"kind": "field_changed", "field": "name"}, {"name": "x"})
    resp = await router.callback_react(make_request("POST", path_params={"ticket": "TKT"}, body=body))
    assert resp.status_code == 200
    assert _json(resp)["data"]["update"] == {"values": {"name": "Al"}}


async def test_callback_react_unknown_ticket_is_404(wired, fake_client_ctx):
    _wire_reaction(wired, fake_client_ctx)
    resp = await router.callback_react(
        make_request("POST", path_params={"ticket": "NOPE"}, body=_body({"kind": "submitted"}, {}))
    )
    assert resp.status_code == 404


async def test_callback_react_bad_body_is_400(wired, fake_client_ctx):
    _wire_reaction(wired, fake_client_ctx)
    await wired.store.add(wired.fake, _reacting_req(wired.store), idle_ttl=86400, ticket="TKT", ticket_ttl=60)
    resp = await router.callback_react(make_request("POST", path_params={"ticket": "TKT"}, body=b'{"values": {}}'))
    assert resp.status_code == 400


async def test_callback_react_on_static_form_is_409(wired, fake_client_ctx):
    _wire_reaction(wired, fake_client_ctx)
    await wired.store.add(
        wired.fake, _reacting_req(wired.store, reaction_tool=None), idle_ttl=86400, ticket="TKT", ticket_ttl=60
    )
    resp = await router.callback_react(
        make_request("POST", path_params={"ticket": "TKT"}, body=_body({"kind": "submitted"}, {}))
    )
    assert resp.status_code == 409


async def test_callback_react_handler_error_is_502(wired, fake_client_ctx):
    _wire_reaction(wired, fake_client_ctx, raises=reaction_module.FormReactionHandlerError("boom"))
    await wired.store.add(wired.fake, _reacting_req(wired.store), idle_ttl=86400, ticket="TKT", ticket_ttl=60)
    body = _body({"kind": "field_changed", "field": "name"}, {"name": "x"})
    resp = await router.callback_react(make_request("POST", path_params={"ticket": "TKT"}, body=body))
    assert resp.status_code == 502


# === the authenticated in-app reaction operation ============================


async def test_react_operation_runs_the_chokepoint(wired, fake_client_ctx):
    _wire_reaction(wired, fake_client_ctx)
    await wired.store.add(wired.fake, _reacting_req(wired.store), idle_ttl=86400)
    result = await ops.react_interaction("r1", {"kind": "field_changed", "field": "name"}, {"name": "x"})
    assert result == {"interaction_id": "r1", "update": {"values": {"name": "Al"}}}


async def test_react_operation_on_static_form_conflicts(wired, fake_client_ctx):
    _wire_reaction(wired, fake_client_ctx)
    await wired.store.add(wired.fake, _reacting_req(wired.store, reaction_tool=None), idle_ttl=86400)
    with pytest.raises(ConflictError):
        await ops.react_interaction("r1", {"kind": "submitted"}, {})


async def test_react_operation_honours_the_audience_gate(wired, fake_client_ctx):
    _wire_reaction(wired, fake_client_ctx)
    await wired.store.add(wired.fake, _reacting_req(wired.store, audience="alice"), idle_ttl=86400)
    # A restricted caller that is not the addressed identity is refused — the answer door's gate.
    with _identity(user_id="bob", owner="acme"), pytest.raises(ForbiddenError):
        await ops.react_interaction("r1", {"kind": "field_changed", "field": "name"}, {"name": "x"})


async def test_react_operation_handler_error_is_upstream(wired, fake_client_ctx):
    _wire_reaction(wired, fake_client_ctx, raises=reaction_module.FormReactionHandlerError("boom"))
    await wired.store.add(wired.fake, _reacting_req(wired.store), idle_ttl=86400)
    with pytest.raises(UpstreamError):
        await ops.react_interaction("r1", {"kind": "field_changed", "field": "name"}, {"name": "x"})


# === the server-side submitted check on the channel-callback answer door ====
#
# The channel-typed callback answer door (``_claim_channel_typed``) runs the form's
# ``submitted`` handler, after the schema check and before the record, via
# ``enforce_submitted_check``. The answer is POSTed DIRECTLY under the ticket with NO prior
# reaction call; a handler returning per-field errors refuses the answer as a 400 carrying
# ``errors`` + ``retry_in_place`` (the form stays pending), while a clean handler records it.

_SUBMITTED_SCHEMA = {"type": "object", "properties": {"slot": {"type": "string", "enum": ["9am", "10am"]}}}
_SUBMITTED_REACTIONS = {"submitted": True, "choices": ["slot"]}


def _wire_submitted(wired, fake_client_ctx, *, returns, captured) -> None:
    # The door reaches ``enforce_submitted_check`` -> ``react`` -> ``_run_reaction`` through the
    # reaction module's own seams: point its store at the same fake and stub the handler run.
    wired.monkeypatch.setattr(reaction_module, "client_ctx", fake_client_ctx)
    wired.monkeypatch.setattr(reaction_module, "interactions_settings", lambda: wired.settings)

    async def _run(**kwargs):
        captured.update(kwargs)
        return returns

    wired.monkeypatch.setattr(reaction_module, "_run_reaction", _run)


async def test_callback_answer_submitted_rejection_refuses_and_keeps_pending(wired, fake_client_ctx):
    captured: dict = {}
    _wire_submitted(wired, fake_client_ctx, returns={"errors": {"slot": "taken"}}, captured=captured)
    await _seed_reacting_form(wired, schema=_SUBMITTED_SCHEMA, reactions=_SUBMITTED_REACTIONS)
    body = json.dumps({"answer": {"slot": "9am"}}).encode()
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=body))
    assert resp.status_code == 400
    payload = _json(resp)
    assert payload["errors"] == {"slot": "taken"}  # the handler's per-field errors ride the refusal
    assert payload["retry_in_place"] is True
    assert captured["event"] == {"kind": "submitted"}  # the server RAN the submit check with the answer
    assert captured["values"] == {"slot": "9am"}
    # Refused: the form stays pending. If the enforcement were removed the answer would record and
    # the door would answer 200 "answered" — these assertions would fail.
    state = await wired.store.get_state(wired.fake, "i1")
    assert state is not None
    assert state.status == "pending"
    assert state.response is None


async def test_callback_answer_submitted_clean_records_the_answer(wired, fake_client_ctx):
    # The clean submit check lets the answer record; the detached continuation fire a claimed
    # async park schedules is a separate concern, stubbed to a no-op here.
    async def _noop_fire(*args, **kwargs):
        return None

    wired.monkeypatch.setattr(callback_module, "fire_continuation_after_claim", _noop_fire)
    captured: dict = {}
    _wire_submitted(wired, fake_client_ctx, returns={}, captured=captured)
    await _seed_reacting_form(wired, schema=_SUBMITTED_SCHEMA, reactions=_SUBMITTED_REACTIONS)
    body = json.dumps({"answer": {"slot": "9am"}}).encode()
    resp = await router.callback(make_request("POST", path_params={"ticket": "TKT"}, body=body))
    assert resp.status_code == 200
    assert _json(resp)["data"]["status"] == "answered"
    assert captured["event"] == {"kind": "submitted"}  # the server RAN the submit check here too
    state = await wired.store.get_state(wired.fake, "i1")
    assert state is not None
    assert state.status == "answered"
    assert state.response is not None
    assert state.response.answer == {"slot": "9am"}
