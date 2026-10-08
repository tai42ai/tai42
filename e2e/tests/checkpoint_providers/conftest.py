"""The checkpoint-provider fixtures (postgres / sqlite, and redis).

Every other stack pins the ``memory`` or ``redis`` checkpoint provider and marks no thread
finished, so neither DELETING horizon of ``sweep_checkpoints`` runs there. These fixtures
boot the provider stack for each DB-backed provider in turn, and one on the module-capable
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


@pytest.fixture(scope="module", params=["postgres", "sqlite"])
def checkpoint_stack(
    request: pytest.FixtureRequest, infra: Infra, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[TaiStack, str]]:
    """Boot the checkpoint-provider stack for each DB-backed provider in turn, yielding
    ``(stack, provider)``. The provider rides through as the fixture param so one
    spec covers both providers and the single deleting sweep branch they share."""
    provider = str(request.param)
    builder = functools.partial(build_checkpoint_stack, provider=provider)
    # boot_stack drives the stack's context-manager teardown when the for-loop resumes
    # at fixture finalization, so the isolated DB clone / sqlite file are reaped.
    for stack in boot_stack(infra, tmp_path_factory.mktemp(f"checkpoint-{provider}"), builder):
        yield stack, provider


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
