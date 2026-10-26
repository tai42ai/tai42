"""A key one process of the fleet starts with belongs to the deployment for the whole fleet.

The settings store is shared by every process on the bus, so a key that ANY bus member (a
serve worker or a backend runtime) starts with, non-empty, belongs to the deployment
everywhere: no save sets it on any door, the settings schema reports it as set by the
deployment, and no process applies a stored copy of it. A process the deployment did not give
the value runs what it would run with no store at all — the field default here.

The stack is the serve + backend recycle stack, with ``TAI_LOG_LEVEL`` (the kit
``LoggingSettings`` field, hot-class, default ``INFO``) set in the BACKEND process env only,
so every observation is made through the serve worker, which was not started with it.

A FastMCP setting the backend alone starts with (``FASTMCP_DOCKET_URL``) is refused on the
serve worker under every name FastMCP reads for it.

A recycle-class key (``BACKEND_MANIFEST_KEY``, bound at boot) follows the same rule on every
process: a worker that would run a stored copy of a key another worker's deployment sets is
recycled by the worker that joined with it, so after the roll no process runs the stored copy
and no reload names a worker awaiting a respawn.

A bus URL kept only in the store, which no worker's deployment sets, counts: every process
reaches the bus through it, withdraws at boot the stored copies of keys another worker's
deployment sets, and the fleet settles without recycling. A process that reaches the bus only
through that stored URL while another worker's deployment sets it refuses to boot, naming it.

The env-reader description a presence row names outlives the row while the backend beats with a
shorter heartbeat TTL than the serve worker.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import redis

from tai42_e2e import child_env, wait_for_async
from tai42_e2e.binaries import spawn_expect_refusal, tai_bin
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.manifests import build_recycle_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.topology import StackConfig
from tai42_e2e.waiting import WaitTimeoutError, wait_absent_async

_KEY = "TAI_LOG_LEVEL"
_BACKEND_VALUE = "WARNING"
_FIELD_DEFAULT = "INFO"
_STORED_VALUE = "ERROR"
_IGNORED_WARNING = f"stored env keys ignored, the deployment sets them and its value wins: {_KEY}"

pytestmark = pytest.mark.needs("process", "mutable", "files")


def _backend_owned_stack(fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub) -> TaiStack:
    """The recycle stack whose backend process alone is started with ``_KEY``."""
    stack = fresh_stack(
        build_recycle_stack,
        resource_kwargs={"llm_base_url": llm_stub.base_url},
        process_env_overrides={"backend": {_KEY: _BACKEND_VALUE}},
    )
    assert stack.process("backend").env.get(_KEY) == _BACKEND_VALUE
    assert _KEY not in stack.process("serve").env, "the serve worker must start without the key"
    assert _KEY not in stack.config.stored_env
    return stack


async def _schema_field(stack: TaiStack, env_var: str) -> dict[str, Any]:
    """The settings-schema row of the field read from ``env_var``, as the serve worker answers."""
    schema = await stack.api().get("/api/config/settings-schema", retry_on_reloading=True)
    rows = [field for group in schema["groups"] for field in group["fields"] if field["env_var"] == env_var]
    assert len(rows) == 1, f"expected one settings-schema field for {env_var}: {rows}"
    return rows[0]


async def test_a_key_only_the_backend_starts_with_is_refused_and_reported_on_serve(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = _backend_owned_stack(fresh_stack, llm_stub)
    api = stack.api()

    field = await _schema_field(stack, _KEY)
    assert field["value_source"] == "deployment", field

    before = await api.get("/api/config/env", retry_on_reloading=True)
    resp = await api.request_raw("POST", "/api/config/env", json={"env": {_KEY: "DEBUG", "E2E_SIBLING": "x"}})
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert "Refusing an env write setting keys set by the deployment:" in error, error
    assert _KEY in error, error
    assert await api.get("/api/config/env", retry_on_reloading=True) == before


async def test_no_process_applies_a_stored_copy_of_a_key_only_the_backend_starts_with(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = _backend_owned_stack(fresh_stack, llm_stub)
    api = stack.api()
    backend_log = stack.process("backend").log_path
    offset = len(backend_log.read_text(encoding="utf-8", errors="replace"))

    # A copy of the key lands in the store outside the app (a hand edit of the ``.env``),
    # then a fleet reload.
    stack.config.stored_env[_KEY] = _STORED_VALUE
    child_env.render_env_file(stack)
    await api.post("/api/config/reload", json={}, retry_on_reloading=True)

    view = await api.get("/api/config/env", retry_on_reloading=True)
    assert view["env"].get(_KEY) == _STORED_VALUE, view
    assert view["deployment_keys"] == [_KEY], view

    # The serve worker runs its field default, not the stored copy.
    field = await _schema_field(stack, _KEY)
    assert (field["value_source"], field["value"]) == ("deployment", _FIELD_DEFAULT), field

    # The backend, which the deployment gave the value, names the ignored stored copy.
    async def backend_warned() -> bool:
        return _IGNORED_WARNING in backend_log.read_text(encoding="utf-8", errors="replace")[offset:]

    await wait_for_async(
        backend_warned, deadline=60.0, message=f"the backend never logged the ignored stored copy of {_KEY}"
    )


# -- a FastMCP setting ------------------------------------------------------------

# FastMCP's docket settings, read under their own name and, through FastMCP's settings, under
# the delimited name and the docket's JSON name.
_DOCKET_KEY = "FASTMCP_DOCKET_URL"
_DOCKET_OTHER_NAMES = ("FASTMCP_DOCKET__URL", "FASTMCP_DOCKET")


@pytest.mark.parametrize("name", _DOCKET_OTHER_NAMES)
async def test_a_fastmcp_setting_only_the_backend_starts_with_is_refused_on_serve_under_every_name_fastmcp_reads(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub, name: str
) -> None:
    stack = fresh_stack(
        build_recycle_stack,
        resource_kwargs={"llm_base_url": llm_stub.base_url},
        process_env_overrides={"backend": {_DOCKET_KEY: "memory://"}},
    )
    assert not any(key.upper().startswith("FASTMCP_DOCKET") for key in stack.process("serve").env), (
        "the serve worker must start without a docket setting"
    )
    api = stack.api()
    value = '{"url": "memory://stored"}' if name == "FASTMCP_DOCKET" else "memory://stored"

    before = await api.get("/api/config/env", retry_on_reloading=True)
    resp = await api.request_raw("POST", "/api/config/env", json={"env": {name: value}})
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert "Refusing an env write setting keys set by the deployment:" in error, error
    assert name in error, error
    assert await api.get("/api/config/env", retry_on_reloading=True) == before


# -- a recycle-class key ---------------------------------------------------------

# A recycle-class field on the loaded backend (bound at boot, ``reload_class = "recycle"``).
_RECYCLE_KEY = "BACKEND_MANIFEST_KEY"
_RECYCLE_DEFAULT = "MANIFEST_KEY"
_RECYCLE_STORED = "E2E_STORED_MANIFEST_KEY"
_RECYCLE_DEPLOYED = "E2E_DEPLOYED_MANIFEST_KEY"
_RECYCLE_IGNORED_WARNING = f"stored env keys ignored, the deployment sets them and its value wins: {_RECYCLE_KEY}"

_RECYCLE_NEEDS = pytest.mark.needs(
    "kind:backend",
    "store:redis",
    "topology:supervised",
    "setting:TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90",
    "setting:TAI_BUS_APPLY_TIMEOUT=330",
)


def _recycle_stack_storing(stored: dict[str, str]) -> Callable[..., StackConfig]:
    """The recycle stack builder whose store holds ``stored`` before any process boots."""

    def build(*args: Any) -> StackConfig:
        config = build_recycle_stack(*args)
        config.stored_env.update(stored)
        return config

    return build


def _lives(stack: TaiStack) -> dict[str, tuple[str, int]]:
    """The live census ``{name: (kind, generation)}``."""
    return {worker.name: (worker.kind, worker.generation) for worker in stack.census()}


async def _awaiting_respawn(stack: TaiStack) -> dict[str, list[str]] | None:
    """Each worker's ``awaiting_respawn`` from one fleet reload, or ``None`` when not every worker applied it."""
    report = await stack.api().post("/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True)
    if report["ok"] is not True or any(row["outcome"] != "applied" for row in report["results"]):
        return None
    return {row["name"]: row["payload"]["awaiting_respawn"] for row in report["results"]}


async def _wait_no_worker_awaits_a_respawn(stack: TaiStack) -> None:
    """Hold until every census row is ready and a fleet reload names no worker awaiting a respawn."""
    last: list[dict[str, list[str]] | None] = []

    async def converged() -> bool:
        if any(worker.state != "ready" for worker in stack.census()):
            return False
        awaiting = await _awaiting_respawn(stack)
        last[:] = [awaiting]
        return awaiting is not None and all(keys == [] for keys in awaiting.values())

    try:
        await wait_for_async(converged, deadline=240.0, interval=2.0, message="a worker still awaits a respawn")
    except WaitTimeoutError as exc:
        raise AssertionError(f"{exc}; the last fleet reload: {last}") from exc


async def _assert_serve_runs_the_field_default(stack: TaiStack) -> None:
    """The serve worker reports the recycle-class field as the deployment's and runs its default."""
    field = await _schema_field(stack, _RECYCLE_KEY)
    assert (field["value_source"], field["value"]) == ("deployment", _RECYCLE_DEFAULT), field
    view = await stack.api().get("/api/config/env", retry_on_reloading=True)
    assert view["env"].get(_RECYCLE_KEY) == _RECYCLE_STORED, view
    assert _RECYCLE_KEY in view["deployment_keys"], view


@pytest.mark.timeout(400)
@_RECYCLE_NEEDS
async def test_no_process_runs_a_recycle_class_stored_copy_of_a_key_only_the_backend_starts_with(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = fresh_stack(
        _recycle_stack_storing({_RECYCLE_KEY: _RECYCLE_STORED}),
        resource_kwargs={"llm_base_url": llm_stub.base_url},
        process_env_overrides={"backend": {_RECYCLE_KEY: _RECYCLE_DEPLOYED}},
    )
    assert _RECYCLE_KEY not in stack.process("serve").env, "the serve worker must start without the key"

    # Whichever process booted first, once the fleet settles no worker runs the stored copy.
    await _wait_no_worker_awaits_a_respawn(stack)
    await _assert_serve_runs_the_field_default(stack)


@pytest.mark.timeout(500)
@_RECYCLE_NEEDS
async def test_a_worker_that_ran_a_stored_copy_is_recycled_when_a_deployment_starts_setting_the_key(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = fresh_stack(
        _recycle_stack_storing({_RECYCLE_KEY: _RECYCLE_STORED}),
        resource_kwargs={"llm_base_url": llm_stub.base_url},
    )
    # No deployment sets the key: every worker runs the stored value, and none awaits a respawn.
    awaiting = await _awaiting_respawn(stack)
    assert awaiting is not None, "not every worker applied the fleet reload"
    assert all(keys == [] for keys in awaiting.values()), awaiting
    field = await _schema_field(stack, _RECYCLE_KEY)
    assert (field["value_source"], field["value"]) == ("stored", _RECYCLE_STORED), field

    before = _lives(stack)
    serve_before = {name: gen for name, (kind, gen) in before.items() if kind == "serve"}
    assert serve_before, f"no serve life before the respawn: {before}"
    backend_log = stack.process("backend").log_path
    offset = len(backend_log.read_text(encoding="utf-8", errors="replace"))

    # The backend's deployment starts setting the key; its supervisor starts it again.
    stack.respawn_with_process_env("backend", {_RECYCLE_KEY: _RECYCLE_DEPLOYED})

    # The backend that joined recycles the serve worker, which ran the stored copy: one new life.
    stack.wait_generation_bump("serve", serve_before, deadline=240.0)
    await _wait_no_worker_awaits_a_respawn(stack)
    serve_after = {name: gen for name, (kind, gen) in _lives(stack).items() if kind == "serve"}
    assert serve_after == {name: gen + 1 for name, gen in serve_before.items()}, (serve_before, serve_after)
    await _assert_serve_runs_the_field_default(stack)

    # The backend, which the deployment now gives the value, names the ignored stored copy.
    assert _RECYCLE_IGNORED_WARNING in backend_log.read_text(encoding="utf-8", errors="replace")[offset:]


# -- a bus URL kept only in the store ---------------------------------------------

_BUS_URL_KEY = "TAI_BUS_REDIS_URL"
# A recycle-class bus setting: the heartbeat a process's bus is built under.
_HEARTBEAT_KEY = "TAI_BUS_HEARTBEAT_TTL"
_HEARTBEAT_STORED = "21"
_HEARTBEAT_DEPLOYED = "17"
_HEARTBEAT_DEFAULT = 15.0
_WITHDRAWN_WARNING = "stored env keys ignored, a worker's deployment sets them and its value wins: "


def _bus_url_stored_stack(*args: Any) -> StackConfig:
    """The recycle stack whose processes reach the bus only through the stored bus URL.

    The store also holds a heartbeat, which the backend's deployment sets as well.
    """
    config = build_recycle_stack(*args)
    return replace(config, bus_url_stored=True, stored_env={**config.stored_env, _HEARTBEAT_KEY: _HEARTBEAT_STORED})


@pytest.mark.timeout(500)
@_RECYCLE_NEEDS
async def test_a_fleet_that_reaches_its_bus_through_a_stored_url_settles_without_recycling(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = fresh_stack(
        _bus_url_stored_stack,
        resource_kwargs={"llm_base_url": llm_stub.base_url},
        process_env_overrides={"backend": {_HEARTBEAT_KEY: _HEARTBEAT_DEPLOYED}},
    )
    for name in ("serve", "backend"):
        assert _BUS_URL_KEY not in stack.process(name).env, f"{name} must start without the bus URL"
    assert _HEARTBEAT_KEY not in stack.process("serve").env, "the serve worker must start without the heartbeat"

    await _wait_no_worker_awaits_a_respawn(stack)
    settled = _lives(stack)

    largest_pttls_ms: dict[str, tuple[str, int]] = {}

    async def a_worker_left_ready_or_its_life() -> dict[str, Any] | None:
        census = stack.census()
        lives = _lives(stack)
        if any(worker.state != "ready" for worker in census) or lives != settled:
            return {"census": census, "lives": lives}
        for name, (kind, pttl) in _presence_pttls_ms(stack).items():
            largest_pttls_ms[name] = (kind, max(pttl, largest_pttls_ms.get(name, (kind, 0))[1]))
        return None

    # Over two heartbeat TTLs every worker stays ready on the same life.
    await wait_absent_async(
        a_worker_left_ready_or_its_life,
        window=2 * float(_HEARTBEAT_STORED),
        interval=1.0,
        message=f"a worker left ready or its life {settled}",
    )

    awaiting = await _awaiting_respawn(stack)
    assert awaiting is not None, "not every worker applied the fleet reload"
    assert all(keys == [] for keys in awaiting.values()), awaiting
    assert _lives(stack) == settled, (settled, _lives(stack))

    # Each worker heartbeats with the TTL it runs: the serve worker the field default, not the
    # stored copy it withdrew, and the backend the value its deployment sets. Sampled every
    # second across several refreshes, the largest PTTL is within a second of the running TTL.
    heartbeat_ms = {"serve": _HEARTBEAT_DEFAULT * 1000, "backend": float(_HEARTBEAT_DEPLOYED) * 1000}
    assert {kind for kind, _ in largest_pttls_ms.values()} == set(heartbeat_ms), largest_pttls_ms
    for name, (kind, pttl) in largest_pttls_ms.items():
        assert heartbeat_ms[kind] - 1500 < pttl <= heartbeat_ms[kind], (name, kind, pttl)

    # The serve worker withdrew the stored heartbeat the backend's deployment sets, and kept
    # the stored bus URL no deployment sets.
    serve_log = stack.process("serve").log_path.read_text(encoding="utf-8", errors="replace")
    withdrawn = [line for line in serve_log.splitlines() if _WITHDRAWN_WARNING in line]
    assert any(_HEARTBEAT_KEY in line for line in withdrawn), withdrawn
    assert not any(_BUS_URL_KEY in line for line in withdrawn), withdrawn


def _presence_pttls_ms(stack: TaiStack) -> dict[str, tuple[str, int]]:
    """Each live worker's kind and the remaining PTTL of its presence key, by slot name.

    A worker sets its presence key with its running heartbeat TTL as the PTTL and refreshes it
    every third of that TTL, so the largest PTTL seen across a refresh is that TTL.
    """
    resources = stack.resources
    client = redis.Redis.from_url(resources.bus_redis_url, decode_responses=True)
    try:
        pttls: dict[str, tuple[str, int]] = {}
        for worker in stack.census():
            pttl = client.pttl(f"{resources.bus_namespace}:bus:presence:{worker.name}")
            if not isinstance(pttl, int):
                raise TypeError(f"PTTL of {worker.name!r} is not an integer: {pttl!r}")
            if pttl > 0:
                pttls[worker.name] = (worker.kind, pttl)
        return pttls
    finally:
        client.close()


def _recycle_stack_storing_the_bus_url(res: Any, variants: Any) -> StackConfig:
    """The recycle stack whose store also holds a copy of the bus URL every process is started with."""
    config = build_recycle_stack(res, variants)
    config.stored_env[_BUS_URL_KEY] = res.bus_redis_url
    return config


@pytest.mark.timeout(300)
@_RECYCLE_NEEDS
async def test_a_process_that_reaches_the_bus_only_through_a_stored_url_another_worker_is_started_with_refuses_to_boot(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub, tmp_path: Path
) -> None:
    stack = fresh_stack(_recycle_stack_storing_the_bus_url, resource_kwargs={"llm_base_url": llm_stub.base_url})
    before = _lives(stack)

    # Another serve process of the same deployment, started without the bus URL.
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env = child_env.child_env(stack, str(tmpdir), "serve")
    del env[_BUS_URL_KEY]
    argv = [tai_bin(), "serve", "--host", "127.0.0.1", "--port", "0", "--workers", "1"]
    stderr = spawn_expect_refusal(argv, env, env["TAI_CONFIG_DIR_PATH"], timeout=120.0)

    assert f"reaches only through a stored copy of {_BUS_URL_KEY}" in stderr, stderr
    assert stack.resources.bus_redis_url not in stderr, stderr
    # The fleet the deployment gave the bus stays up on the same lives.
    assert all(worker.state == "ready" for worker in stack.census())
    assert _lives(stack) == before, (before, _lives(stack))


# -- the env-reader description a presence row names ------------------------------

_SHORT_HEARTBEAT = "3"


def _rows_whose_reader_body_dies_first(stack: TaiStack) -> dict[str, tuple[str, int, int]] | None:
    """Each live row whose env-reader description has less life left than the row itself.

    Keyed by slot name: the digest the row names, the row's PTTL and the description's PTTL
    (``-2`` when it is gone). ``None`` when every description outlives every row naming it.
    """
    resources = stack.resources
    client = redis.Redis.from_url(resources.bus_redis_url, decode_responses=True)
    try:
        dying: dict[str, tuple[str, int, int]] = {}
        for worker in stack.census():
            row_key = f"{resources.bus_namespace}:bus:presence:{worker.name}"
            row, row_pttl = client.get(row_key), client.pttl(row_key)
            if not isinstance(row, str) or not isinstance(row_pttl, int) or row_pttl <= 0:
                continue
            digest = json.loads(row)["env_readers"]
            body_pttl = client.pttl(f"{resources.bus_namespace}:env-readers:{digest}")
            if not isinstance(body_pttl, int):
                raise TypeError(f"PTTL of the description {digest!r} is not an integer: {body_pttl!r}")
            if body_pttl < row_pttl:
                dying[worker.name] = (digest, row_pttl, body_pttl)
        return dying or None
    finally:
        client.close()


async def test_a_reader_description_outlives_every_row_naming_it_whatever_each_worker_s_heartbeat(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = fresh_stack(
        build_recycle_stack,
        resource_kwargs={"llm_base_url": llm_stub.base_url},
        process_env_overrides={"backend": {_HEARTBEAT_KEY: _SHORT_HEARTBEAT}},
    )
    assert _HEARTBEAT_KEY not in stack.process("serve").env, "the serve worker must start without the heartbeat"

    async def a_description_dies_before_its_row() -> dict[str, tuple[str, int, int]] | None:
        return _rows_whose_reader_body_dies_first(stack)

    # The backend beats every second and the serve worker every five, over a window spanning
    # several beats of both.
    await wait_absent_async(
        a_description_dies_before_its_row,
        window=3 * _HEARTBEAT_DEFAULT,
        interval=0.5,
        message="an env-reader description has less life left than a row naming it",
    )
