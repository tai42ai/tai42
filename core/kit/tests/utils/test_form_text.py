"""render_form_text: a completed form's values rendered to non-empty text.

Covers labelling from schema titles, ordering (schema-named first then extras),
value rendering (strings verbatim, everything else compact JSON), the loud
non-finite-float raise, and the non-empty fallback dump.
"""

import math

import pytest

from tai42_kit.utils.data import render_form_text
from tai42_kit.utils.data.form_text import render_form_text as render_direct


def test_public_reexport_is_the_same_function():
    assert render_form_text is render_direct


def test_string_values_render_verbatim():
    assert render_form_text({"name": "ada"}) == "name: ada"


def test_no_schema_keeps_answer_insertion_order():
    text = render_form_text({"b": "2", "a": "1"})
    assert text == "b: 2\na: 1"


def test_schema_title_is_the_label():
    schema = {"properties": {"name": {"title": "Full name"}}}
    assert render_form_text({"name": "ada"}, schema) == "Full name: ada"


def test_blank_title_falls_back_to_key():
    schema = {"properties": {"name": {"title": "  "}}}
    assert render_form_text({"name": "ada"}, schema) == "name: ada"


def test_missing_title_falls_back_to_key():
    schema = {"properties": {"name": {"type": "string"}}}
    assert render_form_text({"name": "ada"}, schema) == "name: ada"


def test_schema_named_fields_render_first_in_schema_order_then_extras():
    schema = {"properties": {"first": {"title": "First"}, "second": {"title": "Second"}}}
    # Answer insertion order is reversed and an unnamed extra is present.
    text = render_form_text({"extra": "e", "second": "s", "first": "f"}, schema)
    assert text == "First: f\nSecond: s\nextra: e"


def test_bool_renders_as_json_not_python_repr():
    assert render_form_text({"ok": True, "no": False}) == "ok: true\nno: false"


def test_numbers_and_containers_render_as_compact_json():
    text = render_form_text({"n": 3, "f": 1.5, "list": [1, 2], "obj": {"k": "v"}})
    assert text == 'n: 3\nf: 1.5\nlist: [1, 2]\nobj: {"k": "v"}'


def test_unicode_is_preserved():
    assert render_form_text({"greet": ["héllo"]}) == 'greet: ["héllo"]'


def test_non_finite_float_raises_loudly():
    with pytest.raises(ValueError, match="JSON compliant"):
        render_form_text({"n": math.inf})


def test_empty_answer_falls_back_to_json_dump():
    assert render_form_text({}) == "{}"


def test_answer_that_renders_to_no_lines_still_non_empty():
    # An answer with no keys renders no lines; the fallback dump keeps it non-empty.
    assert render_form_text({}, {"properties": {"unused": {"title": "Unused"}}}) == "{}"
