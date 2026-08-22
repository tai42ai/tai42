"""The durable session-model rewiring of ``langchain_deep_agent`` (§B2/§B3/§B4).

Covers the run-door invariants the ``StateBackend``→``SandboxSessionBackend`` swap introduces:

* HARD sandbox dependency — ``run``/``astream`` acquire the session BEFORE compile and raise
  ``SandboxUnavailableError`` on a box with no provider; ``append_thread_messages`` acquires no
  session (a checkpoint-only write).
* the acquired session is threaded through ``_resolve_and_build`` → ``build_langchain_deep_agent``
  into the durable backend; a tool-face run gets an ephemeral workspace, a threaded run a
  persistent one.
* settings validation — the digest-pinned ``session_image`` and the ``SessionCredSpec`` variants.
* bearer/static/env credential materialization and the terminal-exit scrub at ``{ws}/.creds``.

Async is driven with ``asyncio.run`` (the suite does not use pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fakeredis import aioredis
from pydantic import SecretStr, TypeAdapter, ValidationError
from tai42_contract.app import tai42_app
from tai42_contract.connectors import ResolvedConnectionAuth
from tai42_contract.sandbox import ExecResult, SandboxSession, SandboxUnavailableError

from tai42_agents._internal.park import lease as lease_mod
from tai42_agents._internal.park.errors import WorkspaceLeaseHeldError
from tai42_agents.langchain_deep_agent import agent as agent_mod
from tai42_agents.langchain_deep_agent import session as session_mod
from tai42_agents.langchain_deep_agent import settings as settings_mod
from tai42_agents.langchain_deep_agent.agent import DeepAgent
from tai42_agents.langchain_deep_agent.session import DeepAgentSession
from tai42_agents.langchain_deep_agent.settings import (
    ConnectionCred,
    LangchainDeepAgentSettings,
    SessionCredSpec,
    StaticCred,
)

from .conftest import APP, RecordingConnectors, RecordingSandboxes

_DIGEST = "registry.example/lean@sha256:" + "a" * 64


# The ``tai42_app`` handle is typed as the CONTRACT facade, whose ``AppSandboxes`` /
# ``AppConnectors`` protocols expose no test-double state; the recording app bound in ``conftest``
# is the concrete fake. Narrow the facets to the fake types to read/set the recording seams
# (``provider`` / ``resolved`` / ``calls``) without a blanket ignore.
def _sandboxes() -> RecordingSandboxes:
    return cast(RecordingSandboxes, tai42_app.sandboxes)


def _connectors() -> RecordingConnectors:
    return cast(RecordingConnectors, tai42_app.connectors)


# ---- settings ------------------------------------------------------------------


def test_session_image_must_be_a_digest_reference() -> None:
    with pytest.raises(ValidationError, match="digest reference"):
        LangchainDeepAgentSettings(session_image="registry.example/lean:latest")
    # A digest reference validates.
    assert LangchainDeepAgentSettings(session_image=_DIGEST).session_image == _DIGEST


def test_session_cred_spec_variants_are_discriminated() -> None:
    # A static entry is a fixed env_name + value; it has NO refresh path, so it carries no
    # delivery knob and extra keys are forbidden (a misplaced ``delivery`` is a loud error).
    static = StaticCred(env_name="X", value=SecretStr("v"))
    assert static.kind == "static"
    with pytest.raises(ValidationError):
        StaticCred(env_name="X", value=SecretStr("v"), delivery="bearer")  # type: ignore[call-arg]
    # A connection reference REQUIRES connection_id + provider_id + sub_service.
    with pytest.raises(ValidationError):
        ConnectionCred(env_name="X", connection_id="c")  # type: ignore[call-arg]
    ref = ConnectionCred(env_name="X", connection_id="c", provider_id="p", sub_service="s")
    assert ref.kind == "connection"
    # Bearer is the default delivery for a refreshable connection cred.
    assert ref.delivery == "bearer"
    # A mixed list routes each dict to its variant off the ``kind`` discriminator.
    adapter = TypeAdapter(list[SessionCredSpec])
    parsed = adapter.validate_python(
        [
            {"kind": "static", "env_name": "A", "value": "v"},
            {"kind": "connection", "env_name": "B", "connection_id": "c", "provider_id": "p", "sub_service": "s"},
        ]
    )
    assert isinstance(parsed[0], StaticCred)
    assert isinstance(parsed[1], ConnectionCred)


# ---- hard sandbox dependency ---------------------------------------------------


def test_run_requires_a_sandbox_provider() -> None:
    """The scratch backend is durable, so a run REQUIRES a provider — a box with none raises the
    every-door ``SandboxUnavailableError`` at the ``require_sandbox`` chokepoint (§B3.7)."""
    _sandboxes().provider = None
    with pytest.raises(SandboxUnavailableError):
        asyncio.run(DeepAgent().run(user_message="go"))


def test_astream_requires_a_sandbox_provider() -> None:
    _sandboxes().provider = None

    async def go() -> None:
        async for _event in DeepAgent().astream(user_message="go"):
            pass

    with pytest.raises(SandboxUnavailableError):
        asyncio.run(go())


def test_append_thread_messages_acquires_no_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """The append path is a checkpoint-only write — no file work, no model call — so it acquires
    NO session and works with the sandbox provider absent (§B3.5)."""
    _sandboxes().provider = None
    captured: dict[str, Any] = {}

    async def fake_resolve_and_build(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    async def fake_awrite(agent: object, config: dict[str, Any], messages: list[Any]) -> None:
        captured["wrote"] = messages

    monkeypatch.setattr(DeepAgent, "_resolve_and_build", staticmethod(fake_resolve_and_build))
    monkeypatch.setattr(agent_mod, "awrite_thread_messages", fake_awrite)

    asyncio.run(
        DeepAgent().append_thread_messages(thread_id="t-append", messages=[{"role": "user", "content": "remember"}])
    )
    # The append path passes NO session (the non-sandbox StateBackend), and never touched the
    # (absent) provider — a live sandbox is not required to record manual-mode history.
    assert captured["session"] is None
    assert captured["wrote"]


# ---- session threading + workspace tier ---------------------------------------


class _Captured(Exception):
    """Short-circuits the drive once the session-threading is captured."""

    def __init__(self, session: Any) -> None:
        self.session = session


def test_run_threads_a_live_session_into_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run acquires a live session and threads it into the compile step — the durable
    ``SandboxSessionBackend`` — rather than leaving the in-graph ``StateBackend`` default."""

    async def capturing_resolve(self: DeepAgent, *, session: Any = None, **_: Any) -> object:
        raise _Captured(session)

    monkeypatch.setattr(DeepAgent, "_resolve_and_build", capturing_resolve)
    with pytest.raises(_Captured) as excinfo:
        asyncio.run(DeepAgent().run(user_message="go"))
    # The run acquired a REAL session (not None) and threaded it into the backend build.
    assert isinstance(excinfo.value.session, SandboxSession)


def test_tool_face_run_gets_an_ephemeral_workspace_and_threaded_a_persistent_one() -> None:
    """A tool-face run (no thread_id) gets a fresh ephemeral workspace no other worker can name;
    a threaded run gets the deterministic agent-namespaced persistent volume."""

    async def go() -> tuple[str, str]:
        ephemeral = await DeepAgentSession.acquire(thread_id=None)
        threaded = await DeepAgentSession.acquire(thread_id="conv-1")
        return ephemeral.workspace_key, threaded.workspace_key

    ephemeral_key, threaded_key = asyncio.run(go())
    # The ephemeral key is a fresh uuid4 hex (32 chars, no dashes); the threaded key is the shared
    # agent-namespaced uuid5 (36 chars with dashes), stable across workers/turns.
    assert len(ephemeral_key) == 32
    assert threaded_key == session_mod.workspace_key_for("langchain_deep_agent", "conv-1")


# ---- credential materialization + scrub ---------------------------------------


def _settings_with_creds(monkeypatch: pytest.MonkeyPatch, *creds: SessionCredSpec) -> None:
    settings = LangchainDeepAgentSettings(session_image=_DIGEST, creds=list(creds))
    monkeypatch.setattr(session_mod, "langchain_deep_agent_settings", lambda: settings)


def test_bearer_cred_is_materialized_then_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refreshable ``delivery="bearer"`` connection cred is re-resolved per turn and written as
    an ``Authorization: Bearer`` credential-helper file under ``{ws}/.creds`` (OUTSIDE the
    agent-writable project tree), then removed on the terminal-exit scrub — the deep-agent
    analogue of the coding agent's teardown scrub (§B4)."""
    _connectors().resolved["conn-gh"] = ResolvedConnectionAuth(access_token=SecretStr("gho_secret"))
    _settings_with_creds(
        monkeypatch,
        ConnectionCred(env_name="github", connection_id="conn-gh", provider_id="github", sub_service="repo"),
    )

    async def go() -> tuple[str, bool]:
        drive = await DeepAgentSession.acquire(thread_id="conv-bearer")
        creds_file = Path(drive.session.workspace_path) / ".creds" / "github"
        body = creds_file.read_text()
        await drive.scrub_credentials()
        return body, creds_file.exists()

    body, exists_after_scrub = asyncio.run(go())
    assert body.strip() == "Authorization: Bearer gho_secret"
    # The bearer material does NOT live under the agent-writable project subtree.
    assert "project" not in Path(".creds", "github").parts
    assert exists_after_scrub is False
    assert _connectors().calls == [("conn-gh", "github", "repo")]


def test_static_and_env_creds_ride_the_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A static value and a connection ``delivery="env"`` value are baked into the CLEAN session
    env at create (no per-turn file); a run's exec sees them as the session-level base env."""
    _connectors().resolved["conn-svc"] = ResolvedConnectionAuth(env={"SVC_TOKEN": SecretStr("svc")})
    _settings_with_creds(
        monkeypatch,
        StaticCred(env_name="STATIC_KEY", value=SecretStr("static-val")),
        ConnectionCred(
            env_name="SVC_TOKEN",
            connection_id="conn-svc",
            provider_id="svc",
            sub_service="api",
            delivery="env",
        ),
    )

    async def go() -> str:
        drive = await DeepAgentSession.acquire(thread_id="conv-env")
        # No bearer file was written (both creds are env-delivery); the values reach exec via env.
        result = await drive.session.exec(
            ["sh", "-lc", 'printf \'%s:%s\' "$STATIC_KEY" "$SVC_TOKEN"'], timeout_seconds=30
        )
        return result.stdout

    assert asyncio.run(go()) == "static-val:svc"


def test_required_bearer_cred_resolving_to_nothing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``required`` connection cred whose resolution yields nothing usable raises loudly (never a
    silent drop) — the identity-less fail-close is the accessor's own raise, tested elsewhere."""
    _connectors().resolved["conn-empty"] = None
    _settings_with_creds(
        monkeypatch,
        ConnectionCred(env_name="EMPTY", connection_id="conn-empty", provider_id="p", sub_service="s", required=True),
    )
    with pytest.raises(RuntimeError, match="resolved to no usable credential"):
        asyncio.run(DeepAgentSession.acquire(thread_id="conv-empty"))


def test_non_required_cred_resolving_to_nothing_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A NON-``required`` connection cred whose resolution yields nothing usable injects nothing and
    is skipped (the ``continue``) — no env value, no bearer file, and no raise (only a ``required``
    empty resolution raises)."""
    _connectors().resolved["conn-skip"] = None
    _settings_with_creds(
        monkeypatch,
        ConnectionCred(env_name="SKIP", connection_id="conn-skip", provider_id="p", sub_service="s", required=False),
    )

    async def go() -> bool:
        drive = await DeepAgentSession.acquire(thread_id="conv-skip")
        # No bearer file was written for the skipped cred (it resolved to nothing usable).
        return (Path(drive.session.workspace_path) / ".creds" / "SKIP").exists()

    assert asyncio.run(go()) is False


def test_bearer_cred_carries_static_headers_alongside_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolved connection auth's static ``headers`` channel is flattened into the materialized
    credential-helper file ALONGSIDE the ``Authorization: Bearer`` token line (never instead of
    it) — so a provider needing a transport header gets it beside the OAuth token."""
    _connectors().resolved["conn-hdr"] = ResolvedConnectionAuth(
        access_token=SecretStr("tok"), headers={"X-Api-Version": SecretStr("2024-08")}
    )
    _settings_with_creds(
        monkeypatch,
        ConnectionCred(env_name="hdr", connection_id="conn-hdr", provider_id="p", sub_service="s"),
    )

    async def go() -> str:
        drive = await DeepAgentSession.acquire(thread_id="conv-hdr")
        return (Path(drive.session.workspace_path) / ".creds" / "hdr").read_text()

    body = asyncio.run(go())
    assert "Authorization: Bearer tok" in body
    assert "X-Api-Version: 2024-08" in body


def test_scrub_failure_raises_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """An un-removable ``.creds`` directory is a LOUD failure, never a silent leave-behind: a
    non-zero ``rm -rf`` exit surfaces the stderr in a raised ``RuntimeError`` (the invariant that no
    injected credential material silently persists past a terminal exit)."""
    _connectors().resolved["conn-scrub"] = ResolvedConnectionAuth(access_token=SecretStr("t"))
    _settings_with_creds(
        monkeypatch,
        ConnectionCred(env_name="S", connection_id="conn-scrub", provider_id="p", sub_service="s"),
    )

    async def go() -> None:
        drive = await DeepAgentSession.acquire(thread_id="conv-scrub")

        async def failing_exec(argv: Any, **kwargs: Any) -> ExecResult:
            return ExecResult(exit_code=1, stdout="", stderr="permission denied removing .creds")

        drive.session.exec = failing_exec  # type: ignore[method-assign]
        await drive.scrub_credentials()

    with pytest.raises(RuntimeError, match="could not scrub bearer credential material"):
        asyncio.run(go())


# ---- FIX 2: the workspace lease serializes session creation on the shared volume ------


@pytest.fixture
def fake_lease_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    """A fakeredis-backed workspace lease: the deep-agent session lease reaches Redis lazily through
    ``lease._lease_client``, so swapping it for an in-memory fake exercises the real ``SET NX`` /
    compare-and-delete without a live server."""
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def fake_client() -> AsyncIterator[aioredis.FakeRedis]:
        yield redis

    monkeypatch.setattr(lease_mod, "_lease_client", fake_client)
    monkeypatch.setattr(lease_mod, "agents_park_redis_settings", lambda: SimpleNamespace(redis_url="redis://fake"))
    return redis


def test_leased_loser_creates_no_session_and_writes_no_creds(
    monkeypatch: pytest.MonkeyPatch, fake_lease_redis: aioredis.FakeRedis
) -> None:
    """FIX 2: ``leased`` takes the workspace lease BEFORE creating the session / materializing creds,
    so a second worker acquiring the SAME ``thread_id`` loses the lease and raises
    ``WorkspaceLeaseHeldError`` from the lease ``__aenter__`` — BEFORE it opens a session or writes
    ``.creds``. The pre-fix acquire-then-lease order let the loser create a session (leaked, only
    TTL-reaped) and clobber the winner's cred write; here the loser leaks nothing."""
    _connectors().resolved["conn-race"] = ResolvedConnectionAuth(access_token=SecretStr("tok"))
    _settings_with_creds(
        monkeypatch,
        ConnectionCred(env_name="R", connection_id="conn-race", provider_id="p", sub_service="s"),
    )
    provider = _sandboxes().require_sandbox()
    creates = 0
    real_create = provider.create_session

    async def counting_create(spec: Any) -> Any:
        nonlocal creates
        creates += 1
        return await real_create(spec)

    monkeypatch.setattr(provider, "create_session", counting_create)

    async def go() -> bool:
        async with DeepAgentSession.leased(thread_id="conv-race") as winner:
            winner_has_creds = (Path(winner.session.workspace_path) / ".creds" / "R").exists()
            # A second same-thread_id worker cannot take the held lease: it raises from the lease
            # __aenter__ BEFORE reaching acquire(), so no second session and no second cred write.
            with pytest.raises(WorkspaceLeaseHeldError):
                async with DeepAgentSession.leased(thread_id="conv-race"):
                    pass
            return winner_has_creds

    winner_has_creds = asyncio.run(go())
    assert winner_has_creds is True
    # Exactly ONE session was created (the winner's) — the loser leaked none.
    assert creates == 1
    # The loser never reached _resolve_creds either, so the connector was resolved exactly once.
    assert _connectors().calls == [("conn-race", "p", "s")]


# ---- FIX 3: importing the agent module must not require the operator env ---------------


def test_import_without_creds_succeeds_and_full_validation_still_raises_at_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIX 3: executing the ``langchain_deep_agent.agent`` module body must NOT require any
    ``TAI_AGENTS_LANGCHAIN_DEEP_*`` env — the registration meta is sourced from the lightweight
    ``langchain_deep_agent_crash_resume`` read alone. The full ``LangchainDeepAgentSettings``
    validation (the digest-pinned ``session_image``) still fires LOUDLY at run start. Proven by
    executing a FRESH copy of the module source with the env cleared (its ``@tai42_app.agents.agent``
    decorator runs against the bound recording app) — the canonical module and its ``DeepAgent``
    class are left UNTOUCHED — then restoring the recording app's registration."""
    # Snapshot the recording-app registration: executing the fresh copy re-runs the decorator, which
    # transiently overwrites the registry/meta with the fresh module's class instance; restore it so
    # no fresh-class state leaks into later tests (the canonical module is never reloaded, so its
    # DeepAgent identity — which other test modules compare against — never diverges).
    original_agent = APP.agents.registry["langchain_deep_agent"]
    original_meta = APP.agents.meta["langchain_deep_agent"]
    for var in ("TAI_AGENTS_LANGCHAIN_DEEP_SESSION_IMAGE", "TAI_AGENTS_LANGCHAIN_DEEP_CRASH_RESUME"):
        monkeypatch.delenv(var, raising=False)
    # Drop the cached full settings so a registration that (before the fix) read them would re-read
    # the now-cleared env and raise — the lightweight ``crash_resume`` reader is uncached and needs
    # no clearing, so this only sharpens the fail-before guard.
    settings_mod.langchain_deep_agent_settings.cache_clear()
    spec = importlib.util.spec_from_file_location("_langchain_deep_agent_reimport", agent_mod.__file__)
    assert spec is not None
    assert spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    try:
        # Executing the module body runs the decorator with NO operator env present; it must NOT
        # raise, and the crash_resume meta defaults to False when unset.
        spec.loader.exec_module(fresh)
        assert APP.agents.meta["langchain_deep_agent"] == {"tai42/crash_resume": False}
        # Full config validation is unchanged — with no digest ``session_image`` it raises loudly, so
        # the loud error lands at run start (the first ``astream``/``run`` reads it), not at import.
        with pytest.raises(ValidationError):
            LangchainDeepAgentSettings()
    finally:
        settings_mod.langchain_deep_agent_settings.cache_clear()
        APP.agents.registry["langchain_deep_agent"] = original_agent
        APP.agents.meta["langchain_deep_agent"] = original_meta
