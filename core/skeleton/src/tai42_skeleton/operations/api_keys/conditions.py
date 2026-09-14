"""The fail-closed jq policy-condition validator door."""

from __future__ import annotations

from typing import Any

from jinja2 import TemplateError
from pydantic import ValidationError
from tai42_contract.access_control.models import JqAuthContext
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data import run_jq_first
from tai42_kit.utils.data.jq_util import get_compiled_jq

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
    """Fail-closed guard: compile — and optionally sample-evaluate — a jq policy
    ``condition`` WITHOUT persisting it.

    A syntactically broken condition raises at enforcement and DENIES the key (a
    lock-out), so authoring flows validate here before saving. The condition is
    rendered exactly as enforcement renders it, compiled with ``get_compiled_jq``, and —
    when a ``sample_context`` is supplied — evaluated against a ``JqAuthContext``-shaped
    sample. This ONLY compiles/evaluates; it never writes any store. Returns ``{"ok":
    true, "result": <bool|null>}`` (``result`` is ``null`` when no sample was evaluated).
    An AUTHOR error is a loud ``BadRequestError`` (400); a server-side fault (an
    unconfigured resource manager, a redis/storage outage rendering a stored condition
    ``id``) is NOT an author error and propagates as a loud 500."""
    # Mirror enforcement's own "was a condition configured?" test exactly (``policy.condition
    # is not None``): a PRESENT condition that renders empty is still configured and denies at
    # enforcement, so it must reach the render-empty lock-out branch below.
    configured = condition is not None
    try:
        rendered = ""
        if condition is not None:
            rendered = await tai42_app.storage.resource_manager.render_templated_text(condition)
        result: Any = None
        if rendered:
            # Compile-validate the expression (the compile half of this endpoint):
            # a broken expression raises here and lands in the verbatim-400 handler.
            get_compiled_jq(rendered)
            if sample_context is not None:
                # Enforcement allows ONLY when the jq emits exactly ``True`` (a truthy
                # non-``True`` value denies), so coerce to that same boolean here — the
                # sample result then honestly mirrors the allow/deny enforcement would
                # reach and stays a clean ``bool | null``. The evaluation runs off-loop
                # under a wall-clock budget (JQ_TIMEOUT_SECONDS).
                result = (await run_jq_first(rendered, JqAuthContext(**sample_context).model_dump())) is True
        elif configured:
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
