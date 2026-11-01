"""``claude_code`` session teardown: every step runs when an earlier one fails, an ephemeral
session is always destroyed, and every failure reaches the caller."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import anyio
import pytest
from fakeredis import aioredis
from pydantic import SecretStr
from tai42_contract.access_control.context import reset_request_user_id, set_request_user_id
from tai42_contract.agent.events import MessageDelta
from tai42_contract.app import tai42_app
from tai42_contract.connectors.models import ResolvedConnectionAuth
from tai42_contract.sandbox import (
    ExecResult,
    SandboxError,
    SandboxExecTimeoutError,
    SandboxSessionSpec,
    SandboxStreamChunk,
    SandboxStreamExit,
)
from tai42_contract.template import TemplatedText
from tai42_kit.sandbox import HostExecHandle, ManagedSandboxSession

import tai42_agents._internal.park.lease as lease_mod
import tai42_agents.claude_code.agent as agent_module
import tai42_agents.claude_code.live_sessions as live_sessions_module
import tai42_agents.claude_code.teardown as teardown_mod
import tai42_agents.claude_code.workspace as workspace_module
from tai42_agents._internal.sandbox_util import workspace_key_for
from tai42_agents.claude_code.agent import ClaudeCodeAgent
from tai42_agents.claude_code.errors import ClaudeCodeError
from tai42_agents.claude_code.settings import ClaudeCodeSettings, ConnectionCred

from ._claude_app import LocalApp, build_local_app
from ._claude_stubs import HANGS_AFTER_TEXT, MESSAGE, payload_for
from ._sandbox_fake import FakeSandbox, FakeSandboxSession, permissive_sandbox_policy
from .conftest import bind_park_index

_DIGEST = "registry.example/claude@sha256:" + "d" * 64
_CREDS_PATH = ".claude-home/.creds/GH_TOKEN"
_LOST_STATUS = "sandbox exec stream ended without an exit status"


@pytest.fixture(autouse=True)
def _clear_live_sessions() -> Iterator[None]:
    live_sessions_module.LIVE_SESSIONS.clear()
    yield
    live_sessions_module.LIVE_SESSIONS.clear()


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    """Route the park index and the workspace lease at one in-memory fakeredis."""
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def _client() -> AsyncIterator[Any]:
        yield redis

    bind_park_index(monkeypatch, redis)
    monkeypatch.setattr(lease_mod, "_lease_client", _client)
    return redis


@pytest.fixture
def destroyed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The ids of the sessions the fake provider destroyed, in order."""
    ids: list[str] = []
    original = FakeSandbox._destroy_session_resources

    async def _recording(self: FakeSandbox, session: ManagedSandboxSession, *, remove_workspace: bool) -> None:
        ids.append(session.id)
        await original(self, session, remove_workspace=remove_workspace)

    monkeypatch.setattr(FakeSandbox, "_destroy_session_resources", _recording)
    return ids


def _is_credential_scrub(argv: Any) -> bool:
    return list(argv[:2]) == ["rm", "-rf"] and argv[-1].endswith("/.claude-home/.creds")


@pytest.fixture
def scrubs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every credential-scrub ``rm`` the drive ran (the argv of each)."""
    calls: list[list[str]] = []
    original = FakeSandboxSession.exec

    async def _recording(self: FakeSandboxSession, argv: Any, **kwargs: Any) -> ExecResult:
        if _is_credential_scrub(argv):
            calls.append(list(argv))
        return await original(self, argv, **kwargs)

    monkeypatch.setattr(FakeSandboxSession, "exec", _recording)
    return calls


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> ClaudeCodeSettings:
    base: dict[str, Any] = {"session_image": _DIGEST, "api_key": SecretStr("modelkey")}
    base.update(overrides)
    settings = ClaudeCodeSettings(**base)
    monkeypatch.setattr(agent_module, "claude_code_settings", lambda: settings)
    return settings


def _run(app: LocalApp, **kwargs: Any) -> list[Any]:
    async def _astream() -> list[Any]:
        with tai42_app.bound(app):
            return [event async for event in ClaudeCodeAgent().astream(**kwargs)]

    return asyncio.run(_astream())


def _drain_raises(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """After the kill, the handle's output raises ``error`` instead of ending."""
    original_kill = HostExecHandle.kill

    async def _kill(self: HostExecHandle) -> None:
        await original_kill(self)

        async def _failing() -> AsyncIterator[SandboxStreamChunk | SandboxStreamExit]:
            raise error
            yield  # pragma: no cover - makes this an async generator

        self._output_iter = _failing()

    monkeypatch.setattr(HostExecHandle, "kill", _kill)


def _drain_loses_the_exit_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the kill, the handle's output ends without an exit status and raises."""
    _drain_raises(monkeypatch, SandboxError(_LOST_STATUS))


def _kill_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The drive's own kill of the runner fails; the provider's later destroy still stops it."""
    original_kill = HostExecHandle.kill
    calls: list[HostExecHandle] = []

    async def _kill(self: HostExecHandle) -> None:
        calls.append(self)
        if len(calls) == 1:
            raise SandboxError("kill failed")
        await original_kill(self)

    monkeypatch.setattr(HostExecHandle, "kill", _kill)


def _scrub_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    original = FakeSandboxSession.exec

    async def _failing(self: FakeSandboxSession, argv: Any, **kwargs: Any) -> ExecResult:
        if _is_credential_scrub(argv):
            return ExecResult(exit_code=1, stdout="", stderr="rm blew up")
        return await original(self, argv, **kwargs)

    monkeypatch.setattr(FakeSandboxSession, "exec", _failing)


async def _cancelled_drive(app: LocalApp) -> bool:
    """Drive a turn inside an anyio cancel scope cancelled at its first text event."""
    with tai42_app.bound(app), anyio.CancelScope() as scope:
        async for event in ClaudeCodeAgent().astream(user_message=TemplatedText(content="hi")):
            if isinstance(event, MessageDelta):
                scope.cancel()
    return scope.cancelled_caught


def test_a_failed_drain_still_scrubs_and_destroys_the_ephemeral_session(
    monkeypatch: pytest.MonkeyPatch, destroyed: list[str], scrubs: list[list[str]]
) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(workspace_module, "runner_payload_files", payload_for(MESSAGE))
    _drain_loses_the_exit_status(monkeypatch)

    with pytest.raises(SandboxError, match=_LOST_STATUS):
        _run(build_local_app(), user_message=TemplatedText(content="hi"))

    assert len(scrubs) == 1
    assert len(destroyed) == 1


def test_a_drain_that_times_out_is_raised_and_the_ephemeral_session_is_destroyed(
    monkeypatch: pytest.MonkeyPatch, destroyed: list[str], scrubs: list[list[str]]
) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(workspace_module, "runner_payload_files", payload_for(MESSAGE))
    _drain_raises(monkeypatch, SandboxExecTimeoutError(timeout_seconds=1.0, stdout_len=0, stderr_len=0))

    with pytest.raises(SandboxExecTimeoutError):
        _run(build_local_app(), user_message=TemplatedText(content="hi"))

    assert len(scrubs) == 1
    assert len(destroyed) == 1


def test_a_failed_kill_still_scrubs_and_destroys_the_ephemeral_session(
    monkeypatch: pytest.MonkeyPatch, destroyed: list[str], scrubs: list[list[str]]
) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(workspace_module, "runner_payload_files", payload_for(MESSAGE))
    _kill_fails(monkeypatch)

    with pytest.raises(SandboxError, match="kill failed"):
        _run(build_local_app(), user_message=TemplatedText(content="hi"))

    assert len(scrubs) == 1
    assert len(destroyed) == 1


def test_several_failed_steps_raise_together_and_the_ephemeral_session_is_destroyed(
    monkeypatch: pytest.MonkeyPatch, destroyed: list[str]
) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(workspace_module, "runner_payload_files", payload_for(MESSAGE))
    _drain_loses_the_exit_status(monkeypatch)
    _scrub_fails(monkeypatch)

    with pytest.raises(ExceptionGroup) as raised:
        _run(build_local_app(), user_message=TemplatedText(content="hi"))

    failures = raised.value.exceptions
    assert [type(failure) for failure in failures] == [SandboxError, ClaudeCodeError]
    assert _LOST_STATUS in str(failures[0])
    assert "credential scrub failed" in str(failures[1])
    assert len(destroyed) == 1


@pytest.mark.usefixtures("fake_redis")
def test_a_failed_drain_still_scrubs_a_threaded_session_and_keeps_it(
    monkeypatch: pytest.MonkeyPatch, destroyed: list[str]
) -> None:
    _settings(
        monkeypatch,
        creds=[ConnectionCred(env_name="GH_TOKEN", connection_id="conn-1", provider_id="github", sub_service="api")],
    )
    monkeypatch.setattr(workspace_module, "runner_payload_files", payload_for(MESSAGE))
    _drain_loses_the_exit_status(monkeypatch)
    app = build_local_app(resolver=lambda *_a: ResolvedConnectionAuth(access_token=SecretStr("tok1")))

    token = set_request_user_id("user-1")
    try:
        with pytest.raises(SandboxError, match=_LOST_STATUS):
            _run(app, user_message=TemplatedText(content="hi"), thread_id="t1")
    finally:
        reset_request_user_id(token)

    session = live_sessions_module.LIVE_SESSIONS[workspace_key_for("claude_code", "t1")]
    with pytest.raises(SandboxError):
        asyncio.run(session.get_file(_CREDS_PATH))
    assert destroyed == []


def test_a_drive_cancelled_in_an_anyio_scope_still_scrubs_and_destroys_the_ephemeral_session(
    monkeypatch: pytest.MonkeyPatch, destroyed: list[str], scrubs: list[list[str]]
) -> None:
    """An anyio cancel scope re-delivers its cancellation at every await; the teardown still
    runs every step before the cancellation goes on."""
    _settings(monkeypatch)
    monkeypatch.setattr(workspace_module, "runner_payload_files", payload_for(HANGS_AFTER_TEXT))

    assert asyncio.run(_cancelled_drive(build_local_app()))
    assert len(scrubs) == 1
    assert len(destroyed) == 1


def test_a_step_failure_under_cancellation_is_logged_and_the_session_destroyed(
    monkeypatch: pytest.MonkeyPatch, destroyed: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """The cancellation is what leaves the caller; the step failure it carries is logged."""
    _settings(monkeypatch)
    monkeypatch.setattr(workspace_module, "runner_payload_files", payload_for(HANGS_AFTER_TEXT))
    _scrub_fails(monkeypatch)

    with caplog.at_level(logging.ERROR, logger=teardown_mod.__name__):
        assert asyncio.run(_cancelled_drive(build_local_app()))

    assert len(destroyed) == 1
    [record] = [r for r in caplog.records if r.name == teardown_mod.__name__]
    assert "while its caller was cancelled" in record.getMessage()
    assert record.exc_info is not None
    assert "credential scrub failed" in str(record.exc_info[1])


async def _closed_at_first_text(app: LocalApp, destroyed: list[str]) -> list[str]:
    """Stop reading a turn at its first text event and close its stream, as a live-caller door
    does; return the sessions destroyed by the time the close returned."""
    with tai42_app.bound(app):
        async with contextlib.aclosing(ClaudeCodeAgent().astream(user_message=TemplatedText(content="hi"))) as events:
            async for event in events:
                if isinstance(event, MessageDelta):
                    break
        return list(destroyed)


def test_closing_the_stream_tears_the_session_down_before_the_close_returns(
    monkeypatch: pytest.MonkeyPatch, destroyed: list[str], scrubs: list[list[str]]
) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(workspace_module, "runner_payload_files", payload_for(HANGS_AFTER_TEXT))

    destroyed_at_close = asyncio.run(_closed_at_first_text(build_local_app(), destroyed))

    assert len(destroyed_at_close) == 1
    assert len(scrubs) == 1


def test_a_teardown_failure_on_close_is_raised_by_the_close(
    monkeypatch: pytest.MonkeyPatch, destroyed: list[str]
) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(workspace_module, "runner_payload_files", payload_for(HANGS_AFTER_TEXT))
    _scrub_fails(monkeypatch)

    with pytest.raises(ClaudeCodeError, match="credential scrub failed"):
        asyncio.run(_closed_at_first_text(build_local_app(), destroyed))

    assert len(destroyed) == 1


class _SlowHandle:
    """An exec handle whose killed output takes a while to end."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def kill(self) -> None:
        self._events.append("killed")

    @property
    def output(self) -> AsyncIterator[SandboxStreamExit]:
        async def _ending() -> AsyncIterator[SandboxStreamExit]:
            await asyncio.sleep(0.2)
            self._events.append("drained")
            yield SandboxStreamExit(exit_code=137)

        return _ending()


class _FailingDestroySession:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def destroy(self) -> None:
        self._events.append("destroyed")
        raise SandboxError("destroy failed")


async def _teardown_cancelled(times: int, events: list[str]) -> list[str]:
    """Cancel an ephemeral session's teardown ``times`` times with plain asyncio cancels; return
    what had happened by the time the caller saw the cancellation."""
    teardown = asyncio.ensure_future(
        teardown_mod.tear_down_session(
            cast("Any", _FailingDestroySession(events)),
            handle=_SlowHandle(events),
            ws="/workspace",
            policy=None,
            secrets=[],
            terminal=False,
            ephemeral=True,
        )
    )
    for _ in range(times):
        await asyncio.sleep(0.02)
        teardown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await teardown
    return list(events)


@pytest.mark.parametrize("times", [1, 2], ids=["one cancel", "two cancels"])
def test_plain_cancels_wait_for_every_teardown_step_and_cancel_once(
    times: int, caplog: pytest.LogCaptureFixture
) -> None:
    events: list[str] = []
    with caplog.at_level(logging.ERROR, logger=teardown_mod.__name__):
        seen = asyncio.run(_teardown_cancelled(times, events))

    assert seen == ["killed", "drained", "destroyed"]
    [record] = [r for r in caplog.records if r.name == teardown_mod.__name__]
    assert record.exc_info is not None
    assert "destroy failed" in str(record.exc_info[1])


class _KillCancelsItselfHandle:
    """An exec handle whose kill raises its own ``CancelledError`` though nothing cancelled it."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def kill(self) -> None:
        self._events.append("killed")
        raise asyncio.CancelledError

    @property
    def output(self) -> AsyncIterator[SandboxStreamExit]:  # pragma: no cover - a failed kill is not drained
        raise AssertionError("a failed kill is not drained")


async def _teardown_with_a_self_cancelling_kill(events: list[str]) -> tuple[BaseException, int]:
    """Tear down an ephemeral session whose kill cancels itself; return what the caller caught
    and the caller's own cancel count."""
    try:
        await teardown_mod.tear_down_session(
            cast("Any", _FailingDestroySession(events)),
            handle=_KillCancelsItselfHandle(events),
            ws="/workspace",
            policy=None,
            secrets=[],
            terminal=False,
            ephemeral=True,
        )
    except BaseException as exc:
        caught = exc
    else:  # pragma: no cover - the teardown always fails here
        raise AssertionError("the teardown did not fail")
    task = asyncio.current_task()
    assert task is not None
    return caught, task.cancelling()


def test_a_step_that_cancels_itself_is_a_step_failure_and_every_step_still_runs() -> None:
    events: list[str] = []
    caught, cancelling = asyncio.run(_teardown_with_a_self_cancelling_kill(events))

    assert events == ["killed", "destroyed"]
    assert cancelling == 0
    assert isinstance(caught, ExceptionGroup)
    step_cancel, destroy_failure = caught.exceptions
    assert isinstance(step_cancel, ClaudeCodeError)
    assert isinstance(step_cancel.__cause__, asyncio.CancelledError)
    assert isinstance(destroy_failure, SandboxError)


# An exec whose descendant leaves the process group (``setsid``) and keeps the exec's
# stdout/stderr open; the exec prints that descendant's pid so the test can stop it.
_PIPE_HOLDER = ("sh", "-c", 'setsid sleep 30 & echo "$!"; exec cat')

# How long a caller's cancel or timeout may take to reach it through a holder's teardown.
_PROMPT_SECONDS = 3.0


async def _drive_cut_short(sandbox: FakeSandbox, cut: Any, holders: list[int]) -> None:
    """Start a holder exec, let it run past its deadline, then end the drive by ``cut`` while
    the session's teardown kills and drains it."""
    session = await sandbox.create_session(
        SandboxSessionSpec(
            image="fake:image", workspace_key="holder", durability="ephemeral", network="egress", ttl_seconds=300
        )
    )
    handle = await session.exec_start(list(_PIPE_HOLDER), timeout_seconds=0.2)
    first = await anext(handle.output)
    assert isinstance(first, SandboxStreamChunk)
    holders.append(int(first.data.split()[0]))
    try:
        await cut()
    finally:
        await teardown_mod.tear_down_session(
            session, handle=handle, ws=session.workspace_path, policy=None, secrets=[], terminal=False, ephemeral=True
        )


async def _cut_by_timeout(sandbox: FakeSandbox, holders: list[int]) -> float:
    async def _wait_forever() -> None:
        await asyncio.Event().wait()

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await _drive_cut_short(sandbox, _wait_forever, holders)
    return loop.time() - started


async def _cut_by_cancel(sandbox: FakeSandbox, holders: list[int]) -> float:
    async def _wait_forever() -> None:
        await asyncio.Event().wait()

    drive = asyncio.ensure_future(_drive_cut_short(sandbox, _wait_forever, holders))
    loop = asyncio.get_running_loop()
    await asyncio.sleep(0.5)
    started = loop.time()
    drive.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drive
    return loop.time() - started


@pytest.mark.parametrize("cut", [_cut_by_timeout, _cut_by_cancel], ids=["timeout", "cancel"])
def test_a_caller_cut_takes_effect_promptly_while_a_descendant_holds_the_exec_pipes(cut: Any) -> None:
    sandbox = FakeSandbox()
    sandbox.bind_policy(permissive_sandbox_policy())
    holders: list[int] = []
    try:
        elapsed = asyncio.run(asyncio.wait_for(cut(sandbox, holders), timeout=20))
    finally:
        for pid in holders:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        sandbox.dispose()

    assert elapsed < _PROMPT_SECONDS
    assert sandbox._ledger == {}
