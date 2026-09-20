"""The shared ``!ENV`` marker grammar, scalar-leaf walk, and required-vs-defaulted scan."""

from __future__ import annotations

from tai42_kit.utils.data.env_markers import (
    ENV_REF,
    EnvMarkerRef,
    scalar_leaves,
    scan_env_marker_refs,
)


def test_grammar_parses_bare_and_defaulted_refs():
    # Group 2 is the optional ``:default`` suffix — empty for a bare ref.
    assert ENV_REF.findall("${VAR}") == [("VAR", "")]
    assert ENV_REF.findall("${VAR:default}") == [("VAR", ":default")]
    # Several refs in one expression, and surrounding text, are all captured.
    assert ENV_REF.findall("${A}-${B:x}") == [("A", ""), ("B", ":x")]


def test_empty_default_matches_no_arm_and_yields_no_ref():
    # The grammar's default arm ``(:[^}]+)?`` requires >=1 char after the colon,
    # so ``${VAR:}`` matches NEITHER the bare nor the defaulted arm and produces
    # no ref. This pins the current (accidental) semantics; it is not a claim the
    # grammar should special-case an empty default.
    assert ENV_REF.findall("${VAR:}") == []
    assert scan_env_marker_refs({"a": "!ENV ${VAR:}"}) == []


def test_scalar_leaves_walks_nested_dict_and_list():
    config = {
        "top": "a",
        "nested": {"inner": "b"},
        "seq": ["c", {"deep": "d"}, 7],
        "number": 42,
    }
    leaves = dict(scalar_leaves(config))
    # Only string scalars are yielded; ints are skipped. Pointers are RFC 6901.
    assert leaves == {
        "/top": "a",
        "/nested/inner": "b",
        "/seq/0": "c",
        "/seq/1/deep": "d",
    }


def test_scalar_leaves_escapes_pointer_tokens():
    assert dict(scalar_leaves({"a/b": "x", "c~d": "y"})) == {"/a~1b": "x", "/c~0d": "y"}


def test_scan_finds_marker_refs_with_pointers():
    config = {
        "url": "!ENV ${REQUIRED_HOST}",
        "port": "!ENV ${PORT:5432}",
        "nested": {"token": "!ENV prefix-${TOKEN}-suffix"},
        "list": ["!ENV ${IN_LIST}", "plain value", "!ENV literal-no-ref"],
    }
    refs = scan_env_marker_refs(config)
    by_var = {ref.var: ref for ref in refs}
    assert set(by_var) == {"REQUIRED_HOST", "PORT", "TOKEN", "IN_LIST"}
    assert by_var["REQUIRED_HOST"].pointer == "/url"
    assert by_var["TOKEN"].pointer == "/nested/token"
    assert by_var["IN_LIST"].pointer == "/list/0"


def test_scan_ignores_non_marker_leaves():
    # A plain string that happens to contain ``${VAR}`` but no ``!ENV `` prefix
    # is not a marker and contributes nothing.
    assert scan_env_marker_refs({"a": "literal ${VAR} text", "b": 3}) == []


def test_required_versus_defaulted_detection():
    refs = scan_env_marker_refs({"a": "!ENV ${BARE}", "b": "!ENV ${WITH:fallback}"})
    by_var = {ref.var: ref for ref in refs}
    assert by_var["BARE"].required is True
    assert by_var["BARE"].default is None
    assert by_var["WITH"].required is False
    assert by_var["WITH"].default == "fallback"


def test_env_marker_ref_required_property():
    assert EnvMarkerRef(var="X", default=None, pointer="/x").required is True
    assert EnvMarkerRef(var="X", default="d", pointer="/x").required is False
