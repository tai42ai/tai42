"""Oracles for the version-write doors: save-version schema / store branches,
rollback target-body validation, the version-tags gates, and their write validator."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from tai42_contract.presets import PresetBody
from tai42_contract.presets.errors import PresetNotFoundError

from tai42_skeleton.app import instance
from tai42_skeleton.operations import NotFoundError
from tai42_skeleton.operations import presets as preset_ops

from .conftest import _create, _manifest

# -- save_version: schema branch + store errors ------------------------------


def test_save_version_schema_error_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("s", fixed_kwargs={"units": "v"})
            with pytest.raises(preset_ops.BadRequestError, match="object schema"):
                await preset_ops.save_version(
                    name="s",
                    fixed_kwargs=None,
                    extensions=None,
                    output_schema={"type": "string"},
                    output_schema_provided=True,
                    description=None,
                )

    asyncio.run(run())


def test_save_version_store_value_error_400(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("s", fixed_kwargs={"units": "v"})

            async def boom(self, *a, **k):
                raise ValueError("bad version payload")

            monkeypatch.setattr(type(instance.app.presets.store), "save_version", boom)
            with pytest.raises(preset_ops.BadRequestError, match="bad version payload"):
                await preset_ops.save_version(
                    name="s",
                    fixed_kwargs={"units": "z"},
                    extensions=None,
                    output_schema=None,
                    output_schema_provided=False,
                    description=None,
                )

    asyncio.run(run())


def test_save_version_store_not_found_404(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("s", fixed_kwargs={"units": "v"})

            async def boom(self, *a, **k):
                raise PresetNotFoundError("s")

            monkeypatch.setattr(type(instance.app.presets.store), "save_version", boom)
            with pytest.raises(NotFoundError, match="not found"):
                await preset_ops.save_version(
                    name="s",
                    fixed_kwargs={"units": "z"},
                    extensions=None,
                    output_schema=None,
                    output_schema_provided=False,
                    description=None,
                )

    asyncio.run(run())


# -- rollback: target-body schema branch -------------------------------------


def test_rollback_target_schema_error_400(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("r", fixed_kwargs={"units": "v"})
            await preset_ops.save_version(
                name="r",
                fixed_kwargs={"units": "w"},
                extensions=None,
                output_schema=None,
                output_schema_provided=False,
                description=None,
            )

            real_get_version = instance.app.presets.store.get_version

            async def poisoned(self, name, version):
                row = await real_get_version(name, version)
                # Present the target version with a non-object output schema so the
                # rollback's pre-commit validation rejects it (400), never re-points.
                body = PresetBody.model_validate(row.body)
                poisoned_body = body.model_copy(update={"output_schema": {"type": "string"}}).model_dump()
                return SimpleNamespace(body=poisoned_body)

            monkeypatch.setattr(type(instance.app.presets.store), "get_version", poisoned)
            with pytest.raises(preset_ops.BadRequestError, match="object schema"):
                await preset_ops.rollback_preset(name="r", version=1)

    asyncio.run(run())


def test_rollback_target_bind_error_400(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("rb", fixed_kwargs={"units": "v"})

            real_get_version = instance.app.presets.store.get_version

            async def poisoned(self, name, version):
                row = await real_get_version(name, version)
                body = PresetBody.model_validate(row.body)
                # A baked key the base tool does not accept fails the dry-run bake (400).
                poisoned_body = body.model_copy(update={"fixed_kwargs": {"not_a_param": 1}}).model_dump()
                return SimpleNamespace(body=poisoned_body)

            monkeypatch.setattr(type(instance.app.presets.store), "get_version", poisoned)
            with pytest.raises(preset_ops.BadRequestError, match="cannot bind"):
                await preset_ops.rollback_preset(name="rb", version=1)

    asyncio.run(run())


# -- version tags: bad version + store-less 501 ------------------------------


def test_set_version_tags_non_int_version_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(preset_ops.BadRequestError, match="version must be an integer"):
                await preset_ops.set_preset_version_tags(name="x", version="abc", tags=[])

    asyncio.run(run())


def test_set_version_tags_store_less_501(monkeypatch) -> None:
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(
                preset_ops.NotSupportedError, match="versioned-document store is not configured"
            ) as exc_info:
                await preset_ops.set_preset_version_tags(name="x", version="1", tags=[])
            assert exc_info.value.extra["code"] == "versioning-not-configured"

    asyncio.run(run())


# -- per-base-tool write validator (save / rollback legs) --------------------


def test_save_version_rejected_by_write_validator_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            issues: list[str] = []

            async def validator(body):
                return list(issues)

            instance.app.presets.register_write_validator("weather", validator)
            await _create("w", fixed_kwargs={"units": "v"})
            issues.append("save rejected")
            with pytest.raises(preset_ops.BadRequestError) as exc_info:
                await preset_ops.save_version(
                    name="w",
                    fixed_kwargs={"units": "z"},
                    extensions=None,
                    output_schema=None,
                    output_schema_provided=False,
                    description=None,
                )
            assert exc_info.value.message == "save rejected"

    asyncio.run(run())


def test_rollback_rejected_by_write_validator_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            issues: list[str] = []

            async def validator(body):
                return list(issues)

            instance.app.presets.register_write_validator("weather", validator)
            await _create("w", fixed_kwargs={"units": "v"})
            await preset_ops.save_version(
                name="w",
                fixed_kwargs={"units": "z"},
                extensions=None,
                output_schema=None,
                output_schema_provided=False,
                description=None,
            )
            issues.append("rollback rejected")
            with pytest.raises(preset_ops.BadRequestError) as exc_info:
                await preset_ops.rollback_preset(name="w", version=1)
            assert exc_info.value.message == "rollback rejected"

    asyncio.run(run())
