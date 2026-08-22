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
    against it at run time. ``input_schema`` is an optional author-set INPUT JSON
    Schema: it becomes the exposed named tool's input contract, and the caller's
    validated object is routed into the base tool's declared payload argument (see
    :class:`PresetInputSchemaSupport`). Only a base tool that DECLARES input-schema
    support accepts one; otherwise a set ``input_schema`` is a loud authoring error.

    Every field must survive carry-forward on a version save: dropping
    ``extensions`` would make the branch tools vanish on reload, dropping
    ``base_tool`` would break the bind, dropping ``description`` would strip the
    tool's description, dropping ``output_schema`` would silently un-enforce the
    structured output, and dropping ``input_schema`` would silently un-enforce the
    structured input.
    """

    base_tool: str
    description: str = ""
    fixed_kwargs: dict[str, Any] = Field(default_factory=dict)
    extensions: list[list[ExtensionElement]] = Field(default_factory=list[list[ExtensionElement]])
    output_schema: dict[str, Any] | None = None
    input_schema: dict[str, Any] | None = None


class PresetInputSchemaSupport(BaseModel):
    """A base tool's declaration that it ACCEPTS a per-preset input schema.

    Most base tools declare none — their typed schema is fixed. A base tool that
    declares support names, via ``payload_arg``, the base tool's OWN argument the
    validated structured input is delivered under: a preset's ``input_schema``
    becomes the exposed named tool's input contract and the caller's validated
    object is routed into ``payload_arg`` on the base call.
    """

    payload_arg: str


class CarryForward:
    """Sentinel for a :meth:`~tai42_contract.presets.PresetStore.save_version`
    editable field the caller did not provide — carry the ACTIVE value forward.

    ``fixed_kwargs`` / ``extensions`` clear with an empty container, so
    ``None`` is their carry-forward sentinel; ``output_schema`` / ``input_schema``
    have no empty container (their cleared state IS ``None``), so each needs a
    distinct sentinel to tell "not provided" (carry forward) apart from an explicit
    ``None`` (clear)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "CARRY_FORWARD"


CARRY_FORWARD = CarryForward()
