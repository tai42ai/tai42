"""Extension contracts.

``ExtensionKind`` is the enum of tool-extension kinds (Wrapper / Transformer /
Backend). Each kind carries its distinguishing rules as explicit properties —
``multiple`` (cardinality), ``preserves_schema`` and ``declares_schema`` (the
input-schema rule), ``preserves_output_shape`` (whether a minted branch may
inherit the base tool's output schema), and ``relocates_execution`` (whether the
kind moves the tool body to another process) — so generic machinery keys off a
property, never off ``kind is WRAPPER``. The per-kind extension shape is in
:mod:`tai42_contract.extensions.kinds`.
"""

from __future__ import annotations

from enum import StrEnum


class ExtensionKind(StrEnum):
    """The three tool-extension kinds and their per-kind properties.

    Every kind branches by rename: an extension mints a NEW-named variant tool
    bound alongside the original, and a stacked combo (``[a, b]``) binds one
    variant per prefix (``tool``, ``tool_a``, ``tool_a_b``). No kind replaces a
    tool in place.

    * ``WRAPPER`` — presents the wrapped tool's input schema unchanged (minus its
      own declared ``reserved_params``); stackable.
    * ``TRANSFORMER`` — presents its OWN composed input schema (a concrete
      makefun-presented signature, never bare ``*args/**kwargs``); stackable.
    * ``BACKEND`` — one execution-backend strategy per tool (``multiple=False``);
      no input-schema rule; relocates the tool body's execution to a worker
      process (``relocates_execution``).
    """

    WRAPPER = "wrapper"
    TRANSFORMER = "transformer"
    BACKEND = "backend"

    @property
    def multiple(self) -> bool:
        """Whether a single tool may carry more than one extension of this kind."""
        return _MULTIPLE[self]

    @property
    def preserves_schema(self) -> bool:
        """Whether an extension of this kind must present the extended tool's
        input schema unchanged (parameter names/types identical; the tool NAME
        always changes — every extension branches to a new-named tool)."""
        return _PRESERVES_SCHEMA[self]

    @property
    def declares_schema(self) -> bool:
        """Whether an extension of this kind must present its OWN concrete input
        schema (a real makefun-presented signature, never bare
        ``*args/**kwargs``)."""
        return _DECLARES_SCHEMA[self]

    @property
    def relocates_execution(self) -> bool:
        """Whether an extension of this kind moves the tool body's execution to
        another process. A BACKEND swaps the execution strategy for a worker (the
        wrapped callable is submitted and runs there, not in the process that
        received the call); a WRAPPER and a TRANSFORMER execute the body
        in-process, inside their own frame.

        This drives the stacking-order rule the apply site enforces: an
        extension that ``requires_body_locality`` (its wrapper only works in the
        process running the tool body, e.g. a proxy layer routing egress through
        a task-scoped contextvar) must bind INSIDE any relocating extension, so
        its wrapper travels with the body to the worker. Bound outside, the
        relocating layer ships only the inner callable and the locality-requiring
        wrapper stays behind in the submitting process, silently not applying."""
        return _RELOCATES_EXECUTION[self]

    @property
    def preserves_output_shape(self) -> bool:
        """Whether an extension of this kind returns the extended tool's result
        shape unchanged, so a branch it mints may inherit the base tool's
        OUTPUT schema. This is an OUTPUT-schema concern, distinct from
        ``preserves_schema`` (which is about the INPUT schema): a WRAPPER returns
        the wrapped tool's result unchanged and a BACKEND swaps the execution
        strategy but returns the same output shape (both preserve), while a
        TRANSFORMER reshapes the result (does not preserve)."""
        return _PRESERVES_OUTPUT_SHAPE[self]


# BACKEND is single-strategy; the other kinds stack.
_MULTIPLE: dict[ExtensionKind, bool] = {
    ExtensionKind.WRAPPER: True,
    ExtensionKind.TRANSFORMER: True,
    ExtensionKind.BACKEND: False,
}

# Only WRAPPER is schema-transparent (its branch must equal the layer's input
# schema); TRANSFORMER reshapes and BACKEND has no schema rule.
_PRESERVES_SCHEMA: dict[ExtensionKind, bool] = {
    ExtensionKind.WRAPPER: True,
    ExtensionKind.TRANSFORMER: False,
    ExtensionKind.BACKEND: False,
}

# Only TRANSFORMER owns (declares) its own composed schema; this separates it
# from BACKEND, the other non-preserving kind, without special-casing the member.
_DECLARES_SCHEMA: dict[ExtensionKind, bool] = {
    ExtensionKind.WRAPPER: False,
    ExtensionKind.TRANSFORMER: True,
    ExtensionKind.BACKEND: False,
}

# Only BACKEND relocates: it swaps the execution strategy for a worker, so the
# tool body runs in another process. WRAPPER and TRANSFORMER call the body
# in-process. The apply site reads this to order a stacked combo: a
# locality-requiring extension binds inside any relocating one.
_RELOCATES_EXECUTION: dict[ExtensionKind, bool] = {
    ExtensionKind.WRAPPER: False,
    ExtensionKind.TRANSFORMER: False,
    ExtensionKind.BACKEND: True,
}

# A WRAPPER returns the wrapped tool's result unchanged and a BACKEND returns
# the same tool's output shape via a swapped execution strategy — both preserve
# the OUTPUT shape, so a branch they mint may inherit the base's output schema.
# A TRANSFORMER reshapes the result (batch -> a list, chain -> the downstream
# tool's output), so its branch must NOT inherit the base's output schema.
_PRESERVES_OUTPUT_SHAPE: dict[ExtensionKind, bool] = {
    ExtensionKind.WRAPPER: True,
    ExtensionKind.TRANSFORMER: False,
    ExtensionKind.BACKEND: True,
}

__all__ = ["ExtensionKind"]
