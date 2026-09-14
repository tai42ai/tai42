"""The presets/tool-meta/versioning/backup facades."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_contract.presets import CARRY_FORWARD

from .base import _Facet

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from fastmcp.tools import Tool
    from tai42_contract.backup import BackupSectionInfo
    from tai42_contract.manifest import ExtensionElement
    from tai42_contract.presets import (
        CarryForward,
        PresetBody,
        PresetInputSchemaSupport,
        PresetSeed,
        PresetStore,
        PresetWriteValidator,
    )
    from tai42_contract.states import StateBinding
    from tai42_contract.template import TemplatedText
    from tai42_contract.tool_meta import ToolMetaStore
    from tai42_contract.versioning import VersionedStore

    from tai42_skeleton.app.route_registry import RouteAction


class VersioningFacet(_Facet):
    """``app.versioning`` — the generic versioned-document store (``AppVersioning``)."""

    @property
    def store(self) -> VersionedStore:
        return self._app._versioned_store


class PresetsFacet(_Facet):
    """``app.presets`` — the preset bind kernel + the typed store view (``AppPresets``)."""

    async def bind(
        self,
        base_tool: str,
        fixed_kwargs: dict[str, Any],
        *,
        name: str,
        description: str = "",
        output_schema: TemplatedText | dict[str, Any] | None = None,
        input_schema: TemplatedText | dict[str, Any] | None = None,
    ) -> Tool:
        return await self._app._preset_bind(
            base_tool,
            fixed_kwargs,
            name=name,
            description=description,
            output_schema=output_schema,
            input_schema=input_schema,
        )

    async def create(
        self,
        name: str,
        base_tool: str,
        description: str,
        fixed_kwargs: dict[str, Any],
        *,
        input_schema: TemplatedText | dict[str, Any] | None = None,
        output_schema: TemplatedText | dict[str, Any] | None = None,
        extensions: list[list[ExtensionElement]] | None = None,
        state_binding: StateBinding | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a preset in-process, returning the record view the HTTP create door
        returns (shared response builder — the two doors cannot drift). Runs the
        identical content path: the ordered name pre-checks, combo/schema/bind
        validation, input-schema support, the base tool's write validator, store write
        THEN register, one ``list_changed``, and the rebind fan-out.

        The caller-authorization tier fence is OFF: the registration-tier fence belongs to
        the HTTP doors, where it authorizes a request principal. An in-process caller
        authorizes its own callers and passes only the base tools it is entitled to author,
        so there is no request principal to fence here — ``enforce_tier`` is ``False`` while
        every content check stays on."""
        from tai42_skeleton.operations.presets import _create_preset_core, _create_response

        combos = extensions or []
        record, report = await _create_preset_core(
            name,
            base_tool,
            description,
            fixed_kwargs,
            combos,
            output_schema,
            input_schema,
            state_binding=state_binding,
            tags=tags,
            enforce_tier=False,
        )
        return await _create_response(
            name,
            base_tool,
            description,
            combos,
            output_schema,
            input_schema,
            active_version=record.active_version,
            report=report,
        )

    async def save_version(
        self,
        name: str,
        *,
        fixed_kwargs: dict[str, Any] | None = None,
        input_schema: TemplatedText | dict[str, Any] | CarryForward | None = CARRY_FORWARD,
        output_schema: TemplatedText | dict[str, Any] | None = None,
        output_schema_provided: bool = False,
        description: str | None = None,
        extensions: list[list[ExtensionElement]] | None = None,
        state_binding: StateBinding | CarryForward | None = CARRY_FORWARD,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Save a new preset version in-process, returning the version row the HTTP save
        door returns (shared response builder — the two doors cannot drift). Omitted
        fields carry the active value forward (``input_schema`` via the ``CARRY_FORWARD``
        sentinel; ``output_schema`` only when ``output_schema_provided`` is ``True``).

        The tier fence is OFF for the reason :meth:`create` states — the registration-tier
        fence authorizes a request principal at the HTTP doors, and an in-process caller
        authorizes its own callers and passes only the base tools it may author — while
        every content check stays on."""
        from tai42_skeleton.operations.presets import _save_version_core, _save_version_response

        row, report = await _save_version_core(
            name,
            fixed_kwargs=fixed_kwargs,
            extensions=extensions,
            output_schema=output_schema,
            output_schema_provided=output_schema_provided,
            description=description,
            input_schema=input_schema,
            state_binding=state_binding,
            tags=tags,
            enforce_tier=False,
        )
        return _save_version_response(row, report)

    def register_write_validator(self, base_tool: str, validator: PresetWriteValidator) -> None:
        return self._app._write_validator_registry.register(base_tool, validator)

    def register_input_schema_support(self, base_tool: str, support: PresetInputSchemaSupport) -> None:
        return self._app._input_schema_support_registry.register(base_tool, support)

    def input_schema_support(self, base_tool: str) -> PresetInputSchemaSupport | None:
        return self._app._input_schema_support_registry.get(base_tool)

    def register_registration_tier(self, base_tool: str, tier: RouteAction) -> None:
        """The authoring-side name for the tier registry :meth:`ToolsFacet.register_tier`
        writes — the SAME registry object, so a tier declared through either name gates
        both authoring a preset over the base tool and running it."""
        return self._app._registration_tier_registry.register(base_tool, tier)

    def registration_tier(self, base_tool: str) -> RouteAction | None:
        return self._app._registration_tier_registry.get(base_tool)

    def register_seed(self, seed: PresetSeed) -> None:
        return self._app._seed_registry.register(seed)

    def seeds(self) -> list[PresetSeed]:
        """Every declared preset seed. Skeleton-only — the startup/reload seed applier
        consults it, so it is not on the ``AppPresets`` protocol (the register-only
        seam), the same precedent :meth:`write_validator` sets."""
        return self._app._seed_registry.all()

    def write_validator(self, base_tool: str) -> PresetWriteValidator | None:
        """The registered write validator for ``base_tool``, or ``None`` when none
        is registered. Skeleton-only — the preset write path consults it, so it is
        not on the ``AppPresets`` protocol (the precedent :meth:`list_active_bodies`
        sets)."""
        return self._app._write_validator_registry.get(base_tool)

    @property
    def store(self) -> PresetStore:
        return self._app._preset_store

    async def list_active_bodies(self) -> dict[str, PresetBody]:
        """Every store-backed preset's active body, keyed by name — one batched
        JOIN read (replaces a per-record ``get_active_body`` round-trip on the list
        route + rehydrate). Reached through the concrete ``_versioned_store`` so the
        concrete-only ``list_active_bodies`` resolves."""
        from tai42_contract.presets import PresetBody

        raw = await self._app._versioned_store.list_active_bodies("preset")
        return {name: PresetBody.model_validate(body) for name, body in raw.items()}

    async def list_active_versioned_bodies(self) -> dict[str, tuple[int, PresetBody]]:
        """Every preset's ``(active_version, active_body)``, keyed by name — one batched
        JOIN read that captures the version pointer and its body TOGETHER (never a
        skewed second read). The version-aware sibling of :meth:`list_active_bodies`,
        used by the rehydration path so the engine retains each preset's version beside
        its body. Reached through the concrete ``_versioned_store``."""
        from tai42_contract.presets import PresetBody

        raw = await self._app._versioned_store.list_active_versioned_bodies("preset")
        return {name: (version, PresetBody.model_validate(body)) for name, (version, body) in raw.items()}

    async def get_active_versioned_body(self, name: str) -> tuple[int, PresetBody]:
        """One preset's ``(active_version, active_body)``, read TOGETHER in one JOIN.

        The version-aware sibling of ``store.get_active_body`` used by the edit-reload
        path so the freshly-active version is retained beside the body it re-binds.
        Maps the generic store's ``DocumentNotFoundError`` to
        :class:`~tai42_contract.presets.errors.PresetNotFoundError`, mirroring
        :class:`~tai42_skeleton.presets.store.PresetStoreView`. Reached through the
        concrete ``_versioned_store``."""
        from tai42_contract.presets import PresetBody
        from tai42_contract.presets.errors import PresetNotFoundError
        from tai42_contract.versioning.errors import DocumentNotFoundError

        try:
            version, body = await self._app._versioned_store.get_active_version_and_body("preset", name)
        except DocumentNotFoundError as exc:
            raise PresetNotFoundError(name) from exc
        return version, PresetBody.model_validate(body)

    async def set_version_tags(self, name: str, version: int, tags: list[str]) -> None:
        """Replace the per-version ``tags`` annotation of one preset version.

        Tags are labels on an immutable version body, not content — this edits the
        annotation only and never rebinds the live tool. Reached through the
        concrete ``_versioned_store`` (the ``set_version_tags`` UPDATE is a
        concrete-store member, not on the ``VersionedStore`` protocol), the same
        precedent as :meth:`list_active_bodies`. Raises
        :class:`~tai42_contract.versioning.errors.DocumentVersionNotFoundError` for an
        unknown preset or version."""
        await self._app._versioned_store.set_version_tags("preset", name, version, tags)


class ToolMetaFacet(_Facet):
    """``app.tool_meta`` — the tool-metadata overlay (folders + per-tool rows), the
    ``tai42_contract.app.AppToolMeta`` namespace: the ``store`` view plus the
    in-process ``patch`` edit seam."""

    @property
    def store(self) -> ToolMetaStore:
        return self._app._tool_meta_store

    async def patch(
        self,
        tool_name: str,
        *,
        tags: list[str] | None = None,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        """Patch a tool's overlay row in-process, returning the record the HTTP PATCH
        door returns (the same operation, so validation is identical: an unknown
        ``folder_id`` is the same loud error). Only the arguments given are written:
        ``folder_id`` places the tool, and a ``tags`` list REPLACES the whole tag set
        (the door's merge-patch semantic — array values are set replacements, never
        merges). ``tags=None`` leaves the tag set untouched; ``folder_id=None`` leaves
        the placement untouched."""
        from tai42_skeleton.operations.tool_meta import upsert_tool_meta

        patch: dict[str, Any] = {}
        if tags is not None:
            patch["tags"] = tags
        if folder_id is not None:
            patch["folder_id"] = folder_id
        return await upsert_tool_meta(tool_name, patch)


class BackupFacet(_Facet):
    """``app.backup`` — the named backup-section registry (``AppBackup``)."""

    def register_section(
        self, name: str, exporter: Callable[[], Any], importer: Callable[[Any], Any], *, secret: bool = False
    ) -> None:
        return self._app._backup_registry.register_section(name, exporter, importer, secret=secret)

    def sections(self) -> list[BackupSectionInfo]:
        return self._app._backup_registry.sections()

    def export_section(self, name: str) -> Any:
        return self._app._backup_registry.export_section(name)

    def import_section(self, name: str, payload: Any) -> Any:
        return self._app._backup_registry.import_section(name, payload)
