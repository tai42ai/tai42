"""The skeleton sandbox seam: the holder, the resolved-policy binding, the facade
accessor, and the identity door.

Mirrors the backend holder suite plus the sandbox-specific seams: register binds the
resolved :class:`SandboxPolicy` onto the provider via the kit ``bind_policy``, the
``sandbox_policy()`` accessor returns that SAME resolved policy and is callable with NO
provider registered, and ``sandbox_info()["policy"]`` reflects the same values (accessor
== door) and is present even with no provider.
"""

from __future__ import annotations

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.sandbox import (
    SandboxPolicy,
    SandboxSessionSpec,
    SandboxUnavailableError,
)
from tai42_kit.sandbox import ManagedSandbox, ManagedSandboxSession

from tai42_skeleton.app.instance import build_app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations.sandbox import sandbox_info
from tai42_skeleton.sandbox.policy import resolve_sandbox_policy
from tai42_skeleton.sandbox.registry import SandboxHolder


class _FakeSandbox(ManagedSandbox):
    """A minimal provider over the shipped kit base: the holder/door tests never
    create a session, so the three runtime primitives are stubs (an empty ledger drives
    ``list_sessions`` / ``reap``)."""

    async def _create_session_resources(self, spec: SandboxSessionSpec) -> ManagedSandboxSession:
        raise NotImplementedError

    async def _destroy_session_resources(self, session: ManagedSandboxSession, *, remove_workspace: bool) -> None:
        raise NotImplementedError

    async def _list_orphan_resources(self) -> list[str]:
        return []


# -- holder -------------------------------------------------------------------


def test_register_binds_the_resolved_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = SandboxPolicy(egress="internal", isolation="vm", scrub_transcript=True, durable=False)
    monkeypatch.setattr("tai42_skeleton.sandbox.registry.resolve_sandbox_policy", lambda: resolved)
    holder = SandboxHolder()
    holder.register_sandbox(_FakeSandbox)
    assert isinstance(holder.sandbox, _FakeSandbox)
    # The resolved policy the holder bound is the value the kit create chokepoint enforces.
    assert holder.sandbox._policy == resolved  # pyright: ignore[reportPrivateUsage]


def test_require_raises_naming_the_setting_and_field() -> None:
    holder = SandboxHolder()
    with pytest.raises(SandboxUnavailableError) as excinfo:
        holder.require()
    message = str(excinfo.value)
    assert "TAI_MCP_SANDBOX" in message
    assert "sandbox_module" in message


def test_require_returns_the_registered_provider() -> None:
    holder = SandboxHolder()
    holder.register_sandbox(_FakeSandbox)
    assert holder.require() is holder.sandbox


def test_second_registration_is_a_loud_conflict() -> None:
    holder = SandboxHolder()
    holder.register_sandbox(_FakeSandbox)
    with pytest.raises(RuntimeError, match="already registered"):
        holder.register_sandbox(_FakeSandbox)


def test_register_rejects_a_non_managed_provider() -> None:
    class _NotManaged:
        pass

    holder = SandboxHolder()
    with pytest.raises(TypeError, match="ManagedSandbox"):
        holder.register_sandbox(_NotManaged)  # pyright: ignore[reportArgumentType]


# -- resolved policy ----------------------------------------------------------


def test_resolve_sandbox_policy_reads_the_four_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_MCP_SANDBOX_EGRESS", "none")
    monkeypatch.setenv("TAI_MCP_SANDBOX_ISOLATION", "vm")
    monkeypatch.setenv("TAI_MCP_SANDBOX_SCRUB_TRANSCRIPT", "true")
    monkeypatch.setenv("TAI_MCP_SANDBOX_DURABLE", "false")
    policy = resolve_sandbox_policy()
    assert policy == SandboxPolicy(egress="none", isolation="vm", scrub_transcript=True, durable=False)


def test_resolved_policy_defaults_are_the_open_platform_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "TAI_MCP_SANDBOX_EGRESS",
        "TAI_MCP_SANDBOX_ISOLATION",
        "TAI_MCP_SANDBOX_SCRUB_TRANSCRIPT",
        "TAI_MCP_SANDBOX_DURABLE",
    ):
        monkeypatch.delenv(var, raising=False)
    policy = resolve_sandbox_policy()
    assert policy == SandboxPolicy(egress="egress", isolation="container", scrub_transcript=False, durable=True)


# -- facade accessor + identity door ------------------------------------------


@pytest.fixture
def bound_app(monkeypatch: pytest.MonkeyPatch):
    app = build_app()
    tai42_app.bind(app)
    monkeypatch.setattr(app, "_manifest", Manifest.model_validate({}), raising=False)
    monkeypatch.setattr(app._sandbox_holder, "_sandbox", None)
    return app


def test_policy_accessor_callable_with_no_provider(bound_app) -> None:
    # The accessor reads operator config, so it resolves REGARDLESS of a provider.
    assert bound_app.sandboxes.sandbox is None
    assert isinstance(bound_app.sandboxes.sandbox_policy(), SandboxPolicy)


async def test_sandbox_info_present_false_shape_and_policy_present(bound_app) -> None:
    # The door never raises with no provider, and the resolved policy is present regardless.
    info = await sandbox_info()
    assert info["present"] is False
    assert info["provider"] is None
    assert info["module"] is None
    assert info["sessions"] == 0
    assert set(info["policy"]) == {"egress", "isolation", "scrub_transcript", "durable"}


async def test_sandbox_info_policy_equals_the_accessor(bound_app) -> None:
    # Accessor == door: both read the ONE shared resolved policy.
    info = await sandbox_info()
    policy = bound_app.sandboxes.sandbox_policy()
    assert info["policy"] == {
        "egress": policy.egress,
        "isolation": policy.isolation,
        "scrub_transcript": policy.scrub_transcript,
        "durable": policy.durable,
    }


async def test_sandbox_info_present_true_when_registered(bound_app, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bound_app._sandbox_holder, "_sandbox", _FakeSandbox())
    info = await sandbox_info()
    assert info["present"] is True
    assert info["provider"] == "_FakeSandbox"
    assert info["module"] == _FakeSandbox.__module__
    assert info["sessions"] == 0
