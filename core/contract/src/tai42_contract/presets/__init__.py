"""The presets contract: the :class:`~tai42_contract.agent.base.PresetSpec` data
model, the :class:`PresetBody` persisted shape, the preset-specific errors, and
the :class:`PresetStore` Protocol.

A *preset* is a base tool with a partial set of keyword arguments baked in,
exposed as a new named, versioned tool. It is the FIRST typed VIEW over the
generic versioned-document store (``kind="preset"``): the store holds the opaque
body, this Protocol is the typed interface over it. tai42-contract owns only the
Protocol + models + errors; the concrete view that delegates to the store,
validates/reshapes the body, and maps the generic errors to these preset errors
lives in the skeleton (no versioning code in the view).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from tai42_contract.agent.base import PresetSpec
from tai42_contract.manifest import ExtensionElement
from tai42_contract.presets.errors import (
    PresetError,
    PresetExistsError,
    PresetNameConflictError,
    PresetNotFoundError,
    PresetVersionNotFoundError,
)
from tai42_contract.presets.models import (
    CARRY_FORWARD,
    CarryForward,
    PresetBody,
    PresetInputSchemaSupport,
)
from tai42_contract.versioning.models import DocumentRecord, DocumentVersion

#: A per-base-tool write validator: given the FULL body about to persist, returns
#: the BLOCKING issue lines that forbid the write (empty = pass). Blocking only —
#: warnings are not a write-path concept.
PresetWriteValidator = Callable[[PresetBody], Awaitable[Sequence[str]]]


@runtime_checkable
class PresetStore(Protocol):
    """The typed interface over the versioned-document store with ``kind="preset"``.

    Delegation, body validation/reshaping, and error mapping are the concrete
    (skeleton) view's job — this Protocol pins only the surface. A preset body is
    always the full :class:`PresetBody` (``{base_tool, description, fixed_kwargs,
    extensions}``); the editable-field methods reconstruct it under the
    carry-forward rules documented on :meth:`save_version`.
    """

    async def create_preset(
        self,
        spec: PresetSpec,
        extensions: Sequence[Sequence[ExtensionElement]],
        output_schema: dict[str, Any] | None = None,
        input_schema: dict[str, Any] | None = None,
    ) -> DocumentRecord:
        """Create a versioned preset. ``spec`` carries ``name``/``description``/
        ``base_tool``/``fixed_kwargs``; ``extensions`` (the combos list) and the
        optional ``output_schema`` / ``input_schema`` ride alongside — all of them
        land in the persisted :class:`PresetBody`. Raise :class:`PresetExistsError`
        on a duplicate name, :class:`PresetNameConflictError` if the name collides
        with a base tool."""
        ...

    async def save_version(
        self,
        name: str,
        fixed_kwargs: dict[str, Any] | None = None,
        extensions: Sequence[Sequence[ExtensionElement]] | None = None,
        output_schema: dict[str, Any] | CarryForward | None = CARRY_FORWARD,
        description: str | None = None,
        *,
        input_schema: dict[str, Any] | CarryForward | None = CARRY_FORWARD,
    ) -> DocumentVersion:
        """Append a new version from the editable body fields.

        A version body is the FULL :class:`PresetBody`, so the view reconstructs
        the new body by ALWAYS carrying ``base_tool`` forward from the ACTIVE
        version body, then applying each of ``fixed_kwargs``, ``extensions`` under
        one UNIFORM sentinel: omitted/``None`` = carry the active value forward
        unchanged; an explicit empty list (``extensions=[]``) = deliberately CLEAR
        that field. An explicitly provided empty list is a value, never "not
        provided". ``output_schema`` and ``input_schema`` each follow the SAME rule
        but with a distinct :data:`CARRY_FORWARD` sentinel (their cleared state is
        ``None``, so ``None`` cannot double as "not provided"): omitted = carry
        forward, an explicit ``None`` = clear, an explicit dict = wins.
        ``description`` is editable per
        version: ``None`` = carry the active description forward, an explicit
        non-empty string = set it. The RESULTING description — explicit or carried
        — is validated non-empty (raising :class:`ValueError`, surfaced as a 400),
        so an explicit ``""`` is rejected and a carry-forward from a legacy body
        with an empty description raises rather than persisting an empty tool
        docstring. An empty INNER combo
        (``[[]]`` or any ``[]`` member of ``extensions``) is REJECTED. The new body
        never DROPS a field. Raise :class:`PresetNotFoundError` if the preset is
        absent."""
        ...

    async def list_presets(self) -> list[DocumentRecord]:
        """List the active versioned presets."""
        ...

    async def get_preset(self, name: str) -> DocumentRecord:
        """Fetch a preset's active record. Raise :class:`PresetNotFoundError` if
        absent."""
        ...

    async def get_active_kwargs(self, name: str) -> dict[str, Any]:
        """Return the active version's baked ``fixed_kwargs``. Raise
        :class:`PresetNotFoundError` if absent."""
        ...

    async def list_versions(self, name: str) -> list[DocumentVersion]:
        """List every version of the preset, each carrying its ``is_current``
        signal. Raise :class:`PresetNotFoundError` if the preset is absent."""
        ...

    async def get_version(self, name: str, version: int) -> DocumentVersion:
        """Fetch one version. Raise :class:`PresetVersionNotFoundError` if that
        version does not exist."""
        ...

    async def get_active_body(self, name: str) -> PresetBody:
        """Return the FULL active-version body (``{base_tool, description,
        fixed_kwargs, extensions}``) so reload/startup can re-register from
        the whole body, not just ``fixed_kwargs``. Raise
        :class:`PresetNotFoundError` if absent."""
        ...

    async def rollback(self, name: str, version: int) -> DocumentRecord:
        """Re-point the active version to ``version`` (no data copy). Raise
        :class:`PresetVersionNotFoundError` if that version does not exist."""
        ...

    async def soft_delete(self, name: str) -> None:
        """Soft-delete the preset, keeping its version history (audit). Raise
        :class:`PresetNotFoundError` if absent."""
        ...

    async def rename_preset(self, name: str, new_name: str) -> DocumentRecord:
        """Re-key a preset from ``name`` to ``new_name``, delegating to the generic
        store's rename under ``kind="preset"``. The version history, every per-version
        ``tags`` label, and the ``active_version`` pointer are preserved by the store
        contract — a rename moves a key, it never rewrites the body. Raise
        :class:`PresetNotFoundError` for an absent ``name``, :class:`PresetExistsError`
        when ``new_name`` is already a live preset, and :class:`PresetNameConflictError`
        when the injected collision predicate says ``new_name`` is held by a live
        non-preset tool (the same predicate :meth:`create_preset` consults)."""
        ...


__all__ = [
    "CARRY_FORWARD",
    "CarryForward",
    "PresetBody",
    "PresetError",
    "PresetExistsError",
    "PresetInputSchemaSupport",
    "PresetNameConflictError",
    "PresetNotFoundError",
    "PresetSpec",
    "PresetStore",
    "PresetVersionNotFoundError",
    "PresetWriteValidator",
]
