"""The one catalogue of a send's platform-drawn texts — the ``Wording`` model and its admission.

``Wording`` carries the caller's words for ONE send: a question's or a notification's buttons,
headings, input hints, message lines, answer-page sentences, refusal sentences, settled-card
badges and screen-reader labels, grouped into chapters (``form``, ``choice``, ``answer``,
``lines``, ``answer_page``, ``status``). :data:`ADMISSION` records which member a given send may
draw; :func:`check_wording_for_ask` and :func:`check_wording_for_notification` consult it so a
member that rides a send it cannot be drawn on is refused loudly rather than silently dropped.
"""

from __future__ import annotations

from collections.abc import Iterator

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

#: Cap on a caller-set refusal sentence (``answer.rejected`` / ``answer.rejected_final``) — the
#: participant-facing text drawn when an answer is refused. A single participant-facing message,
#: so a small bound; channels impose their own tighter message caps. Matches the conversation
#: route's ``error_reply_text`` bound.
REJECTION_NOTICE_MAX_CHARS = 2000


def _member_non_blank(value: str | None, info: ValidationInfo, prefix: str) -> str | None:
    # A member either names the surface's own default (``None``) or carries the caller's own
    # words; an empty or whitespace-only value names nothing and is a caller bug.
    if value is not None and not value.strip():
        raise ValueError(f"{prefix}.{info.field_name} must be non-blank when set")
    return value


class PageCopy(BaseModel):
    """The heading and sentence of one platform-drawn answer page. Frozen, strict keys.

    ``heading`` is the page's ``<title>`` and ``<h1>``; ``text`` is its one sentence. Each is
    the sender's own words, in the end-user's language; absent, the page shows its own generic
    word. A set value is non-blank; an empty object names nothing and is refused; an unknown key
    is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    heading: str | None = None
    text: str | None = None

    @field_validator("heading", "text")
    @classmethod
    def _non_blank(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _member_non_blank(value, info, "wording.answer_page page")

    @model_validator(mode="after")
    def _at_least_one_set(self) -> PageCopy:
        if self.heading is None and self.text is None:
            raise ValueError("an answer page copy must set at least one of heading, text")
        return self


class FieldWording(BaseModel):
    """The caller's words for one form field's platform-drawn controls. Frozen, strict keys.

    ``choice_label`` is the placeholder of the field's picker (its empty-state prompt); ``invalid``
    is the sentence shown when the field's value does not have its declared shape. Each is the
    sender's own words, in the end-user's language; absent, the surface shows its own generic word
    (for ``invalid``, its own word for the failing shape). A set value is non-blank; an empty object
    names nothing and is refused; an unknown key is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    choice_label: str | None = None
    invalid: str | None = None

    @field_validator("choice_label", "invalid")
    @classmethod
    def _non_blank(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _member_non_blank(value, info, "wording.form.fields entry")

    @model_validator(mode="after")
    def _at_least_one_set(self) -> FieldWording:
        if self.choice_label is None and self.invalid is None:
            raise ValueError("a form field wording must set at least one member")
        return self


class FormWording(BaseModel):
    """The caller's words for a form's platform-drawn chrome, for ONE send. Frozen, strict keys.

    ``title`` heads the form surface; ``open`` labels the button that opens the form; ``cancel``
    labels its cancel/close; ``back`` labels the button that returns to the previous step of a
    stepped form; ``submit`` labels the terminal button of a form with no pages; ``review_empty``
    is the line the review step shows before anything is filled; ``fields`` carries per-field
    words keyed by property name. Each text is the sender's own words, in the end-user's language;
    absent, the surface shows its own generic word. A set value is non-blank; an empty object names
    nothing and is refused; an unknown key is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str | None = None
    open: str | None = None
    cancel: str | None = None
    back: str | None = None
    submit: str | None = None
    review_empty: str | None = None
    fields: dict[str, FieldWording] = {}

    @field_validator("title", "open", "cancel", "back", "submit", "review_empty")
    @classmethod
    def _non_blank(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _member_non_blank(value, info, "wording.form")

    @model_validator(mode="after")
    def _at_least_one_set(self) -> FormWording:
        if (
            self.title is None
            and self.open is None
            and self.cancel is None
            and self.back is None
            and self.submit is None
            and self.review_empty is None
            and not self.fields
        ):
            raise ValueError("wording.form must set at least one member")
        return self


class ChoiceWording(BaseModel):
    """The caller's words for a choice list, for ONE send. Frozen, strict keys.

    ``label`` is the word on the control that presents the list (the button or control that opens
    it); ``reply_hint`` is the line under a list rendered as numbered text telling the end user how
    to reply; ``group_label`` is the accessible name of a flat group of option buttons. Each text
    is the sender's own words, in the end-user's language; absent, the surface shows its own generic
    word. A set value is non-blank; an empty object names nothing and is refused; an unknown key is
    refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str | None = None
    reply_hint: str | None = None
    group_label: str | None = None

    @field_validator("label", "reply_hint", "group_label")
    @classmethod
    def _non_blank(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _member_non_blank(value, info, "wording.choice")

    @model_validator(mode="after")
    def _at_least_one_set(self) -> ChoiceWording:
        if self.label is None and self.reply_hint is None and self.group_label is None:
            raise ValueError("wording.choice must set at least one of label, reply_hint, group_label")
        return self


class AnswerWording(BaseModel):
    """The caller's words for a non-form question's answer controls and refusal sentences. Frozen, strict keys.

    ``open`` labels the control that opens the question's answer page; ``confirm`` labels its
    affirmative control; ``decline`` labels its negative control; ``submit`` labels the button that
    sends a typed or picked answer; ``reply_hint`` is the hint shown in an empty reply input.
    ``rejected`` is the sentence drawn when an answer is refused and may be given again; its
    OPTIONAL ``{reason}`` token takes the door's reason by a plain substitution. ``rejected_final``
    is the sentence drawn when a refused answer also closes the question; it too takes an OPTIONAL
    ``{reason}``. Each is the sender's own words, in the end-user's language; absent, the surface
    shows its own generic word. A set value is non-blank; ``rejected`` / ``rejected_final`` are
    bounded by :data:`REJECTION_NOTICE_MAX_CHARS`; an empty object names nothing and is refused; an
    unknown key is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    open: str | None = None
    confirm: str | None = None
    decline: str | None = None
    submit: str | None = None
    reply_hint: str | None = None
    rejected: str | None = None
    rejected_final: str | None = None

    @field_validator("open", "confirm", "decline", "submit", "reply_hint", "rejected", "rejected_final")
    @classmethod
    def _non_blank(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _member_non_blank(value, info, "wording.answer")

    @field_validator("rejected", "rejected_final")
    @classmethod
    def _within_cap(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and len(value) > REJECTION_NOTICE_MAX_CHARS:
            raise ValueError(
                f"wording.answer.{info.field_name} must be at most {REJECTION_NOTICE_MAX_CHARS} characters, "
                f"got {len(value)}"
            )
        return value

    @model_validator(mode="after")
    def _at_least_one_set(self) -> AnswerWording:
        if (
            self.open is None
            and self.confirm is None
            and self.decline is None
            and self.submit is None
            and self.reply_hint is None
            and self.rejected is None
            and self.rejected_final is None
        ):
            raise ValueError("wording.answer must set at least one member")
        return self


class LineWording(BaseModel):
    """The caller's words for the lines of a delivery message, for ONE send. Frozen, strict keys.

    ``answer_link`` is the sentence carrying the tappable answer link and MUST contain the ``{url}``
    token (the channel fills it by a plain substitution); ``deadline`` names the deadline and MUST
    contain ``{deadline}`` (the channel fills it with its own formatted instant); ``reply`` tells
    the end user how to reply and carries no token; ``options`` lists the choices and MUST contain
    ``{options}`` (the channel fills it with its own joined list). Each is the sender's own words,
    in the end-user's language; absent, the surface shows its own generic word. A set value is
    non-blank; an empty object names nothing and is refused; an unknown key is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer_link: str | None = None
    deadline: str | None = None
    reply: str | None = None
    options: str | None = None

    @field_validator("answer_link", "deadline", "reply", "options")
    @classmethod
    def _non_blank(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _member_non_blank(value, info, "wording.lines")

    @field_validator("answer_link")
    @classmethod
    def _answer_link_token(cls, value: str | None) -> str | None:
        if value is not None and "{url}" not in value:
            raise ValueError("wording.lines.answer_link must contain the {url} token")
        return value

    @field_validator("deadline")
    @classmethod
    def _deadline_token(cls, value: str | None) -> str | None:
        if value is not None and "{deadline}" not in value:
            raise ValueError("wording.lines.deadline must contain the {deadline} token")
        return value

    @field_validator("options")
    @classmethod
    def _options_token(cls, value: str | None) -> str | None:
        if value is not None and "{options}" not in value:
            raise ValueError("wording.lines.options must contain the {options} token")
        return value

    @model_validator(mode="after")
    def _at_least_one_set(self) -> LineWording:
        if self.answer_link is None and self.deadline is None and self.reply is None and self.options is None:
            raise ValueError("wording.lines must set at least one of answer_link, deadline, reply, options")
        return self


class FormPageWording(BaseModel):
    """The caller's words for the form page's own sentences, for ONE send. Frozen, strict keys.

    ``failed`` is the line when the send did not land; ``network_error`` is the line when the
    browser could not reach the server; ``update_failed`` is the line when a reacting form could
    not refresh and takes an OPTIONAL ``{reason}`` token; ``choose_one`` is the validation line for
    a required multi-choice left empty; ``date_unavailable`` is the validation line for a blocked
    date. Each is the sender's own words, in the end-user's language; absent, the page shows its own
    generic word. A set value is non-blank; an empty object names nothing and is refused; an unknown
    key is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    failed: str | None = None
    network_error: str | None = None
    update_failed: str | None = None
    choose_one: str | None = None
    date_unavailable: str | None = None

    @field_validator("failed", "network_error", "update_failed", "choose_one", "date_unavailable")
    @classmethod
    def _non_blank(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _member_non_blank(value, info, "wording.answer_page.form")

    @model_validator(mode="after")
    def _at_least_one_set(self) -> FormPageWording:
        if (
            self.failed is None
            and self.network_error is None
            and self.update_failed is None
            and self.choose_one is None
            and self.date_unavailable is None
        ):
            raise ValueError("wording.answer_page.form must set at least one member")
        return self


class AnswerPageWording(BaseModel):
    """The caller's words for the pages a question's link opens, for ONE send. Frozen, strict keys.

    ``confirm`` is the confirm page's copy; ``done`` the already-answered page's; ``awaiting`` the
    reply-on-the-channel page's; ``submitted`` the form page's post-submit copy; ``form`` carries the
    form page's own validation/error sentences. Each is drawn where a surface has that page or
    sentence. An empty object names nothing and is refused; an unknown key is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    confirm: PageCopy | None = None
    done: PageCopy | None = None
    awaiting: PageCopy | None = None
    submitted: PageCopy | None = None
    form: FormPageWording | None = None

    @model_validator(mode="after")
    def _at_least_one_set(self) -> AnswerPageWording:
        if (
            self.confirm is None
            and self.done is None
            and self.awaiting is None
            and self.submitted is None
            and self.form is None
        ):
            raise ValueError("wording.answer_page must set at least one of confirm, done, awaiting, submitted, form")
        return self


class CountdownWording(BaseModel):
    """The caller's words for a live question card's countdown, for ONE send. Frozen, strict keys.

    ``seconds_left`` is the visible plural line and MUST contain the ``{seconds}`` token (the card
    fills it with the number remaining); ``one_second_left`` is the visible singular line;
    ``under_minute`` / ``under_half_minute`` / ``under_ten_seconds`` are the spoken lines at the
    three thresholds. Each is the sender's own words, in the end-user's language; absent, the card
    shows its own generic word. A set value is non-blank; an empty object names nothing and is
    refused; an unknown key is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seconds_left: str | None = None
    one_second_left: str | None = None
    under_minute: str | None = None
    under_half_minute: str | None = None
    under_ten_seconds: str | None = None

    @field_validator("seconds_left", "one_second_left", "under_minute", "under_half_minute", "under_ten_seconds")
    @classmethod
    def _non_blank(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _member_non_blank(value, info, "wording.status.countdown")

    @field_validator("seconds_left")
    @classmethod
    def _seconds_left_token(cls, value: str | None) -> str | None:
        if value is not None and "{seconds}" not in value:
            raise ValueError("wording.status.countdown.seconds_left must contain the {seconds} token")
        return value

    @model_validator(mode="after")
    def _at_least_one_set(self) -> CountdownWording:
        if (
            self.seconds_left is None
            and self.one_second_left is None
            and self.under_minute is None
            and self.under_half_minute is None
            and self.under_ten_seconds is None
        ):
            raise ValueError("wording.status.countdown must set at least one member")
        return self


class StatusWording(BaseModel):
    """The caller's words for in-flight and settled status copy, for ONE send. Frozen, strict keys.

    ``sending`` is the screen-reader label while a send is in flight; ``answered`` / ``expired`` /
    ``closed`` are the badges once a question settles; ``sent`` is the badge after an ask-less form
    is submitted; ``unavailable`` is the line when a form's door no longer resolves; ``countdown``
    carries the live-countdown words. Each is the sender's own words, in the end-user's language;
    absent, the surface shows its own generic word. A set value is non-blank; an empty object names
    nothing and is refused; an unknown key is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sending: str | None = None
    answered: str | None = None
    expired: str | None = None
    closed: str | None = None
    sent: str | None = None
    unavailable: str | None = None
    countdown: CountdownWording | None = None

    @field_validator("sending", "answered", "expired", "closed", "sent", "unavailable")
    @classmethod
    def _non_blank(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _member_non_blank(value, info, "wording.status")

    @model_validator(mode="after")
    def _at_least_one_set(self) -> StatusWording:
        if (
            self.sending is None
            and self.answered is None
            and self.expired is None
            and self.closed is None
            and self.sent is None
            and self.unavailable is None
            and self.countdown is None
        ):
            raise ValueError("wording.status must set at least one member")
        return self


class Wording(BaseModel):
    """The one catalogue of a send's platform-drawn texts, for ONE send. Frozen, strict keys.

    Every platform-drawn text of a question or a notification — a button, a heading, an input hint,
    a line of the message, a sentence of an answer page, a screen-reader label, the sentence that
    tells the end user their answer was refused, a settled card's badge — carries the caller's
    words, the platform's word as the default. ``wording`` is the catalogue of those words for ONE
    send, in the end-user's language, grouped by what each word decorates: the form (``form``), the
    choice list (``choice``), a question's answer controls and refusal sentences (``answer``), the
    lines of the delivery message (``lines``), the pages a question's link opens (``answer_page``),
    and in-flight and settled status (``status``). A page's primary button is the one text not here:
    it is on the page (``pages[].button_label``). Each member is drawn where a surface has that
    control or sentence and is admitted only on a send that draws it (:data:`ADMISSION`); a surface
    with no such control draws nothing for it. A set value is non-blank; an empty chapter names
    nothing and is refused; an unknown key is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    form: FormWording | None = None
    choice: ChoiceWording | None = None
    answer: AnswerWording | None = None
    lines: LineWording | None = None
    answer_page: AnswerPageWording | None = None
    status: StatusWording | None = None

    @model_validator(mode="after")
    def _at_least_one_chapter(self) -> Wording:
        if (
            self.form is None
            and self.choice is None
            and self.answer is None
            and self.lines is None
            and self.answer_page is None
            and self.status is None
        ):
            raise ValueError("wording must set at least one chapter")
        return self


# The five channel-deliverable ask formats and the three notification surfaces a member may ride.
# ``notify:form`` is a notification carrying a form ``schema``; ``notify:options`` a flat option
# list; ``notify:sections`` a sectioned option list.
_ASK_CONTEXTS = frozenset({"text", "confirm", "select", "form", "external"})

#: Which send each ``wording`` member may be drawn on, keyed by its dotted member path. A member on
#: a send not listed draws nothing there, so it is refused loudly rather than silently dropped.
ADMISSION: dict[str, frozenset[str]] = {
    # ``form`` — the form surface (a ``form`` ask; a notification carrying a ``schema``).
    "form.title": frozenset({"form", "notify:form"}),
    "form.open": frozenset({"form", "notify:form"}),
    "form.cancel": frozenset({"form", "notify:form"}),
    "form.back": frozenset({"form", "notify:form"}),
    "form.submit": frozenset({"form", "notify:form"}),
    "form.review_empty": frozenset({"form", "notify:form"}),
    "form.fields": frozenset({"form", "notify:form"}),
    # ``choice`` — the choice list.
    "choice.label": frozenset({"select", "notify:options", "notify:sections"}),
    "choice.reply_hint": frozenset({"text", "select", "notify:options", "notify:sections"}),
    "choice.group_label": frozenset({"select", "notify:options"}),
    # ``answer`` — a non-form question's answer controls and refusal sentences.
    "answer.open": frozenset({"confirm", "external"}),
    "answer.confirm": frozenset({"confirm", "external"}),
    "answer.decline": frozenset({"confirm"}),
    "answer.submit": frozenset({"text", "select"}),
    "answer.reply_hint": frozenset({"text"}),
    "answer.rejected": frozenset({"text", "confirm", "select", "form"}),
    "answer.rejected_final": frozenset({"text", "confirm", "select", "form"}),
    # ``lines`` — the lines of the delivery message.
    "lines.answer_link": frozenset({"confirm", "external"}),
    "lines.deadline": frozenset({"text", "confirm", "select", "form", "external"}),
    "lines.reply": frozenset({"text", "select"}),
    "lines.options": frozenset({"text", "select"}),
    # ``answer_page`` — the pages a question's link opens.
    "answer_page.confirm": frozenset({"confirm", "external"}),
    "answer_page.done": frozenset({"text", "confirm", "select", "form", "external"}),
    "answer_page.awaiting": frozenset({"text", "select"}),
    "answer_page.submitted": frozenset({"form"}),
    "answer_page.form": frozenset({"form"}),
    # ``status`` — in-flight and settled status copy.
    "status.sending": frozenset({"text", "confirm", "select", "form", "external", "notify:form"}),
    "status.answered": frozenset({"text", "confirm", "select", "form", "external"}),
    "status.expired": frozenset({"text", "confirm", "select", "form", "external"}),
    "status.closed": frozenset({"text", "confirm", "select", "form", "external", "notify:form"}),
    "status.sent": frozenset({"notify:form"}),
    "status.unavailable": frozenset({"notify:form"}),
    "status.countdown": frozenset({"text", "confirm", "select", "form", "external"}),
}

#: Members admitted on a ``text`` ask ONLY when it carries suggested ``options`` — a text ask with
#: no options draws no choice list, so these name nothing there.
_OPTIONS_GATED_ON_TEXT = frozenset({"choice.reply_hint", "lines.options"})


def _set_member_paths(wording: Wording) -> Iterator[str]:
    # Yield the dotted path of every member that carries a value — a chapter member set to a word,
    # a nested sub-model, or a non-empty ``form.fields`` map. The admission is decided at this
    # granularity (the chapter's direct member), never at the leaves of a nested sub-model.
    for chapter_name, chapter in wording:
        if chapter is None:
            continue
        for member_name, value in chapter:
            if value is None:
                continue
            if isinstance(value, dict) and not value:
                continue
            yield f"{chapter_name}.{member_name}"


def check_wording_for_ask(wording: Wording, answer_format: str, *, has_options: bool, has_pages: bool) -> None:
    """Refuse a ``wording`` member the ask's ``answer_format`` draws no control or sentence for.

    ``answer_format`` is one of the five channel-deliverable formats. Each set member is checked
    against :data:`ADMISSION`; a member outside the format's set names a control that format never
    draws and is refused loudly. ``choice.reply_hint`` and ``lines.options`` ride a ``text`` ask
    only when it carries suggested ``options`` (``has_options``); ``form.submit`` is refused beside
    ``pages`` (``has_pages``) — a paged form's terminal button is the last page's ``button_label``.
    Raises ``ValueError`` naming the member and the context.
    """
    for path in _set_member_paths(wording):
        if answer_format not in ADMISSION[path]:
            raise ValueError(f"{answer_format} answer_format draws no wording.{path}")
        if answer_format == "text" and path in _OPTIONS_GATED_ON_TEXT and not has_options:
            raise ValueError(f"text answer_format draws no wording.{path} without suggested options")
        if path == "form.submit" and answer_format == "form" and has_pages:
            raise ValueError("wording.form.submit is refused with pages (a paged form's terminal button is a page's)")


def check_wording_for_notification(
    wording: Wording, *, has_schema: bool, has_options: bool, has_sections: bool, has_pages: bool
) -> None:
    """Refuse a ``wording`` member the notification's surface draws no control or sentence for.

    A notification draws its words from ONE interactive surface: a form (``schema``), a flat option
    list (``options``) or a sectioned option list (``sections``). The surface fixes the context
    (``notify:form`` / ``notify:options`` / ``notify:sections``); each set member is checked against
    :data:`ADMISSION` for it, and every member is refused on a notification with no such surface.
    ``form.submit`` is refused beside ``pages`` (``has_pages``) — a paged form's terminal button is
    the last page's ``button_label``. Raises ``ValueError`` naming the member and the surface.
    """
    if has_schema:
        context: str | None = "notify:form"
    elif has_options:
        context = "notify:options"
    elif has_sections:
        context = "notify:sections"
    else:
        context = None
    for path in _set_member_paths(wording):
        if context is None or context not in ADMISSION[path]:
            surface = context if context is not None else "a notification with no choice or form surface"
            raise ValueError(f"{surface} draws no wording.{path}")
        if path == "form.submit" and context == "notify:form" and has_pages:
            raise ValueError("wording.form.submit is refused with pages (a paged form's terminal button is a page's)")
