"""Tests for the one ``wording`` catalogue — every sub-model's shape, the token rules, the
per-send admission on the ask and notify chokepoints, and the per-field cross-check."""

from __future__ import annotations

import re
from typing import Any

import pytest

from tai42_contract.interactions.models import (
    REJECTION_NOTICE_MAX_CHARS,
    AnswerPageWording,
    AnswerWording,
    ChoiceWording,
    CountdownWording,
    FieldWording,
    FormData,
    FormPageWording,
    FormWording,
    LineWording,
    PageCopy,
    StatusWording,
    Wording,
    check_form_wording_fields,
    check_wording_for_ask,
    check_wording_for_notification,
)
from tai42_contract.interactions.models.wording import ADMISSION

# === sub-model shape: non-blank, at-least-one-set, strict keys ==============


def test_wording_requires_at_least_one_chapter():
    with pytest.raises(ValueError, match=r"wording must set at least one chapter"):
        Wording()


def test_wording_refuses_an_unknown_chapter():
    with pytest.raises(ValueError, match=r"Extra inputs are not permitted"):
        Wording.model_validate({"nope": {"title": "X"}})


def test_page_copy_members_non_blank_at_least_one_and_strict():
    page = PageCopy(heading="Confirm", text="Tap to send")
    assert (page.heading, page.text) == ("Confirm", "Tap to send")
    with pytest.raises(ValueError, match=r"wording.answer_page page.heading must be non-blank when set"):
        PageCopy(heading="   ")
    with pytest.raises(ValueError, match=r"an answer page copy must set at least one of heading, text"):
        PageCopy()
    with pytest.raises(ValueError, match=r"Extra inputs are not permitted"):
        PageCopy.model_validate({"heading": "H", "nope": "x"})


def test_field_wording_non_blank_at_least_one_and_strict():
    assert FieldWording(choice_label="Pick one").choice_label == "Pick one"
    with pytest.raises(ValueError, match=r"wording.form.fields entry.choice_label must be non-blank when set"):
        FieldWording(choice_label="  ")
    with pytest.raises(ValueError, match=r"a form field wording must set at least one member"):
        FieldWording()
    with pytest.raises(ValueError, match=r"Extra inputs are not permitted"):
        FieldWording.model_validate({"nope": "x"})


def test_form_wording_non_blank_at_least_one_and_strict():
    form = FormWording(title="Formulario", open="Fill it in", submit="Go")
    assert (form.title, form.open, form.submit) == ("Formulario", "Fill it in", "Go")
    with pytest.raises(ValueError, match=r"wording.form.title must be non-blank when set"):
        FormWording(title="   ")
    with pytest.raises(ValueError, match=r"wording.form must set at least one member"):
        FormWording()
    with pytest.raises(ValueError, match=r"Extra inputs are not permitted"):
        FormWording.model_validate({"nope": "x"})
    # the per-field map alone satisfies at-least-one.
    assert FormWording(fields={"colour": FieldWording(choice_label="Pick")}).fields["colour"].choice_label == "Pick"


def test_choice_wording_non_blank_at_least_one_and_strict():
    choice = ChoiceWording(label="Open the list", reply_hint="Reply with one", group_label="Options")
    assert choice.group_label == "Options"
    with pytest.raises(ValueError, match=r"wording.choice.reply_hint must be non-blank when set"):
        ChoiceWording(reply_hint="  ")
    with pytest.raises(ValueError, match=r"wording.choice must set at least one of label, reply_hint, group_label"):
        ChoiceWording()
    with pytest.raises(ValueError, match=r"Extra inputs are not permitted"):
        ChoiceWording.model_validate({"nope": "x"})


def test_answer_wording_non_blank_at_least_one_and_strict():
    answer = AnswerWording(open="Answer", confirm="Yes", decline="No", submit="Send", reply_hint="Type here")
    assert (answer.decline, answer.submit) == ("No", "Send")
    with pytest.raises(ValueError, match=r"wording.answer.decline must be non-blank when set"):
        AnswerWording(decline="  ")
    with pytest.raises(ValueError, match=r"wording.answer must set at least one member"):
        AnswerWording()
    with pytest.raises(ValueError, match=r"Extra inputs are not permitted"):
        AnswerWording.model_validate({"nope": "x"})


def test_answer_wording_rejected_cap():
    assert AnswerWording(rejected="x" * REJECTION_NOTICE_MAX_CHARS).rejected is not None
    with pytest.raises(ValueError, match=f"wording.answer.rejected must be at most {REJECTION_NOTICE_MAX_CHARS}"):
        AnswerWording(rejected="x" * (REJECTION_NOTICE_MAX_CHARS + 1))
    with pytest.raises(ValueError, match=f"wording.answer.rejected_final must be at most {REJECTION_NOTICE_MAX_CHARS}"):
        AnswerWording(rejected_final="x" * (REJECTION_NOTICE_MAX_CHARS + 1))


def test_line_wording_tokens_non_blank_at_least_one_and_strict():
    lines = LineWording(
        answer_link="Answer here: {url}", deadline="By {deadline}", reply="Reply", options="Pick {options}"
    )
    assert lines.reply == "Reply"
    with pytest.raises(ValueError, match=re.escape("wording.lines.answer_link must contain the {url} token")):
        LineWording(answer_link="Answer here")
    with pytest.raises(ValueError, match=re.escape("wording.lines.deadline must contain the {deadline} token")):
        LineWording(deadline="By soon")
    with pytest.raises(ValueError, match=re.escape("wording.lines.options must contain the {options} token")):
        LineWording(options="Pick one")
    with pytest.raises(ValueError, match=r"wording.lines.reply must be non-blank when set"):
        LineWording(reply="  ")
    with pytest.raises(
        ValueError, match=r"wording.lines must set at least one of answer_link, deadline, reply, options"
    ):
        LineWording()
    with pytest.raises(ValueError, match=r"Extra inputs are not permitted"):
        LineWording.model_validate({"nope": "x"})


def test_form_page_wording_non_blank_at_least_one_and_strict():
    page = FormPageWording(
        failed="Did not send", network_error="No connection", update_failed="Could not refresh {reason}"
    )
    assert page.failed == "Did not send"
    with pytest.raises(ValueError, match=r"wording.answer_page.form.choose_one must be non-blank when set"):
        FormPageWording(choose_one="  ")
    with pytest.raises(ValueError, match=r"wording.answer_page.form must set at least one member"):
        FormPageWording()
    with pytest.raises(ValueError, match=r"Extra inputs are not permitted"):
        FormPageWording.model_validate({"nope": "x"})


def test_answer_page_wording_at_least_one_and_strict():
    page = AnswerPageWording(confirm=PageCopy(heading="Confirm"), form=FormPageWording(failed="No"))
    assert page.confirm is not None
    with pytest.raises(
        ValueError, match=r"wording.answer_page must set at least one of confirm, done, awaiting, submitted, form"
    ):
        AnswerPageWording()
    with pytest.raises(ValueError, match=r"Extra inputs are not permitted"):
        AnswerPageWording.model_validate({"nope": {"heading": "H"}})


def test_countdown_wording_seconds_token_non_blank_at_least_one_and_strict():
    countdown = CountdownWording(seconds_left="{seconds} left", one_second_left="1 left", under_minute="Under a minute")
    assert countdown.one_second_left == "1 left"
    with pytest.raises(
        ValueError, match=re.escape("wording.status.countdown.seconds_left must contain the {seconds} token")
    ):
        CountdownWording(seconds_left="a few left")
    with pytest.raises(ValueError, match=r"wording.status.countdown.under_minute must be non-blank when set"):
        CountdownWording(under_minute="  ")
    with pytest.raises(ValueError, match=r"wording.status.countdown must set at least one member"):
        CountdownWording()
    with pytest.raises(ValueError, match=r"Extra inputs are not permitted"):
        CountdownWording.model_validate({"nope": "x"})


def test_status_wording_non_blank_at_least_one_and_strict():
    status = StatusWording(sending="Sending", answered="Answered", countdown=CountdownWording(one_second_left="1 left"))
    assert status.sending == "Sending"
    with pytest.raises(ValueError, match=r"wording.status.answered must be non-blank when set"):
        StatusWording(answered="  ")
    with pytest.raises(ValueError, match=r"wording.status must set at least one member"):
        StatusWording()
    with pytest.raises(ValueError, match=r"Extra inputs are not permitted"):
        StatusWording.model_validate({"nope": "x"})


# === the admission matrix: every member on every context ====================

_MEMBER_VALUE: dict[str, Any] = {
    "form.title": "Title",
    "form.open": "Open",
    "form.cancel": "Cancel",
    "form.back": "Back",
    "form.submit": "Submit",
    "form.review_empty": "Nothing yet",
    "form.fields": {"colour": {"choice_label": "Pick"}},
    "choice.label": "Open the list",
    "choice.reply_hint": "Reply with one",
    "choice.group_label": "Options",
    "answer.open": "Answer",
    "answer.confirm": "Yes",
    "answer.decline": "No",
    "answer.submit": "Send",
    "answer.reply_hint": "Type here",
    "answer.rejected": "Try again {reason}",
    "answer.rejected_final": "Closed {reason}",
    "lines.answer_link": "Answer here: {url}",
    "lines.deadline": "By {deadline}",
    "lines.reply": "Reply on the channel",
    "lines.options": "Pick {options}",
    "answer_page.confirm": {"heading": "Confirm"},
    "answer_page.done": {"heading": "Done"},
    "answer_page.awaiting": {"heading": "Awaiting"},
    "answer_page.submitted": {"heading": "Submitted"},
    "answer_page.form": {"failed": "No"},
    "status.sending": "Sending",
    "status.answered": "Answered",
    "status.expired": "Expired",
    "status.closed": "Closed",
    "status.sent": "Sent",
    "status.unavailable": "Gone",
    "status.countdown": {"one_second_left": "1 left"},
}


def _one(path: str) -> Wording:
    chapter, member = path.split(".", 1)
    return Wording.model_validate({chapter: {member: _MEMBER_VALUE[path]}})


def test_member_value_map_covers_every_admitted_path():
    # A guard: every admission row has a sample value, so the matrix tests below cover the whole table.
    assert set(_MEMBER_VALUE) == set(ADMISSION)


@pytest.mark.parametrize("path", sorted(ADMISSION))
def test_admission_matrix_on_the_ask_chokepoint(path: str):
    wording = _one(path)
    # ``has_options`` isolates the admission from the text-with-options gate; ``has_pages`` False
    # isolates it from the form.submit-with-pages gate — both gates have their own tests.
    for answer_format in ("text", "confirm", "select", "form", "external"):
        if answer_format in ADMISSION[path]:
            check_wording_for_ask(wording, answer_format, has_options=True, has_pages=False)
        else:
            with pytest.raises(ValueError, match=re.escape(f"{answer_format} answer_format draws no wording.{path}")):
                check_wording_for_ask(wording, answer_format, has_options=True, has_pages=False)


@pytest.mark.parametrize("path", sorted(ADMISSION))
def test_admission_matrix_on_the_notify_chokepoint(path: str):
    wording = _one(path)
    for context, flags in (
        ("notify:form", {"has_schema": True, "has_options": False, "has_sections": False, "has_pages": False}),
        ("notify:options", {"has_schema": False, "has_options": True, "has_sections": False, "has_pages": False}),
        ("notify:sections", {"has_schema": False, "has_options": False, "has_sections": True, "has_pages": False}),
    ):
        if context in ADMISSION[path]:
            check_wording_for_notification(wording, **flags)
        else:
            with pytest.raises(ValueError, match=re.escape(f"{context} draws no wording.{path}")):
                check_wording_for_notification(wording, **flags)


def test_notify_with_no_surface_refuses_every_member():
    with pytest.raises(
        ValueError, match=r"a notification with no choice or form surface draws no wording.choice.label"
    ):
        check_wording_for_notification(
            _one("choice.label"), has_schema=False, has_options=False, has_sections=False, has_pages=False
        )


# === the two conditional gates ==============================================


def test_text_options_gated_members_need_suggested_options():
    for path in ("choice.reply_hint", "lines.options"):
        wording = _one(path)
        check_wording_for_ask(wording, "text", has_options=True, has_pages=False)
        with pytest.raises(
            ValueError, match=re.escape(f"text answer_format draws no wording.{path} without suggested")
        ):
            check_wording_for_ask(wording, "text", has_options=False, has_pages=False)


def test_form_submit_refused_with_pages_accepted_without():
    wording = _one("form.submit")
    check_wording_for_ask(wording, "form", has_options=False, has_pages=False)
    with pytest.raises(ValueError, match=r"wording.form.submit is refused with pages"):
        check_wording_for_ask(wording, "form", has_options=False, has_pages=True)


def test_form_submit_refused_with_pages_on_the_notify_form_too():
    wording = _one("form.submit")
    check_wording_for_notification(wording, has_schema=True, has_options=False, has_sections=False, has_pages=False)
    with pytest.raises(ValueError, match=r"wording.form.submit is refused with pages"):
        check_wording_for_notification(wording, has_schema=True, has_options=False, has_sections=False, has_pages=True)


# === the per-field cross-check ==============================================


def _schema(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props}


def test_form_wording_fields_accepts_enum_and_options_bearing_fields():
    schema = _schema(
        {
            "size": {"type": "string", "enum": ["s", "m", "l"]},
            "tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
            "colour": {"type": "string"},
            "picks": {"type": "array", "items": {"type": "string"}},
        }
    )
    data = FormData.model_validate({"options": {"colour": [{"value": "red"}], "picks": [{"value": "x"}]}})
    fields = {
        "size": FieldWording(choice_label="Pick a size"),
        "tags": FieldWording(choice_label="Pick tags"),
        "colour": FieldWording(choice_label="Pick a colour"),
        "picks": FieldWording(choice_label="Pick some"),
    }
    check_form_wording_fields(schema, data, fields)


def test_form_wording_fields_refuses_a_free_string_field():
    # A plain string with no enum and no per-send options draws a free input, not a choice list.
    schema = _schema({"note": {"type": "string"}})
    with pytest.raises(ValueError, match=r"wording.form.fields names 'note', which draws no choice list"):
        check_form_wording_fields(schema, None, {"note": FieldWording(choice_label="Pick")})


def test_form_wording_fields_refuses_an_unknown_property():
    schema = _schema({"size": {"type": "string", "enum": ["s", "m"]}})
    with pytest.raises(ValueError, match=r"wording.form.fields names 'ghost', which is not a declared property"):
        check_form_wording_fields(schema, None, {"ghost": FieldWording(choice_label="Pick")})


# === the per-field ``invalid`` sentence =====================================


def test_field_wording_invalid_round_trips_alone_and_refuses_blank():
    wording = FieldWording(invalid="Enter a valid email address")
    assert wording.invalid == "Enter a valid email address"
    assert wording.choice_label is None
    assert FieldWording.model_validate(wording.model_dump()) == wording
    with pytest.raises(ValueError, match=r"wording.form.fields entry.invalid must be non-blank when set"):
        FieldWording(invalid="   ")


@pytest.mark.parametrize(
    "prop",
    [
        {"type": "string", "pattern": "^[a-z]+$"},
        {"type": "string", "format": "email"},
        {"type": "integer", "minimum": 1},
        {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}, "minItems": 1},
        {"type": "string", "format": "date", "minDate": "2026-01-01"},
    ],
)
def test_form_wording_fields_admits_invalid_on_a_field_with_a_shape(prop: dict[str, Any]):
    check_form_wording_fields(_schema({"field": prop}), None, {"field": FieldWording(invalid="Not right")})


def test_form_wording_fields_refuses_invalid_on_a_field_with_no_shape():
    schema = _schema({"note": {"type": "string"}})
    with pytest.raises(ValueError, match=r"wording.form.fields names 'note', which declares no shape to break"):
        check_form_wording_fields(schema, None, {"note": FieldWording(invalid="Not right")})


def test_form_wording_fields_checks_each_member_by_its_own_rule():
    # ``choice_label`` keeps its "draws a choice list" rule even beside an admitted ``invalid``.
    schema = _schema({"code": {"type": "string", "pattern": "^[0-9]+$"}})
    with pytest.raises(ValueError, match=r"wording.form.fields names 'code', which draws no choice list"):
        check_form_wording_fields(schema, None, {"code": FieldWording(choice_label="Pick", invalid="Digits only")})
