"""Versioned-authoring facets: the versioned-document store, presets, and the tool-meta overlay."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tai42_contract.app.facets.routing import RouteAction
from tai42_contract.presets import (
    CARRY_FORWARD,
    CarryForward,
    PresetInputSchemaSupport,
    PresetSeed,
    PresetStore,
    PresetWriteValidator,
)
from tai42_contract.states.binding import StateBinding
from tai42_contract.versioning import VersionedStore

if TYPE_CHECKING:
    from fastmcp.tools import Tool

    from tai42_contract.manifest import ExtensionElement
    from tai42_contract.tool_meta import ToolMetaStore


@runtime_checkable
class AppVersioning(Protocol):
    """The generic versioned-document store namespace (``app.versioning``).

    This is the platform persistence primitive — append-only versions + an active
    pointer + rollback over an opaque JSONB body, discriminated by ``kind``. Direct
    consumers (e.g. AC policies under ``kind="ac_policy"``) reach it here; presets
    reach it through :class:`AppPresets`, never as a new kind.
    """

    @property
    def store(self) -> VersionedStore: ...


@runtime_checkable
class AppPresets(Protocol):
    """The presets namespace (``app.presets``) — the typed view + the bind kernel.

    ``store`` is the :class:`~tai42_contract.presets.PresetStore`-typed view over
    ``app.versioning.store`` (``kind="preset"``). ``bind`` is the kernel BOTH tiers
    (ephemeral and versioned) build their live tool through, so its typed-schema
    behavior reaches both.
    """

    async def bind(
        self,
        base_tool: str,
        fixed_kwargs: dict[str, Any],
        *,
        name: str,
        description: str = "",
        tags: list[str] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> Tool:
        """Return a FastMCP tool transform of ``base_tool`` as a new named tool.

        Built in ONE ``Tool.from_tool`` call: each ``fixed_kwargs`` key is baked as
        a HIDDEN, FIXED constant (removed from the exposed schema; a caller that
        passes it is rejected — it cannot be overridden at runtime) while the
        REMAINING arguments keep the base tool's real typed schema (names, types,
        descriptions). ``tags`` sets the transformed tool's native tags. An
        ``output_schema`` (an object JSON Schema) is baked into an agent base's
        ``response_format`` (forcing structured output) or advertised + validated on
        a plain tool base. ``bind`` is async because it must resolve the base
        ``Tool`` object (via ``app.tools.get_tool(base_tool)``) to feed the
        transform."""
        ...

    async def create(
        self,
        name: str,
        base_tool: str,
        description: str,
        fixed_kwargs: dict[str, Any],
        *,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        extensions: list[list[ExtensionElement]] | None = None,
        state_binding: StateBinding | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a versioned preset in-process and return its record view.

        The in-process authoring seam beside the HTTP create door: it runs the same
        content path (name pre-checks, combo/schema/bind validation, input-schema
        support, the base tool's write validator, store write THEN register, and the
        rebind fan-out) and returns the same record shape, so the two doors cannot drift.
        A body its base tool cannot accept, a colliding name, or an ``input_schema`` over
        a base tool with no registered support is a loud error that persists nothing. The
        caller-authorization tier fence is OFF — the registration-tier fence authorizes a
        request principal at the HTTP doors; an in-process caller authorizes its own callers
        and passes only the base tools it is entitled to author — while every content check
        stays on."""
        ...

    async def save_version(
        self,
        name: str,
        *,
        fixed_kwargs: dict[str, Any] | None = None,
        input_schema: dict[str, Any] | CarryForward | None = CARRY_FORWARD,
        output_schema: dict[str, Any] | None = None,
        output_schema_provided: bool = False,
        description: str | None = None,
        extensions: list[list[ExtensionElement]] | None = None,
        state_binding: StateBinding | CarryForward | None = CARRY_FORWARD,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Save a new version of an existing preset in-process and return the version row.

        The in-process sibling of the HTTP save-version door, running the identical
        content path and returning the same shape. Omitted fields carry the active
        value forward (``input_schema`` via the ``CARRY_FORWARD`` sentinel;
        ``output_schema`` only when ``output_schema_provided`` is ``True``). A body that
        cannot bind is a loud error that commits nothing; the tier fence is OFF for the
        reason :meth:`create` states."""
        ...

    def register_write_validator(self, base_tool: str, validator: PresetWriteValidator) -> None:
        """Register the write validator for ``base_tool`` (one per base tool;
        duplicate registration raises).

        A base-tool plugin calls this through the ``tai42_app`` handle when its tool
        module loads. The validator runs on every write that persists a body —
        create / save-version / rollback — and in the dry-run ``validate`` verdict,
        so a body its base tool cannot accept is a loud 400 that never persists. A
        base tool with no registered validator gets no extra check."""
        ...

    def register_input_schema_support(self, base_tool: str, support: PresetInputSchemaSupport) -> None:
        """Declare that ``base_tool`` ACCEPTS a per-preset input schema (one per base
        tool; duplicate registration raises).

        A base-tool plugin calls this through the ``tai42_app`` handle when its tool
        module loads. A preset that sets an ``input_schema`` over a base tool with no
        registered support is a loud authoring error at the shared preset-authoring
        chokepoint, never a silently-ignored schema."""
        ...

    def input_schema_support(self, base_tool: str) -> PresetInputSchemaSupport | None:
        """The input-schema support ``base_tool`` declared, or ``None`` if it declared
        none (its typed schema is fixed)."""
        ...

    def register_registration_tier(self, base_tool: str, tier: RouteAction) -> None:
        """Declare the authz character required to AUTHOR (create/save/rollback/rename)
        a preset over ``base_tool`` (one per base tool; duplicate registration raises).

        Default authoring is the presets' own ``write`` action; a base tool with no
        declaration keeps that default. A base tool declaring ``fenced`` requires the
        admin fence to author a preset over it. The shared preset-authoring chokepoint
        reads this and enforces it BEFORE any store write on every authoring door."""
        ...

    def registration_tier(self, base_tool: str) -> RouteAction | None:
        """The authoring authz tier ``base_tool`` declared, or ``None`` if it declared
        none (authoring keeps the presets' default ``write`` action)."""
        ...

    def register_seed(self, seed: PresetSeed) -> None:
        """Declare a default preset the platform seeds at import time.

        A plugin calls this through the ``tai42_app`` handle when its module loads.
        The declared seeds are applied by the startup/reload seed applier — created
        when absent, a preset already present left untouched. Declaring two seeds
        under the same ``name`` raises loudly — a silent overwrite could drop one
        plugin's default under another's."""
        ...

    @property
    def store(self) -> PresetStore: ...


@runtime_checkable
class AppToolMeta(Protocol):
    """The tool-metadata namespace (``app.tool_meta``) — the organizational overlay
    over any live tool: a folder tree plus a per-tool row (display name, folder
    placement, tags, badges, a hidden override).

    ``store`` is the :class:`~tai42_contract.tool_meta.ToolMetaStore` view the routes
    and the preset lifecycle cascade read and write. ``patch`` is the in-process edit
    seam beside the HTTP PATCH door.
    """

    @property
    def store(self) -> ToolMetaStore: ...

    async def patch(
        self,
        tool_name: str,
        *,
        tags: list[str] | None = None,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        """Patch a tool's overlay row in-process and return its record.

        The in-process sibling of the HTTP PATCH door, running the same operation so
        validation is identical (an unknown ``folder_id`` is a loud error). Only the
        arguments given are written: ``folder_id`` places the tool, and a ``tags`` list
        REPLACES the whole tag set (array values are set replacements, not merges).
        ``tags=None`` leaves the tag set untouched; ``folder_id=None`` leaves the
        placement untouched."""
        ...
