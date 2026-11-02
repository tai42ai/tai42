"""The session checks have teeth: each FAILS loudly against a provider that breaks exactly the
session property it certifies.

These drive the checks of :mod:`tai42_kit.sandbox.conformance_session` against deliberately
non-conformant fakes: one that lets an ephemeral workspace outlive its session, keeps a reaped
session resolvable, answers a missing file, or accepts a spec it should reject.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from tai42_contract.sandbox import (
    SandboxError,
    SandboxSessionSpec,
)

from tai42_kit.sandbox import SandboxConformanceConfig
from tai42_kit.sandbox.conformance_session import (
    check_file_transfer,
    check_persistent_survives_reap,
    check_reap_and_destroy,
    check_spec_rejection,
)

from .fakes import FAKE_CONFORMANCE_CONFIG, FakeSandbox, FakeSandboxSession, with_permissive_policy


def _capped_spec() -> SandboxSessionSpec:
    return SandboxSessionSpec(
        image="fake:image", workspace_key="capped", durability="ephemeral", network="egress", ttl_seconds=300, cpu=1.0
    )


class _PhantomFileSession(FakeSandboxSession):
    async def get_file(self, path: str) -> bytes:
        try:
            return await super().get_file(path)
        except SandboxError:
            return b"phantom"  # a miss that should have raised


class _PhantomFileSandbox(FakeSandbox):
    session_cls: ClassVar[type[FakeSandboxSession]] = _PhantomFileSession


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


async def test_a_missing_file_that_does_not_raise_is_caught() -> None:
    with pytest.raises(AssertionError, match="get_file on a miss"):
        await check_file_transfer(with_permissive_policy(_PhantomFileSandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_an_ephemeral_workspace_that_outlives_its_session_is_caught() -> None:
    with pytest.raises(AssertionError, match="ephemeral workspace outlived"):
        await check_persistent_survives_reap(
            with_permissive_policy(FakeSandbox(ephemeral_persists=True)), FAKE_CONFORMANCE_CONFIG
        )


async def test_a_reaped_session_still_resolvable_is_caught() -> None:
    with pytest.raises(AssertionError, match="reaped session was still resolvable"):
        await check_reap_and_destroy(with_permissive_policy(_ClingySandbox()), FAKE_CONFORMANCE_CONFIG)


async def test_a_spec_the_provider_should_reject_but_accepts_is_caught() -> None:
    config = SandboxConformanceConfig(image="fake:image", reject_specs=[_capped_spec()])
    with pytest.raises(AssertionError, match="was not rejected"):
        await check_spec_rejection(with_permissive_policy(FakeSandbox(reject_caps=False)), config)
