"""The answer-check view of an ask: one mapping shared with the live ``ask`` door.

``question_format`` builds the ``QuestionFormat`` an ask with the same arguments checks its answer
against. Its payload equals what the live door stores for those arguments, minus EXTERNAL's
delivery keys, for every format; a neutral consumer reaches it and ``check_answer`` through the
facet alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, get_type_hints

import pytest
from pydantic import ValidationError
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    AnswerFormat,
    AnswerMismatchError,
    QuestionFormat,
    SuspendedInteraction,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)
from tai42_contract.states import StateContext, SubjectCandidates
from tai42_contract.tools import tool_call_frame
from tai42_kit.utils.state_context import state_context

from tai42_skeleton.app.instance import app
from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.interactions import InteractionStore, ask
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.answer_check import check_answer
from tai42_skeleton.interactions.question_format import QuestionArguments, question_format
from tai42_skeleton.interactions.settings import InteractionsSettings

# Imported at collection, before any test boots a manifest onto the shared app: a builtin's
# registration decorators resolve its module against the bound app's manifest.
with tai42_app.bound(app):
    from tai42_skeleton.tools.builtin import interactions as builtin

_FORM_SCHEMA = {
    "type": "object",
    "properties": {"mode": {"type": "string", "enum": ["a", "b"]}, "count": {"type": "integer"}},
    "required": ["mode"],
}
_INTEGER = {"type": "integer"}
_CONTEXT = StateContext(
    door="conversation",
    candidates=SubjectCandidates(target_kind="tool", target_name="t", by_kind={"person": "pA"}),
)

# The ask arguments of each case: (answer_format, the format arguments).
_CASES: list[tuple[str, dict[str, Any]]] = [
    ("text", {}),
    ("text", {"options": ["yes please", "no thanks"]}),
    ("confirm", {}),
    ("select", {"options": ["red", "blue"]}),
    ("form", {"schema": _FORM_SCHEMA}),
    ("form", {"schema": _FORM_SCHEMA, "data": {"values": {"count": 3}}}),
    ("free", {}),
    ("free", {"schema": _INTEGER}),
    ("external", {}),
    ("external", {"schema": _INTEGER}),
]


@pytest.fixture(autouse=True)
def settings(monkeypatch, fake_redis, fake_client_ctx) -> InteractionsSettings:
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    settings = InteractionsSettings(public_base_url="https://cb.example")
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(helper_module, "interactions_settings", lambda: settings)
    return settings


async def _stored_payload(fake_redis, settings: InteractionsSettings, answer_format: str, **arguments: Any) -> Any:
    """The ``format_payload`` the live ask door stores for these arguments."""
    link = "https://sign.example/{callback_url}" if answer_format == "external" else None
    # A free ask is answered by the run's caller, so it parks under a caller-addressed ask.
    to = "caller" if answer_format == "free" else "user"
    tool_token = set_resume_continuation_tool("probe_resume")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    try:
        with tool_call_frame(), state_context(_CONTEXT):
            parked = await ask(
                "?",
                answer_format=answer_format,
                link=link,
                to=to,
                mode="async",
                expiry_at=datetime.now(UTC) + timedelta(hours=1),
                **arguments,
            )
    finally:
        reset_execution_identity(id_token)
        reset_resume_continuation_tool(tool_token)
    assert isinstance(parked, SuspendedInteraction)
    state = await InteractionStore(settings.key_prefix).get_state(fake_redis, parked.interaction_id)
    assert state is not None
    return state.request.format_payload


@pytest.mark.parametrize(("answer_format", "arguments"), _CASES)
async def test_the_answer_check_view_equals_what_the_live_door_stores(
    fake_redis, settings: InteractionsSettings, answer_format: str, arguments: dict[str, Any]
) -> None:
    stored = await _stored_payload(fake_redis, settings, answer_format, **arguments)
    if answer_format == "external":
        stored = {key: value for key, value in stored.items() if key not in {"url", "verifier"}} or None

    assert question_format(answer_format, **arguments) == QuestionFormat(
        answer_format=AnswerFormat(answer_format), format_payload=stored
    )


def test_an_external_view_carries_no_delivery_keys() -> None:
    assert question_format("external", schema=_INTEGER).format_payload == {"schema": _INTEGER}
    assert question_format("external").format_payload is None


def test_a_text_view_carries_its_suggested_replies_and_accepts_any_string() -> None:
    view = question_format("text", options=["yes please"])

    assert view.format_payload == {"options": ["yes please"]}
    assert check_answer(view, "something else entirely") == "something else entirely"


def test_a_free_view_carries_its_schema() -> None:
    assert question_format("free", schema=_INTEGER).format_payload == {"schema": _INTEGER}


@pytest.mark.parametrize(
    ("answer_format", "arguments", "match"),
    [
        ("essay", {}, "unknown answer_format: 'essay'"),
        ("confirm", {"options": ["a"]}, "options are not valid with answer_format 'confirm'"),
        ("select", {"data": {"values": {}}}, "data and pages are not valid with answer_format 'select'"),
        ("text", {"reactions": {"choices": []}}, "reaction_tool and reactions are not valid with answer_format 'text'"),
        ("select", {}, "answer_format 'select' requires options"),
        ("form", {}, "answer_format 'form' requires a schema"),
        ("form", {"schema": _FORM_SCHEMA, "data": {"values": {"undeclared": 1}}}, "unknown property 'undeclared'"),
    ],
    ids=[
        "unknown-format",
        "options-on-confirm",
        "data-on-select",
        "reactions-on-text",
        "select-no-options",
        "form-no-schema",
        "data-for-an-undeclared-property",
    ],
)
def test_an_argument_the_format_refuses_is_a_value_error(
    answer_format: str, arguments: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        question_format(answer_format, **arguments)


@pytest.mark.parametrize(
    ("answer_format", "arguments"),
    [
        ("select", {"options": "red"}),
        ("form", {"schema": _FORM_SCHEMA, "data": {"values": "count"}}),
    ],
    ids=["options-a-string", "data-values-not-a-map"],
)
def test_a_value_of_the_wrong_shape_is_a_value_error(answer_format: str, arguments: dict[str, Any]) -> None:
    # A pydantic ``ValidationError`` is the ``ValueError`` a wrong shape raises.
    with pytest.raises(ValidationError):
        question_format(answer_format, **arguments)


def test_the_view_takes_the_ask_tool_own_argument_types() -> None:
    ask_hints = get_type_hints(builtin.ask)
    assert get_type_hints(QuestionArguments) == {name: ask_hints[name] for name in get_type_hints(QuestionArguments)}


def test_a_neutral_consumer_checks_an_answer_through_the_facet_alone() -> None:
    # A consumer holding only a declared format: it asks the facet for the view, then checks
    # answers against it — never building a payload of its own.
    with tai42_app.bound(app):
        view = tai42_app.interactions.question_format("free", schema=_INTEGER)

        assert tai42_app.interactions.check_answer(view, 42) == 42
        with pytest.raises(AnswerMismatchError, match="is not of type 'integer'"):
            tai42_app.interactions.check_answer(view, "not an integer")
