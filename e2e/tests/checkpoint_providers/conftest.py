"""The checkpoint-provider fixtures (postgres / sqlite, and redis).

Every other stack pins the ``memory`` or ``redis`` checkpoint provider and marks no thread
finished, so neither DELETING horizon of ``sweep_checkpoints`` runs there. These fixtures
boot the provider stack for each DB-backed provider in turn on its own shared-value default, the
``postgres`` one once more with the shared values switched on, and one on the module-capable
checkpoint Redis (the builder + conn-string helper live in ``_checkpoint_support``)."""

from __future__ import annotations

import functools
from collections.abc import Iterator

import pytest

from tai42_e2e import Infra
from tai42_e2e.booting import boot_stack
from tai42_e2e.stack import TaiStack

from ._checkpoint_support import build_checkpoint_stack  # pyright: ignore[reportMissingImports]

# No backend worker: the sweep runs in the serve worker handling the HTTP request.
pytestmark = pytest.mark.backendless


@pytest.fixture(
    scope="module",
    params=[("postgres", None), ("postgres", True), ("sqlite", None)],
    ids=["postgres", "postgres-shared", "sqlite"],
)
def checkpoint_stack(
    request: pytest.FixtureRequest, infra: Infra, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[TaiStack, str, bool | None]]:
    """Boot the checkpoint-provider stack for each DB-backed provider in turn, yielding
    ``(stack, provider, shared_values)``. The provider and the shared-values switch (``None``:
    the provider's default) ride through as the fixture param so one spec covers every
    configuration and the single deleting sweep branch they share."""
    provider, shared_values = request.param
    builder = functools.partial(build_checkpoint_stack, provider=provider, shared_values=shared_values)
    name = f"checkpoint-{provider}" + ("" if shared_values is None else "-shared")
    # boot_stack drives the stack's context-manager teardown when the for-loop resumes
    # at fixture finalization, so the isolated DB clone / sqlite file are reaped.
    for stack in boot_stack(infra, tmp_path_factory.mktemp(name), builder):
        yield stack, provider, shared_values


@pytest.fixture(scope="module")
def redis_checkpoint_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """The checkpoint-provider stack on the ``redis`` provider, this stack's logical DB on the
    module-capable checkpoint Redis. Skips when ``TAI_E2E_CHECKPOINT_REDIS_URL`` is unset."""
    if infra.checkpoint_redis is None:
        pytest.skip(
            "checkpoint Redis not configured; set TAI_E2E_CHECKPOINT_REDIS_URL and start "
            "`docker compose --profile agents-redis up -d`"
        )
    builder = functools.partial(build_checkpoint_stack, provider="redis")
    yield from boot_stack(infra, tmp_path_factory.mktemp("checkpoint-redis"), builder, allocate_checkpoint_db=True)
