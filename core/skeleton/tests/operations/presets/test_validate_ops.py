"""Oracles for the dry-run validate door: create-mode and version-mode verdict
branches, the write-validator verdict, and the referees store-less 404."""

from __future__ import annotations

import asyncio

import pytest

from tai42_skeleton.app import instance
from tai42_skeleton.operations import NotFoundError
from tai42_skeleton.operations import presets as preset_ops

from .conftest import _create, _manifest

# -- validate: create-mode verdict branches ----------------------------------


def test_validate_create_invalid_name_verdict(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            data = await preset_ops.validate_preset(name="bad/name", base_tool="weather")
            assert data["valid"] is False
            assert "invalid preset name" in data["error"]

    asyncio.run(run())


def test_validate_create_quarantined_verdict(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A quarantined name whose stored record does not resolve to an active body
            # (create-mode) yields the quarantine verdict.
            instance.app.preset_manager._quarantine["qname"] = "occupied"
            data = await preset_ops.validate_preset(name="qname", base_tool="weather")
            assert data["valid"] is False
            assert "quarantined preset" in data["error"]

    asyncio.run(run())


def test_validate_create_agent_name_collision_verdict(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            monkeypatch.setattr(preset_ops, "_agent_tool_names", lambda: {"agentic"})
            data = await preset_ops.validate_preset(name="agentic", base_tool="weather")
            assert data["valid"] is False
            assert "agent tool name" in data["error"]

    asyncio.run(run())


def test_validate_create_base_is_preset_verdict(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("basep", fixed_kwargs={"units": "v"})
            data = await preset_ops.validate_preset(name="onbasep", base_tool="basep")
            assert data["valid"] is False
            assert "is itself a preset" in data["error"]

    asyncio.run(run())


def test_validate_create_authoring_error_verdict(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # An agent base baking a field the agent does not honor — the create-mode
            # authoring gate returns an invalid verdict.
            data = await preset_ops.validate_preset(
                name="newauth", base_tool="locked_agent", fixed_kwargs={"unhonored": 1}
            )
            assert data["valid"] is False
            assert data["error"]

    asyncio.run(run())


# -- validate: version-mode authoring error ----------------------------------


def test_validate_version_authoring_error(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # An authored agent whose new ``fixed_kwargs`` bakes a field the agent does
            # not honor: the version-mode validate runs the full authoring gate.
            await _create("va", base_tool="locked_agent", fixed_kwargs={"secret_config": {"a": 1}})
            data = await preset_ops.validate_preset(name="va", fixed_kwargs={"unhonored": 1})
            assert data["valid"] is False
            assert data["error"]

    asyncio.run(run())


# -- validate: write-validator verdict ---------------------------------------


def test_validate_verdict_carries_write_validator_issues(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def validator(body):
                return ["draft would be rejected"]

            instance.app.presets.register_write_validator("weather", validator)
            data = await preset_ops.validate_preset(name="w", base_tool="weather", fixed_kwargs={"units": "v"})
            assert data["valid"] is False
            assert data["error"] == "draft would be rejected"

    asyncio.run(run())


# -- referees: store-less 404 ------------------------------------------------


def test_referees_store_less_404(monkeypatch) -> None:
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(NotFoundError, match="not found"):
                await preset_ops.preset_referees(name="nope")

    asyncio.run(run())
