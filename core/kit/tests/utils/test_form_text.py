"""render_form_text: a completed form's values rendered to non-empty text.

Covers labelling from schema titles, ordering (schema-named first then extras),
value rendering (strings verbatim, everything else JSON with every float written
as a display slot writes it, inside a list or an object too), the loud
non-finite-float raise, the non-empty fallback dump, and that the data package
imports in a fresh interpreter.
"""

import math
import subprocess
import sys

import pytest

from tai42_kit.interactions import display_slot_text
from tai42_kit.utils.data import render_form_text
from tai42_kit.utils.data.form_text import render_form_text as render_direct


def test_the_data_package_imports_first_in_a_fresh_interpreter():
    result = subprocess.run(
        [sys.executable, "-c", "import tai42_kit.utils.data"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


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


@pytest.mark.parametrize(
    ("value", "text"),
    [(10.0, "10"), (-3.0, "-3"), (10.5, "10.5"), (5e-05, "0.00005"), (1e21, "1e+21"), (1e-07, "1e-7")],
)
def test_a_float_answer_reads_as_a_display_slot_writes_it(value: float, text: str):
    # A number field answered as ``10`` reads ``10`` whichever numeric type the channel coerced it to.
    assert render_form_text({"n": value}) == f"n: {text}"
    assert render_form_text({"n": value}) == f"n: {display_slot_text(value)}"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ([10.0], "[10]"),
        ({"k": 10.0}, '{"k": 10}'),
        ([{"a": [1e-07, 2.5]}, 5e-05], '[{"a": [1e-7, 2.5]}, 0.00005]'),
        ([10**22, 1e21], "[10000000000000000000000, 1e+21]"),
    ],
)
def test_a_float_inside_a_list_or_object_reads_as_a_display_slot_writes_it(value: object, text: str):
    # A number inside a list or object answer reads as the same number answered on its own.
    assert render_form_text({"n": value}) == f"n: {text}"


def test_a_non_finite_float_inside_a_list_raises_loudly():
    with pytest.raises(ValueError, match="must be finite"):
        render_form_text({"n": [math.nan]})


@pytest.mark.parametrize("value", [2**53 + 1, 10**22, 10**400])
def test_an_integer_answer_keeps_every_digit(value: int):
    assert render_form_text({"n": value}) == f"n: {value}"


def test_unicode_is_preserved():
    assert render_form_text({"greet": ["héllo"]}) == 'greet: ["héllo"]'


def test_non_finite_float_raises_loudly():
    with pytest.raises(ValueError, match="must be finite"):
        render_form_text({"n": math.inf})


def test_empty_answer_falls_back_to_json_dump():
    assert render_form_text({}) == "{}"


def test_answer_that_renders_to_no_lines_still_non_empty():
    # An answer with no keys renders no lines; the fallback dump keeps it non-empty.
    assert render_form_text({}, {"properties": {"unused": {"title": "Unused"}}}) == "{}"
