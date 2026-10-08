"""The fail-closed jq policy-condition validator door."""

from __future__ import annotations

from typing import Any

from jinja2 import TemplateError
from pydantic import ValidationError
from tai42_contract.access_control.models import JqAuthContext
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.jq_util import get_compiled_jq

from tai42_skeleton.access_control.policy import ConditionVerdict, PolicyEnforcer, render_condition
from tai42_skeleton.operations import BadRequestError, operation
from tai42_skeleton.operations.response_models_group_a import ConditionCheckResult
from tai42_skeleton.template import TemplateNotFoundError

from .models import ConditionValidation


@operation(
    summary="Validate a jq policy condition",
    tags=["access-control"],
    errors=[BadRequestError],
    request_model=ConditionValidation,
    response_model=ConditionCheckResult,
)
async def validate_condition(
    condition: TemplatedText | None,
    sample_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fail-closed guard: compile — and optionally sample-evaluate — a jq policy ``condition``, unpersisted.

    A syntactically broken condition raises at enforcement and DENIES the key (a
    lock-out), so authoring flows validate here before saving. The condition is
    rendered through the SAME ``render_condition`` enforcement uses, compiled with
    ``get_compiled_jq``, and — when a ``sample_context`` is supplied — decided by the SAME
    ``PolicyEnforcer.evaluate`` against a ``JqAuthContext``-shaped sample, so the
    validator's verdict is enforcement's by construction. This ONLY compiles/evaluates; it
    never writes any store. Returns ``{"ok": true, "result": <bool|null>}`` (``result`` is
    ``null`` when no sample was evaluated).
    An AUTHOR error is a loud ``BadRequestError`` (400); a server-side fault (an
    unconfigured resource manager, a redis/storage outage rendering a stored condition
    ``id``) is NOT an author error and propagates as a loud 500.
    """
    try:
        rendered = await render_condition(condition)
        result: Any = None
        if rendered.text:
            # Compile-validate the expression (the compile half of this endpoint):
            # a broken expression raises here and lands in the verbatim-400 handler.
            get_compiled_jq(rendered.text)
            if sample_context is not None:
                # The sample result is enforcement's own verdict on the sample (ALLOW only
                # when the jq emits exactly ``True``) — a clean ``bool | null``.
                verdict = await PolicyEnforcer.evaluate(JqAuthContext(**sample_context).model_dump(), rendered)
                result = verdict is ConditionVerdict.ALLOW
        elif rendered.configured:
            # A configured condition that renders to an EMPTY string denies at
            # enforcement (fail-closed), so reporting ``ok`` would tell the author a
            # lock-out condition is safe — the exact footgun this guard exists to
            # catch. Surface it as a loud validation failure instead.
            raise BadRequestError(
                "condition renders empty, which denies at enforcement and would lock the key out; "
                "author a condition that renders to a non-empty jq expression"
            )
    except (ValueError, ValidationError, TemplateError, TemplateNotFoundError) as exc:
        # AUTHOR errors only — the jq compile/eval ``ValueError`` (the jq lib's error
        # type), the pydantic ``ValidationError`` from a malformed ``sample_context``,
        # a jinja ``TemplateError`` from a broken inline condition, and
        # ``TemplateNotFoundError`` for a missing stored condition ``id``. Their message is
        # the actionable feedback the author needs, surfaced verbatim as a 400. Any
        # other exception (an unconfigured resource manager ``RuntimeError``, a
        # redis/storage outage) is a server fault and propagates as a loud 500.
        raise BadRequestError(str(exc)) from exc
    return {"ok": True, "result": result}
