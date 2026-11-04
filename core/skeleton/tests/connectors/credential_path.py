"""A managed OAuth connection end to end, for the listing and reload tests of the credential path.

One declared provider ``acme`` whose ``mail`` server is a real loopback MCP server
(:mod:`mcp_fakes`) and whose token endpoint is a loopback protocol fixture (:mod:`oauth_fakes`); the
connection's record lives sealed in an in-memory compare-and-set store wired under the real resolver.
Every store read, lock take, store write and token request is counted, so a test asserts what a
listing touched. A real second event loop in its own thread stands for the serving loop when a test
binds it as the refresh owner.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from tai42_contract.connectors.models import AuthHealthState, ConnectionRecord
from tai42_contract.connectors.providers import (
    McpServerDescriptor,
    OAuthEndpoints,
    ProviderDescriptor,
    SubServiceDescriptor,
)
from tai42_contract.manifest import TaiMCPConfig
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.connectors.runtime.resolver as resolver_mod
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.providers import registry
from tai42_skeleton.connectors.runtime import refresh_owner
from tai42_skeleton.connectors.service.manifest_writer import _build_entry

from .mcp_fakes import FakeMcpServer
from .oauth_fakes import FakeOAuthServer, Reply

CONNECTION_ID = "33333333-3333-4333-8333-333333333333"


class SealedStore:
    """The connection's record, sealed as the real store holds it, behind a compare-and-set ``put``."""

    def __init__(self) -> None:
        self.blob: bytes | None = None
        self.writes = 0
        self.unreachable = False

    def seal(self, record: ConnectionRecord) -> None:
        self.blob = crypto.encrypt(record.to_storage_json().encode(), aad=record.connection_id)

    def record(self) -> ConnectionRecord:
        if self.blob is None:
            raise AssertionError("no record stored")
        return ConnectionRecord.model_validate_json(crypto.decrypt(self.blob, aad=CONNECTION_ID))

    async def put(
        self,
        connection_id,
        blob,
        *,
        create_only=False,
        expected_blob=None,
        session_expires_at=None,
        provider_id=None,
        alias=None,
    ):
        if self.unreachable:
            raise ConnectionError("store unreachable")
        if expected_blob is not None and expected_blob != self.blob:
            return False
        self.blob = blob
        self.writes += 1
        return True


class CredentialPath:
    """The wiring and its counters; see the module docstring."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, mcp_server: FakeMcpServer, oauth: FakeOAuthServer) -> None:
        self.mcp = mcp_server
        self.oauth = oauth
        self.store = SealedStore()
        self.loads = 0
        self.locks = 0
        self.lock_held = False
        self.cooldown = False
        # Other connections, read-only: a sealed record to serve, or an exception its load raises.
        self.others: dict[str, ConnectionRecord | BaseException] = {}
        self.descriptor = ProviderDescriptor(
            id="acme",
            display_name="Acme",
            kind="oauth",
            origin="system",
            category="productivity",
            oauth=OAuthEndpoints(authorize=f"{oauth.base}/authorize", token=f"{oauth.base}/token"),
            client_id_env="ACME_CLIENT_ID",
            client_secret_env="ACME_CLIENT_SECRET",
            sub_services={
                "mail": SubServiceDescriptor(
                    id="mail",
                    display_name="Mail",
                    scopes=["mail.read"],
                    mcp_server=McpServerDescriptor(type="http", url=mcp_server.url),
                ),
            },
        )
        self.entry: TaiMCPConfig = _build_entry(
            descriptor=self.descriptor, sub_service_id="mail", alias="work", connection_id=CONNECTION_ID
        )
        registry.reset_registry()
        registry.register_connector(self.descriptor)

        monkeypatch.setenv("CONNECTORS_KEK", base64.b64encode(bytes(range(32))).decode())
        monkeypatch.setenv("CONNECTORS_STATE_HMAC_KEY", base64.b64encode(bytes(range(32, 64))).decode())
        monkeypatch.setenv("ACME_CLIENT_ID", "client-123")
        monkeypatch.setenv("ACME_CLIENT_SECRET", "secret-xyz")
        reset_all_settings()

        async def _load(connection_id: str) -> tuple[ConnectionRecord, bytes]:
            self.loads += 1
            if connection_id != CONNECTION_ID:
                other = self.others[connection_id]
                if isinstance(other, BaseException):
                    raise other
                return other, crypto.encrypt(other.to_storage_json().encode(), aad=connection_id)
            if self.store.unreachable:
                raise ConnectionError("store unreachable")
            if self.store.blob is None:
                from tai42_skeleton.connectors.store.persistence import ConnectionNotFoundError

                raise ConnectionNotFoundError(connection_id)
            return self.store.record(), self.store.blob

        @asynccontextmanager
        async def _lock(connection_id: str):
            self.locks += 1
            while self.lock_held:
                await asyncio.sleep(0.01)
            yield

        async def _cooldown_active(connection_id: str) -> bool:
            return self.cooldown

        async def _none(connection_id: str) -> None:
            return None

        async def _claim(connection_id: str, seconds: int) -> bool:
            return False

        monkeypatch.setattr(resolver_mod, "load_record_with_blob", _load)
        monkeypatch.setattr(resolver_mod, "connection_lock", _lock)
        monkeypatch.setattr(resolver_mod, "token_store", lambda: self.store)
        monkeypatch.setattr(resolver_mod, "refresh_cooldown_active", _cooldown_active)
        monkeypatch.setattr(resolver_mod, "open_refresh_cooldown", _none)
        monkeypatch.setattr(resolver_mod, "clear_refresh_cooldown", _none)
        monkeypatch.setattr(resolver_mod, "claim_keep_alive", _claim)
        resolver_mod.reset_keep_alive_stamps()
        refresh_owner.reset()
        mcp_server.reset()
        oauth.clear()

    # -- the record ------------------------------------------------------------------------------

    def connect(
        self,
        *,
        expires_in: float = 3600,
        access_token: str = "at-old",
        refresh_token: str | None = "rt-old",
        health: AuthHealthState = AuthHealthState.HEALTHY,
    ) -> None:
        now = datetime.now(UTC)
        self.store.seal(
            ConnectionRecord(
                connection_id=CONNECTION_ID,
                provider_id="acme",
                kind="oauth",
                alias="work",
                account_identity="alice@example.com",
                enabled_sub_services=["mail"],
                granted_scopes=["mail.read"],
                access_token=SecretStr(access_token),
                refresh_token=SecretStr(refresh_token) if refresh_token is not None else None,
                access_token_expires_at=now + timedelta(seconds=expires_in),
                auth_health_state=health,
                created_at=now,
            )
        )

    def stored(self) -> ConnectionRecord:
        return self.store.record()

    # -- the token endpoint ------------------------------------------------------------------------

    def token_endpoint(
        self,
        *,
        delay: float = 0.0,
        status: int = 200,
        body: dict | None = None,
        hold: Callable[[], object] | None = None,
    ) -> None:
        """Answer every refresh with a rotated pair (after ``delay`` seconds, and once ``hold`` returns)."""
        self.oauth.route(
            "POST",
            "/token",
            Reply(
                status=status,
                delay=delay,
                hold=hold,
                body=body
                if body is not None
                else {"access_token": "at-new", "refresh_token": "rt-rotated", "expires_in": 3600},
            ),
        )

    @property
    def token_requests(self) -> int:
        return len(self.oauth.requests_to("/token", "POST"))

    # -- what reached the MCP server ---------------------------------------------------------------

    @property
    def attempts(self) -> list[str | None]:
        """The ``Authorization`` of every connection attempt (``initialize``) the server received."""
        return [seen.authorization for seen in self.mcp.initializes]

    def eventually_rotated(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            stored = self.stored()
            if stored.refresh_token is not None and stored.refresh_token.get_secret_value() == "rt-rotated":
                return
            time.sleep(0.02)
        raise AssertionError("the refresh did not complete and store the rotated token")


@contextmanager
def owner_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A long-lived loop in its own thread, bound as the refresh owner (the serving loop's stand-in).

    It closes as ``asyncio.run`` closes its loop: once the refresh owner is shut down and the kit's
    resources kept for the loop are released, every task still on the loop (a late start's apply
    the test did not wait out, say) is cancelled and run to its end, so whatever it holds is
    released before the loop closes. A
    task that fails instead of ending on its cancellation fails the teardown.
    """

    async def _bind() -> None:
        refresh_owner.bind_to_running_loop()

    async def _close() -> None:
        await refresh_owner.aclose()
        await _end_loop_resources()

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        asyncio.run_coroutine_threadsafe(_bind(), loop).result(5)
        yield loop
    finally:
        try:
            asyncio.run_coroutine_threadsafe(_close(), loop).result(30)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
            loop.close()


class JobLoop:
    """An in-process backend job's own loop, run the way the backends run it: one job at a time.

    Each :meth:`run` is one job: on a thread of its own, in a copy of the caller's context, the
    loop runs exactly the coroutine it is handed (``run_until_complete``) and then stops. A task
    the job left on the loop runs again only when a later job runs on the same loop, as on a pool
    that reuses its job loops; a test that runs one job models a pool that makes a loop per job
    and never runs it again.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def run(self, coro: Any, timeout: float = 30.0) -> Any:
        """Run ``coro`` as one job on this loop from this (non-loop) thread, and return its result."""
        outcome: list[Any] = []
        failure: list[BaseException] = []

        def _job() -> None:
            try:
                outcome.append(self.loop.run_until_complete(coro))
            except BaseException as error:
                failure.append(error)

        thread = threading.Thread(target=contextvars.copy_context().run, args=(_job,), daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError(f"the job did not end within {timeout}s")
        if failure:
            raise failure[0]
        return outcome[0]

    async def run_awaited(self, coro: Any, timeout: float = 30.0) -> Any:
        """Run ``coro`` as one job on this loop from a running loop, which it does not block."""
        return await asyncio.to_thread(self.run, coro, timeout)


@contextmanager
def job_loop() -> Iterator[JobLoop]:
    """An in-process backend job's own loop (:class:`JobLoop`), apart from the serving loop.

    At exit the kit's resources kept for the loop are released and its left tasks are ended, on
    one last run of the loop, as at :func:`owner_loop`'s close, and the loop is closed.
    """
    loop = asyncio.new_event_loop()
    job = JobLoop(loop)
    try:
        yield job
    finally:
        try:
            job.run(_end_loop_resources())
        finally:
            loop.close()


async def _end_loop_resources() -> None:
    """On the running loop, at its close: release the kit's resources kept for the loop, end the left
    tasks, then the async generators and the executor."""
    from tai42_kit.loop_bound import release_loop_bound_resources

    await release_loop_bound_resources()
    await _end_left_tasks()
    loop = asyncio.get_running_loop()
    await loop.shutdown_asyncgens()
    await loop.shutdown_default_executor()


async def _end_left_tasks() -> None:
    """Cancel every other task on the running loop and wait for each to end; one that failed instead raises."""
    left = asyncio.all_tasks() - {asyncio.current_task()}
    for task in left:
        task.cancel()
    await asyncio.gather(*left, return_exceptions=True)
    failures = [error for task in left if not task.cancelled() and (error := task.exception()) is not None]
    if failures:
        raise BaseExceptionGroup("tasks left on the loop failed when cancelled", failures)


def on_loop(loop: asyncio.AbstractEventLoop, coro: Any, timeout: float = 30.0) -> Any:
    """Run ``coro`` on ``loop`` from this (non-loop) thread and return its result."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


def bind_as_serving(state: LifecycleState, loop: asyncio.AbstractEventLoop) -> None:
    """Bind ``loop`` (running in its own thread) as ``state``'s serving loop, from inside that loop."""

    async def _bind() -> None:
        state.bind_serving_loop()

    on_loop(loop, _bind(), timeout=5)
