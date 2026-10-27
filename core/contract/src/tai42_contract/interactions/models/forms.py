"""Per-send form models and their against-schema validation.

``FormOption``/``FormData``/``FormPage`` are the per-send data layered over a form's
published schema; ``DisplayBlock`` is an ordered display element (heading/body/image) a
page shows beside its input fields.
``check_form_data``/``check_form_pages``/``check_form_reactions`` cross-check that data
against the schema (prefilled values, per-send option lists, one-page-per-property
coverage with unique display slots, and reaction triggers that name real fields/pages).
The reaction triggers and their events live in
:mod:`tai42_contract.interactions.models.form_reactions`; the vocabulary of a field whose value
breaks its declared shape lives in :mod:`tai42_contract.interactions.models.form_shape_errors`.
``__proto__`` is the one name a form refuses for a top-level property or a display slot:
JavaScript reads it as an object's prototype, so a form drawn in a browser cannot keep it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from tai42_contract.interactions.models.form_dates import check_time_pages
from tai42_contract.interactions.models.form_reactions import FormReactions
from tai42_contract.interactions.models.form_shape_errors import SHAPE_KEYWORDS

if TYPE_CHECKING:
    from tai42_contract.interactions.models.wording import FieldWording

_SCALAR_FORM_TYPES = ("string", "boolean", "integer", "number")

#: Abuse bound on a caller-set per-send ``form_tag`` — an opaque correlation the caller
#: attaches to a form notification and the platform hands back on submit, never interprets.
#: It is a short transport value every form channel embeds VERBATIM in its own carry (a
#: WhatsApp flow-token segment, a web form record, an in-band answer part), so it is bounded,
#: not a free body.
FORM_TAG_MAX_CHARS = 64
#: A ``form_tag`` is drawn ONLY from the RFC3986 unreserved set (``A-Z a-z 0-9 - . _ ~``) and
#: is 1..``FORM_TAG_MAX_CHARS`` characters. The set excludes ``:`` so a channel that packs the
#: tag into a colon-delimited token (the WhatsApp flow token) keeps it a single segment.
FORM_TAG_RE = re.compile(rf"^[A-Za-z0-9_.~-]{{1,{FORM_TAG_MAX_CHARS}}}$")


def check_form_tag(value: str) -> str:
    """Validate a caller-set opaque per-send ``form_tag`` and return it unchanged.

    The platform NEVER interprets the value; it only bounds the transport so every channel
    embeds the tag verbatim in its carry. A tag is non-blank, at most ``FORM_TAG_MAX_CHARS``
    characters, and drawn only from the RFC3986 unreserved set (``A-Z a-z 0-9 - . _ ~`` —
    excludes ``:``). Raises ``ValueError`` naming the BOUND, never the value (the value is
    opaque and may be sensitive).
    """
    if not FORM_TAG_RE.fullmatch(value):
        raise ValueError(
            f"form_tag must be 1 to {FORM_TAG_MAX_CHARS} characters from the RFC3986 unreserved "
            f"set (A-Z a-z 0-9 and - . _ ~)"
        )
    return value


class FormInputMode(StrEnum):
    """The kind of text a form ``string`` field collects — the keyboard a channel draws for it.

    It is a rendering hint, never a validator: the value is always the string typed
    (``numeric`` keeps ``"007"`` as ``"007"``, never a number; a value that may hold letters
    is ``text``), and the answer door still enforces only the schema's ``type``, ``pattern``
    and string ``format``s. A channel draws the kind's control where it has one and degrades to
    a plain text field by its documented rule where it does not. The vocabulary is the open
    HTML ``inputmode`` set, so no one channel's naming enters the platform.
    """

    TEXT = "text"
    NUMERIC = "numeric"
    DECIMAL = "decimal"
    TEL = "tel"
    EMAIL = "email"
    URL = "url"


class FormControl(StrEnum):
    """The control a surface draws for a form property in place of its shape's default."""

    CHIPS = "chips"
    TEXTAREA = "textarea"


class FormOption(BaseModel):
    """One per-send choice for a form field. Frozen.

    ``value`` is the string submitted as the answer, ``label`` (when set) is shown
    to the human in its place, and ``description`` (when set) is an OPTIONAL secondary
    line shown under ``label`` — a surface keeps a short label and moves the rest (a
    time, a price) onto the second line. A per-send option list REPLACES a property's
    schema ``enum`` for ONE send — the published form is unchanged, so a variant needs
    no re-publish. The ``description`` carries content, so a surface whose control has
    no native second-line affordance draws it by that surface's own documented rule —
    never silently drops it; a per-medium length cap is each channel's own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str
    label: str | None = None
    description: str | None = None

    @field_validator("value")
    @classmethod
    def _value_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("form option value must be non-blank")
        return value

    @field_validator("label")
    @classmethod
    def _label_non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("form option label must be non-blank when present")
        return value

    @field_validator("description")
    @classmethod
    def _description_non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("form option description must be non-blank when present")
        return value


class FormData(BaseModel):
    """Per-send data layered over a form's published schema for ONE send.

    ``values`` prefills top-level properties — each entry keyed by property name,
    its value shown filled in and validated against that property's schema.
    ``options`` supplies a per-send choice list for a property whose schema is a
    string (or an array of strings), keyed by property name: the list REPLACES that
    property's ``enum`` for this send only (labels shown, values submitted). The
    model holds only the shape; the cross-check against the schema (unknown
    property, a value that fails its schema, options on a non-string property, an
    empty list) is done once by the interaction request. Frozen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    values: dict[str, Any] = {}
    options: dict[str, list[FormOption]] = {}


class DisplayBlock(BaseModel):
    """One ordered display element shown on a form page beside its input fields. Frozen.

    ``kind`` chooses the element: ``heading`` and ``body`` carry ``text``; ``image``
    carries a ``src`` (its reference) and an optional ``alt`` (the text a surface that
    cannot draw the image shows instead). A block is either STATIC — its ``text``/``src``
    is given here — or declares a ``slot``: a name a form reaction's ``display`` update
    fills while the form is open (a computed total is a display slot). A static block
    requires its content; a slotted block may omit it (the reaction supplies it). A
    block carries only the content its ``kind`` allows (no ``src``/``alt`` on text, no
    ``text`` on an image). Raises ``ValueError`` on any ill-formed combination.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["heading", "body", "image"]
    text: str | None = None
    src: str | None = None
    alt: str | None = None
    slot: str | None = None

    @model_validator(mode="after")
    def _check_content(self) -> DisplayBlock:
        if self.slot is not None and not self.slot.strip():
            raise ValueError("display block slot must be non-blank when present")
        if self.kind == "image":
            if self.text is not None:
                raise ValueError("display block of kind 'image' carries src/alt, not text")
            if self.src is not None and not self.src.strip():
                raise ValueError("display block src must be non-blank when present")
            if self.alt is not None and not self.alt.strip():
                raise ValueError("display block alt must be non-blank when present")
            if self.slot is None and not self.src:
                raise ValueError("display block of kind 'image' requires a src (or a slot a reaction fills)")
        else:
            if self.src is not None or self.alt is not None:
                raise ValueError(f"display block of kind {self.kind!r} carries text, not src/alt")
            if self.text is not None and not self.text.strip():
                raise ValueError("display block text must be non-blank when present")
            if self.slot is None and not self.text:
                raise ValueError(f"display block of kind {self.kind!r} requires text (or a slot a reaction fills)")
        return self


class FormPage(BaseModel):
    """One step of a stepped form. Frozen.

    ``title`` heads the step. ``fields`` names the top-level properties collected on an
    INPUT page (``kind == "input"``, the default); a REVIEW page (``kind == "review"``)
    carries NO input fields (a terminal/summary step) and so has an empty ``fields``.
    ``display`` is the ordered list of :class:`DisplayBlock` shown on the page. Across a
    form's ``pages`` every property appears exactly once on the INPUT pages (the
    interaction request enforces the coverage); absent ``pages`` means one page.

    ``button_label`` is the text of the page's primary button — the one that advances from
    an input page or submits from the last — in the sender's own words and the end-user's
    language; absent, the surface shows its own generic word. A page's primary button is
    labelled HERE, on the page; the texts that are not a page's — the one that opens the form,
    the one that cancels it, and every other platform-drawn word of the send — are the wording
    catalogue's (``wording.form`` and its sibling chapters). The platform's word is the default
    on every one of them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    fields: list[str]
    display: list[DisplayBlock] = []
    kind: Literal["input", "review"] = "input"
    button_label: str | None = None

    @field_validator("title")
    @classmethod
    def _title_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("form page title must be non-blank")
        return value

    @field_validator("button_label")
    @classmethod
    def _button_label_non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("form page button_label must be non-blank when set")
        return value

    @model_validator(mode="after")
    def _check_fields_for_kind(self) -> FormPage:
        # An input page collects at least one field; a review page carries none (it is a
        # terminal summary step). An empty ``fields`` is therefore valid iff the page is a
        # review page, and a review page naming fields is a caller bug.
        if self.kind == "review":
            if self.fields:
                raise ValueError(f"review form page {self.title!r} carries no input fields")
        elif not self.fields:
            raise ValueError("form page fields must be a non-empty list")
        return self


def _schema_properties(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    # The form's top-level ``properties`` as a typed map of name -> property schema.
    # An absent / non-object ``properties`` yields an empty map; a property whose own
    # schema is not an object is dropped (a value/option keyed to it reads as unknown).
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name, prop in cast("dict[Any, Any]", properties).items():
        if isinstance(prop, dict):
            result[str(name)] = cast("dict[str, Any]", prop)
    return result


# The one form property and display slot name refused: assigning it on a JavaScript object sets the
# object's prototype instead of a key, so a form drawn in a browser cannot keep it.
_REFUSED_FORM_NAME = "__proto__"
_REFUSED_FORM_NAME_REASON = (
    "JavaScript reads it as an object's prototype, so a form drawn in a browser cannot keep a {what} by that name"
)


def check_form_property_names(schema: dict[str, Any]) -> None:
    """Refuse a form whose top-level ``properties`` declares the name ``__proto__``.

    Raises ``ValueError`` naming the property and why no browser-drawn form can keep it.
    """
    properties = schema.get("properties")
    if isinstance(properties, dict) and _REFUSED_FORM_NAME in properties:
        reason = _REFUSED_FORM_NAME_REASON.format(what="field")
        raise ValueError(f"form schema property {_REFUSED_FORM_NAME!r} is not an allowed name: {reason}")


def _form_option_values(prop: dict[str, Any], options: list[FormOption] | None) -> list[str] | None:
    # The allowed string set for a prefilled value: the per-send option values when a
    # per-send list is given (it replaces the enum for this send), else the property's
    # own ``enum`` — or None when the property constrains nothing.
    if options is not None:
        return [option.value for option in options]
    enum = prop.get("enum")
    if isinstance(enum, list):
        return [str(choice) for choice in cast("list[Any]", enum)]
    return None


def _form_property_is_stringish(prop: dict[str, Any]) -> bool:
    # A property a per-send option list may target: a string, or an array whose items
    # are strings. Any other property carries choices no single control can render.
    if prop.get("type") == "string":
        return True
    items = prop.get("items")
    return (
        prop.get("type") == "array"
        and isinstance(items, dict)
        and cast("dict[str, Any]", items).get("type") == "string"
    )


def _form_property_is_option_bearing(prop: dict[str, Any]) -> bool:
    # A property whose CHOICE LIST a reaction may supply/replace: a string that declares an
    # ``enum``, or an array whose items are strings. Any other property carries no choice
    # list a reaction could feed.
    if prop.get("type") == "string":
        return isinstance(prop.get("enum"), list)
    items = prop.get("items")
    return (
        prop.get("type") == "array"
        and isinstance(items, dict)
        and cast("dict[str, Any]", items).get("type") == "string"
    )


def _check_scalar_form_value(
    name: str, ptype: Any, value: Any, prop: dict[str, Any], options: list[FormOption] | None
) -> None:
    # Validate one prefilled ``value`` against a scalar property schema (string/boolean/
    # integer/number) plus any enum / per-send option constraint on a string. Raises
    # ``ValueError`` naming the field.
    if ptype == "string":
        if not isinstance(value, str):
            raise ValueError(f"form data value for {name!r} must be a string")
        allowed = _form_option_values(prop, options)
        if allowed is not None and value not in allowed:
            raise ValueError(f"form data value for {name!r} must be one of {allowed}")
    elif ptype == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"form data value for {name!r} must be a boolean")
    elif ptype == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"form data value for {name!r} must be an integer")
    elif ptype == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"form data value for {name!r} must be a number")


def _check_array_form_value(name: str, prop: dict[str, Any], value: Any, options: list[FormOption] | None) -> None:
    # Validate one prefilled ``value`` against an array-of-strings property (plus any
    # allowed-set constraint). Raises ``ValueError`` naming the field.
    items = cast("dict[str, Any]", prop["items"])
    if items.get("type") != "string":
        raise ValueError(f"form data value for {name!r} must be a list of strings")
    if not isinstance(value, list):
        raise ValueError(f"form data value for {name!r} must be a list of strings")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    value_list = cast("list[Any]", value)
    if not all(isinstance(item, str) for item in value_list):
        raise ValueError(f"form data value for {name!r} must be a list of strings")
    allowed = _form_option_values(items, options)
    if allowed is not None:
        bad = [item for item in value_list if item not in allowed]
        if bad:
            raise ValueError(f"form data value for {name!r} contains choices outside the allowed set: {bad}")


def _check_form_value(name: str, prop: dict[str, Any], value: Any, options: list[FormOption] | None) -> None:
    # Validate one prefilled ``value`` against its property's schema. A property without a
    # renderable scalar (or array-of-strings) type cannot be shown filled in, so it raises
    # rather than storing an unrenderable prefill. Raises ``ValueError`` naming the field.
    ptype = prop.get("type")
    if ptype in _SCALAR_FORM_TYPES:
        _check_scalar_form_value(name, ptype, value, prop, options)
    elif ptype == "array" and isinstance(prop.get("items"), dict):
        _check_array_form_value(name, prop, value, options)
    else:
        raise ValueError(
            f"form data cannot prefill {name!r}: its schema type {ptype!r} is not a renderable scalar "
            f"({', '.join(_SCALAR_FORM_TYPES)}) or an array of strings"
        )


def check_form_data(schema: dict[str, Any], data: FormData) -> None:
    """Validate a form's per-send :class:`FormData` against its schema.

    Every ``values`` / ``options`` key must be a declared top-level property, each
    prefilled value must fit its property's schema, and a per-send option list must
    target only a string (or array-of-strings) property and be non-empty. Raises
    ``ValueError`` naming the offending field.
    """
    props = _schema_properties(schema)
    for name, option_list in data.options.items():
        prop = props.get(name)
        if prop is None:
            raise ValueError(f"form data options names unknown property {name!r}")
        if not _form_property_is_stringish(prop):
            raise ValueError(f"form data options for {name!r} require a string (or array-of-strings) property")
        if not option_list:
            raise ValueError(f"form data options for {name!r} must be a non-empty list")
    for name, value in data.values.items():
        prop = props.get(name)
        if prop is None:
            raise ValueError(f"form data values names unknown property {name!r}")
        _check_form_value(name, prop, value, data.options.get(name))


def _form_property_draws_choice(prop: dict[str, Any], named_in_options: bool) -> bool:
    # Whether a form property draws a choice list a per-field word could label: a property named in
    # the per-send ``data.options`` (its list replaces the enum for this send), a string with an
    # ``enum``, or an array whose items are strings with an ``enum``. Any other property draws a
    # free input or a shape no single picker renders.
    if named_in_options:
        return True
    if prop.get("type") == "string":
        return isinstance(prop.get("enum"), list)
    items = prop.get("items")
    return (
        prop.get("type") == "array"
        and isinstance(items, dict)
        and cast("dict[str, Any]", items).get("type") == "string"
        and isinstance(cast("dict[str, Any]", items).get("enum"), list)
    )


def _form_property_has_shape(prop: dict[str, Any]) -> bool:
    # Whether a form property declares a shape its value can break: any of :data:`SHAPE_KEYWORDS`
    # on the property itself or on an array's ``items``.
    items = prop.get("items")
    item_keys: set[str] = set(cast("dict[str, Any]", items)) if isinstance(items, dict) else set()
    return any(key in prop or key in item_keys for key in SHAPE_KEYWORDS)


def _form_property_demands_a_value(prop: dict[str, Any], top_level_required: bool) -> bool:
    # Whether a form property can miss a required value, at the field or anywhere inside it: the
    # field is named in the schema's top-level ``required``, or the property (or a nested object or
    # array item inside it) carries a non-empty ``required`` list of its own.
    if top_level_required:
        return True
    inner = prop.get("required")
    if isinstance(inner, list) and inner:
        return True
    nested: list[Any] = []
    properties = prop.get("properties")
    if isinstance(properties, dict):
        nested.extend(cast("dict[str, Any]", properties).values())
    if isinstance(prop.get("items"), dict):
        nested.append(prop["items"])
    return any(
        isinstance(child, dict) and _form_property_demands_a_value(cast("dict[str, Any]", child), False)
        for child in nested
    )


def _form_property_draws_free_list(prop: dict[str, Any], named_in_options: bool) -> bool:
    # Whether a form property draws the add/remove list a list word could label: an array that does
    # not draw a choice list (a choice list is a checkbox group with no add or remove control).
    return prop.get("type") == "array" and not _form_property_draws_choice(prop, named_in_options)


def check_form_wording_fields(schema: dict[str, Any], data: FormData | None, fields: dict[str, FieldWording]) -> None:
    """Validate a form's per-field ``wording`` keys against its schema and per-send data.

    Every key of ``wording.form.fields`` must name a declared top-level property, and each set
    member must have something to word on it: ``choice_label`` labels the field's picker, so the
    property must DRAW a choice list — a string with an ``enum`` or named in the per-send
    ``data.options``, or an array of strings with an ``items.enum`` or named in ``data.options``;
    ``invalid`` words a value that breaks the field's declared shape, so the property must carry at
    least one of :data:`SHAPE_KEYWORDS`; ``required`` words a missing required value, so the field
    must be named in the schema's ``required`` or carry a ``required`` list inside it;
    ``true_label`` / ``false_label`` word a boolean's two values, so the property must be a
    ``boolean``; ``length_hint`` words a text control's maximum, so the property must be a
    ``string`` with an integer ``maxLength``; ``add_item`` / ``remove_item`` / ``item_label`` /
    ``no_items`` word a free list's controls, so the property must be an array that draws no choice
    list. Raises ``ValueError`` naming the offending property.
    """
    props = _schema_properties(schema)
    option_names: set[str] = set(data.options) if data is not None else set()
    raw_required = schema.get("required")
    required_names: set[str] = (
        {name for name in cast("list[Any]", raw_required) if isinstance(name, str)}
        if isinstance(raw_required, list)
        else set()
    )
    for name, wording in fields.items():
        prop = props.get(name)
        if prop is None:
            raise ValueError(f"wording.form.fields names {name!r}, which is not a declared property")
        named_in_options = name in option_names
        if wording.choice_label is not None and not _form_property_draws_choice(prop, named_in_options):
            raise ValueError(f"wording.form.fields names {name!r}, which draws no choice list")
        if wording.invalid is not None and not _form_property_has_shape(prop):
            raise ValueError(f"wording.form.fields names {name!r}, which declares no shape to break")
        if wording.required is not None and not _form_property_demands_a_value(prop, name in required_names):
            raise ValueError(f"wording.form.fields names {name!r}, which is never required")
        if (wording.true_label is not None or wording.false_label is not None) and prop.get("type") != "boolean":
            raise ValueError(f"wording.form.fields names {name!r}, which is not a boolean field")
        if wording.length_hint is not None and not (
            prop.get("type") == "string" and isinstance(prop.get("maxLength"), int)
        ):
            raise ValueError(f"wording.form.fields names {name!r}, which declares no maxLength")
        list_words = (wording.add_item, wording.remove_item, wording.item_label, wording.no_items)
        if any(word is not None for word in list_words) and not _form_property_draws_free_list(prop, named_in_options):
            raise ValueError(f"wording.form.fields names {name!r}, which draws no free list")


def check_form_pages(schema: dict[str, Any], pages: list[FormPage]) -> None:
    """Validate a form's ``pages`` against its schema.

    Every top-level property must appear exactly once across the INPUT pages (a review
    page carries no input fields and so contributes none), and every named field must be
    a declared property. A display ``slot`` name is unique across all pages (two blocks
    cannot fill the same slot) and is not ``__proto__``. A ``timeOf`` time sits on the page of its
    date (:func:`~tai42_contract.interactions.models.form_dates.check_time_pages`). Raises
    ``ValueError`` naming the missing / duplicate / unknown field, the misplaced time, or the
    repeated or refused slot.
    """
    declared = list(_schema_properties(schema))
    seen: list[str] = []
    for page in pages:
        for field in page.fields:
            if field not in declared:
                raise ValueError(f"form page {page.title!r} names unknown property {field!r}")
            if field in seen:
                raise ValueError(f"form page property {field!r} appears on more than one page")
            seen.append(field)
    missing = [name for name in declared if name not in seen]
    if missing:
        raise ValueError(f"form pages omit properties: {missing}")
    check_time_pages(schema, [(page.title, list(page.fields)) for page in pages])
    _check_display_slots(pages)


def _check_display_slots(pages: list[FormPage]) -> None:
    # Each display slot name is unique across the pages and is not the refused name.
    slots: list[str] = []
    for page in pages:
        for block in page.display:
            if block.slot is None:
                continue
            if block.slot == _REFUSED_FORM_NAME:
                reason = _REFUSED_FORM_NAME_REASON.format(what="slot")
                raise ValueError(f"form display slot {block.slot!r} is not an allowed name: {reason}")
            if block.slot in slots:
                raise ValueError(f"form display slot {block.slot!r} appears on more than one block")
            slots.append(block.slot)


def check_form_reactions(schema: dict[str, Any], pages: list[FormPage] | None, reactions: FormReactions) -> None:
    """Validate a form's reaction triggers against its schema and pages.

    Every name in ``field_changed`` must be a declared top-level property, every name in
    ``page_advanced`` must be the title of a declared page (a form with no ``pages`` has no
    page to advance from) AND that title must be UNIQUE among the pages — a ``page_advanced``
    trigger keys a page by its title, so a title shared by two pages names an ambiguous
    advance that would fire on both. Every name in ``choices`` must be a declared string-enum
    or array-of-strings property (the only properties whose choice list a reaction can feed).
    The event kinds themselves are validated by :class:`FormReactions`. Raises ``ValueError``
    naming the unknown/ambiguous page, the unknown field, or the non-option-bearing choice
    property.
    """
    declared = _schema_properties(schema)
    for field in reactions.field_changed:
        if field not in declared:
            raise ValueError(f"form reaction field_changed names unknown property {field!r}")
    page_titles = [page.title for page in pages] if pages is not None else []
    for title in reactions.page_advanced:
        if title not in page_titles:
            raise ValueError(f"form reaction page_advanced names unknown page {title!r}")
        if page_titles.count(title) > 1:
            raise ValueError(
                f"form reaction page_advanced names ambiguous page {title!r}: "
                f"{page_titles.count(title)} pages share this title"
            )
    for field in reactions.choices:
        prop = declared.get(field)
        if prop is None:
            raise ValueError(f"form reaction choices names unknown property {field!r}")
        if not _form_property_is_option_bearing(prop):
            raise ValueError(f"form reaction choices for {field!r} require a string-enum or array-of-strings property")
