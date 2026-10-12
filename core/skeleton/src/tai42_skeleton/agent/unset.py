"""The sentinel an agent run tool defaults each optional parameter to.

The agent binding builds the run tool's signature with it and the tool-binding package's client-tool
and dispatch seams strip it, so it lives in this leaf that imports no skeleton module.
"""

from __future__ import annotations

from pydantic_core import core_schema


class _Unset:
    """Marker for an agent run-tool parameter the caller did not supply.

    The synthesized run tool presents a concrete keyword-only parameter per
    ``ToolInput`` field so a wrapper/transformer extension can compose over a real
    signature. An optional field's parameter defaults to the module-level
    :data:`_UNSET` instance; the body forwards only the parameters whose value is
    NOT that sentinel, preserving ``from_tool_input``'s set-fields-only contract (a
    materialized model default would forward every field). ``__get_pydantic_core_schema__``
    maps the sentinel to an ``any`` schema so pydantic derives the parameter as
    optional and omits the non-serializable sentinel default from the JSON schema.
    """

    __get_pydantic_core_schema__ = classmethod(lambda cls, source, handler: core_schema.any_schema())


_UNSET = _Unset()
