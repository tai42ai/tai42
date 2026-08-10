"""The preset body model — the typed JSONB ``body`` a preset stores under
``kind="preset"`` in the generic versioned-document store.

This is the SHAPE only. The concrete view that validates and reshapes it (and
enforces the sentinel/empty-combo rules documented on
:meth:`~tai42_contract.presets.PresetStore.save_version`) lives in the skeleton,
mirroring the AC-policy view — a contract holds models, never logic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from tai42_contract.manifest import ExtensionElement


class PresetBody(BaseModel):
    """The persisted body of a versioned preset.

    ``base_tool`` is the tool the preset binds; ``description`` is the preset's
    human description; ``fixed_kwargs`` are the baked kwargs (each becomes a
    hidden, fixed constant on the bound tool). ``extensions`` is a list of
    extension COMBOS — the SAME shape as a manifest ``extensions`` map value,
    each combo element an extension name or a ``{"name", "config"}`` mapping
    binding author config (e.g. an ``ask_external`` verifier) — fed unconverted
    to the structured runtime-attach API at register (an empty outer list means
    no extensions; an empty INNER combo is rejected by the validating view).
    ``output_schema`` is an optional author-set OUTPUT JSON Schema (an object
    schema): on an AGENT base it is baked into the run tool's ``response_format``
    so the agent FORCES a structured output matching it; on a plain tool it is
    advertised as the bound tool's output schema and every result is validated
    against it at run time.

    Every field must survive carry-forward on a version save: dropping
    ``extensions`` would make the branch tools vanish on reload, dropping
    ``base_tool`` would break the bind, dropping ``description`` would strip the
    tool's description, and dropping ``output_schema`` would silently un-enforce
    the structured output.
    """

    base_tool: str
    description: str = ""
    fixed_kwargs: dict[str, Any] = Field(default_factory=dict)
    extensions: list[list[ExtensionElement]] = Field(default_factory=list[list[ExtensionElement]])
    output_schema: dict[str, Any] | None = None


class CarryForward:
    """Sentinel for a :meth:`~tai42_contract.presets.PresetStore.save_version`
    editable field the caller did not provide — carry the ACTIVE value forward.

    ``fixed_kwargs`` / ``extensions`` clear with an empty container, so
    ``None`` is their carry-forward sentinel; ``output_schema`` has no empty
    container (its cleared state IS ``None``), so it needs a distinct sentinel to
    tell "not provided" (carry forward) apart from an explicit ``None`` (clear)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "CARRY_FORWARD"


CARRY_FORWARD = CarryForward()
