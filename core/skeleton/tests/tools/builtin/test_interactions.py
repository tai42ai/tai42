"""Builtin ``ask`` tool: a thin shim over the interactions helper. It must
forward every argument verbatim, return the helper's answer, and never swallow a
timeout.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime

import pytest
from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_contract.access_control.context import reset_request_user_id, set_request_user_id
from tai42_contract.interactions import AnswerMismatchPolicy, InteractionResponse

from tai42_skeleton.access_control.request_scopes import (
    reset_request_identity_claims,
    set_request_identity_claims,
)
from tai42_skeleton.access_control.user import CrossIdentityAudienceError
from tai42_skeleton.interactions import InteractionStore, InteractionTimeoutError
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.settings import interactions_settings
from tai42_skeleton.tools.builtin import interactions as builtin_interactions

from ..._helpers import await_add_event


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    # the interactions surface is OFF with no Redis. These tests exercise the ON
    # feature, so configure its store — the fake connection still stands in; only the
    # presence gate reads this env var. The settings are cached, so clear the cache after
    # setting the env (an earlier test may have cached the unconfigured value) and again on
    # teardown so the configured value does not leak to a later test.
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    interactions_settings.cache_clear()
    yield
    interactions_settings.cache_clear()


@contextmanager
def _restricted(own_id: str, owner: str | None = None) -> Iterator[None]:
    """Bind a RESTRICTED owned-key caller isolated to its OWN id ``own_id``. The owner
    claim (a DISTINCT ``owner-of-{own_id}`` by default) is what MARKS the caller
    restricted, but the isolation identity is its own id — each key is its own island —
    so the write clamp scopes writes to ``own_id``, never the owner."""
    owner_claim = owner if owner is not None else f"owner-of-{own_id}"
    claims_token = set_request_identity_claims({OWNER_USER_ID_CLAIM: owner_claim})
    uid_token = set_request_user_id(own_id)
    try:
        yield
    finally:
        reset_request_user_id(uid_token)
        reset_request_identity_claims(claims_token)


class _RecordingHelper:
    def __init__(self, *, answer: object = None, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self._answer = answer
        self._raise = raise_exc

    async def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        if self._raise is not None:
            raise self._raise
        return self._answer


async def test_ask_forwards_arguments_and_returns_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _RecordingHelper(answer="blue")
    monkeypatch.setattr(builtin_interactions, "_ask", helper)

    result = await builtin_interactions.ask(
        "Favourite colour?",
        answer_format="select",
        options=["red", "blue"],
        schema=None,
        group_id="g1",
        timeout=30.0,
        link=None,
    )

    assert result == "blue"
    assert helper.calls == [
        (
            ("Favourite colour?",),
            {
                "answer_format": "select",
                "options": ["red", "blue"],
                "schema": None,
                "data": None,
                "pages": None,
                "reaction_tool": None,
                "reactions": None,
                "wording": None,
                "group_id": "g1",
                "timeout": 30.0,
                "link": None,
                "channel": None,
                "recipient": None,
                "on_mismatch": AnswerMismatchPolicy.RETRY,
                "audience": None,
                "media": None,
                "mode": "sync",
                "expiry_at": None,
                "to": "user",
                "payload": None,
                "on_expiry": "kill",
            },
        )
    ]


async def test_ask_defaults_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _RecordingHelper(answer="hi")
    monkeypatch.setattr(builtin_interactions, "_ask", helper)

    result = await builtin_interactions.ask("Anything?")

    assert result == "hi"
    assert helper.calls == [
        (
            ("Anything?",),
            {
                "answer_format": "text",
                "options": None,
                "schema": None,
                "data": None,
                "pages": None,
                "reaction_tool": None,
                "reactions": None,
                "wording": None,
                "group_id": None,
                "timeout": None,
                "link": None,
                "channel": None,
                "recipient": None,
                "on_mismatch": AnswerMismatchPolicy.RETRY,
                "audience": None,
                "media": None,
                "mode": "sync",
                "expiry_at": None,
                "to": "user",
                "payload": None,
                "on_expiry": "kill",
            },
        )
    ]


async def test_ask_forwards_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _RecordingHelper(answer="ok")
    monkeypatch.setattr(builtin_interactions, "_ask", helper)

    result = await builtin_interactions.ask("Ping?", channel="telegram")

    assert result == "ok"
    assert helper.calls[0][1]["channel"] == "telegram"


async def test_ask_forwards_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _RecordingHelper(answer="ok")
    monkeypatch.setattr(builtin_interactions, "_ask", helper)

    result = await builtin_interactions.ask("Ping?", channel="telegram", recipient="@ops")

    assert result == "ok"
    assert helper.calls[0][1]["channel"] == "telegram"
    assert helper.calls[0][1]["recipient"] == "@ops"


async def test_ask_forwards_on_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tool's ``on_mismatch`` string is coerced to the contract policy enum and
    # forwarded to the helper, so a tool author can set the bridge digression policy.
    helper = _RecordingHelper(answer="ok")
    monkeypatch.setattr(builtin_interactions, "_ask", helper)

    result = await builtin_interactions.ask("Ping?", channel="telegram", on_mismatch="bridge")

    assert result == "ok"
    assert helper.calls[0][1]["on_mismatch"] is AnswerMismatchPolicy.BRIDGE


async def test_ask_forwards_wording_rejected_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    # The caller's refusal sentence rides the wording catalogue and is forwarded verbatim;
    # on_mismatch defaults to retry.
    helper = _RecordingHelper(answer="ok")
    monkeypatch.setattr(builtin_interactions, "_ask", helper)

    wording = {"answer": {"rejected": "Please answer yes or no ({reason})."}}
    result = await builtin_interactions.ask("Ping?", channel="telegram", wording=wording)

    assert result == "ok"
    assert helper.calls[0][1]["wording"] == wording
    assert helper.calls[0][1]["on_mismatch"] is AnswerMismatchPolicy.RETRY


def test_ask_tool_schema_advertises_the_wording_and_mismatch_fields() -> None:
    # The typed signature fastmcp derives advertises the fields to the LLM: a bounded
    # retry|bridge on_mismatch choice defaulting to retry, and an optional wording object.
    # Neither is required, so every existing ask keeps working unchanged.
    from fastmcp.utilities.types import get_cached_typeadapter

    schema = get_cached_typeadapter(builtin_interactions.ask).json_schema()
    props = schema["properties"]
    assert props["on_mismatch"]["enum"] == ["retry", "bridge"]
    assert props["on_mismatch"]["default"] == "retry"
    assert "wording" in props
    assert "on_mismatch" not in schema.get("required", [])
    assert "wording" not in schema.get("required", [])


async def test_ask_forwards_media(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _RecordingHelper(answer="ok")
    monkeypatch.setattr(builtin_interactions, "_ask", helper)

    media = [{"kind": "image", "url": "https://cdn.example/p.png", "caption": "A product"}]
    result = await builtin_interactions.ask("Which?", media=media)

    assert result == "ok"
    assert helper.calls[0][1]["media"] == media


async def test_ask_forwards_mode_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    helper = _RecordingHelper(answer="ok")
    monkeypatch.setattr(builtin_interactions, "_ask", helper)

    expiry = datetime.now(UTC) + timedelta(hours=1)
    await builtin_interactions.ask("Ping?", mode="async", expiry_at=expiry)

    assert helper.calls[0][1]["mode"] == "async"
    assert helper.calls[0][1]["expiry_at"] == expiry


async def test_ask_forwards_reaction_tool_and_reactions(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reacting-form ask through the TOOL door forwards both reaction halves to the ask
    # seam verbatim, so a form sent via run_tool/agent/MCP can react.
    helper = _RecordingHelper(answer="ok")
    monkeypatch.setattr(builtin_interactions, "_ask", helper)

    reactions = {"field_changed": True, "submitted": True, "choices": ["city"]}
    result = await builtin_interactions.ask(
        "Pick?",
        answer_format="form",
        mode="async",
        reaction_tool="recalc",
        reactions=reactions,
    )

    assert result == "ok"
    assert helper.calls[0][1]["reaction_tool"] == "recalc"
    assert helper.calls[0][1]["reactions"] == reactions


async def test_ask_forwards_reaction_fields_as_none_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # A plain ask forwards both reaction halves as None, so a static form is unchanged.
    helper = _RecordingHelper(answer="ok")
    monkeypatch.setattr(builtin_interactions, "_ask", helper)

    result = await builtin_interactions.ask("Anything?")

    assert result == "ok"
    assert helper.calls[0][1]["reaction_tool"] is None
    assert helper.calls[0][1]["reactions"] is None


async def test_ask_forwards_the_wording_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    # The caller's per-send wording catalogue crosses the TOOL door verbatim, so a form/select/
    # question sent via run_tool/agent/MCP can localize every platform-drawn text.
    helper = _RecordingHelper(answer="ok")
    monkeypatch.setattr(builtin_interactions, "_ask", helper)

    wording = {"form": {"open": "Rellenar", "cancel": "Cancelar", "back": "Atras", "title": "Formulario"}}
    result = await builtin_interactions.ask("Pick?", answer_format="form", wording=wording)

    assert result == "ok"
    assert helper.calls[0][1]["wording"] == wording


async def test_ask_forwards_the_wording_as_none_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # A plain ask forwards the wording catalogue as None, so the surfaces keep their defaults.
    helper = _RecordingHelper(answer="ok")
    monkeypatch.setattr(builtin_interactions, "_ask", helper)

    result = await builtin_interactions.ask("Anything?")

    assert result == "ok"
    assert helper.calls[0][1]["wording"] is None


async def test_ask_propagates_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _RecordingHelper(raise_exc=InteractionTimeoutError("no answer"))
    monkeypatch.setattr(builtin_interactions, "_ask", helper)

    with pytest.raises(InteractionTimeoutError, match="no answer"):
        await builtin_interactions.ask("Still there?")


# -- write-side isolation clamp: a restricted caller touches ONLY its own slice ---
# Exercised end-to-end through the agent-facing builtin shim over the REAL helper
# (no monkeypatched helper), so the clamp is proven on the surface an agent uses.


async def _persisted_audience(fake_redis, store: InteractionStore, *, audience: str | None) -> str | None:
    """Drive the blocking shim to completion with ``audience``: answer the question
    the moment it is persisted, capture the ``audience`` off the stored request, and
    return it (asserting the shim returned the recorded answer)."""
    captured: dict[str, str | None] = {}

    async def answer_when_asked() -> None:
        interaction_id, group_id = await await_add_event(fake_redis, store)
        state = await store.get_state(fake_redis, interaction_id)
        assert state is not None
        captured["audience"] = state.request.audience
        await store.record_answer(
            fake_redis,
            InteractionResponse(
                interaction_id=interaction_id,
                answer="ok",
                answered_by="tester",
                answered_at=datetime.now(UTC),
            ),
            group_id,
            reply_ttl=60,
        )

    answerer = asyncio.create_task(answer_when_asked())
    result = await builtin_interactions.ask("proceed?", audience=audience, timeout=5)
    await answerer
    assert result == "ok"
    return captured["audience"]


async def test_restricted_ask_rejects_other_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # A restricted bob addressing alice is a cross-identity inject/exfil attempt: it
    # is rejected loudly as an authorization denial (CrossIdentityAudienceError, the
    # write-side mirror of the answer door's 403) and NO state is written (no redis
    # connection is even opened, so no InteractionRequest with audience=alice can land
    # in alice's slice).
    calls: list = []

    @asynccontextmanager
    async def tracking_ctx(*args, **kwargs):
        calls.append((args, kwargs))
        yield None

    monkeypatch.setattr(helper_module, "client_ctx", tracking_ctx)
    with (
        _restricted("bob"),
        pytest.raises(CrossIdentityAudienceError, match="a restricted caller may address only its own identity"),
    ):
        await builtin_interactions.ask("proceed?", audience="alice")
    assert calls == []


async def test_restricted_ask_rejects_own_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    # Under key-keyed isolation each key is its own island, so the caller's OWN OWNER
    # is a FOREIGN target with no write privilege. A restricted bob (own id bob,
    # owner-claim alice) addressing its owner alice is rejected loudly as a
    # CrossIdentityAudienceError and NO redis connection is even opened, so no request
    # with audience=alice can land in the owner's slice. Pins that the owner is no
    # longer a privileged write target; an owner-privileged model would let it through.
    calls: list = []

    @asynccontextmanager
    async def tracking_ctx(*args, **kwargs):
        calls.append((args, kwargs))
        yield None

    monkeypatch.setattr(helper_module, "client_ctx", tracking_ctx)
    with (
        _restricted("bob", owner="alice"),
        pytest.raises(CrossIdentityAudienceError, match="a restricted caller may address only its own identity"),
    ):
        await builtin_interactions.ask("proceed?", audience="alice")
    assert calls == []


async def test_restricted_ask_scopes_unset_audience_to_self(monkeypatch, fake_redis, fake_client_ctx) -> None:
    # An unset audience is scoped to the restricted caller's OWN identity, never its owner.
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    store = InteractionStore(helper_module.interactions_settings().key_prefix)
    with _restricted("bob"):
        persisted = await _persisted_audience(fake_redis, store, audience=None)
    assert persisted == "bob"


async def test_restricted_ask_allows_own_identity(monkeypatch, fake_redis, fake_client_ctx) -> None:
    # Addressing its OWN identity passes unchanged.
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    store = InteractionStore(helper_module.interactions_settings().key_prefix)
    with _restricted("bob"):
        persisted = await _persisted_audience(fake_redis, store, audience="bob")
    assert persisted == "bob"


async def test_unrestricted_ask_may_address_any_identity(monkeypatch, fake_redis, fake_client_ctx) -> None:
    # An unrestricted caller (no bound owner claim) is NOT clamped — it may address
    # any identity.
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    store = InteractionStore(helper_module.interactions_settings().key_prefix)
    persisted = await _persisted_audience(fake_redis, store, audience="alice")
    assert persisted == "alice"


# -- the parked-entry tools answer compact entries ---------------------------


_ENTRY_KEYS = {"id": "c1", "status": "asking", "to": "caller", "question": "proceed?", "answer_format": "confirm"}


class _ParkedInteractions:
    """An interactions facet double whose run has one caller ask parked."""

    def _entry(self):
        from tai42_contract.interactions import ParkedEntry

        return ParkedEntry.model_validate(_ENTRY_KEYS)

    def _asks_outcome(self, **kwargs):
        from tai42_contract.interactions import SuspendedInteraction, VisitOutcome

        sentinel = SuspendedInteraction(interaction_id="c1", interaction_ids=["c1"], caller_interaction_ids=["c1"])
        return VisitOutcome(kind="asks", asks=[self._entry()], suspended=sentinel, **kwargs)

    async def list_parked(self):
        return [self._entry()]

    async def resume_parked(self, interaction_id, payload=None):
        return self._asks_outcome(action="resumed")

    async def cancel_parked(self, ids):
        return self._asks_outcome(action="started", cancelled=list(ids))


@pytest.fixture
def parked_facet(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from tai42_contract.app import tai42_app

    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(interactions=_ParkedInteractions()))


async def test_list_parked_answers_each_entry_without_its_unset_fields(parked_facet: None) -> None:
    # An entry's unset optional fields are absent from the tool's answer, never null keys.
    assert await builtin_interactions.list_parked() == [_ENTRY_KEYS]


async def test_resume_parked_answers_its_new_asks_without_their_unset_fields(parked_facet: None) -> None:
    answer = await builtin_interactions.resume_parked("c1", "yes")

    assert answer["asks"] == [_ENTRY_KEYS]


async def test_cancel_parked_answers_its_remaining_asks_without_their_unset_fields(parked_facet: None) -> None:
    answer = await builtin_interactions.cancel_parked(["c0"])

    assert answer["cancelled"] == ["c0"]
    assert answer["asks"] == [_ENTRY_KEYS]
