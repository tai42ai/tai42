"""Stack allocate-build-boot orchestration shared by the pytest fixtures.

The profile fixtures (in ``tests/conftest.py`` and the per-suite conftests) call
:func:`boot_stack` to turn a manifest builder into a live, isolated
:class:`~tai42_e2e.stack.TaiStack` for the duration of a fixture scope. Keeping the
orchestration in the harness library — rather than a conftest-local helper — lets
a per-suite conftest reuse it without importing another conftest module (two
conftests share the same basename, so a cross-conftest import is ambiguous)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from tai42_e2e import diagnostics
from tai42_e2e.harness import allocate_resources, release_resources, seed_bootstrap_key
from tai42_e2e.stack import Infra, StackConfig, StackResources, TaiStack
from tai42_e2e.variants import Variants


def allocate_and_build(
    infra: Infra,
    root: Path,
    builder: Callable[[StackResources, Variants], StackConfig],
    resource_kwargs: Mapping[str, Any] | None,
    allocate_checkpoint_db: bool,
) -> tuple[StackResources, StackConfig]:
    """Allocate a stack's resources and render its config. A builder that raises
    (a profile whose prerequisite is missing) reaps the allocation instead of
    leaking the Redis index, the Postgres clone and the broker lease for the rest
    of the session."""
    resources = allocate_resources(
        infra, root, allocate_checkpoint_db=allocate_checkpoint_db, **(resource_kwargs or {})
    )
    try:
        return resources, builder(resources, infra.variants)
    except BaseException:
        release_resources(infra, resources)
        raise


def boot_stack(
    infra: Infra,
    root: Path,
    builder: Callable[[StackResources, Variants], StackConfig],
    *,
    resource_kwargs: Mapping[str, Any] | None = None,
    seed_auth: bool = False,
    allocate_checkpoint_db: bool = False,
) -> Iterator[TaiStack]:
    """Allocate, build, boot, and yield a stack; tear it down at scope end. A
    generator so a fixture ``yield from`` s it."""
    resources, config = allocate_and_build(infra, root, builder, resource_kwargs, allocate_checkpoint_db)
    # Seed the auth bootstrap (Redis identity/context + PG policy/route rows)
    # BEFORE the stack boots: readiness probes /health and /metrics, which are
    # denied (403) under access control until the route table pins them public,
    # so the seed has to land before the processes answer their first request.
    stack = TaiStack(config, infra, resources, root)
    try:
        # The auth seed runs before boot (readiness probes are denied until the
        # route table pins them public), so a seed failure would otherwise leak
        # the resources allocated above; reap them on failure.
        auth_token = seed_bootstrap_key(infra, resources) if seed_auth else None
    except BaseException:
        stack.teardown()
        raise
    # Set the token BEFORE boot: readiness drains the boot reload gate through the
    # MCP probe, which an access-controlled stack fences, so the probe must carry the
    # seeded root token that boot's own readiness runs under.
    stack.auth_token = auth_token
    with stack, diagnostics.track(stack):
        yield stack
