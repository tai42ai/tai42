"""A webhook event the ingress answers 200 is kept: its hook run survives a crash, a shutdown and a
config apply, and a run that cannot finish is held as failed where an operator retries it.

A hook that has started its tool replays that start alone, with the same idempotency key, only when
the tool declares crash-resume; any other started run is held as failed and never fired again until
an operator retries it. A tool's own retry policy fires exactly its declared attempts. A delivery id
the verifier already claimed is answered ``already_seen`` and fires nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

from tai42_e2e import Infra, StackResources, wait_for, wait_for_async
from tai42_e2e.binaries import tai_bin
from tai42_e2e.booting import boot_stack
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.manifests import build_hook_runs_replicas_stack, build_hook_runs_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.tcprelay import TcpRelay, wait_relay_ready
from tai42_e2e.topology import StackConfig
from tai42_e2e.variants import Variants

pytestmark = pytest.mark.needs("probe-tools", "store:redis")

_INTERRUPTED = "interrupted after the hook acted (start)"
_SECRET_ENV = "E2E_HOOK_RUNS_SECRET"
_RETRY_BASE_SECONDS = 1.0

# A lease held by a killed process lapses well inside a gate's 30 s ceiling, and the sweep looks
# every second, so a reclaim lands within a few seconds.
_RESTART_ENV = {"HOOKS_RUN_LEASE_SECONDS": "3", "HOOKS_RUN_SWEEP_SECONDS": "1"}

# A condition jq cannot evaluate over the webhook body ``_fire`` posts, and one it can.
_RAISING_CONDITION = ".any | tonumber > 0"
_WORKING_CONDITION = ".any | length > 0"


def _quick_retry_stack(res: StackResources, variants: Variants) -> StackConfig:
    config = build_hook_runs_stack(res, variants)
    env = {
        **config.env,
        "HOOKS_RUN_RETRY_BASE_SECONDS": str(_RETRY_BASE_SECONDS),
        "HOOKS_RUN_SWEEP_SECONDS": "1",
        _SECRET_ENV: "known-to-test",
    }
    return replace(config, env=env)


@pytest.fixture(scope="module")
def hook_runs_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """One stack for the legs that restart nothing."""
    yield from boot_stack(infra, tmp_path_factory.mktemp("hook-runs"), _quick_retry_stack)


def _interactions_behind(relay: TcpRelay) -> Callable[[StackResources, Variants], StackConfig]:
    """The quick-retry stack, its interactions store reached through ``relay`` on the stack's own DB."""

    def build(res: StackResources, variants: Variants) -> StackConfig:
        config = _quick_retry_stack(res, variants)
        url = urlunsplit(urlsplit(res.redis_url)._replace(netloc=f"{relay.listen_host}:{relay.port}"))
        return replace(config, env={**config.env, "INTERACTIONS_REDIS_URL": url})

    return build


# -- helpers ----------------------------------------------------------------------------


async def _register(api: ApiClient, uniq: Callable[[str], str], *, topic: str, tool: str, **tool_kwargs: Any) -> None:
    await api.post(
        "/api/hooks",
        json={
            "name": uniq("hook"),
            "topic": topic,
            "tool": tool,
            "tool_kwargs": tool_kwargs,
            "execution_key": uniq("exec"),
        },
    )


async def _fire(api: ApiClient, topic: str) -> None:
    response = await api.request_raw("POST", f"/universal_webhook/{topic}", json={"any": "payload"})
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "accepted", "topic": topic}


async def _runs(api: ApiClient, topic: str, status: str | None = None) -> list[dict[str, Any]]:
    query = "limit=200" if status is None else f"status={status}&limit=200"
    page = await api.get(f"/api/hook-runs?{query}")
    return [run for run in page["items"] if run["topic"] == topic]


async def _failed_run(api: ApiClient, topic: str, *, deadline: float = 30.0) -> dict[str, Any]:
    async def failed() -> dict[str, Any] | None:
        runs = await _runs(api, topic, "failed")
        return runs[0] if runs else None

    return await wait_for_async(failed, deadline=deadline, message=f"no hook run on {topic} was held as failed")


async def _gone(api: ApiClient, topic: str, *, deadline: float = 30.0) -> None:
    async def gone() -> bool:
        return not await _runs(api, topic)

    await wait_for_async(gone, deadline=deadline, message=f"the hook run on {topic} was never finished")


async def _notified(api: ApiClient, run_id: str) -> None:
    async def notified() -> bool:
        feed = await api.get("/api/notifications")
        return any(run_id in note["message"] for note in feed["notifications"])

    await wait_for_async(notified, deadline=10.0, message=f"no operator notification names hook run {run_id}")


def _records(stack: TaiStack, key: str) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in stack.records(key)]


def _wait_records(stack: TaiStack, key: str, count: int, *, deadline: float = 30.0) -> list[dict[str, Any]]:
    def enough() -> list[dict[str, Any]] | None:
        records = _records(stack, key)
        return records if len(records) >= count else None

    return wait_for(enough, deadline=deadline, message=f"expected {count} record(s) under {key!r}")


async def _elapse(seconds: float) -> None:
    """Let ``seconds`` pass on the clock, so a later read sees what the SUT did meanwhile."""
    until = time.monotonic() + seconds

    async def elapsed() -> bool:
        return time.monotonic() >= until

    await wait_for_async(elapsed, deadline=seconds + 5.0, interval=0.25, message="the clock never advanced")


def _cli(stack: TaiStack, tmp_path: Path, *args: str) -> Any:
    """Run ``tai --server <url> --json <args...>`` against the auth-off stack and parse its output.

    ``TAI_API_KEY`` is a fake constant so the CLI never falls through to its interactive key
    prompt (the auth-off stack accepts any value)."""
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": str(tmp_path),
        "TAI_API_KEY": "sk-e2e-cli-not-a-real-key",
    }
    argv = [tai_bin(), "--server", stack.origin(), "--json", *args]
    result = subprocess.run(argv, env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, f"exit {result.returncode}; stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    return json.loads(result.stdout)


def _cli_failed_run(stack: TaiStack, tmp_path: Path, topic: str) -> dict[str, Any]:
    def failed() -> dict[str, Any] | None:
        page = _cli(stack, tmp_path, "hooks", "runs", "list", "--status", "failed", "--limit", "200")
        runs = [run for run in page["items"] if run["topic"] == topic]
        return runs[0] if runs else None

    return wait_for(failed, deadline=30.0, interval=0.5, message=f"tai hooks runs list shows no failed run on {topic}")


def _session_of(pid: int) -> int:
    return os.getsid(pid)


# -- 1. a failure before the hook acted is retried until it clears --------------------


@pytest.mark.needs("process")
async def test_a_failure_before_the_hook_acted_is_retried_until_it_clears(
    infra: Infra, fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    redis_host, redis_port = infra.settings.redis_host_port
    relay = TcpRelay(redis_host, redis_port)
    try:
        relay.start()
        wait_relay_ready(relay)
    except BaseException:
        relay.stop()
        raise
    stack = fresh_stack(_interactions_behind(relay), relays=[relay])
    api = stack.api()
    topic, key = uniq("topic").replace("_", "-"), uniq("rec")
    # The hook's subject makes each attempt read the run's parked interactions before it acts.
    await api.post(
        "/api/hooks",
        json={
            "name": uniq("hook"),
            "topic": topic,
            "tool": "e2e_record",
            "tool_kwargs": {"key": key, "value": "fired"},
            "execution_key": uniq("exec"),
            "subject": {
                "target_kind": "agent",
                "target_name": "a-42",
                "kind": "watch",
                "key_expr": {"content": ".any"},
            },
        },
    )

    relay.sever()
    await _fire(api, topic)

    async def retried() -> dict[str, Any] | None:
        runs = await _runs(api, topic, "outstanding")
        return runs[0] if runs and runs[0]["attempts"] >= 2 and runs[0]["last_error_kind"] else None

    run = await wait_for_async(retried, deadline=30.0, message="the run was not retried while the store was down")
    # The store being out of reach is a transient kind, retried with backoff before the hook acts.
    assert (run["acted"], run["last_error_kind"]) == ("", "unavailable")
    assert _records(stack, key) == []

    relay.restore()
    wait_relay_ready(relay)
    [record] = _wait_records(stack, key, 1)
    assert record["value"] == "fired"
    await _gone(api, topic)
    assert len(_records(stack, key)) == 1


# -- 2. a crash after the hook acted, crash-resume tool ---------------------------------


@pytest.mark.needs("process")
async def test_a_crash_resume_start_is_replayed_with_its_key_after_a_restart(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    stack = fresh_stack(build_hook_runs_stack, env_overrides=_RESTART_ENV)
    api = stack.api()
    topic, key = uniq("topic").replace("_", "-"), uniq("gate")
    await _register(api, uniq, topic=topic, tool="e2e_deferred_gate_resumable", key=key)

    await _fire(api, topic)
    [first] = _wait_records(stack, key, 1)
    stack.kill("serve")
    stack.restart("serve")

    first_again, replay = _wait_records(stack, key, 2)
    assert first_again == first
    assert replay["pid"] != first["pid"]
    assert replay["idempotency_key"] == first["idempotency_key"]
    assert replay["idempotency_key"]

    stack.infra.redis.open_gate(key)
    await _gone(stack.api(), topic)
    assert len(_records(stack, key)) == 2


# -- 3. a reclaim by another replica, crash-resume tool ---------------------------------


@pytest.mark.needs("process", "topology:replicas")
async def test_another_replica_s_sweep_replays_a_killed_replica_s_start_once(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    stack = fresh_stack(build_hook_runs_replicas_stack, env_overrides=_RESTART_ENV)
    api_a, api_b = stack.api(port=stack.port_a), stack.api(port=stack.port_b)
    topic, key = uniq("topic").replace("_", "-"), uniq("gate")
    await _register(api_a, uniq, topic=topic, tool="e2e_deferred_gate_resumable", key=key)

    await _fire(api_a, topic)
    [first] = _wait_records(stack, key, 1)
    assert _session_of(first["pid"]) == stack.process("serve-a").pid
    stack.kill("serve-a")

    _first, replay = _wait_records(stack, key, 2)
    assert _session_of(replay["pid"]) == stack.process("serve-b").pid
    assert replay["idempotency_key"] == first["idempotency_key"]

    stack.infra.redis.open_gate(key)
    await _gone(api_b, topic)
    assert len(_records(stack, key)) == 2


# -- 4. a crash after the hook acted, a tool without crash-resume -----------------------


@pytest.mark.needs("process", "cli")
async def test_an_unflagged_start_is_held_failed_after_a_crash_and_an_operator_replays_it(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str], tmp_path: Path
) -> None:
    stack = fresh_stack(build_hook_runs_stack, env_overrides=_RESTART_ENV)
    topic, key = uniq("topic").replace("_", "-"), uniq("gate")
    await _register(stack.api(), uniq, topic=topic, tool="e2e_deferred_gate", key=key)

    await _fire(stack.api(), topic)
    [first] = _wait_records(stack, key, 1)
    stack.kill("serve")
    stack.restart("serve")

    run = _cli_failed_run(stack, tmp_path, topic)
    assert (run["acted"], run["last_error"]) == ("start", _INTERRUPTED)
    await _notified(stack.api(), run["id"])
    await _elapse(2.0)
    assert len(_records(stack, key)) == 1

    retried = _cli(stack, tmp_path, "hooks", "runs", "retry", run["id"])
    assert retried["id"] == run["id"]
    _first, replay = _wait_records(stack, key, 2)
    assert replay["idempotency_key"] == first["idempotency_key"] == run["id"]
    assert replay["key"] == first["key"] == key

    stack.infra.redis.open_gate(key)
    await _gone(stack.api(), topic)


# -- 5. a replayed delivery fires nothing ----------------------------------------------


@pytest.mark.needs("setting:webhook-verifier:shared_secret", f"setting:{_SECRET_ENV}=known-to-test")
async def test_a_replayed_delivery_is_already_seen_and_fires_nothing(
    hook_runs_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = hook_runs_stack.api()
    topic, key = uniq("topic").replace("_", "-"), uniq("rec")
    await _register(api, uniq, topic=topic, tool="e2e_record", key=key, value="fired")
    await api.put(
        f"/api/hooks/topics/{topic}/verifier",
        json={
            "verifier": "shared_secret",
            "config": {"header": "X-E2E-Secret", "secret_env": _SECRET_ENV, "id_header": "X-E2E-Delivery-Id"},
        },
    )
    headers = {"X-E2E-Secret": "known-to-test", "X-E2E-Delivery-Id": uniq("delivery")}
    path = f"/universal_webhook/{topic}"

    first = await api.request_raw("POST", path, headers=headers, json={"delivery": "one"})
    assert (first.status_code, first.json()) == (200, {"status": "accepted", "topic": topic})
    _wait_records(hook_runs_stack, key, 1)
    await _gone(api, topic)

    again = await api.request_raw("POST", path, headers=headers, json={"delivery": "one"})
    assert (again.status_code, again.json()) == (200, {"status": "already_seen", "topic": topic})
    await _elapse(2.0)
    assert len(_records(hook_runs_stack, key)) == 1
    assert await _runs(api, topic) == []


# -- 6. a condition that cannot evaluate is held failed, visible and retryable ----------


@pytest.mark.needs("cli")
async def test_a_condition_that_cannot_evaluate_is_held_failed_and_an_operator_retries_it(
    hook_runs_stack: TaiStack, uniq: Callable[[str], str], tmp_path: Path
) -> None:
    stack = hook_runs_stack
    api = stack.api()
    topic, key = uniq("topic").replace("_", "-"), uniq("rec")
    hook = {
        "name": uniq("hook"),
        "topic": topic,
        "tool": "e2e_record",
        "tool_kwargs": {"key": key, "value": "fired"},
        "execution_key": uniq("exec"),
    }
    await api.post("/api/hooks", json={**hook, "condition": {"content": _RAISING_CONDITION}})

    await _fire(api, topic)
    run = _cli_failed_run(stack, tmp_path, topic)
    assert (run["attempts"], run["acted"], run["last_error_kind"]) == (1, "", "bad_input")
    await _notified(api, run["id"])
    await _elapse(_RETRY_BASE_SECONDS * 3)
    assert [r["attempts"] for r in await _runs(api, topic, "failed")] == [1]
    assert _records(stack, key) == []

    await api.post("/api/hooks", json={**hook, "condition": {"content": _WORKING_CONDITION}})
    _cli(stack, tmp_path, "hooks", "runs", "retry", run["id"])
    [record] = _wait_records(stack, key, 1)
    assert record["value"] == "fired"
    await _gone(api, topic)

    await api.post("/api/hooks", json={**hook, "condition": {"content": _RAISING_CONDITION}})
    await _fire(api, topic)
    second = _cli_failed_run(stack, tmp_path, topic)
    _cli(stack, tmp_path, "hooks", "runs", "discard", second["id"], "--yes")
    assert await _runs(api, topic) == []
    assert len(_records(stack, key)) == 1


# -- 7. a tool failure after the hook acted ---------------------------------------------


async def test_a_tool_failure_after_the_hook_acted_fires_once_and_an_operator_replays_it(
    hook_runs_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = hook_runs_stack.api()
    topic, key = uniq("topic").replace("_", "-"), uniq("count")
    await _register(api, uniq, topic=topic, tool="e2e_fail_until", key=key, failures=1)

    await _fire(api, topic)
    run = await _failed_run(api, topic)
    assert (run["attempts"], run["acted"], run["last_error_kind"]) == (1, "start", "unavailable")
    assert hook_runs_stack.infra.redis.count(key) == 1
    await _notified(api, run["id"])

    await _elapse(_RETRY_BASE_SECONDS * 3)
    assert hook_runs_stack.infra.redis.count(key) == 1
    assert [r["id"] for r in await _runs(api, topic, "failed")] == [run["id"]]

    await api.post(f"/api/hook-runs/{run['id']}/retry")
    [record] = _wait_records(hook_runs_stack, key, 1)
    assert record["count"] == 2
    assert record["idempotency_key"] == run["id"]
    await _gone(api, topic)


# -- 8. a declared tool retry policy fires its own attempts only ------------------------


async def test_a_declared_tool_retry_policy_fires_exactly_its_attempts(
    hook_runs_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = hook_runs_stack.api()
    topic, key = uniq("topic").replace("_", "-"), uniq("count")
    await _register(api, uniq, topic=topic, tool="e2e_fail_until_retried", key=key, failures=5)

    await _fire(api, topic)
    run = await _failed_run(api, topic)
    assert (run["attempts"], run["acted"]) == (1, "start")
    assert hook_runs_stack.infra.redis.count(key) == 3

    await _elapse(_RETRY_BASE_SECONDS * 3)
    assert hook_runs_stack.infra.redis.count(key) == 3
    assert _records(hook_runs_stack, key) == []


# -- 10. a shutdown in the middle of a tool ---------------------------------------------


@pytest.mark.needs("process", "kind:tool_runs", "cli")
@pytest.mark.parametrize("tool", ["e2e_deferred_gate", "e2e_deferred_gate_resumable"])
async def test_a_shutdown_in_the_middle_of_a_tool(
    tool: str, fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str], tmp_path: Path
) -> None:
    stack = fresh_stack(build_hook_runs_stack, env_overrides={**_RESTART_ENV, "HOOKS_RUN_SHUTDOWN_GRACE_SECONDS": "0"})
    topic, key = uniq("topic").replace("_", "-"), uniq("gate")
    await _register(stack.api(), uniq, topic=topic, tool=tool, key=key)

    await _fire(stack.api(), topic)
    [first] = _wait_records(stack, key, 1)
    stack.restart("serve")

    if tool == "e2e_deferred_gate":
        run = _cli_failed_run(stack, tmp_path, topic)
        assert (run["acted"], run["last_error"]) == ("start", _INTERRUPTED)
        await _elapse(2.0)
        assert len(_records(stack, key)) == 1
        return

    _first, replay = _wait_records(stack, key, 2)
    assert replay["pid"] != first["pid"]
    assert replay["idempotency_key"] == first["idempotency_key"]
    stack.infra.redis.open_gate(key)
    await _gone(stack.api(), topic)


# -- 11. a config apply in the middle of a tool -----------------------------------------


async def _apply_hot_flip(api: ApiClient, uniq: Callable[[str], str]) -> None:
    """Save and apply a profile that flips the hot ``TAI_LOG_LEVEL``, the rest of the stored env unchanged."""
    stored = (await api.get("/api/config/env"))["env"]
    new_level = "DEBUG" if (stored.get("TAI_LOG_LEVEL") or "INFO").upper() != "DEBUG" else "INFO"
    name = uniq("swap")
    await api.put(
        f"/api/config/profiles/{name}", json={"env": {**stored, "TAI_LOG_LEVEL": new_level}}, retry_on_reloading=True
    )
    await api.post(f"/api/config/profiles/{name}/apply", retry_on_reloading=True, timeout=120.0)


@pytest.mark.timeout(300)
@pytest.mark.needs("kind:tool_runs", "mutable", "setting:one-worker-per-address")
@pytest.mark.parametrize("tool", ["e2e_deferred_gate", "e2e_deferred_gate_resumable"])
async def test_a_config_apply_in_the_middle_of_a_tool(
    tool: str, fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    stack = fresh_stack(build_hook_runs_stack, env_overrides=_RESTART_ENV)
    api = stack.api()
    topic, key = uniq("topic").replace("_", "-"), uniq("gate")
    await _register(api, uniq, topic=topic, tool=tool, key=key)

    await _fire(api, topic)
    [first] = _wait_records(stack, key, 1)
    await _apply_hot_flip(api, uniq)

    if tool == "e2e_deferred_gate":
        run = await _failed_run(api, topic)
        assert run["acted"] == "start"
        assert run["last_error"].startswith(_INTERRUPTED)
        assert run["last_error_kind"] not in (None, "cancelled")
        await _notified(api, run["id"])
        await _elapse(2.0)
        assert len(_records(stack, key)) == 1
        return

    _first, replay = _wait_records(stack, key, 2)
    assert replay["pid"] == first["pid"]
    assert replay["idempotency_key"] == first["idempotency_key"]
    stack.infra.redis.open_gate(key)
    await _gone(api, topic)
