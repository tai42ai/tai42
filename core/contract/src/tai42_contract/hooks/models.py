from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from tai42_contract.conversations import ConversationTargetKind
from tai42_contract.states.models import SUBJECT_KIND_RE
from tai42_contract.template import ConditionMixin, ExprMixin

# A hook's ``topic`` is dispatched as ONE path segment of the public webhook URL
# (``/universal_webhook/{topic}``), and its ``name`` addresses the hook on every
# management/delete door. A ``/`` (or a trailing newline) in either would mint a
# record no webhook delivery can ever route to and no door can address — so the
# charset is bounded to a single lowercase path segment, mirroring the sub-MCP
# slug rule. ``\Z`` (not ``$``) anchors the true end of string so a trailing
# ``\n`` cannot slip through.
_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\Z")


class TopicVerifierBinding(BaseModel):
    """A per-topic webhook-verifier binding: the name of a registered verifier
    plus its per-topic ``config``.

    This is the persisted shape a hooks manager stores for a topic. ``verifier``
    names a registered :class:`~tai42_contract.webhooks.WebhookVerifier`; ``config``
    is that verifier's per-binding configuration and NEVER holds a secret value —
    only a ``secret_env`` naming the environment variable that does. The model is
    frozen so a validated binding cannot be mutated after it is read back.
    """

    model_config = ConfigDict(frozen=True)

    verifier: str = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)


class HookSubject(BaseModel):
    """The optional state subject a hook fire targets: the conversation-target scope
    ``(target_kind, target_name)``, the subject ``kind`` (matching
    :data:`~tai42_contract.states.SUBJECT_KIND_RE`), and a ``key_expr`` jq evaluated
    over the event payload at fire — it must yield a non-empty string, else the fire
    fails loudly like any hook error. Frozen; deposited as the ambient state context so
    a state write during the fire is keyed and attributed to the hook."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_kind: ConversationTargetKind
    target_name: str = Field(min_length=1)
    kind: str
    key_expr: str = Field(min_length=1)

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        if not SUBJECT_KIND_RE.fullmatch(value):
            raise ValueError(f"subject kind {value!r} must match {SUBJECT_KIND_RE.pattern}")
        return value


class HookRegister(ConditionMixin, ExprMixin):
    """The client-facing register-a-hook request body: the fields a caller supplies.

    ``execution_key_fingerprint`` is server-derived at bind and deliberately absent
    here; :class:`HookParams` is this shape plus that one stored field.
    """

    # Empty name/topic/tool would store a record no door can address, look up or route.
    name: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    tool: str = Field(min_length=1)

    @field_validator("name", "topic")
    @classmethod
    def _require_url_safe_segment(cls, value: str, info: ValidationInfo) -> str:
        if not _SEGMENT_RE.match(value):
            raise ValueError(
                f"{info.field_name} {value!r} must match {_SEGMENT_RE.pattern} (one lowercase URL path segment)"
            )
        return value

    execution_key: str = Field(
        min_length=1,
        description=(
            "The api-key ``user_id`` the hook fires as; its live stored grants authorize "
            "every tool call the fire makes."
        ),
    )
    tool_kwargs: dict[str, Any] = Field(default_factory=dict)

    # The optional state subject the fire targets; ``None`` leaves the fire with no
    # ambient state context, so a state tool it calls must carry an explicit subject.
    subject: HookSubject | None = None

    # ``expr``/``expr_id``/``condition``/``condition_id`` come from the mixins;
    # the ``*_kwargs`` fields are re-declared non-optional (hooks default them
    # to {} and never carry None).
    expr_kwargs: dict[str, Any] = Field(default_factory=dict)  # pyright: ignore[reportIncompatibleVariableOverride]
    condition_kwargs: dict[str, Any] = Field(default_factory=dict)  # pyright: ignore[reportIncompatibleVariableOverride]


class HookParams(HookRegister):
    """The stored hook record: :class:`HookRegister` plus the server-derived
    ``execution_key_fingerprint``. Managers persist this shape, never the request body."""

    execution_key_fingerprint: str = Field(
        min_length=1,
        description=(
            "The bound key's per-mint identity, derived server-side at bind and never "
            "client-supplied. The fire matches it against the live key, so a revoke+remint "
            "of the same ``user_id`` fails closed."
        ),
    )
