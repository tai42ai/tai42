"""Render-mixin models.

``ConditionMixin`` / ``ExprMixin`` carry the jq/template fields a schema needs to
render a condition or an expression. The ``rendered_condition`` / ``rendered_expr``
methods are impl (they reach the live ``resource_manager``) and live with the
``ResourceManager`` impl, not in this pure-model contract — only the field shape
is the contract.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ConditionMixin(BaseModel):
    condition: str | None = None
    condition_id: str | None = None
    condition_kwargs: dict[str, Any] | None = None


class ExprMixin(BaseModel):
    expr: str | None = None
    expr_id: str | None = None
    expr_kwargs: dict[str, Any] | None = None


__all__ = ["ConditionMixin", "ExprMixin"]
