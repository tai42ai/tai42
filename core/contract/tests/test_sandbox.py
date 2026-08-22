"""Tests for the sandbox contract: the neutral session models, the resolved
:class:`SandboxPolicy` + ordering helpers, the error family, the provider/session
ABCs, and the ``AppSandboxes`` / ``AppInteractions`` facade seams plus the
manifest wiring (kind, binding, ``sandbox_module`` field)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from ._helpers import protocol_members

# -- Models --------------------------------------------------------------------


def _spec(**overrides: Any):
    from tai42_contract.sandbox import SandboxSessionSpec

    base: dict[str, Any] = {
        "image": "registry.example/img@sha256:abc",
        "workspace_key": "ws-01",
        "durability": "ephemeral",
        "ttl_seconds": 3600,
    }
    base.update(overrides)
    return SandboxSessionSpec.model_validate(base)


def test_session_spec_minimal_defaults():
    from tai42_contract.sandbox import SandboxSessionSpec

    spec = _spec()
    assert isinstance(spec, SandboxSessionSpec)
    assert spec.env == {}
    assert spec.network == "none"
    assert spec.isolation is None
    assert spec.cpu is None
    assert spec.memory_mb is None
    assert spec.labels == {}


def test_ttl_seconds_must_be_positive():
    with pytest.raises(ValidationError):
        _spec(ttl_seconds=0)
    with pytest.raises(ValidationError):
        _spec(ttl_seconds=-5)
    assert _spec(ttl_seconds=1).ttl_seconds == 1


@pytest.mark.parametrize("key", ["ws-01", "A_b-9", "x" * 64])
def test_workspace_key_accepts_valid_charset(key: str):
    assert _spec(workspace_key=key).workspace_key == key


@pytest.mark.parametrize("key", ["", "x" * 65, "has space", "bad/slash", "dot.dot", "unicodé"])
def test_workspace_key_rejects_out_of_charset(key: str):
    with pytest.raises(ValueError, match="workspace_key"):
        _spec(workspace_key=key)


@pytest.mark.parametrize("tier", ["ephemeral", "persistent"])
def test_durability_literal_accepts_both_tiers(tier: str):
    assert _spec(durability=tier).durability == tier


def test_durability_literal_rejects_unknown_tier():
    with pytest.raises(ValidationError):
        _spec(durability="durable")


def test_isolation_accepts_none_and_every_tier():
    assert _spec(isolation=None).isolation is None
    for tier in ("none", "container", "vm"):
        assert _spec(isolation=tier).isolation == tier


def test_isolation_rejects_unknown_tier():
    with pytest.raises(ValidationError):
        _spec(isolation="hypervisor")


@pytest.mark.parametrize("net", ["none", "internal", "egress"])
def test_network_literal_accepts_every_tier(net: str):
    assert _spec(network=net).network == net


def test_network_literal_rejects_unknown_tier():
    with pytest.raises(ValidationError):
        _spec(network="wide-open")


def test_env_values_are_secret_and_masked_in_repr():
    from pydantic import SecretStr

    spec = _spec(env={"API_KEY": "s3cr3t"})
    assert isinstance(spec.env["API_KEY"], SecretStr)
    assert spec.env["API_KEY"].get_secret_value() == "s3cr3t"
    # The secret must never surface in repr/str (its only credential channel is
    # the session, never a log line).
    assert "s3cr3t" not in repr(spec)
    assert "s3cr3t" not in str(spec)


def test_exec_result_carries_streams_and_code():
    from tai42_contract.sandbox import ExecResult

    res = ExecResult(exit_code=0, stdout="ok", stderr="")
    assert (res.exit_code, res.stdout, res.stderr) == (0, "ok", "")


def test_session_info_carries_workspace_path():
    from tai42_contract.sandbox import SandboxSessionInfo

    now = datetime.now(UTC)
    info = SandboxSessionInfo(
        id="s1",
        workspace_key="ws-01",
        workspace_path="/workspace",
        durability="persistent",
        created_at=now,
        expires_at=now,
    )
    assert info.workspace_path == "/workspace"
    assert info.labels == {}


def test_stream_chunk_and_exit_shapes():
    from tai42_contract.sandbox import SandboxStreamChunk, SandboxStreamExit

    chunk = SandboxStreamChunk(stream="stdout", data=b"hi")
    assert chunk.stream == "stdout"
    assert chunk.data == b"hi"
    assert SandboxStreamExit(exit_code=3).exit_code == 3
    with pytest.raises(ValidationError):
        SandboxStreamChunk.model_validate({"stream": "both", "data": b""})


# -- Policy --------------------------------------------------------------------


def test_sandbox_policy_has_four_fields():
    from tai42_contract.sandbox import SandboxPolicy

    policy = SandboxPolicy(egress="internal", isolation="container", scrub_transcript=True, durable=False)
    assert set(SandboxPolicy.model_fields) == {"egress", "isolation", "scrub_transcript", "durable"}
    assert policy.egress == "internal"
    assert policy.isolation == "container"
    assert policy.scrub_transcript is True
    assert policy.durable is False


def test_network_openness_orders_none_internal_egress():
    from tai42_contract.sandbox import network_openness

    assert network_openness("none") < network_openness("internal") < network_openness("egress")


def test_isolation_strength_orders_none_container_vm():
    from tai42_contract.sandbox import isolation_strength

    assert isolation_strength("none") < isolation_strength("container") < isolation_strength("vm")


# -- Errors --------------------------------------------------------------------


def test_error_family_all_derive_from_sandbox_error():
    from tai42_contract.sandbox import (
        SandboxError,
        SandboxExecTimeoutError,
        SandboxSessionNotFoundError,
        SandboxSpecRejectedError,
        SandboxUnavailableError,
    )

    for cls in (
        SandboxUnavailableError,
        SandboxSessionNotFoundError,
        SandboxSpecRejectedError,
    ):
        assert issubclass(cls, SandboxError)
    assert issubclass(SandboxExecTimeoutError, SandboxError)


def test_session_not_found_carries_id():
    from tai42_contract.sandbox import SandboxSessionNotFoundError

    err = SandboxSessionNotFoundError("s9")
    assert err.session_id == "s9"
    assert "s9" in str(err)


def test_exec_timeout_carries_lengths_never_content():
    from tai42_contract.sandbox import SandboxExecTimeoutError

    err = SandboxExecTimeoutError(timeout_seconds=2.5, stdout_len=10, stderr_len=4)
    assert err.timeout_seconds == 2.5
    assert err.stdout_len == 10
    assert err.stderr_len == 4
    # Only lengths — the message reports byte counts, never captured output.
    assert "10 bytes" in str(err)
    assert "4 bytes" in str(err)


# -- ABCs ----------------------------------------------------------------------


def test_sandbox_abcs_are_abstract():
    from tai42_contract.sandbox import Sandbox, SandboxExecHandle, SandboxSession

    for abc_cls in (Sandbox, SandboxSession, SandboxExecHandle):
        assert abc_cls.__abstractmethods__, f"{abc_cls.__name__} has no abstract methods"
        with pytest.raises(TypeError):
            abc_cls()  # pyright: ignore[reportAbstractUsage]


def test_sandbox_abstract_methods_pinned_by_name():
    from tai42_contract.sandbox import Sandbox, SandboxExecHandle, SandboxSession

    assert Sandbox.__abstractmethods__ == frozenset(
        {"create_session", "get_session", "list_sessions", "destroy_session", "reap"}
    )
    assert SandboxSession.__abstractmethods__ == frozenset(
        {"id", "workspace_path", "info", "exec", "exec_start", "put_file", "get_file", "touch", "destroy"}
    )
    assert SandboxExecHandle.__abstractmethods__ == frozenset({"write_stdin", "close_stdin", "output", "kill"})


# -- Facet: AppSandboxes / AppInteractions -------------------------------------


def test_app_sandboxes_protocol_members():
    from tai42_contract.app import AppSandboxes

    assert protocol_members(AppSandboxes) == {
        "register_sandbox",
        "sandbox",
        "require_sandbox",
        "sandbox_policy",
    }


def test_require_sandbox_return_and_raise_types_are_the_contract_types():
    from typing import get_type_hints

    from tai42_contract.app import AppSandboxes
    from tai42_contract.sandbox import Sandbox, SandboxPolicy

    # The raising acquisition chokepoint returns the provider ABC; the read accessor
    # returns the contract-defined policy the plugin imports.
    assert get_type_hints(AppSandboxes.require_sandbox)["return"] is Sandbox
    assert get_type_hints(AppSandboxes.sandbox_policy)["return"] is SandboxPolicy


def test_require_sandbox_raises_the_contract_unavailable_type():
    # ``require_sandbox`` is the single acquisition chokepoint; its declared raise is
    # the ONE contract type every consumer catches when no provider backs the seam.
    from tai42_contract.sandbox import SandboxError, SandboxUnavailableError

    assert issubclass(SandboxUnavailableError, SandboxError)


def test_app_interactions_exposes_ask_user_typed_by_the_contract_protocol():
    import inspect
    from typing import get_type_hints

    from tai42_contract.app import AppInteractions
    from tai42_contract.interactions import AskUser

    assert protocol_members(AppInteractions) == {"ask_user"}
    # ``ask_user`` is a read property typed as the already-carried AskUser Protocol.
    prop = inspect.getattr_static(AppInteractions, "ask_user")
    assert isinstance(prop, property)
    assert prop.fget is not None
    assert get_type_hints(prop.fget)["return"] is AskUser


# -- Manifest wiring: kind, binding, sandbox_module ----------------------------


def test_sandbox_kind_and_binding_mirror_backend():
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS, ManifestBinding, PluginItemKind

    assert PluginItemKind.SANDBOX.value == "sandbox"
    assert KIND_MANIFEST_BINDINGS[PluginItemKind.SANDBOX] == ManifestBinding(
        field="sandbox_module", mode="scalar_module", payload="module"
    )
    # Same wiring shape as BACKEND, the scalar-module sibling.
    assert KIND_MANIFEST_BINDINGS[PluginItemKind.SANDBOX].mode == KIND_MANIFEST_BINDINGS[PluginItemKind.BACKEND].mode


def test_manifest_sandbox_module_field():
    from tai42_contract.manifest import Manifest

    assert "sandbox_module" in Manifest.model_fields
    assert Manifest().sandbox_module is None
    assert Manifest(sandbox_module="tai42_sandbox_docker").sandbox_module == "tai42_sandbox_docker"
