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
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.secrets import SecretValue

from tai42_skeleton.operations import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    NotSupportedError,
    UnavailableError,
)
from tai42_skeleton.operations import tool_runs as ops
from tai42_skeleton.operations.decorator import operation_metadata_of
from tai42_skeleton.operations.errors import PermissionDenied
from tai42_skeleton.operations.tool_runs import ToolRunStore
from tai42_skeleton.routers.tool_runs_settings import ToolRunsSettings

from .._fakes.tool_runs_redis import FakeRedis


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

    async def run_tool(self, key, arguments, *, offload_sync=False):
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
        monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=tools))
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


async def test_background_run_of_a_parking_tool_records_parked_not_succeeded(wired):
    # R8: a detached tool-run whose tool async-parks returns the generic SuspendedInteraction
    # sentinel; the recorder reflects a PARKED terminal keyed by the parked interaction id —
    # never a ``succeeded`` record over an unfinished run. GENERIC: any parking tool.
    from tai42_contract.interactions import SuspendedInteraction

    tools = wired.install()
    tools.result = SuspendedInteraction(interaction_id="i-detached")
    out = await ops.submit_run("alpha", {"x": 1})
    await _drain()

    record = await wired.store.get_run(wired.fake, out["run_id"])
    assert record["status"] == "parked"
    assert json.loads(record["result"]) == {"interaction_id": "i-detached", "expiry_at": None}


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
    # so a question the tool raises through ask_user is attributed to the run. The
    # binding lives on the run's own context and is released with the run.
    from tai42_skeleton.interactions.origin import get_interaction_origin

    tools = wired.install()
    seen: dict[str, str | None] = {}

    async def _run_tool(key, arguments, *, offload_sync=False):
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
        raise PermissionDenied("access denied: POST /api/config/env is not permitted")

    wired.monkeypatch.setattr(ops, "authorize_submitted_tool", _deny)
    with pytest.raises(PermissionDenied, match="not permitted"):
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
