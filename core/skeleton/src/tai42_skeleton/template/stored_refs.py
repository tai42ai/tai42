"""The stored template ids a stored model names, at their sites, one reader per stored shape.

A write door checks the ids its reader answers before it stores the model, and the template
delete doors read the same lists to find what still names an id.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tai42_contract.conversations import ConversationRoute
from tai42_contract.hooks import HookRegister
from tai42_contract.interactions import ParkableDoorMixin
from tai42_contract.template import TemplatedText
from tai42_contract.tools import TemplateRef


def _ref(text: TemplatedText | None, site: str) -> list[TemplateRef]:
    if text is not None and text.id is not None:
        return [TemplateRef(id=text.id, site=site)]
    return []


def door_contract_template_refs(fields: ParkableDoorMixin) -> list[TemplateRef]:
    """The four door expressions: sites ``start_expr``, ``cancel_expr``, ``resume_expr``, ``extras_expr``."""
    return [
        *_ref(fields.start_expr, "start_expr"),
        *_ref(fields.cancel_expr, "cancel_expr"),
        *_ref(fields.resume_expr, "resume_expr"),
        *_ref(fields.extras_expr, "extras_expr"),
    ]


def hook_template_refs(params: HookRegister) -> list[TemplateRef]:
    """A hook's ``condition``, ``subject.key_expr``, then its four door expressions."""
    key_expr = params.subject.key_expr if params.subject is not None else None
    return [
        *_ref(params.condition, "condition"),
        *_ref(key_expr, "subject.key_expr"),
        *door_contract_template_refs(params),
    ]


def route_template_refs(route: ConversationRoute) -> list[TemplateRef]:
    """A conversation route's four door expressions, then ``reply_expr``."""
    return [*door_contract_template_refs(route), *_ref(route.reply_expr, "reply_expr")]


def condition_template_refs(condition: TemplatedText | Mapping[str, Any] | None) -> list[TemplateRef]:
    """A policy's or role's ``condition``; a mapping (the stored row's form) is parsed first.

    A malformed mapping raises the parse's ``ValueError``.
    """
    if isinstance(condition, Mapping):
        condition = TemplatedText.model_validate(condition)
    return _ref(condition, "condition")


__all__ = [
    "condition_template_refs",
    "door_contract_template_refs",
    "hook_template_refs",
    "route_template_refs",
]
