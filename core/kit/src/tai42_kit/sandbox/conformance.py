"""An importable conformance suite any sandbox provider runs against a live
instance.

The bar a provider must clear is "map every neutral field onto your runtime, or
reject what you cannot honor". That bar is only real if it is executable, so it
lives here as a driveable suite rather than a review checklist: a provider's
tests build a live :class:`~tai42_kit.sandbox.base.ManagedSandbox`, hand it to
:func:`run_sandbox_conformance`, and get the same behaviour every consumer relies
on — session lifecycle, the ``spec.env`` credential channel, the interactive
byte-stream seam, workspace-path resolution, consumer-label round-trip, TTL reap,
and loud spec rejection.

Two harness rules the model's neutral defaults would otherwise trip: every spec
pins ``network="egress"`` EXPLICITLY (the model default ``"none"`` is the one a
direct-host provider rejects, so a defaulted spec would red every create), and
the suite binds a permissive :class:`SandboxPolicy` before driving
``create_session`` (an unbound policy is a loud programming error). The
spec-reject case is PARAMETERIZED per provider — what one provider cannot honor
(an unenforceable cap, an unsupported durability tier) differs from the next.

pytest-free and dependency-free, so it ships in the wheel and any test runner can
call it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import SecretStr
from tai42_contract.sandbox import (
    SandboxError,
    SandboxExecTimeoutError,
    SandboxPolicy,
    SandboxSessionNotFoundError,
    SandboxSessionSpec,
    SandboxSpecRejectedError,
    SandboxStreamChunk,
    SandboxStreamExit,
)

from tai42_kit.sandbox.base import ManagedSandbox


@dataclass(frozen=True)
class SandboxConformanceConfig:
    """What a provider tells the suite about its own runtime.

    ``image`` is any reference the provider accepts. ``reject_specs`` are the
    specs THIS provider must reject loudly (an unenforceable cap, an unsupported
    durability tier) — parameterized because it differs per provider.
    ``check_persistent_survives`` runs the persistent-workspace survival case; a
    single-tier provider sets it False and lists a persistent spec in
    ``reject_specs`` instead.
    """

    image: str
    reject_specs: Sequence[SandboxSessionSpec] = field(default_factory=tuple)
    check_persistent_survives: bool = True


def permissive_policy() -> SandboxPolicy:
    """The most permissive policy: the egress ceiling wide open, no isolation
    floor, persistent allowed. The suite binds it so only a PROVIDER inability —
    never the policy chokepoint — can reject a conformance spec."""
    return SandboxPolicy(egress="egress", isolation="none", scrub_transcript=False, durable=True)


def _spec(
    config: SandboxConformanceConfig,
    *,
    workspace_key: str = "conf-ws",
    durability: str = "ephemeral",
    ttl_seconds: int = 300,
    env: dict[str, SecretStr] | None = None,
    labels: dict[str, str] | None = None,
) -> SandboxSessionSpec:
    """A conformance spec with ``network`` pinned to ``egress`` — never the model
    default ``none`` a direct-host provider rejects."""
    return SandboxSessionSpec(
        image=config.image,
        workspace_key=workspace_key,
        durability=durability,  # pyright: ignore[reportArgumentType]
        network="egress",
        ttl_seconds=ttl_seconds,
        env=env or {},
        labels=labels or {},
    )


async def check_exec_and_env(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """A session runs ``exec`` to completion, and a ``spec.env`` variable is
    visible as the base env of that exec — pinning ``spec.env`` as every exec's
    credential channel."""
    session = await sandbox.create_session(_spec(config, env={"CONF_SECRET": SecretStr("conf-value")}))
    try:
        result = await session.exec(["printenv", "CONF_SECRET"], timeout_seconds=30)
        assert result.exit_code == 0, f"printenv exited {result.exit_code}"
        assert "conf-value" in result.stdout, "spec.env value was not the exec's base env"
    finally:
        await session.destroy()


async def check_interactive_exec(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """The interactive seam: written stdin is echoed on the output stream, the
    stream ends with one exit item, ``kill`` after exit is a no-op, and
    write-after-exit raises a typed :class:`SandboxError`."""
    session = await sandbox.create_session(_spec(config))
    try:
        handle = await session.exec_start(["cat"], timeout_seconds=30)
        await handle.write_stdin(b"conf-ping\n")
        await handle.close_stdin()

        stdout = bytearray()
        exit_code: int | None = None
        async for item in handle.output:
            if isinstance(item, SandboxStreamChunk):
                if item.stream == "stdout":
                    stdout.extend(item.data)
            elif isinstance(item, SandboxStreamExit):
                exit_code = item.exit_code

        assert exit_code is not None, "the interactive stream carried no exit item"
        assert b"conf-ping" in bytes(stdout), "written stdin was not echoed on the output stream"

        await handle.kill()  # idempotent no-op once the exec has exited

        try:
            await handle.write_stdin(b"after-exit")
        except SandboxError:
            pass
        else:
            raise AssertionError("write_stdin after exit did not raise a typed SandboxError")
    finally:
        await session.destroy()


async def check_file_transfer(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """``put_file`` / ``get_file`` round-trip a workspace-relative path, and a miss
    raises a typed :class:`SandboxError`."""
    session = await sandbox.create_session(_spec(config))
    try:
        await session.put_file("note.txt", b"conf-bytes")
        assert await session.get_file("note.txt") == b"conf-bytes"

        try:
            await session.get_file("absent.txt")
        except SandboxError:
            pass
        else:
            raise AssertionError("get_file on a miss did not raise a typed SandboxError")
    finally:
        await session.destroy()


async def check_workspace_path(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """``session.workspace_path`` equals ``info().workspace_path``, and a relative
    ``cwd`` resolves UNDER the workspace root."""
    session = await sandbox.create_session(_spec(config))
    try:
        info = await session.info()
        assert session.workspace_path == info.workspace_path, "workspace_path did not round-trip through info()"

        default_cwd = await session.exec(["pwd"], timeout_seconds=30)
        assert default_cwd.stdout.strip() == session.workspace_path, "unset cwd did not default to workspace_path"

        await session.put_file("sub/nested.txt", b"nested")
        nested_cwd = await session.exec(["pwd"], cwd="sub", timeout_seconds=30)
        assert nested_cwd.stdout.strip().startswith(session.workspace_path), (
            "a relative cwd did not resolve under workspace_path"
        )
    finally:
        await session.destroy()


async def check_labels_round_trip(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """A consumer's labels round-trip exactly through ``info().labels`` — the
    reserved ``tai42.sandbox`` markers stay on the runtime resource and never leak
    back to the consumer — and the requested ``image`` is surfaced on ``info()``."""
    session = await sandbox.create_session(_spec(config, labels={"team": "conf"}))
    try:
        info = await session.info()
        assert info.labels == {"team": "conf"}, "consumer labels did not round-trip through info()"
        assert info.image == config.image, "the requested image was not surfaced on info()"
    finally:
        await session.destroy()


async def check_touch_extends(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """``touch`` extends ``expires_at`` — the keep-alive turn."""
    session = await sandbox.create_session(_spec(config))
    try:
        before = (await session.info()).expires_at
        await session.touch()
        after = (await session.info()).expires_at
        assert after >= before, "touch did not extend expires_at"
    finally:
        await session.destroy()


async def check_persistent_survives_reap(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """A persistent workspace survives its session's reap; an ephemeral one dies
    with it."""
    persistent_key = "conf-persist"
    first = await sandbox.create_session(_spec(config, workspace_key=persistent_key, durability="persistent"))
    await first.put_file("kept.txt", b"survives")
    await _expire_and_reap(sandbox, first.id)

    second = await sandbox.create_session(_spec(config, workspace_key=persistent_key, durability="persistent"))
    try:
        assert await second.get_file("kept.txt") == b"survives", "a persistent workspace did not survive reap"
    finally:
        await second.destroy()

    ephemeral_key = "conf-ephemeral"
    scratch = await sandbox.create_session(_spec(config, workspace_key=ephemeral_key, durability="ephemeral"))
    await scratch.put_file("gone.txt", b"scratch")
    await _expire_and_reap(sandbox, scratch.id)

    replacement = await sandbox.create_session(_spec(config, workspace_key=ephemeral_key, durability="ephemeral"))
    try:
        await replacement.get_file("gone.txt")
    except SandboxError:
        pass
    else:
        raise AssertionError("an ephemeral workspace outlived its session")
    finally:
        await replacement.destroy()


async def check_reap_and_destroy(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """A session past its ttl is reaped and gone; ``destroy_session`` is
    idempotent on an already-gone session."""
    session = await sandbox.create_session(_spec(config, workspace_key="conf-reap"))
    reaped = await _expire_and_reap(sandbox, session.id)
    assert session.id in reaped, "an expired session was not reaped"

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


async def check_exec_timeout(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """An ``exec`` past its ``timeout_seconds`` is killed and raises
    :class:`SandboxExecTimeoutError`."""
    session = await sandbox.create_session(_spec(config))
    try:
        try:
            await session.exec(["sleep", "5"], timeout_seconds=0.2)
        except SandboxExecTimeoutError:
            pass
        else:
            raise AssertionError("an exec past its timeout was not killed")
    finally:
        await session.destroy()


async def _expire_and_reap(sandbox: ManagedSandbox, session_id: str) -> list[str]:
    """Force ``session_id`` past its deadline and reap. Drives the kit's own
    ledger ``expires_at`` back (uniform across every ``ManagedSandbox`` provider)
    so the suite need not wait out a wall-clock ttl."""
    record = sandbox._ledger[session_id]
    record.expires_at = record.created_at
    return await sandbox.reap()


async def run_sandbox_conformance(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """Drive the full suite against a live ``sandbox``.

    Binds a permissive policy first (an unbound policy is a loud programming
    error), then runs every step. An empty return is the certification: this
    provider gives every consumer the same session lifecycle, credential channel,
    interactive seam, path resolution, label round-trip, TTL reap, and loud
    rejection.
    """
    sandbox.bind_policy(permissive_policy())
    await check_exec_and_env(sandbox, config)
    await check_interactive_exec(sandbox, config)
    await check_file_transfer(sandbox, config)
    await check_workspace_path(sandbox, config)
    await check_labels_round_trip(sandbox, config)
    await check_touch_extends(sandbox, config)
    if config.check_persistent_survives:
        await check_persistent_survives_reap(sandbox, config)
    await check_reap_and_destroy(sandbox, config)
    await check_spec_rejection(sandbox, config)
    await check_exec_timeout(sandbox, config)
