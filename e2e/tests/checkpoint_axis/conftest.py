"""B5 — the checkpoint-provider axis fixture (postgres / sqlite).

Every other stack pins the ``memory`` or ``redis`` checkpoint provider, so the
DELETING branch of ``sweep_checkpoints`` — reached only for ``{postgres, sqlite}``
with a TTL set (``operations/checkpoints.py``) — never runs. This fixture
boots the axis stack for each DB-backed provider in turn (the builder + conn-string
helper live in ``_checkpoint_support``)."""

from __future__ import annotations

import functools
from collections.abc import Iterator

import pytest

from tai42_e2e.booting import boot_stack
from tai42_e2e.stack import Infra, TaiStack

from ._checkpoint_support import build_checkpoint_stack  # pyright: ignore[reportMissingImports]

# No backend worker: the sweep runs in the serve worker handling the HTTP request.
pytestmark = pytest.mark.backendless


@pytest.fixture(scope="module", params=["postgres", "sqlite"])
def checkpoint_stack(
    request: pytest.FixtureRequest, infra: Infra, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[TaiStack, str]]:
    """Boot the checkpoint-axis stack for each DB-backed provider in turn, yielding
    ``(stack, provider)``. The provider rides through as the fixture param so one
    spec covers both providers and the single deleting sweep branch they share."""
    provider = str(request.param)
    builder = functools.partial(build_checkpoint_stack, provider=provider)
    # boot_stack drives the stack's context-manager teardown when the for-loop resumes
    # at fixture finalization, so the isolated DB clone / sqlite file are reaped.
    for stack in boot_stack(infra, tmp_path_factory.mktemp(f"checkpoint-{provider}"), builder):
        yield stack, provider
