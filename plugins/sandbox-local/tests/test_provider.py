"""Unit tests for the direct/host provider — real host subprocesses, no engine.

Each test drives a genuine ``LocalSandbox`` over a temp workspace root and asserts
the spec→host-subprocess mapping, the clean-env / realpath-containment security
invariants, durability→host-dir mapping, loud rejection of what the direct mode
cannot enforce, and orphan recovery.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from tai42_contract.sandbox import (
    SandboxError,
    SandboxExecTimeoutError,
    SandboxSessionSpec,
    SandboxSpecRejectedError,
)

import tai42_sandbox_local

LocalSandbox = tai42_sandbox_local.LocalSandbox


def _spec(
    *,
    workspace_key: str = "ws-unit",
    durability: str = "ephemeral",
    network: str = "egress",
    isolation: str | None = "none",
    image: str = "host",
    env: dict[str, SecretStr] | None = None,
    cpu: float | None = None,
    memory_mb: int | None = None,
    labels: dict[str, str] | None = None,
    ttl_seconds: int = 300,
) -> SandboxSessionSpec:
    return SandboxSessionSpec(
        image=image,
        workspace_key=workspace_key,
        durability=durability,  # pyright: ignore[reportArgumentType]
        network=network,  # pyright: ignore[reportArgumentType]
        isolation=isolation,  # pyright: ignore[reportArgumentType]
        env=env or {},
        cpu=cpu,
        memory_mb=memory_mb,
        labels=labels or {},
        ttl_seconds=ttl_seconds,
    )


def _expire_and_reap(sandbox: LocalSandbox, session_id: str) -> None:
    record = sandbox._ledger[session_id]
    record.expires_at = record.created_at


# -- registration ----------------------------------------------------------------


def test_import_registers_the_provider(stub_app: Any) -> None:
    assert stub_app.sandboxes.registered is LocalSandbox


# -- spec -> host subprocess -----------------------------------------------------


async def test_exec_uses_clean_env_not_host_environ(sandbox: LocalSandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST_ONLY_VAR", "host-value")
    session = await sandbox.create_session(_spec(env={"SPEC_VAR": SecretStr("spec-value")}))
    try:
        result = await session.exec(["printenv"], timeout_seconds=30)
        assert "HOST_ONLY_VAR" not in result.stdout, "the child inherited a host env var"
        assert "spec-value" in result.stdout, "spec.env was not the child's base env"
    finally:
        await session.destroy()


async def test_exec_feeds_and_half_closes_stdin(sandbox: LocalSandbox) -> None:
    session = await sandbox.create_session(_spec())
    try:
        result = await session.exec(["cat"], stdin=b"piped-in", timeout_seconds=30)
        assert result.exit_code == 0
        assert result.stdout == "piped-in"
    finally:
        await session.destroy()


async def test_unset_cwd_defaults_to_workspace(sandbox: LocalSandbox) -> None:
    session = await sandbox.create_session(_spec())
    try:
        result = await session.exec(["pwd"], timeout_seconds=30)
        assert result.stdout.strip() == session.workspace_path
    finally:
        await session.destroy()


async def test_relative_cwd_resolves_within_workspace(sandbox: LocalSandbox) -> None:
    session = await sandbox.create_session(_spec())
    try:
        await session.put_file("sub/marker.txt", b"x")
        result = await session.exec(["pwd"], cwd="sub", timeout_seconds=30)
        assert result.stdout.strip().startswith(session.workspace_path)
        assert result.stdout.strip() == os.path.join(session.workspace_path, "sub")
    finally:
        await session.destroy()


async def test_relative_cwd_escaping_workspace_raises(sandbox: LocalSandbox) -> None:
    session = await sandbox.create_session(_spec())
    try:
        with pytest.raises(SandboxError):
            await session.exec(["pwd"], cwd="../..", timeout_seconds=30)
    finally:
        await session.destroy()


async def test_absolute_cwd_runs_as_given(sandbox: LocalSandbox, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    session = await sandbox.create_session(_spec())
    try:
        # isolation="none" -> the subprocess already has full host reach, so an
        # absolute cwd is allowed as given (no containment gain claimed).
        result = await session.exec(["pwd"], cwd=str(outside), timeout_seconds=30)
        assert result.exit_code == 0
        assert result.stdout.strip() == os.path.realpath(str(outside))
    finally:
        await session.destroy()


# -- durability -> host directory ------------------------------------------------


async def test_persistent_dir_survives_reap_and_adopt(sandbox: LocalSandbox, local_root: Path) -> None:
    first = await sandbox.create_session(_spec(workspace_key="dur-keep", durability="persistent"))
    await first.put_file("kept.txt", b"survives")
    persistent_dir = local_root / "dur-keep"
    assert persistent_dir.is_dir()

    _expire_and_reap(sandbox, first.id)
    reaped = await sandbox.reap()
    assert first.id in reaped
    assert persistent_dir.is_dir(), "reap removed a persistent workspace"
    assert (persistent_dir / "kept.txt").read_bytes() == b"survives"

    # A second create on the same key adopts the existing dir (no wipe, no second dir).
    second = await sandbox.create_session(_spec(workspace_key="dur-keep", durability="persistent"))
    try:
        assert await second.get_file("kept.txt") == b"survives"
    finally:
        await second.destroy()


async def test_ephemeral_dir_gone_after_reap(sandbox: LocalSandbox) -> None:
    session = await sandbox.create_session(_spec(workspace_key="dur-scratch", durability="ephemeral"))
    scratch = session.workspace_path
    assert os.path.isdir(scratch)

    _expire_and_reap(sandbox, session.id)
    await sandbox.reap()
    assert not os.path.exists(scratch), "an ephemeral workspace outlived its session"


async def test_idempotent_adopt_keeps_existing_file(sandbox: LocalSandbox, local_root: Path) -> None:
    first = await sandbox.create_session(_spec(workspace_key="adopt", durability="persistent"))
    await first.put_file("pre.txt", b"pre-existing")

    second = await sandbox.create_session(_spec(workspace_key="adopt", durability="persistent"))
    try:
        assert first.workspace_path == second.workspace_path, "adopt produced a second directory"
        assert await second.get_file("pre.txt") == b"pre-existing"
        # Exactly one directory for the key.
        assert (local_root / "adopt").is_dir()
    finally:
        await second.destroy()
        await first.destroy()


async def test_image_recorded_in_sidecar(sandbox: LocalSandbox, local_root: Path) -> None:
    import json

    session = await sandbox.create_session(
        _spec(workspace_key="img", durability="persistent", image="registry.example/app@sha256:abc")
    )
    try:
        sidecar = local_root / ".tai-sandbox" / "img.json"
        assert sidecar.is_file()
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["image"] == "registry.example/app@sha256:abc"
        assert payload["durability"] == "persistent"
    finally:
        await session.destroy()


async def test_unwritable_root_raises_naming_root(sandbox: LocalSandbox, local_root: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")
    os.chmod(local_root, 0o500)
    try:
        with pytest.raises(SandboxError) as excinfo:
            await sandbox.create_session(_spec(workspace_key="denied", durability="persistent"))
        assert str(local_root) in str(excinfo.value), "the error did not name the configured root"
    finally:
        os.chmod(local_root, 0o700)


# -- capability rejection --------------------------------------------------------


@pytest.mark.parametrize("isolation", ["container", "vm"])
async def test_isolation_above_none_rejected(sandbox: LocalSandbox, isolation: str) -> None:
    with pytest.raises(SandboxSpecRejectedError):
        await sandbox.create_session(_spec(isolation=isolation))


async def test_isolation_none_accepted(sandbox: LocalSandbox) -> None:
    session = await sandbox.create_session(_spec(isolation="none"))
    try:
        result = await session.exec(["printf", "ok"], timeout_seconds=30)
        assert result.stdout == "ok"
    finally:
        await session.destroy()


@pytest.mark.parametrize("network", ["none", "internal"])
async def test_network_below_egress_rejected(sandbox: LocalSandbox, network: str) -> None:
    with pytest.raises(SandboxSpecRejectedError):
        await sandbox.create_session(_spec(network=network))


async def test_caps_rejected(sandbox: LocalSandbox) -> None:
    with pytest.raises(SandboxSpecRejectedError):
        await sandbox.create_session(_spec(cpu=1.0))
    with pytest.raises(SandboxSpecRejectedError):
        await sandbox.create_session(_spec(memory_mb=256))


# -- secret hygiene --------------------------------------------------------------


async def test_secret_never_in_repr_or_error(sandbox: LocalSandbox) -> None:
    secret = "top-secret-value"
    session = await sandbox.create_session(_spec(env={"CREDENTIAL": SecretStr(secret)}))
    try:
        assert secret not in repr(session)

        with pytest.raises(SandboxError) as spawn_err:
            await session.exec(["/nonexistent/binary-xyz"], timeout_seconds=30)
        assert secret not in str(spawn_err.value)

        with pytest.raises(SandboxExecTimeoutError) as timeout_err:
            await session.exec(["sleep", "5"], timeout_seconds=0.2)
        assert secret not in str(timeout_err.value)
    finally:
        await session.destroy()


# -- interactive exec ------------------------------------------------------------


async def test_interactive_exec_stream_and_lifetime(sandbox: LocalSandbox) -> None:
    from tai42_contract.sandbox import SandboxStreamChunk, SandboxStreamExit

    session = await sandbox.create_session(_spec())
    try:
        handle = await session.exec_start(["cat"], timeout_seconds=30)
        await handle.write_stdin(b"echo-me\n")
        await handle.close_stdin()

        stdout = bytearray()
        exit_code: int | None = None
        async for item in handle.output:
            if isinstance(item, SandboxStreamChunk):
                if item.stream == "stdout":
                    stdout.extend(item.data)
            elif isinstance(item, SandboxStreamExit):
                exit_code = item.exit_code

        assert exit_code == 0
        assert b"echo-me" in bytes(stdout)

        await handle.kill()  # idempotent no-op after exit

        with pytest.raises(SandboxError):
            await handle.write_stdin(b"after-exit")
    finally:
        await session.destroy()


async def test_exec_timeout_kills_and_reports_lengths(sandbox: LocalSandbox) -> None:
    session = await sandbox.create_session(_spec())
    try:
        with pytest.raises(SandboxExecTimeoutError) as excinfo:
            await session.exec(["sleep", "5"], timeout_seconds=0.2)
        err = excinfo.value
        assert isinstance(err.stdout_len, int)
        assert isinstance(err.stderr_len, int)
    finally:
        await session.destroy()


# -- file transfer ---------------------------------------------------------------


async def test_file_transfer_round_trip_and_containment(sandbox: LocalSandbox) -> None:
    session = await sandbox.create_session(_spec())
    try:
        await session.put_file("dir/note.txt", b"contents")
        assert await session.get_file("dir/note.txt") == b"contents"

        with pytest.raises(SandboxError):
            await session.get_file("absent.txt")

        with pytest.raises(SandboxError):
            await session.put_file("../escape.txt", b"nope")

        with pytest.raises(SandboxError):
            await session.get_file("../../etc/passwd")
    finally:
        await session.destroy()


# -- teardown coherence ----------------------------------------------------------


async def test_destroy_removes_persistent_and_is_idempotent(sandbox: LocalSandbox, local_root: Path) -> None:
    session = await sandbox.create_session(_spec(workspace_key="teardown", durability="persistent"))
    persistent_dir = local_root / "teardown"
    sidecar = local_root / ".tai-sandbox" / "teardown.json"
    assert persistent_dir.is_dir()
    assert sidecar.is_file()

    await sandbox.destroy_session(session.id)
    assert not persistent_dir.exists(), "destroy did not remove the persistent workspace"
    assert not sidecar.exists(), "destroy did not remove the sidecar"

    # Idempotent on an already-gone session/directory.
    await sandbox.destroy_session(session.id)


# -- orphan recovery -------------------------------------------------------------


async def test_orphan_recovery_keeps_persistent_and_destroys_scratch(sandbox: LocalSandbox, local_root: Path) -> None:
    import json

    # A persistent workspace + sidecar the live ledger does not know.
    orphan_ws = local_root / "orphaned"
    orphan_ws.mkdir()
    (orphan_ws / "data.txt").write_bytes(b"durable")
    sidecar_dir = local_root / ".tai-sandbox"
    sidecar_dir.mkdir()
    (sidecar_dir / "orphaned.json").write_text(json.dumps({"workspace_key": "orphaned"}), encoding="utf-8")

    # A leftover ephemeral scratch dir from a crashed process.
    ephemeral_parent = local_root / ".ephemeral"
    ephemeral_parent.mkdir()
    scratch = ephemeral_parent / "crashed-scratch"
    scratch.mkdir()

    descriptors = await sandbox.recover_orphans()

    assert orphan_ws.is_dir(), "a persistent orphan workspace was not left in place"
    assert not scratch.exists(), "a leftover ephemeral scratch dir was not destroyed"
    assert any("orphaned" in d for d in descriptors)
    assert any("crashed-scratch" in d for d in descriptors)


async def test_orphan_scan_skips_control_dirs(sandbox: LocalSandbox, local_root: Path) -> None:
    (local_root / ".ephemeral").mkdir()
    (local_root / ".tai-sandbox").mkdir()

    descriptors = await sandbox.recover_orphans()

    # Neither control dir is ever reported as an orphan workspace.
    assert not any(".ephemeral" in d and "persistent workspace" in d for d in descriptors)
    assert not any(".tai-sandbox" in d for d in descriptors)


async def test_orphan_scan_returns_empty_when_root_absent(sandbox: LocalSandbox, tmp_path: Path) -> None:
    """A root that is not a directory (never created / recycled away) yields no
    descriptors rather than raising — the scan is a best-effort sweep."""
    from tai42_kit.settings import reset_all_settings

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SANDBOX_LOCAL_ROOT", str(tmp_path / "never-created"))
        reset_all_settings()
        try:
            assert await sandbox.recover_orphans() == []
        finally:
            reset_all_settings()


async def test_orphan_scan_skips_non_dir_and_non_workspace_entries(sandbox: LocalSandbox, local_root: Path) -> None:
    """A plain file in the root, a name outside the workspace-key charset, and a name
    the live ledger already knows are all skipped — never reported as an orphan."""
    # A regular file directly in the root (not a directory).
    (local_root / "stray-file").write_bytes(b"x")
    # A directory whose name cannot be a workspace key (a space is outside the charset).
    (local_root / "not a key").mkdir()
    # A live persistent session, so its key is a KNOWN entry the scan must skip.
    live = await sandbox.create_session(_spec(workspace_key="live-known", durability="persistent"))
    try:
        descriptors = await sandbox.recover_orphans()
        assert not any("stray-file" in d for d in descriptors)
        assert not any("not a key" in d for d in descriptors)
        assert not any("live-known" in d for d in descriptors), "a live-known workspace was reported as orphan"
    finally:
        await live.destroy()


# -- provider host-error branches ------------------------------------------------


async def test_ephemeral_mkdtemp_failure_names_root(sandbox: LocalSandbox, local_root: Path) -> None:
    """An unwritable ephemeral parent makes ``mkdtemp`` fail, surfacing a typed error
    NAMING the configured root rather than a silent scratch downgrade."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")
    ephemeral_parent = local_root / ".ephemeral"
    ephemeral_parent.mkdir()
    os.chmod(ephemeral_parent, 0o500)
    try:
        with pytest.raises(SandboxError) as excinfo:
            await sandbox.create_session(_spec(workspace_key="eph-denied", durability="ephemeral"))
        assert str(local_root) in str(excinfo.value), "the error did not name the configured root"
    finally:
        os.chmod(ephemeral_parent, 0o700)


async def test_sidecar_write_failure_names_root(sandbox: LocalSandbox, local_root: Path) -> None:
    """An unwritable sidecar dir makes the sidecar open fail, surfacing a typed error
    NAMING the configured root."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")
    sidecar_dir = local_root / ".tai-sandbox"
    sidecar_dir.mkdir()
    os.chmod(sidecar_dir, 0o500)
    try:
        with pytest.raises(SandboxError) as excinfo:
            await sandbox.create_session(_spec(workspace_key="sc-denied", durability="persistent"))
        assert str(local_root) in str(excinfo.value), "the error did not name the configured root"
    finally:
        os.chmod(sidecar_dir, 0o700)


def test_remove_tree_non_miss_oserror_surfaces_typed(sandbox: LocalSandbox, tmp_path: Path) -> None:
    """A genuine (non-``FileNotFound``) rmtree failure raises a typed error NAMING the
    path; ``rmtree`` on a plain file raises ``NotADirectoryError``."""
    victim = tmp_path / "a-file"
    victim.write_bytes(b"x")
    with pytest.raises(SandboxError, match="failed to remove"):
        sandbox._remove_tree(str(victim))


def test_remove_file_non_miss_oserror_surfaces_typed(sandbox: LocalSandbox, tmp_path: Path) -> None:
    """A genuine (non-``FileNotFound``) remove failure raises a typed error NAMING the
    path; ``os.remove`` on a directory raises ``IsADirectoryError``."""
    victim = tmp_path / "a-dir"
    victim.mkdir()
    with pytest.raises(SandboxError, match="failed to remove"):
        sandbox._remove_file(str(victim))


# -- absolute-path containment pins (direct-host tightening) ---------------------


async def test_absolute_path_under_workspace_round_trips(sandbox: LocalSandbox) -> None:
    """An ABSOLUTE path built from ``workspace_path`` is accepted and round-trips
    through ``put_file`` / ``get_file`` — the contract's every-provider guarantee."""
    session = await sandbox.create_session(_spec())
    try:
        abs_under = os.path.join(session.workspace_path, "nested", "note.txt")
        await session.put_file(abs_under, b"contained")
        assert await session.get_file(abs_under) == b"contained"
    finally:
        await session.destroy()


async def test_absolute_path_outside_workspace_refused(sandbox: LocalSandbox, tmp_path: Path) -> None:
    """An ABSOLUTE path resolving OUTSIDE the workspace is refused LOUDLY on both
    transfers — on this host provider it would be a real host read/write (the permitted
    direct-host containment tightening)."""
    session = await sandbox.create_session(_spec())
    try:
        outside = str(tmp_path / "outside.txt")
        with pytest.raises(SandboxError, match="escapes the sandbox workspace"):
            await session.put_file(outside, b"nope")
        with pytest.raises(SandboxError, match="escapes the sandbox workspace"):
            await session.get_file("/etc/passwd")
    finally:
        await session.destroy()
