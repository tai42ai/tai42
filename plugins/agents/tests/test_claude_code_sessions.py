"""``claude_code`` threaded drives: the cross-worker workspace lease, SDK session capture +
resume, per-turn bearer refresh, the terminal credential scrub (vs park survival), and the
async park. The park index + lease are routed at an in-memory fakeredis.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from pydantic import SecretStr
from tai42_contract.access_control.context import reset_request_user_id, set_request_user_id
from tai42_contract.agent.events import MessageFinal, SuspendedFinal
from tai42_contract.app import tai42_app
from tai42_contract.connectors.models import ResolvedConnectionAuth
from tai42_contract.interactions import SuspendedInteraction
from tai42_contract.sandbox import ExecResult, SandboxError, SandboxPolicy
from tests._claude_app import LocalApp, build_local_app
from tests._claude_stubs import (
    ASYNC_ASK,
    CRED_ECHO,
    MESSAGE,
    MESSAGE_SESSION_OTHER,
    REDACT_TRANSCRIPT,
    RESUME_ONCE,
    RESUME_REDRIVE,
    TOOL_CALL_PARK,
    payload_for,
)
from tests._sandbox_fake import FakeSandboxSession

import tai42_agents._internal.park.index as idx
import tai42_agents._internal.park.lease as lease_mod
import tai42_agents.claude_code.agent as agent_module
from tai42_agents._internal.park import workspace_lease
from tai42_agents._internal.park.errors import WorkspaceLeaseHeldError
from tai42_agents._internal.park.index import compute_superstep_id
from tai42_agents._internal.sandbox_util import workspace_key_for
from tai42_agents.claude_code.agent import ClaudeCodeAgent, ClaudeCodeError
from tai42_agents.claude_code.protocol import ProtocolError
from tai42_agents.claude_code.settings import ClaudeCodeSettings, ConnectionCred

# A minimal resume options snapshot (the shape ``astream`` stores on the park identity's
# ``rebuild_kwargs`` and ``aresume_park`` rebuilds the turn from).
_RESUME_SNAPSHOT: dict[str, Any] = {
    "user_message": "",
    "system_message": "",
    "tool_names": [],
    "skills": [],
    "inline_skills": [],
    "response_format": None,
    "max_turns": None,
    "subagents": [],
}


def _scrub_on_policy() -> SandboxPolicy:
    """The permissive geometry with the platform transcript-scrub knob turned ON."""
    return SandboxPolicy(egress="egress", isolation="none", scrub_transcript=True, durable=True)


_DIGEST = "registry.example/claude@sha256:" + "d" * 64
_CREDS_PATH = ".claude-home/.creds/GH_TOKEN"


@pytest.fixture(autouse=True)
def _clear_live_sessions() -> Iterator[None]:
    agent_module._LIVE_SESSIONS.clear()
    yield
    agent_module._LIVE_SESSIONS.clear()


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    """Route BOTH the park index and the workspace lease at one in-memory fakeredis."""
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def _client() -> AsyncIterator[Any]:
        yield redis

    settings = SimpleNamespace(redis_url="redis://fake")
    monkeypatch.setattr(idx, "_park_client", _client)
    monkeypatch.setattr(idx, "agents_park_redis_settings", lambda: settings)
    monkeypatch.setattr(lease_mod, "_lease_client", _client)
    return redis


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> ClaudeCodeSettings:
    base: dict[str, Any] = {"session_image": _DIGEST, "api_key": SecretStr("modelkey")}
    base.update(overrides)
    settings = ClaudeCodeSettings(**base)
    monkeypatch.setattr(agent_module, "claude_code_settings", lambda: settings)
    return settings


async def _astream(app: LocalApp, **kwargs: Any) -> list[Any]:
    with tai42_app.bound(app):
        return [event async for event in ClaudeCodeAgent().astream(**kwargs)]


def _run(app: LocalApp, **kwargs: Any) -> list[Any]:
    return asyncio.run(_astream(app, **kwargs))


def _bearer_cred() -> ConnectionCred:
    return ConnectionCred(env_name="GH_TOKEN", connection_id="conn-1", provider_id="github", sub_service="api")


@pytest.mark.usefixtures("fake_redis")
def test_threaded_turn_captures_and_persists_the_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(MESSAGE))
    events = _run(build_local_app(), user_message="hi", thread_id="t1")
    assert any(isinstance(e, MessageFinal) for e in events)
    session = agent_module._LIVE_SESSIONS[workspace_key_for("claude_code", "t1")]
    raw = asyncio.run(session.get_file(".runner/session_id"))
    assert b"sess-1" in raw


@pytest.mark.usefixtures("fake_redis")
def test_second_threaded_turn_reuses_and_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(MESSAGE))
    app = build_local_app()
    key = workspace_key_for("claude_code", "t1")

    async def _two_turns() -> list[Any]:
        await _astream(app, user_message="turn1", thread_id="t1")
        first = agent_module._LIVE_SESSIONS[key]
        events = await _astream(app, user_message="turn2", thread_id="t1")
        # Turn 2 reuses the same live session (its create-time spec.env intact) and resumes the
        # persisted SDK id — the MESSAGE stub reports the same id, so no mismatch is raised.
        assert agent_module._LIVE_SESSIONS[key] is first
        return events

    events = asyncio.run(_two_turns())
    assert any(isinstance(e, MessageFinal) for e in events)


@pytest.mark.usefixtures("fake_redis")
def test_bearer_file_refreshes_across_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, creds=[_bearer_cred()])
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(CRED_ECHO))
    tokens = iter(["tok1", "tok2"])

    def resolver(*_a: str) -> ResolvedConnectionAuth:
        return ResolvedConnectionAuth(access_token=SecretStr(next(tokens)))

    app = build_local_app(resolver=resolver)

    async def _two_turns() -> tuple[list[Any], list[Any]]:
        return (
            await _astream(app, user_message="t1", thread_id="t1"),
            await _astream(app, user_message="t2", thread_id="t1"),
        )

    token = set_request_user_id("user-1")
    try:
        first, second = asyncio.run(_two_turns())
    finally:
        reset_request_user_id(token)
    assert any(isinstance(e, MessageFinal) and "tok1" in e.text for e in first)
    # Turn 2 RE-WROTE the bearer file on the reused session — the refreshed token reached it.
    assert any(isinstance(e, MessageFinal) and "tok2" in e.text for e in second)


@pytest.mark.usefixtures("fake_redis")
def test_terminal_exit_scrubs_the_bearer_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, creds=[_bearer_cred()])
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(MESSAGE))
    app = build_local_app(resolver=lambda *_a: ResolvedConnectionAuth(access_token=SecretStr("tok1")))
    token = set_request_user_id("user-1")
    try:
        _run(app, user_message="hi", thread_id="t1")
    finally:
        reset_request_user_id(token)
    session = agent_module._LIVE_SESSIONS[workspace_key_for("claude_code", "t1")]
    with pytest.raises(SandboxError):
        asyncio.run(session.get_file(_CREDS_PATH))


@pytest.mark.usefixtures("fake_redis")
def test_required_connection_cred_resolving_to_nothing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, creds=[_bearer_cred()])
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(MESSAGE))
    app = build_local_app(resolver=lambda *_a: None)
    token = set_request_user_id("user-1")
    try:
        with pytest.raises(Exception, match="resolved to nothing"):
            _run(app, user_message="hi", thread_id="t1")
    finally:
        reset_request_user_id(token)


@pytest.mark.usefixtures("fake_redis")
def test_held_lease_busy_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(MESSAGE))
    app = build_local_app()

    async def _drive_while_held() -> None:
        key = workspace_key_for("claude_code", "t1")
        async with workspace_lease(key, lease_ms=60_000):
            with tai42_app.bound(app):
                async for _ in ClaudeCodeAgent().astream(user_message="hi", thread_id="t1"):
                    pass

    with pytest.raises(WorkspaceLeaseHeldError):
        asyncio.run(_drive_while_held())


@pytest.mark.usefixtures("fake_redis")
def test_proxied_tool_that_parks_suspends_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # R4: a proxied tool that async-parks returns the generic SuspendedInteraction sentinel;
    # _on_tool_call recognizes it by TYPE and drives the SAME park tail the agent's own ask
    # takes — persist the durable index, stop the runner, surface one SuspendedFinal.
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(TOOL_CALL_PARK))
    deadline = datetime.now(UTC) + timedelta(minutes=5)

    def parking_tool(**_kwargs: Any) -> SuspendedInteraction:
        return SuspendedInteraction(interaction_id="i-tool", expiry_at=deadline)

    app = build_local_app(tool_runners={"parkingtool": parking_tool})

    async def _park_then_read() -> tuple[list[Any], Any]:
        # The astream drive and the index read share ONE event loop (the fakeredis client is
        # loop-bound), so the persisted entry is read back on the same loop it was written on.
        events = await _astream(app, user_message="deploy it", thread_id="t1", tool_names=["parkingtool"])
        return events, await idx.read_park_entry("i-tool")

    token = set_request_user_id("user-1")
    try:
        events, entry = asyncio.run(_park_then_read())
    finally:
        reset_request_user_id(token)
    suspended = [e for e in events if isinstance(e, SuspendedFinal)]
    assert len(suspended) == 1
    assert suspended[0].interaction_ids == ["i-tool"]
    assert suspended[0].thread_id == "t1"
    # The park is recorded into the durable index, resumable by the tool's interaction id.
    assert entry is not None
    assert entry["agent_name"] == "claude_code"
    assert entry["thread_id"] == "t1"


@pytest.mark.usefixtures("fake_redis")
def test_async_ask_on_threaded_run_parks(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, creds=[_bearer_cred()])
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(ASYNC_ASK))

    async def ask_user(_question: str, *, expiry_at: datetime | None = None, **_: Any) -> Any:
        return SuspendedInteraction(interaction_id="int-1", expiry_at=expiry_at or datetime.now(UTC))

    app = build_local_app(
        ask_user=ask_user, resolver=lambda *_a: ResolvedConnectionAuth(access_token=SecretStr("tok1"))
    )
    token = set_request_user_id("user-1")
    try:
        events = _run(app, user_message="deploy it", thread_id="t1")
    finally:
        reset_request_user_id(token)
    suspended = [e for e in events if isinstance(e, SuspendedFinal)]
    assert len(suspended) == 1
    assert suspended[0].interaction_ids == ["int-1"]
    assert suspended[0].thread_id == "t1"
    # A park-suspend exit does NOT scrub the bearer file — it survives for the expiry resume.
    session = agent_module._LIVE_SESSIONS[workspace_key_for("claude_code", "t1")]
    assert asyncio.run(session.get_file(_CREDS_PATH))  # present, no SandboxError


@pytest.mark.usefixtures("fake_redis")
def test_park_persists_a_resumable_index_entry(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(ASYNC_ASK))

    async def ask_user(_question: str, *, expiry_at: datetime | None = None, **_: Any) -> Any:
        deadline = expiry_at or datetime.now(UTC) + timedelta(hours=1)
        return SuspendedInteraction(interaction_id="int-9", expiry_at=deadline)

    async def _park_then_read() -> Any:
        await _astream(build_local_app(ask_user=ask_user), user_message="deploy", thread_id="t9")
        return await idx.read_park_entry("int-9")

    entry = asyncio.run(_park_then_read())
    assert entry is not None
    assert entry["agent_name"] == "claude_code"
    assert entry["thread_id"] == "t9"


# ---- §A3.8 crash-after-terminal idempotence record ----------------------------------------


@pytest.mark.usefixtures("fake_redis")
def test_resume_terminal_record_dedups_a_redelivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resumed super-step that reaches a clean terminal writes a durable
    ``.runner/terminal/<superstep_id>.json`` record; a redelivered resume (the winner crashed
    between the terminal and the index finalize) reads that record and re-produces the SAME
    output WITHOUT re-driving the SDK session — proven by swapping in a stub that would return a
    DIFFERENT terminal if it ran, and asserting the recorded output is returned instead (§A3.8)."""
    _settings(monkeypatch)
    app = build_local_app()
    rebuild = {"thread_id": "tr", "options_snapshot": _RESUME_SNAPSHOT}
    resume_map = {"int-1": {"int-1": "yes"}}

    async def _drive_twice() -> tuple[Any, Any]:
        agent = ClaudeCodeAgent()
        with tai42_app.bound(app):
            monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(RESUME_ONCE))
            first = await agent.aresume_park(rebuild_kwargs=rebuild, thread_id="tr", resume_map=resume_map)
            # Redelivery: a stub that WOULD terminate with a different value if the SDK re-drove.
            monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(RESUME_REDRIVE))
            second = await agent.aresume_park(rebuild_kwargs=rebuild, thread_id="tr", resume_map=resume_map)
        return first, second

    first, second = asyncio.run(_drive_twice())
    assert first == "done-once"
    # The redelivery returned the RECORDED output, not the "re-driven" value the swapped stub emits.
    assert second == "done-once"
    # The durable record sits at the super-step path derived from the resumed interaction ids.
    session = agent_module._LIVE_SESSIONS[workspace_key_for("claude_code", "tr")]
    superstep_id = compute_superstep_id(["int-1"])
    raw = asyncio.run(session.get_file(f".runner/terminal/{superstep_id}.json"))
    assert b"done-once" in raw


# ---- §A3.9(iii) transcript-content redaction ----------------------------------------------


@pytest.mark.usefixtures("fake_redis")
def test_transcript_content_is_redacted_when_scrub_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the platform ``scrub_transcript`` knob ON, a terminal exit rewrites the KEPT
    transcript so every injected-credential VALUE (here the model credential) is replaced by the
    fixed marker — the transcript FILE survives (resume needs it), only its secret strings go."""
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(REDACT_TRANSCRIPT))
    app = build_local_app(policy=_scrub_on_policy())
    _run(app, user_message="hi", thread_id="tscrub")

    session = agent_module._LIVE_SESSIONS[workspace_key_for("claude_code", "tscrub")]
    body = asyncio.run(session.get_file(".claude-home/transcript.jsonl")).decode("utf-8")
    assert "modelkey" not in body
    assert "[REDACTED]" in body


@pytest.mark.usefixtures("fake_redis")
def test_transcript_redaction_failure_raises_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transcript-redaction pass that cannot rewrite the transcript raises LOUDLY (never a
    silent leave-behind of secret material) — the same loudness as the credential-file scrub."""
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(REDACT_TRANSCRIPT))
    app = build_local_app(policy=_scrub_on_policy())

    original_exec = FakeSandboxSession.exec

    async def _failing_exec(self: FakeSandboxSession, argv: Any, **kwargs: Any) -> ExecResult:
        # Fail ONLY the redaction pass (its inlined script carries the marker); every other exec
        # (the credential scrub, materialize commands) runs normally.
        if list(argv[:2]) == ["python", "-c"] and "[REDACTED]" in argv[2]:
            return ExecResult(exit_code=1, stdout="", stderr="redaction blew up")
        return await original_exec(self, argv, **kwargs)

    monkeypatch.setattr(FakeSandboxSession, "exec", _failing_exec)
    with pytest.raises(ClaudeCodeError, match="transcript redaction failed"):
        _run(app, user_message="hi", thread_id="tfail")


# ---- resume session-id gate + malformed persisted session id (§A4) ------------------------


@pytest.mark.usefixtures("fake_redis")
def test_resume_session_id_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second threaded turn resumes the persisted SDK session id; a hello reporting a DIFFERENT
    id is a loud protocol error, never a silent drive of the wrong session (agent.py ~547)."""
    _settings(monkeypatch)
    app = build_local_app()

    async def _two_turns() -> None:
        monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(MESSAGE))
        await _astream(app, user_message="turn1", thread_id="t1")  # persists sess-1
        # Turn 2 reports sess-2 in its hello — mismatched against the resumed sess-1.
        monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(MESSAGE_SESSION_OTHER))
        await _astream(app, user_message="turn2", thread_id="t1")

    with pytest.raises(ProtocolError, match="resumed id"):
        asyncio.run(_two_turns())


@pytest.mark.usefixtures("fake_redis")
@pytest.mark.parametrize("corrupt", [b"not-json-at-all", b'{"session_id": ""}', b'{"other": 1}'])
def test_malformed_persisted_session_id_raises(monkeypatch: pytest.MonkeyPatch, corrupt: bytes) -> None:
    """On a thread WITH prior turns, a malformed persisted ``.runner/session_id`` (bad JSON, an
    empty id, or a missing key) is a loud protocol error rather than a silent fresh session
    (agent.py ~716-719)."""
    _settings(monkeypatch)
    monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(MESSAGE))
    app = build_local_app()
    key = workspace_key_for("claude_code", "t1")

    async def _turn_then_corrupt() -> None:
        await _astream(app, user_message="turn1", thread_id="t1")  # creates the session id file
        session = agent_module._LIVE_SESSIONS[key]
        await session.put_file(".runner/session_id", corrupt)
        await _astream(app, user_message="turn2", thread_id="t1")  # reads the corrupted file

    with pytest.raises(ProtocolError, match="malformed"):
        asyncio.run(_turn_then_corrupt())


# ---- §A3.8 forged / mismatched terminal record is treated as absent -----------------------


@pytest.mark.usefixtures("fake_redis")
@pytest.mark.parametrize(
    "forge",
    [
        # A stray key fails ``extra="forbid"`` schema validation → treated as absent.
        b'{"superstep_id": "%s", "structured": false, "text": "forged", "bogus": 1}',
        # A well-formed record whose ``superstep_id`` does not match the resumed one → absent.
        b'{"superstep_id": "other", "structured": false, "text": "forged"}',
    ],
)
def test_forged_terminal_record_is_ignored_and_redrives(monkeypatch: pytest.MonkeyPatch, forge: bytes) -> None:
    """A resume reads the durable §A3.8 record only when it schema-validates AND its
    ``superstep_id`` matches; a forged or mismatched record is treated as ABSENT, so the resume
    RE-DRIVES rather than returning garbage (agent.py ~741-744)."""
    _settings(monkeypatch)
    app = build_local_app()
    rebuild = {"thread_id": "tf", "options_snapshot": _RESUME_SNAPSHOT}
    resume_map = {"int-1": {"int-1": "yes"}}
    superstep_id = compute_superstep_id(["int-1"])
    key = workspace_key_for("claude_code", "tf")

    async def _drive() -> tuple[Any, Any]:
        agent = ClaudeCodeAgent()
        with tai42_app.bound(app):
            monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(RESUME_ONCE))
            first = await agent.aresume_park(rebuild_kwargs=rebuild, thread_id="tf", resume_map=resume_map)
            # Overwrite the legit record with a forged one; the redelivery must reject it.
            session = agent_module._LIVE_SESSIONS[key]
            body = forge % superstep_id.encode("utf-8") if b"%s" in forge else forge
            await session.put_file(f".runner/terminal/{superstep_id}.json", body)
            monkeypatch.setattr(agent_module, "runner_payload_files", payload_for(RESUME_REDRIVE))
            second = await agent.aresume_park(rebuild_kwargs=rebuild, thread_id="tf", resume_map=resume_map)
        return first, second

    first, second = asyncio.run(_drive())
    assert first == "done-once"
    # The forged record was ignored, so the redelivery RE-DROVE (the swapped stub's value).
    assert second == "re-driven"
