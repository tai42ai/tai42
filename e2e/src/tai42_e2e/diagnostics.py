"""Failure diagnostics: on a failed test that used a stack, attach the tail of
each process log, the rendered manifest, the masked deployment and stored env maps,
and the port table so the failure is actionable from a CI page without a re-run."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_e2e.procs import ProcessHandle
    from tai42_e2e.stack import TaiStack

# Stacks currently live within a running test, tracked so the failure hook can
# find them without threading the fixture through every assertion.
_active: list[TaiStack] = []

# The report of each boot that failed, taken while its logs are there, until a test report collects it.
_failed_boots: list[str] = []

_SECRET_HINTS = ("secret", "password", "kek", "hmac", "api_key", "token", "private_key")


def register(stack: TaiStack) -> None:
    """Mark ``stack`` active so the failure hook can dump it."""
    _active.append(stack)


def unregister(stack: TaiStack) -> None:
    """Drop ``stack`` from the active set (idempotent)."""
    with contextlib.suppress(ValueError):
        _active.remove(stack)


@contextlib.contextmanager
def track(stack: TaiStack) -> Iterator[TaiStack]:
    """Register ``stack`` as active for the duration of a test."""
    register(stack)
    try:
        yield stack
    finally:
        unregister(stack)


def _mask(key: str, value: str) -> str:
    lowered = key.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        return "***"
    return value


def _log_tails(procs: Mapping[str, ProcessHandle]) -> list[str]:
    lines: list[str] = []
    for name, handle in procs.items():
        lines.append(f"--- {name} log tail ---")
        lines.append(handle.log_tail())
    return lines


def stack_report(stack: TaiStack) -> str:
    """``stack``'s coordinates, masked env maps, manifest and process log tails."""
    lines = [f"=== stack {stack.config.name!r} ({stack.config.topology.value}) ==="]
    lines.append(f"app_ports={stack.app_ports} metrics_port={stack.metrics_port} metrics_dir={stack.metrics_dir}")
    lines.append("deployment env:")
    for key, value in sorted(stack.config.env.items()):
        lines.append(f"  {key}={_mask(key, value)}")
    for process, env in sorted(stack.config.process_env.items()):
        lines.append(f"deployment env of {process} only:")
        for key, value in sorted(env.items()):
            lines.append(f"  {key}={_mask(key, value)}")
    lines.append("stored env (seed):")
    for key, value in sorted(stack.config.stored_env.items()):
        lines.append(f"  {key}={_mask(key, value)}")
    lines.append("manifest:")
    lines.append("  " + repr(stack.config.manifest))
    lines.extend(_log_tails(stack.procs))
    return "\n".join(lines)


def service_report(name: str, port: int, procs: Mapping[str, ProcessHandle]) -> str:
    """The port and process log tails of a service that is not a stack."""
    return "\n".join([f"=== service {name!r} on port {port} ===", *_log_tails(procs)])


def record_failed_boot(report: str) -> None:
    """Keep a failed boot's ``report`` for the report of the test that booted it, whatever happens to what it
    describes next."""
    _failed_boots.append(report)


def take_failed_boots() -> str:
    """The reports of the boots that failed since the last call, which leaves none kept."""
    reports = "\n\n".join(_failed_boots)
    _failed_boots.clear()
    return reports


def report() -> str:
    """A diagnostic dump for every currently-active stack."""
    if not _active:
        return ""
    return "\n\n".join(stack_report(stack) for stack in _active)
