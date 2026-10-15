"""A checkpoint store registered by a plugin module, selected by the deployment, end to end.

The stack lists the fixture module that registers the ``probe`` store under ``lifecycle_modules``
and sets ``LLM_PROVIDER_CHECKPOINT=probe``: the SUT's own sweep builds that store and reports it,
with its waiting horizon swept by the store's own ledger. A deployment naming a store no module
registered fails at the store's first use, naming the stores that are registered. The kit's
contract tests prove the features a registration gets; this spec proves the manifest door.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator

import pytest

from tai42_e2e import Infra
from tai42_e2e.booting import boot_stack
from tai42_e2e.stack import TaiStack

from ._checkpoint_support import build_registered_store_stack  # pyright: ignore[reportMissingImports]

pytestmark = pytest.mark.backendless


@pytest.fixture(scope="module")
def registered_store_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    builder = functools.partial(build_registered_store_stack, provider="probe")
    yield from boot_stack(infra, tmp_path_factory.mktemp("checkpoint-registered"), builder)


@pytest.fixture(scope="module")
def unregistered_store_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    builder = functools.partial(build_registered_store_stack, provider="nope")
    yield from boot_stack(infra, tmp_path_factory.mktemp("checkpoint-unregistered"), builder)


@pytest.mark.needs("setting:LLM_PROVIDER_CHECKPOINT=probe", "setting:lifecycle_modules")
async def test_the_store_a_plugin_registers_is_the_one_the_sweep_runs_on(registered_store_stack: TaiStack) -> None:
    result = await registered_store_stack.api().post("/api/checkpoints/sweep", json={})
    assert result["provider"] == "probe", result
    assert result["skipped"] is None, result
    assert result["waiting_swept"] == [], result
    assert result["finished_swept"] == [], result


@pytest.mark.needs("setting:LLM_PROVIDER_CHECKPOINT=nope", "files")
async def test_a_store_no_module_registered_fails_at_first_use_naming_the_registered(
    unregistered_store_stack: TaiStack,
) -> None:
    response = await unregistered_store_stack.api().request_raw("POST", "/api/checkpoints/sweep", json={})
    assert response.status_code == 500, response.text
    serve_log = unregistered_store_stack.process("serve").log_path.read_text(encoding="utf-8", errors="replace")
    refusals = [
        line
        for line in serve_log.splitlines()
        if "UnknownCheckpointProviderError: Unsupported checkpoint provider: 'nope'" in line
    ]
    assert refusals, serve_log[-2000:]
    known = refusals[-1].split("known: ", 1)[1].split(", ")
    assert "probe" in known
    assert "redis" in known
