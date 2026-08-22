"""The conformance suite has teeth: each check FAILS loudly against a provider
that violates exactly the property it certifies.

These drive the individual check functions against deliberately non-conformant
fakes, proving the suite would red a real provider that (say) let a workspace
outlive its session or swallowed a write-after-exit — the same guarantees a
shipped provider must keep.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

import pytest
from pydantic import SecretStr
from tai42_contract.sandbox import ExecResult, SandboxError, SandboxSessionSpec

from tai42_kit.sandbox import SandboxConformanceConfig, permissive_policy
from tai42_kit.sandbox.conformance import (
    check_exec_timeout,
    check_file_transfer,
    check_interactive_exec,
    check_persistent_survives_reap,
    check_reap_and_destroy,
    check_spec_rejection,
)

from .fakes import FakeExecHandle, FakeSandbox, FakeSandboxSession

_CONFIG = SandboxConformanceConfig(image="fake:image")


def _capped_spec() -> SandboxSessionSpec:
    return SandboxSessionSpec(
        image="fake:image", workspace_key="capped", durability="ephemeral", network="egress", ttl_seconds=300, cpu=1.0
    )


class _LenientHandle(FakeExecHandle):
    async def write_stdin(self, data: bytes) -> None:
        self._buffer.extend(data)  # never raises, even after exit


class _LenientSession(FakeSandboxSession):
    handle_cls: ClassVar[type[FakeExecHandle]] = _LenientHandle


class _LenientSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _LenientSession


class _PhantomFileSession(FakeSandboxSession):
    async def get_file(self, path: str) -> bytes:
        try:
            return await super().get_file(path)
        except SandboxError:
            return b"phantom"  # a miss that should have raised


class _PhantomFileSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _PhantomFileSession


class _NoTimeoutSession(FakeSandboxSession):
    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, SecretStr] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: float,
    ) -> ExecResult:
        if argv[0] == "sleep":
            return ExecResult(exit_code=0, stdout="", stderr="")  # ignores the timeout
        return await super().exec(argv, cwd=cwd, env=env, stdin=stdin, timeout_seconds=timeout_seconds)


class _NoTimeoutSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _NoTimeoutSession


class _ClingySandbox(FakeSandbox):
    """A provider that fails to forget a reaped session — get_session still
    resolves it."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: dict[str, FakeSandboxSession] = {}

    async def _create_session_resources(self, spec: SandboxSessionSpec) -> FakeSandboxSession:
        session = await super()._create_session_resources(spec)
        assert isinstance(session, FakeSandboxSession)
        self._seen[session.id] = session
        return session

    async def get_session(self, session_id: str) -> FakeSandboxSession:
        return self._seen[session_id]


def _bind(sandbox: FakeSandbox) -> FakeSandbox:
    sandbox.bind_policy(permissive_policy())
    return sandbox


async def test_a_swallowed_write_after_exit_is_caught() -> None:
    with pytest.raises(AssertionError, match="write_stdin after exit"):
        await check_interactive_exec(_bind(_LenientSandbox()), _CONFIG)


async def test_a_missing_file_that_does_not_raise_is_caught() -> None:
    with pytest.raises(AssertionError, match="get_file on a miss"):
        await check_file_transfer(_bind(_PhantomFileSandbox()), _CONFIG)


async def test_an_ephemeral_workspace_that_outlives_its_session_is_caught() -> None:
    with pytest.raises(AssertionError, match="ephemeral workspace outlived"):
        await check_persistent_survives_reap(_bind(FakeSandbox(ephemeral_persists=True)), _CONFIG)


async def test_a_reaped_session_still_resolvable_is_caught() -> None:
    with pytest.raises(AssertionError, match="reaped session was still resolvable"):
        await check_reap_and_destroy(_bind(_ClingySandbox()), _CONFIG)


async def test_a_spec_the_provider_should_reject_but_accepts_is_caught() -> None:
    config = SandboxConformanceConfig(image="fake:image", reject_specs=[_capped_spec()])
    with pytest.raises(AssertionError, match="was not rejected"):
        await check_spec_rejection(_bind(FakeSandbox(reject_caps=False)), config)


async def test_an_exec_that_ignores_its_timeout_is_caught() -> None:
    with pytest.raises(AssertionError, match="exec past its timeout"):
        await check_exec_timeout(_bind(_NoTimeoutSandbox()), _CONFIG)
