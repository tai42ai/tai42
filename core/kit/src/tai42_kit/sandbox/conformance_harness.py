"""The conformance harness: what a provider tells the suite, the policy it binds, the specs and stops it drives.

Two harness rules the model's neutral defaults would otherwise trip: every spec
pins ``network="egress"`` EXPLICITLY (the model default ``"none"`` is the one a
direct-host provider rejects, so a defaulted spec would red every create), and
the suite binds a permissive :class:`SandboxPolicy` before driving
``create_session`` (an unbound policy is a loud programming error). The
spec-reject case is PARAMETERIZED per provider — what one provider cannot honor
(an unenforceable cap, an unsupported durability tier) differs from the next.
A case whose command starts a process that leaves its process group stops it
with :func:`stop_descendant`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import SecretStr
from tai42_contract.sandbox import SandboxPolicy, SandboxSessionSpec

from tai42_kit.sandbox.session import ManagedSandboxSession

# The bound of the one-shot exec that stops a descendant a case started.
_DESCENDANT_STOP_TIMEOUT_SECONDS = 10.0


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
    """The most permissive policy: egress ceiling wide open, no isolation floor, persistent allowed.

    The suite binds it so only a PROVIDER inability — never the policy chokepoint — can reject
    a conformance spec.
    """
    return SandboxPolicy(egress="egress", isolation="none", scrub_transcript=False, durable=True)


def conformance_spec(
    config: SandboxConformanceConfig,
    *,
    workspace_key: str = "conf-ws",
    durability: str = "ephemeral",
    ttl_seconds: int = 300,
    env: dict[str, SecretStr] | None = None,
    labels: dict[str, str] | None = None,
) -> SandboxSessionSpec:
    """A conformance spec with ``network`` pinned to ``egress``.

    Never the model default ``none`` a direct-host provider rejects.
    """
    return SandboxSessionSpec(
        image=config.image,
        workspace_key=workspace_key,
        durability=durability,  # pyright: ignore[reportArgumentType]
        network="egress",
        ttl_seconds=ttl_seconds,
        env=env or {},
        labels=labels or {},
    )


async def stop_descendant(session: ManagedSandboxSession, argv: Sequence[str], case: str) -> None:
    """Stop, through the one-shot exec ``argv``, the descendant the ``case`` case started; a failed stop fails it.

    A case whose command starts a process that leaves its process group stops that process
    itself, since a provider whose execs run as host processes leaves it running past the session.
    """
    result = await session.exec(list(argv), timeout_seconds=_DESCENDANT_STOP_TIMEOUT_SECONDS)
    if result.exit_code != 0:
        raise AssertionError(
            f"the {case} case could not stop the descendant it started"
            f" (exit {result.exit_code}): {result.stderr.strip()}"
        )
