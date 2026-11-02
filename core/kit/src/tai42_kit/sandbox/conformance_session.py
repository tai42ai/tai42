"""The session-lifecycle checks of the sandbox conformance suite.

Each check drives a live :class:`~tai42_kit.sandbox.base.ManagedSandbox` through one part of a
session's life every consumer relies on: file transfer, workspace-path resolution, the
consumer-label round-trip, ``touch``'s keep-alive, a persistent workspace surviving its session's
reap and an ephemeral one dying with it, the TTL reap and an idempotent ``destroy_session``, and
the loud rejection of a spec the provider cannot honor.
"""

from __future__ import annotations

from tai42_contract.sandbox import SandboxError, SandboxSessionNotFoundError, SandboxSpecRejectedError

from tai42_kit.sandbox.base import ManagedSandbox
from tai42_kit.sandbox.conformance_harness import SandboxConformanceConfig, conformance_spec


async def check_file_transfer(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """``put_file`` / ``get_file`` round-trip a workspace-relative path.

    A miss raises a typed :class:`SandboxError`.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        await session.put_file("note.txt", b"conf-bytes")
        if await session.get_file("note.txt") != b"conf-bytes":
            raise AssertionError

        try:
            await session.get_file("absent.txt")
        except SandboxError:
            pass
        else:
            raise AssertionError("get_file on a miss did not raise a typed SandboxError")
    finally:
        await session.destroy()


async def check_workspace_path(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """``session.workspace_path`` equals ``info().workspace_path``.

    A relative ``cwd`` resolves UNDER the workspace root.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        info = await session.info()
        if session.workspace_path != info.workspace_path:
            raise AssertionError("workspace_path did not round-trip through info()")

        default_cwd = await session.exec(["pwd"], timeout_seconds=30)
        if default_cwd.stdout.strip() != session.workspace_path:
            raise AssertionError("unset cwd did not default to workspace_path")

        await session.put_file("sub/nested.txt", b"nested")
        nested_cwd = await session.exec(["pwd"], cwd="sub", timeout_seconds=30)
        if not (nested_cwd.stdout.strip().startswith(session.workspace_path)):
            raise AssertionError("a relative cwd did not resolve under workspace_path")
    finally:
        await session.destroy()


async def check_labels_round_trip(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """A consumer's labels round-trip exactly through ``info().labels``.

    The reserved ``tai42.sandbox`` markers stay on the runtime resource and never leak back to
    the consumer, and the requested ``image`` is surfaced on ``info()``.
    """
    session = await sandbox.create_session(conformance_spec(config, labels={"team": "conf"}))
    try:
        info = await session.info()
        if info.labels != {"team": "conf"}:
            raise AssertionError("consumer labels did not round-trip through info()")
        if info.image != config.image:
            raise AssertionError("the requested image was not surfaced on info()")
    finally:
        await session.destroy()


async def check_touch_extends(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """``touch`` extends ``expires_at`` — the keep-alive turn."""
    session = await sandbox.create_session(conformance_spec(config))
    try:
        before = (await session.info()).expires_at
        await session.touch()
        after = (await session.info()).expires_at
        if after < before:
            raise AssertionError("touch did not extend expires_at")
    finally:
        await session.destroy()


async def check_persistent_survives_reap(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """A persistent workspace survives its session's reap; an ephemeral one dies with it."""
    persistent_key = "conf-persist"
    first = await sandbox.create_session(
        conformance_spec(config, workspace_key=persistent_key, durability="persistent")
    )
    await first.put_file("kept.txt", b"survives")
    await _expire_and_reap(sandbox, first.id)

    second = await sandbox.create_session(
        conformance_spec(config, workspace_key=persistent_key, durability="persistent")
    )
    try:
        if await second.get_file("kept.txt") != b"survives":
            raise AssertionError("a persistent workspace did not survive reap")
    finally:
        await second.destroy()

    ephemeral_key = "conf-ephemeral"
    scratch = await sandbox.create_session(
        conformance_spec(config, workspace_key=ephemeral_key, durability="ephemeral")
    )
    await scratch.put_file("gone.txt", b"scratch")
    await _expire_and_reap(sandbox, scratch.id)

    replacement = await sandbox.create_session(
        conformance_spec(config, workspace_key=ephemeral_key, durability="ephemeral")
    )
    try:
        await replacement.get_file("gone.txt")
    except SandboxError:
        pass
    else:
        raise AssertionError("an ephemeral workspace outlived its session")
    finally:
        await replacement.destroy()


async def check_reap_and_destroy(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """A session past its ttl is reaped and gone; ``destroy_session`` is idempotent on an already-gone session."""
    session = await sandbox.create_session(conformance_spec(config, workspace_key="conf-reap"))
    reaped = await _expire_and_reap(sandbox, session.id)
    if session.id not in reaped:
        raise AssertionError("an expired session was not reaped")

    try:
        await sandbox.get_session(session.id)
    except SandboxSessionNotFoundError:
        pass
    else:
        raise AssertionError("a reaped session was still resolvable")

    await sandbox.destroy_session(session.id)  # idempotent on already-gone


async def check_spec_rejection(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """Every provider-declared reject spec is refused loudly at create."""
    for spec in config.reject_specs:
        try:
            await sandbox.create_session(spec)
        except SandboxSpecRejectedError:
            continue
        raise AssertionError(f"a spec the provider cannot honor was not rejected: {spec.workspace_key!r}")


async def _expire_and_reap(sandbox: ManagedSandbox, session_id: str) -> list[str]:
    """Force ``session_id`` past its deadline and reap.

    Drives the kit's own ledger ``expires_at`` back (uniform across every ``ManagedSandbox``
    provider) so the suite need not wait out a wall-clock ttl.
    """
    record = sandbox._ledger[session_id]
    record.expires_at = record.created_at
    return await sandbox.reap()
