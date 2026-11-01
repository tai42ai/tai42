"""The deployment block a host-side ``tai plugins`` command runs in: the boot read, then the
process app bound onto ``tai42_app`` for the block, as a server binds it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import click
import pytest
from tai42_contract.app import tai42_app

from tai42_skeleton.app import instance
from tai42_skeleton.cli.native.host_boot import deployment


async def test_the_deployment_block_reads_the_boot_manifest_and_binds_the_process_app_for_the_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    read = object()

    class _Lifecycle:
        async def read_boot_manifest(self) -> Any:
            events.append("boot:read")
            return read

    process_app = SimpleNamespace(lifecycle=_Lifecycle())
    monkeypatch.setattr(instance, "app", process_app, raising=False)
    outer = SimpleNamespace(lifecycle=object())

    with tai42_app.bound(outer):
        async with deployment() as manifest:
            assert manifest is read
            assert tai42_app.lifecycle is process_app.lifecycle
        assert tai42_app.lifecycle is outer.lifecycle
    assert events == ["boot:read"]


async def test_the_deployment_block_names_an_absent_config_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def _absent_provider() -> Any:
        raise ImportError("config mode 'vault' needs the 'tai42_config_vault' provider (install tai42-config-vault)")

    monkeypatch.delattr(instance, "app", raising=False)
    monkeypatch.setattr(instance, "build_app", _absent_provider)

    with pytest.raises(click.ClickException, match="needs the 'tai42_config_vault' provider"):
        async with deployment():
            pytest.fail("the block ran without a process app")
