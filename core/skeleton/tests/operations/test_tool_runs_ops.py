"""Op-level oracles for the background tool-run operations.

These pin ``submit_run`` / ``get_run`` / ``list_tool_runs`` behavior DIRECTLY
through the operation functions (flat params, typed raises) — independent of the
route adapter that the router tests drive — and pin the declared metadata
(destructive, the tier-1 meta-executor block, reload gate, error classes). Redis
is the focused in-memory fake wired at the operation module's ``client_ctx`` seam.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.interactions import AnswerFormat, InteractionRequest, SuspendedInteraction
from tai42_contract.secrets import SecretValue
from tai42_contract.states import StateContext, StateSubject, SubjectCandidates
from tai42_kit.utils.state_context import state_context

from tai42_skeleton.interactions import visit as visit_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.operations import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    NotSupportedError,
    UnavailableError,
)
from tai42_skeleton.operations import tool_runs as ops
from tai42_skeleton.operations.decorator import operation_metadata_of
from tai42_skeleton.operations.errors import PermissionDeniedError
from tai42_skeleton.operations.tool_runs import ToolRunStore
from tai42_skeleton.routers.tool_runs_settings import ToolRunsSettings

from .._fakes.interactions_redis import FakeRedis as InteractionsFakeRedis
from .._fakes.tool_runs_redis import FakeRedis

# The addressed subject a parkable submit/fire names, and the matching candidates a caller ask
# is seeded and indexed under, so the visit's subject listing reaches the ask.
_SUBJECT = StateSubject(target_kind="agent", target_name="a", kind="person", key="pA")
_CANDIDATES = SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pA"})


def _interactions_double() -> SimpleNamespace:
    """The ``app.interactions`` seam the run paths reach: the real visit + its two shaping helpers.

    The background-submit and inline-hook run paths drive the shared visit and shape their terminal
    record through ``tai42_app.interactions.{visit,normalise_started,park_answer}``; this exposes the
    real callables, whose interactions-store seams the tests wire when a park must be read back.
    """
    return SimpleNamespace(
        visit=visit_module.visit,
        park_answer=visit_module.park_answer,
        normalise_started=visit_module.normalise_started,
    )


def _bind_impl(monkeypatch, tools) -> None:
    """Bind a fake ``tai42_app`` exposing the tools double AND the interactions seam."""
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=tools, interactions=_interactions_double()))


def _wire_interactions_store(monkeypatch):
    """Point the visit's interactions-store seams at a fresh fake and mark the store configured.

    Returns the ``(fake, store)`` pair so a caller ask can be seeded and the visit reads it back.
    """
    fake = InteractionsFakeRedis()
    settings = InteractionsSettings()

    @asynccontextmanager
    async def ctx(client_cls, s=None, *, fresh=False, **kwargs):
        yield fake

    monkeypatch.setattr(visit_module, "client_ctx", ctx)
    monkeypatch.setattr(visit_module, "interactions_settings", lambda: settings)
    monkeypatch.setattr(visit_module, "interactions_store_configured", lambda: True)
    return fake, InteractionStore(settings.key_prefix)


async def _seed_caller_ask(store: InteractionStore, fake, iid: str) -> None:
    """Seed one live ``to="caller"`` ask on the ``_CANDIDATES`` subject, so the visit lists it."""
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=60)
    request = InteractionRequest(
        interaction_id=iid,
        group_id="g1",
        question="proceed?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=expiry,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        continuation_state_context=StateContext(door="api", candidates=_CANDIDATES),
        expiry_at=expiry,
        asked_by=[],
    )
    await store.add(fake, request, idle_ttl=86400, to="caller")


@pytest.fixture(autouse=True)
def _tool_runs_store_configured(monkeypatch):
    # the tool-run surface is OFF with no Redis. These tests exercise the ON
    # feature, so configure its store — the fake connection still stands in; only the
    # presence gate reads this env var.
    monkeypatch.setenv("TAI_TOOL_RUNS_REDIS_URL", "redis://localhost:6379/0")


class _FakeTools:
    def __init__(self, registered: set[str] | None = None) -> None:
        self.result: object = None
        self.calls: list[tuple] = []
        self._registered = registered if registered is not None else {"alpha"}

    async def get_tools(self):
        return {name: SimpleNamespace(name=name) for name in self._registered}

    async def run_tool(self, key, arguments, *, offload_sync=False, extras=None):
        self.calls.append((key, arguments, offload_sync))
        return self.result


@pytest.fixture
def wired(monkeypatch):
    fake = FakeRedis()
    settings = ToolRunsSettings()

    @asynccontextmanager
    async def ctx(client_cls, s=None, *, fresh=False, **kwargs):
        yield fake

    monkeypatch.setattr(ops, "client_ctx", ctx)
    monkeypatch.setattr(ops, "tool_runs_settings", lambda: settings)
    monkeypatch.setattr(ops, "_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(ops, "_ACTIVE_RUNS", 0)

    def install(registered: set[str] | None = None) -> _FakeTools:
        tools = _FakeTools(registered)
        _bind_impl(monkeypatch, tools)
        return tools

    yield SimpleNamespace(
        fake=fake, settings=settings, install=install, monkeypatch=monkeypatch, store=ToolRunStore(settings.key_prefix)
    )

    for task in list(ops._SUPERVISORS):
        task.cancel()


async def _drain() -> None:
    tasks = list(ops._SUPERVISORS)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2.0)


async def test_submit_returns_run_id_and_runs_through_the_offload_seam(wired):
    tools = wired.install()
    tools.result = {"ok": 1}
    out = await ops.submit_run("alpha", {"x": 2})
    assert isinstance(out["run_id"], str)
    await _drain()
    # Background path runs through the same seam with the sync offload gate on.
    assert tools.calls == [("alpha", {"x": 2}, True)]
    record = await wired.store.get_run(wired.fake, out["run_id"])
    assert record["status"] == "succeeded"


async def test_background_submit_of_a_user_parking_tool_records_the_suspended_sentinel(wired):
    # A background submit whose tool parks on USER asks alone (no caller ask) records a PARKED
    # terminal carrying the full suspended sentinel — the SAME shape a poller reads off the sync
    # door, never a succeeded record over an unfinished run. GENERIC: any parking tool.
    tools = wired.install()
    tools.result = SuspendedInteraction(interaction_id="i-detached")
    out = await ops.submit_run("alpha", {"x": 1})
    await _drain()

    record = await wired.store.get_run(wired.fake, out["run_id"])
    assert record["status"] == "parked"
    # No caller ask: ``caller_interaction_ids`` is empty, so a poller's submitter knows the park is
    # answered out of band (a user answer), not through ``resume_parked``.
    assert json.loads(record["result"]) == {
        "interaction_id": "i-detached",
        "expiry_at": None,
        "resume_owner": None,
        "interaction_ids": ["i-detached"],
        "caller_interaction_ids": [],
    }


async def test_background_submit_of_a_caller_asking_tool_records_the_ask_entries(wired):
    # A background submit whose tool asks its CALLER records a PARKED terminal whose result is the
    # ask entries — the SAME ``{"asks": [...]}`` shape the sync door returns — so a poller sees the
    # question and answers it through ``resume_parked`` on the submit's subject.
    ifake, istore = _wire_interactions_store(wired.monkeypatch)
    await _seed_caller_ask(istore, ifake, "c1")
    tools = wired.install()
    tools.result = SuspendedInteraction(interaction_id="c1", interaction_ids=["c1"], caller_interaction_ids=["c1"])
    out = await ops.submit_run("alpha", {}, subject=_SUBJECT)
    await _drain()

    record = await wired.store.get_run(wired.fake, out["run_id"])
    assert record["status"] == "parked"
    answer = json.loads(record["result"])
    assert list(answer) == ["asks"]
    assert [entry["id"] for entry in answer["asks"]] == ["c1"]
    assert answer["asks"][0]["to"] == "caller"
    assert answer["asks"][0]["question"] == "proceed?"


async def test_hook_run_recorded_of_a_caller_asking_tool_records_the_ask_entries(wired):
    # The inline hook/trigger path (``run_recorded`` → the ``propagate_failure`` supervisor, which
    # runs INSIDE the hook door's own visit) records the SAME park answer: a caller-ask park carries
    # the ask entries, read back over the fire's own ambient subject context.
    ifake, istore = _wire_interactions_store(wired.monkeypatch)
    await _seed_caller_ask(istore, ifake, "c1")
    tools = wired.install()
    tools.result = SuspendedInteraction(interaction_id="c1", interaction_ids=["c1"], caller_interaction_ids=["c1"])

    with state_context(StateContext(door="conversation", candidates=_CANDIDATES, actor="u-1")):
        await ops.run_recorded("alpha", {})

    entries = await ops.list_tool_runs("alpha")
    assert [entry["status"] for entry in entries] == ["parked"]
    record = await wired.store.get_run(wired.fake, entries[0]["run_id"])
    answer = json.loads(record["result"])
    assert list(answer) == ["asks"]
    assert [entry["id"] for entry in answer["asks"]] == ["c1"]
    assert answer["asks"][0]["to"] == "caller"


async def test_hook_run_recorded_of_a_user_parking_tool_records_the_suspended_sentinel(wired):
    # The inline hook/trigger path of a USER-only park records the same suspended sentinel the
    # submit door does: ``caller_interaction_ids`` empty, no store lookup needed.
    tools = wired.install()
    tools.result = SuspendedInteraction(interaction_id="u-hook")

    with state_context(StateContext(door="conversation", candidates=_CANDIDATES, actor="u-1")):
        await ops.run_recorded("alpha", {})

    entries = await ops.list_tool_runs("alpha")
    assert [entry["status"] for entry in entries] == ["parked"]
    record = await wired.store.get_run(wired.fake, entries[0]["run_id"])
    assert json.loads(record["result"]) == {
        "interaction_id": "u-hook",
        "expiry_at": None,
        "resume_owner": None,
        "interaction_ids": ["u-hook"],
        "caller_interaction_ids": [],
    }


async def test_crash_resume_re_drive_records_caller_asks_under_the_fires_own_subject(wired):
    # The crash-resume re-drive replays the recorded fire under the SAME subject the fire ran under
    # (its persisted ``state_context``), never the reader door's ambient one: a re-driven caller-
    # asking tool records a PARKED terminal carrying the ask entries, read back over that subject.
    from tai42_skeleton.operations.tool_runs import reconcile
    from tai42_skeleton.states.context import current_state_context

    ifake, istore = _wire_interactions_store(wired.monkeypatch)
    await _seed_caller_ask(istore, ifake, "c1")
    tools = wired.install()
    tools.result = SuspendedInteraction(interaction_id="c1", interaction_ids=["c1"], caller_interaction_ids=["c1"])
    seen: dict = {}
    original = tools.run_tool

    async def _capturing(key, arguments, *, offload_sync=False, extras=None):
        ctx = current_state_context()
        seen["door"] = ctx.door if ctx is not None else None
        return await original(key, arguments, offload_sync=offload_sync, extras=extras)

    tools.run_tool = _capturing
    fire_context = StateContext(door="hook", candidates=_CANDIDATES, actor="svc-key")
    record = {
        "tool_name": "alpha",
        "arguments": "{}",
        "extras": "{}",
        "state_context": json.dumps(fire_context.model_dump(mode="json")),
    }
    await reconcile._crash_resume("r-x", record)

    entries = await ops.list_tool_runs("alpha")
    assert [entry["status"] for entry in entries] == ["parked"]
    answer = json.loads((await wired.store.get_run(wired.fake, entries[0]["run_id"]))["result"])
    assert list(answer) == ["asks"]
    assert [entry["id"] for entry in answer["asks"]] == ["c1"]
    assert answer["asks"][0]["to"] == "caller"
    # The deposited context is the FIRE's hook door, never the reader door's ambient one.
    assert seen["door"] == "hook"


async def test_crash_resume_re_drive_records_the_suspended_sentinel_for_a_user_park(wired):
    # A re-driven tool that parks on USER asks alone records the full suspended sentinel — the same
    # shape the sync door returns — under the re-driven fire's subject.
    from tai42_skeleton.operations.tool_runs import reconcile

    _wire_interactions_store(wired.monkeypatch)
    tools = wired.install()
    tools.result = SuspendedInteraction(interaction_id="i-detached")
    fire_context = StateContext(door="hook", candidates=_CANDIDATES)
    record = {
        "tool_name": "alpha",
        "arguments": "{}",
        "extras": "{}",
        "state_context": json.dumps(fire_context.model_dump(mode="json")),
    }
    await reconcile._crash_resume("r-y", record)

    entries = await ops.list_tool_runs("alpha")
    assert [entry["status"] for entry in entries] == ["parked"]
    result = json.loads((await wired.store.get_run(wired.fake, entries[0]["run_id"]))["result"])
    assert result == {
        "interaction_id": "i-detached",
        "expiry_at": None,
        "resume_owner": None,
        "interaction_ids": ["i-detached"],
        "caller_interaction_ids": [],
    }


async def test_crash_resume_re_drive_deposits_no_context_without_a_stored_one(wired):
    # A record with no stored context (a fire that ran under no subject) re-drives with NO context
    # deposited: the run executes and records its result, the ambient context left ``None``.
    from tai42_skeleton.operations.tool_runs import reconcile
    from tai42_skeleton.states.context import current_state_context

    _wire_interactions_store(wired.monkeypatch)
    tools = wired.install()
    tools.result = {"ok": 1}
    seen: dict = {}
    original = tools.run_tool

    async def _capturing(key, arguments, *, offload_sync=False, extras=None):
        seen["ctx"] = current_state_context()
        return await original(key, arguments, offload_sync=offload_sync, extras=extras)

    tools.run_tool = _capturing
    record = {"tool_name": "alpha", "arguments": "{}", "extras": "{}"}
    await reconcile._crash_resume("r-z", record)

    assert seen["ctx"] is None
    entries = await ops.list_tool_runs("alpha")
    assert [entry["status"] for entry in entries] == ["succeeded"]


async def test_background_submit_of_an_unencodable_result_records_failed_read_at_the_poll(wired):
    # A tool whose result cannot be JSON-encoded is refused at the dispatch seam with the named
    # ``ToolResultEncodingError``; the background supervisor's terminal-write path records the run
    # ``failed`` with that message, read cleanly at the poll door — never a 500 at the poll read.
    from tai42_skeleton.tools.binding import ToolResultEncodingError

    tools = wired.install()

    async def _raise(key, arguments, *, offload_sync=False, extras=None):
        raise ToolResultEncodingError(key, "$.token")

    tools.run_tool = _raise
    out = await ops.submit_run("alpha", {})
    await _drain()

    record = await wired.store.get_run(wired.fake, out["run_id"])
    assert record["status"] == "failed"
    assert "alpha" in record["error"]
    assert "$.token" in record["error"]


async def test_background_run_masks_wrapped_secrets_in_the_stored_record(wired):
    # A background run has no live-caller door: a wrapped secret in the result is
    # masked to the placeholder before it lands in the durable record.
    tools = wired.install()
    tools.result = {"token": SecretValue("tok-4242-xyzzy")}
    out = await ops.submit_run("alpha", {})
    await _drain()

    record = await wired.store.get_run(wired.fake, out["run_id"])
    assert record["status"] == "succeeded"
    assert json.loads(record["result"]) == {"token": "[secret]"}
    # The real secret never reaches the persisted record JSON.
    assert "tok-4242-xyzzy" not in record["result"]


async def test_background_run_of_a_secret_preset_masks_the_real_secret_in_the_record(wired):
    # A PRESET run by name through the in-process dispatch. Its forwarding fn
    # re-enters the parent tool's convert_result in-process,
    # which must NOT reveal here — the wrapper has to survive to the background recorder
    # so the durable record carries the placeholder, never the real secret.
    from tai42_skeleton.app.instance import app
    from tai42_skeleton.manifest import Manifest

    manifest = Manifest.model_validate(
        {"tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["vault"]}]}
    )
    async with app.app_context(manifest):
        await app.preset_manager.register("acme_vault", "vault", {"account": "acme"}, [], "Acme vault")
        try:
            out = await ops.submit_run("acme_vault", {})
            await _drain()

            record = await wired.store.get_run(wired.fake, out["run_id"])
            assert record["status"] == "succeeded"
            assert json.loads(record["result"]) == {"account": "acme", "token": "[secret]"}
            # The real secret never reaches the persisted record JSON.
            assert "tok-acme" not in record["result"]
        finally:
            await app.preset_manager.remove("acme_vault")


async def test_background_run_of_a_secret_preset_schema_failure_redacts_the_record_error(wired):
    # A secret preset whose output_schema the REAL value violates: the guard raises,
    # the background recorder persists the failure — but the durable ``error`` text must
    # carry only the redacted judgement (json path), never the real token the schema
    # judged. A verbatim jsonschema message would leak ``tok-x`` into the record.
    from tai42_skeleton.app.instance import app
    from tai42_skeleton.manifest import Manifest

    schema = {
        "type": "object",
        "properties": {"account": {"type": "string"}, "token": {"type": "string", "minLength": 6}},
        "required": ["account", "token"],
    }
    manifest = Manifest.model_validate(
        {"tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["vault"]}]}
    )
    async with app.app_context(manifest):
        await app.preset_manager.register("x_vault", "vault", {"account": "x"}, [], "Short vault", output_schema=schema)
        try:
            out = await ops.submit_run("x_vault", {})
            await _drain()

            record = await wired.store.get_run(wired.fake, out["run_id"])
            assert record["status"] == "failed"
            # The redacted judgement keeps the json path...
            assert "$.token" in record["error"]
            # ...but the real token never reaches the persisted record.
            assert "tok-x" not in record["error"]
        finally:
            await app.preset_manager.remove("x_vault")


async def test_run_binds_its_run_id_as_interaction_origin(wired):
    # The supervisor binds the run's id as the interaction origin for the tool body,
    # so a question the tool raises through ask is attributed to the run. The
    # binding lives on the run's own context and is released with the run.
    from tai42_skeleton.interactions.origin import get_interaction_origin

    tools = wired.install()
    seen: dict[str, str | None] = {}

    async def _run_tool(key, arguments, *, offload_sync=False, extras=None):
        seen["origin"] = get_interaction_origin()
        return {"ok": 1}

    tools.run_tool = _run_tool
    out = await ops.submit_run("alpha", {})
    await _drain()
    assert seen["origin"] == out["run_id"]
    assert get_interaction_origin() is None


async def test_submit_authorizes_the_submitted_tool_before_recording(wired):
    # The submitted tool is authorized against the live caller — with its exact arguments —
    # before a slot is reserved or a record is written.
    wired.install(registered={"alpha"})
    seen: list[tuple] = []

    async def _spy(tool_name, arguments):
        seen.append((tool_name, dict(arguments)))

    wired.monkeypatch.setattr(ops, "authorize_submitted_tool", _spy)
    await ops.submit_run("alpha", {"x": 2})
    assert seen == [("alpha", {"x": 2})]
    await _drain()


async def test_submit_denied_tool_is_refused_before_any_record(wired):
    # A denial from the submitted-tool authorization is the caller's 403, raised before any
    # slot is reserved, record written, or supervisor spawned.
    wired.install(registered={"write_env"})

    async def _deny(tool_name, arguments):
        raise PermissionDeniedError("access denied: POST /api/config/env is not permitted")

    wired.monkeypatch.setattr(ops, "authorize_submitted_tool", _deny)
    with pytest.raises(PermissionDeniedError, match="not permitted"):
        await ops.submit_run("write_env", {"k": "v"})
    assert list(ops._SUPERVISORS) == []
    assert ops._ACTIVE_RUNS == 0


async def test_submit_unknown_tool_raises_not_found_before_any_record(wired):
    wired.install(registered={"alpha"})
    with pytest.raises(NotFoundError, match="unknown tool: nope"):
        await ops.submit_run("nope", {})
    assert list(ops._SUPERVISORS) == []


async def test_submit_at_capacity_raises_unavailable(wired):
    wired.install(registered={"slow"})
    wired.monkeypatch.setattr(ops, "tool_runs_settings", lambda: ToolRunsSettings(max_concurrent_runs=1))
    wired.monkeypatch.setattr(ops, "_ACTIVE_RUNS", 1)  # the only slot is taken
    with pytest.raises(UnavailableError, match="tool-run capacity reached"):
        await ops.submit_run("slow", {})


async def test_get_run_unknown_raises_not_found(wired):
    with pytest.raises(NotFoundError, match="not found"):
        await ops.get_run("does-not-exist")


async def test_get_run_returns_running_view(wired):
    await wired.store.create_run(wired.fake, "r1", "alpha", "2026-01-01T00:00:00", 1.0, wired.settings)
    view = await ops.get_run("r1")
    assert view == {"run_id": "r1", "tool_name": "alpha", "status": "running", "started_at": "2026-01-01T00:00:00"}


async def test_list_tool_runs_empty_for_unknown_tool(wired):
    assert await ops.list_tool_runs("alpha") == []


async def test_list_tool_runs_returns_present_records(wired):
    await wired.store.create_run(wired.fake, "r1", "alpha", "2026-01-01T00:00:00", 1.0, wired.settings)
    entries = await ops.list_tool_runs("alpha")
    assert [e["run_id"] for e in entries] == ["r1"]
    assert "result" not in entries[0]
    assert "error" not in entries[0]


async def test_full_view_carries_the_parsed_resumed_interactions(wired):
    # The terminal record's ``resumed_interactions`` field (a JSON-encoded id list) is
    # parsed back into the full GET view.
    await wired.store.create_run(wired.fake, "r1", "alpha", "2026-01-01T00:00:00", 1.0, wired.settings)
    await wired.store.mark_terminal_if_running(
        wired.fake,
        "r1",
        {
            "status": "succeeded",
            "finished_at": "2026-01-01T00:01:00",
            "result": json.dumps({"ok": 1}),
            "resumed_interactions": json.dumps(["i-a", "i-b"]),
        },
        wired.settings.result_ttl_seconds,
    )
    view = await ops.get_run("r1")
    assert view["resumed_interactions"] == ["i-a", "i-b"]


async def test_list_view_omits_resumed_interactions(wired):
    # The trimmed list view stays id/tool/status/timestamps only — never the resumed list.
    await wired.store.create_run(wired.fake, "r1", "alpha", "2026-01-01T00:00:00", 1.0, wired.settings)
    await wired.store.mark_terminal_if_running(
        wired.fake,
        "r1",
        {
            "status": "succeeded",
            "finished_at": "2026-01-01T00:01:00",
            "result": json.dumps({"ok": 1}),
            "resumed_interactions": json.dumps(["i-a"]),
        },
        wired.settings.result_ttl_seconds,
    )
    entries = await ops.list_tool_runs("alpha")
    assert "resumed_interactions" not in entries[0]


def test_metadata_declares_the_tier1_destructive_submit_and_read_ops():
    submit = operation_metadata_of(ops.submit_run)
    assert submit.destructive is True
    assert submit.meta_executor is True  # a "run any tool by name" door — never MCP-projected
    assert submit.reload_gated is True
    # the store-unconfigured OFF gate adds NotSupportedError (501) beside the
    # capacity UnavailableError (503).
    assert set(submit.error_classes) == {BadRequestError, NotFoundError, NotSupportedError, UnavailableError}

    get = operation_metadata_of(ops.get_run)
    assert get.destructive is False
    assert get.meta_executor is False
    assert set(get.error_classes) == {ForbiddenError, NotFoundError}

    listing = operation_metadata_of(ops.list_tool_runs)
    assert listing.destructive is False
    assert listing.meta_executor is False
    assert set(listing.error_classes) == {BadRequestError}


# -- the store-unconfigured OFF gate ------------------------------------
# With no tool-run Redis the surface is honestly OFF. The gate reads the presence
# env fresh, so delenv-ing BOTH the feature var and the shared default (overriding
# the autouse setenv) forces it — and it fires BEFORE any registry/record work.


async def test_submit_off_when_store_unconfigured_raises_not_supported(monkeypatch):
    # Submit refuses up front with the named 501 code — no tool is authorized, no
    # slot reserved, no record written.
    monkeypatch.delenv("TAI_TOOL_RUNS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    with pytest.raises(NotSupportedError) as exc_info:
        await ops.submit_run("alpha", {"x": 1})
    assert exc_info.value.extra["code"] == "tool-runs-not-configured"


async def test_get_run_off_when_store_unconfigured_raises_not_found(monkeypatch):
    # With no store no run can exist — a 404 byte-identical to a genuine miss, so the
    # door is no oracle for the store's absence.
    monkeypatch.delenv("TAI_TOOL_RUNS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    with pytest.raises(NotFoundError, match="run 'abc' not found"):
        await ops.get_run("abc")


async def test_list_tool_runs_off_when_store_unconfigured_returns_empty(monkeypatch):
    # With no store the honest answer to "my runs of this tool" is the empty list.
    monkeypatch.delenv("TAI_TOOL_RUNS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    assert await ops.list_tool_runs("alpha") == []


# -- submit binds the caller's own execution identity ------------------------


class _IdentityReadingTools(_FakeTools):
    """A tools double whose run_tool records the execution identity it runs under —
    the detached supervisor copies the submitting context, so what this sees is what
    an async-parking tool would rebind its continuation as."""

    def __init__(self) -> None:
        super().__init__()
        self.seen_identities: list[object] = []

    async def run_tool(self, key, arguments, *, offload_sync=False, extras=None):
        from tai42_skeleton.authz.execution_identity import get_execution_identity

        self.seen_identities.append(get_execution_identity())
        return await super().run_tool(key, arguments, offload_sync=offload_sync)


async def test_submit_binds_the_callers_own_key_into_the_detached_run(wired):
    # An HTTP submit carries no execution identity; the door rebuilds the CALLER'S OWN
    # key (live grants) and the spawned supervisor inherits it — the seam an
    # async-parking tool needs to rebind its continuation.
    from tai42_skeleton.authz.identity import CallerIdentity

    tools = _IdentityReadingTools()
    _bind_impl(wired.monkeypatch, tools)
    wired.monkeypatch.setattr(ops, "request_identity", lambda: ("usr-caller", False))

    rebuilt = CallerIdentity(user_id="usr-caller", execution_key_fingerprint="fp-live")

    async def _rebuild(key: str):
        assert key == "usr-caller"
        return rebuilt

    from tai42_skeleton.authz import execution as authz_execution

    wired.monkeypatch.setattr(authz_execution, "rebuild_execution_identity", _rebuild)

    await ops.submit_run("alpha", {"x": 1})
    await _drain()

    assert tools.seen_identities == [rebuilt]
    # The submit request itself is released — the binding lives only in the copy.
    from tai42_skeleton.authz.execution_identity import get_execution_identity

    assert get_execution_identity() is None


async def test_submit_from_a_fire_keeps_the_fires_binding(wired):
    # A hook/schedule fire submits WITH its identity already bound; the door must
    # inherit it untouched, never clobber it with a rebuild. The submit ALSO sees
    # a request identity and the rebuild WOULD return a different identity — only
    # the no-clobber guard keeps the fire's binding, so removing the guard flips
    # the detached run to the caller identity and fails this test.
    from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
    from tai42_skeleton.authz.identity import CallerIdentity

    tools = _IdentityReadingTools()
    _bind_impl(wired.monkeypatch, tools)
    wired.monkeypatch.setattr(ops, "request_identity", lambda: ("usr-caller", False))
    fire_identity = CallerIdentity(user_id="svc-fire", execution_key_fingerprint="fp-fire")

    async def _rebuild(key: str):
        return CallerIdentity(user_id="usr-caller", execution_key_fingerprint="fp-live")

    from tai42_skeleton.authz import execution as authz_execution

    wired.monkeypatch.setattr(authz_execution, "rebuild_execution_identity", _rebuild)

    token = set_execution_identity(fire_identity)
    try:
        await ops.submit_run("alpha", {"x": 1})
        await _drain()
    finally:
        reset_execution_identity(token)

    assert tools.seen_identities == [fire_identity]


async def test_submit_degrades_to_unbound_when_the_rebuild_cannot_answer(wired):
    # A rebuild the infrastructure cannot answer degrades to the pre-bind behavior:
    # the submit succeeds, the detached run stays unbound, nothing propagates.
    from tai42_skeleton.authz import execution as authz_execution
    from tai42_skeleton.authz.execution_identity import get_execution_identity

    tools = _IdentityReadingTools()
    _bind_impl(wired.monkeypatch, tools)
    wired.monkeypatch.setattr(ops, "request_identity", lambda: ("usr-caller", False))

    async def _rebuild_raises(key: str):
        raise RuntimeError("policy store unreachable")

    wired.monkeypatch.setattr(authz_execution, "rebuild_execution_identity", _rebuild_raises)

    out = await ops.submit_run("alpha", {"x": 1})
    await _drain()

    assert isinstance(out["run_id"], str)
    assert tools.seen_identities == [None]
    assert get_execution_identity() is None


async def test_submit_unauthenticated_binds_nothing(wired):
    # No caller id (gate off / anonymous): the run stays identity-less, exactly as
    # before — the async-ask seam then fail-closes loudly inside the tool.
    tools = _IdentityReadingTools()
    _bind_impl(wired.monkeypatch, tools)
    wired.monkeypatch.setattr(ops, "request_identity", lambda: (None, False))

    await ops.submit_run("alpha", {"x": 1})
    await _drain()

    assert tools.seen_identities == [None]
