"""The condition validator and the enforcer reach the same verdict on every shape of condition.

The validator's sample evaluation and the enforcer both decide through
``PolicyEnforcer.evaluate`` over one ``render_condition``, so a condition the author is told
admits the sample is exactly one enforcement admits, and one it refuses is refused.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from starlette.authentication import AuthenticationError
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText

from tai42_skeleton.access_control.policy import (
    ConditionVerdict,
    PolicyEnforcer,
    PolicyEvaluationError,
    render_condition,
)
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.operations.api_keys import validate_condition

from .._helpers import inline_templated_text

_SAMPLE: dict[str, Any] = {
    "sub": "u1",
    "scopes": ["a"],
    "identity": {},
    "policy": {"limit": 3},
    "context": {},
    "request": {"method": "GET", "path": "/api/x"},
    "system": {"time": 0},
}


class _Renderer:
    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        return inline_templated_text(text)


class _Storage:
    resource_manager = _Renderer()


class _App:
    storage = _Storage()


@pytest.fixture(autouse=True)
def _renderer() -> Iterator[None]:
    with tai42_app.bound(_App()):
        yield


async def _enforced(condition: TemplatedText | None) -> str:
    """``allow`` / ``deny`` / ``error`` as enforcement answers on the sample."""
    enforcer = PolicyEnforcer(AccessControlSettings())
    try:
        await enforcer.enforce(_SAMPLE, await render_condition(condition))
    except PolicyEvaluationError:
        return "error"
    except AuthenticationError:
        return "deny"
    return "allow"


async def _validated(condition: TemplatedText | None) -> str:
    """``allow`` / ``deny`` / ``error`` as the validator answers on the sample."""
    payload = condition.model_dump() if condition is not None else None
    try:
        result = await validate_condition(
            condition=TemplatedText.model_validate(payload) if payload is not None else None,
            sample_context=_SAMPLE,
        )
    except BadRequestError:
        return "error"
    if result["result"] is None or result["result"] is True:
        return "allow"
    return "deny"


@pytest.mark.parametrize(
    ("condition", "verdict", "enforced", "validated"),
    [
        (None, ConditionVerdict.ALLOW, "allow", "allow"),
        (TemplatedText(content=""), ConditionVerdict.RENDERED_EMPTY, "deny", "error"),
        (TemplatedText(content="1"), ConditionVerdict.DENY, "deny", "deny"),
        (TemplatedText(content='"yes"'), ConditionVerdict.DENY, "deny", "deny"),
        (TemplatedText(content=".policy.limit == 3"), ConditionVerdict.ALLOW, "allow", "allow"),
        (TemplatedText(content=".policy.limit == 4"), ConditionVerdict.DENY, "deny", "deny"),
    ],
    ids=["absent", "rendered-empty", "truthy-number", "truthy-string", "true", "false"],
)
async def test_the_validator_and_the_enforcer_agree(
    condition: TemplatedText | None, verdict: ConditionVerdict, enforced: str, validated: str
) -> None:
    rendered = await render_condition(condition)
    assert await PolicyEnforcer.evaluate(_SAMPLE, rendered) is verdict
    assert await _enforced(condition) == enforced
    # A configured condition that renders empty denies at enforcement; the validator refuses
    # to call it valid (its lock-out guard), so the author never saves a lock-out.
    assert await _validated(condition) == validated


async def test_an_evaluation_error_propagates_from_evaluate_and_is_typed_by_enforce() -> None:
    condition = TemplatedText(content='error("boom")')
    rendered = await render_condition(condition)
    with pytest.raises(ValueError, match="boom"):
        await PolicyEnforcer.evaluate(_SAMPLE, rendered)
    assert await _enforced(condition) == "error"
    assert await _validated(condition) == "error"


async def test_render_condition_marks_a_present_condition_configured() -> None:
    assert (await render_condition(None)).configured is False
    assert (await render_condition(None)).text == ""
    empty = await render_condition(TemplatedText(content=""))
    assert (empty.text, empty.configured) == ("", True)
    full = await render_condition(TemplatedText(content="true"))
    assert (full.text, full.configured) == ("true", True)
