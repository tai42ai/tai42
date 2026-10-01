"""The two mid-form reaction doors — the unauthenticated channel-callback sibling (under the
ticket) and the authenticated in-app sibling (under the answer door's correlation) — both route
every reaction through the ONE ``react`` chokepoint with the caps/correlation of the door they
sibling, adding nothing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import AnswerFormat, InteractionRequest

from tai42_skeleton.interactions import reaction as reaction_module
from tai42_skeleton.operations import interactions as ops
from tai42_skeleton.operations.errors import ConflictError, ForbiddenError, UpstreamError
from tai42_skeleton.routers import interactions as router

from ._harness import _identity, _json, make_request


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
