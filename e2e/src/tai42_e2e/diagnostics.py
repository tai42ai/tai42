"""Failure diagnostics: on a failed test that used a stack, attach the tail of
each process log, the rendered manifest, the masked env map, and the port table
so the failure is actionable from a CI page without a re-run."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_e2e.stack import TaiStack

# Stacks currently live within a running test, tracked so the failure hook can
# find them without threading the fixture through every assertion.
_active: list[TaiStack] = []

_SECRET_HINTS = ("secret", "password", "kek", "hmac", "api_key", "token")


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


def _stack_report(stack: TaiStack) -> str:
    lines = [f"=== stack {stack.config.name!r} ({stack.config.topology.value}) ==="]
    lines.append(f"app_ports={stack.app_ports} metrics_port={stack.metrics_port} metrics_dir={stack.metrics_dir}")
    lines.append("env:")
    for key, value in sorted(stack.config.env.items()):
        lines.append(f"  {key}={_mask(key, value)}")
    lines.append("manifest:")
    lines.append("  " + repr(stack.config.manifest))
    for name, handle in stack._procs.items():
        lines.append(f"--- {name} log tail ---")
        lines.append(handle.log_tail())
    return "\n".join(lines)


def report() -> str:
    """A diagnostic dump for every currently-active stack."""
    if not _active:
        return ""
    return "\n\n".join(_stack_report(stack) for stack in _active)
