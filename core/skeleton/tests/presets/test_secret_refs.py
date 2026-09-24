"""The two bind steps for ``!ENV ${VAR}`` secret references in a preset's ``fixed_kwargs``.

``resolve_secret_refs`` materialises each reference: a present variable resolves to a
masked ``SecretValue``; an absent variable with a ``:default`` resolves to that default
in the clear; an absent required variable — or a marker that is not a single
``${VAR[:default]}`` reference — raises loudly naming the variable and the leaf.
Non-marker leaves and non-string values pass through untouched, and the input is never
mutated.

``reveal_typed_refs`` then walks the base tool's input JSON schema alongside the baked
values and reveals every resolved secret sitting under a TYPED string leaf — a top-level
``token: str``, a ``list[str]`` element, a typed model field, a ``dict[str, str]`` value —
so each receives the resolved string that pydantic validation would otherwise reject. A
leaf under a permissive schema (``Any`` / ``object``), and every value under a permissive
container schema, stays wrapped.
"""

from __future__ import annotations

import pytest
from tai42_contract.secrets import SecretValue

from tai42_skeleton.presets.secret_refs import resolve_secret_refs, reveal_typed_refs


def test_present_var_resolves_to_a_masked_secret_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESET_TOKEN", "s3cr3t")
    resolved = resolve_secret_refs({"token": "!ENV ${PRESET_TOKEN}"})
    secret = resolved["token"]
    assert isinstance(secret, SecretValue)
    assert secret.reveal() == "s3cr3t"


def test_absent_required_var_raises_naming_var_and_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRESET_MISSING", raising=False)
    with pytest.raises(ValueError, match=r"PRESET_MISSING") as exc:
        resolve_secret_refs({"token": "!ENV ${PRESET_MISSING}"})
    assert "/token" in str(exc.value)


def test_absent_var_with_default_resolves_to_the_default_in_the_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRESET_REGION", raising=False)
    resolved = resolve_secret_refs({"region": "!ENV ${PRESET_REGION:eu-west}"})
    # A default is opt-in non-secret config: stored/baked in the clear, never wrapped.
    assert resolved["region"] == "eu-west"
    assert not isinstance(resolved["region"], SecretValue)


def test_present_var_wins_over_its_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESET_REGION", "us-east")
    resolved = resolve_secret_refs({"region": "!ENV ${PRESET_REGION:eu-west}"})
    secret = resolved["region"]
    assert isinstance(secret, SecretValue)
    assert secret.reveal() == "us-east"


def test_nested_leaves_resolve_through_dicts_and_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESET_KEY", "abc")
    monkeypatch.delenv("PRESET_HOST", raising=False)
    resolved = resolve_secret_refs(
        {"headers": {"authorization": "!ENV ${PRESET_KEY}"}, "hosts": ["!ENV ${PRESET_HOST:localhost}", "plain"]}
    )
    assert isinstance(resolved["headers"]["authorization"], SecretValue)
    assert resolved["headers"]["authorization"].reveal() == "abc"
    assert resolved["hosts"] == ["localhost", "plain"]


def test_nested_absent_required_var_names_the_full_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRESET_DEEP", raising=False)
    with pytest.raises(ValueError, match=r"PRESET_DEEP") as exc:
        resolve_secret_refs({"outer": {"inner": ["!ENV ${PRESET_DEEP}"]}})
    assert "/outer/inner/0" in str(exc.value)


def test_non_marker_and_non_string_values_pass_through_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRESET_X", raising=False)
    original = {
        "plain": "just a string",
        "looks_like_ref": "${PRESET_X}",  # no !ENV prefix → not a marker
        "count": 7,
        "flag": True,
        "nothing": None,
        "nums": [1, 2.5],
    }
    resolved = resolve_secret_refs(original)
    assert resolved == original


def test_malformed_marker_raises_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESET_A", "a")
    monkeypatch.setenv("PRESET_B", "b")
    # Surrounding text / more than one reference is not a single secret reference.
    with pytest.raises(ValueError, match=r"malformed"):
        resolve_secret_refs({"url": "!ENV https://${PRESET_A}/${PRESET_B}"})


def test_input_is_not_mutated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESET_TOKEN", "s3cr3t")
    original = {"token": "!ENV ${PRESET_TOKEN}", "nested": {"k": "!ENV ${PRESET_TOKEN}"}}
    resolve_secret_refs(original)
    assert original["token"] == "!ENV ${PRESET_TOKEN}"
    assert original["nested"] == {"k": "!ENV ${PRESET_TOKEN}"}


# -- reveal_typed_refs: reveal a resolved value under every TYPED string leaf -----------

_STR_PARAM = {"properties": {"token": {"type": "string"}}}
_OPTIONAL_STR_PARAM = {"properties": {"token": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
_PERMISSIVE_PARAM = {"properties": {"token": {}}}
_TYPED_DICT_PARAM = {"properties": {"payload": {"type": "object", "additionalProperties": {"type": "string"}}}}
_PERMISSIVE_DICT_PARAM = {"properties": {"payload": {"type": "object", "additionalProperties": True}}}
_TYPED_LIST_PARAM = {"properties": {"tokens": {"type": "array", "items": {"type": "string"}}}}
_MODEL_PARAM = {
    "properties": {"creds": {"$ref": "#/$defs/Creds"}},
    "$defs": {"Creds": {"type": "object", "properties": {"token": {"type": "string"}}}},
}
_UNION_OBJECT_PARAM = {
    "properties": {"creds": {"anyOf": [{"$ref": "#/$defs/Creds"}, {"type": "null"}]}},
    "$defs": {"Creds": {"type": "object", "properties": {"token": {"type": "string"}}}},
}
_ANY_FIELD_PARAM = {"properties": {"creds": {"type": "object", "properties": {"token": {}}}}}
_CYCLIC_PARAM = {
    "properties": {"node": {"$ref": "#/$defs/Node"}},
    "$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}, "token": {}}}},
}
# ``str | Any`` / ``str | object`` — pydantic emits a typed branch beside a permissive one.
_STR_OR_ANY_PARAM = {"properties": {"token": {"anyOf": [{"type": "string"}, {}]}}}
_TYPED_DICT_OR_ANY_PARAM = {
    "properties": {"payload": {"anyOf": [{"type": "object", "additionalProperties": {"type": "string"}}, {}]}}
}
# ``str | dict[str, str]`` — every branch is typed.
_STR_OR_DICT_PARAM = {
    "properties": {
        "token": {"anyOf": [{"type": "string"}, {"type": "object", "additionalProperties": {"type": "string"}}]}
    }
}


def test_reveal_unwraps_a_secret_baked_into_a_typed_str_param() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"token": secret}, _STR_PARAM)
    # A ``str`` parameter would reject the wrapper at pydantic validation, so it is
    # revealed to its plain value.
    assert revealed == {"token": "s3cr3t"}
    assert not isinstance(revealed["token"], SecretValue)


def test_reveal_unwraps_a_secret_baked_into_an_optional_str_param() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"token": secret}, _OPTIONAL_STR_PARAM)
    assert revealed == {"token": "s3cr3t"}


def test_reveal_keeps_the_wrapper_for_a_permissive_param() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"token": secret}, _PERMISSIVE_PARAM)
    # A permissive (``Any`` / ``object``) parameter accepts the wrapper unchanged, so it
    # stays wrapped and is masked wherever the run is recorded.
    assert revealed["token"] is secret


def test_reveal_unwraps_a_secret_in_a_typed_list_element() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"tokens": [secret, "plain"]}, _TYPED_LIST_PARAM)
    # Each ``list[str]`` element is validated as a ``str``, so the wrapped element is
    # revealed and the plain one passes through.
    assert revealed == {"tokens": ["s3cr3t", "plain"]}


def test_reveal_unwraps_a_secret_in_a_typed_dict_value() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"payload": {"token": secret}}, _TYPED_DICT_PARAM)
    # ``dict[str, str]`` types every value as a ``str`` via ``additionalProperties``, so the
    # nested wrapper is revealed.
    assert revealed == {"payload": {"token": "s3cr3t"}}


def test_reveal_unwraps_a_secret_in_a_typed_model_field() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"creds": {"token": secret}}, _MODEL_PARAM)
    # A ``$ref`` model field resolves within the schema's ``$defs`` down to a typed
    # ``token: str`` leaf, which is revealed.
    assert revealed == {"creds": {"token": "s3cr3t"}}


def test_reveal_unwraps_a_secret_through_a_union_of_object_and_null() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"creds": {"token": secret}}, _UNION_OBJECT_PARAM)
    # The object branch of the union carries the typed leaf; the null branch does not
    # match a dict value. The leaf is revealed.
    assert revealed == {"creds": {"token": "s3cr3t"}}


def test_reveal_keeps_the_wrapper_under_a_permissive_container_schema() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"payload": {"token": secret}}, _PERMISSIVE_DICT_PARAM)
    # ``additionalProperties: true`` types the object's VALUES permissively, so a nested
    # wrapper stays wrapped and is masked wherever the run is recorded.
    assert revealed["payload"]["token"] is secret


def test_reveal_keeps_the_wrapper_for_an_any_typed_model_field() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"creds": {"token": secret}}, _ANY_FIELD_PARAM)
    # The model's ``token`` field is ``Any`` (an empty leaf schema): permissive, so the
    # wrapper survives even though its container is typed.
    assert revealed["creds"]["token"] is secret


def test_reveal_leaves_a_typed_looking_subtree_under_a_permissive_container_wrapped() -> None:
    secret = SecretValue("s3cr3t")
    permissive = {"properties": {"payload": {}}}
    revealed = reveal_typed_refs({"payload": {"inner": {"token": secret}}}, permissive)
    # A permissive schema admits anything, so nothing beneath it is typed: the whole
    # subtree, however typed-looking, is left wrapped and never descended.
    assert revealed["payload"] is not None
    assert revealed["payload"]["inner"]["token"] is secret


def test_reveal_does_not_hang_on_a_cyclic_ref() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"node": {"child": {"child": {}, "token": secret}, "token": secret}}, _CYCLIC_PARAM)
    # A self-referential ``$ref`` terminates (the finite value drives the walk; the ref
    # cycle is guarded); the ``Any`` ``token`` leaves stay wrapped.
    assert revealed["node"]["token"] is secret
    assert revealed["node"]["child"]["token"] is secret


def test_reveal_unwraps_a_secret_under_a_oneof_leaf() -> None:
    secret = SecretValue("s3cr3t")
    schema = {"properties": {"token": {"oneOf": [{"type": "string"}, {"type": "integer"}]}}}
    revealed = reveal_typed_refs({"token": secret}, schema)
    # A ``oneOf`` leaf is typed when at least one branch is; the string branch reveals it.
    assert revealed == {"token": "s3cr3t"}


def test_reveal_unwraps_a_secret_through_an_allof_merged_object() -> None:
    secret = SecretValue("s3cr3t")
    schema = {
        "properties": {"creds": {"allOf": [{"type": "object"}, {"properties": {"token": {"type": "string"}}}]}},
    }
    revealed = reveal_typed_refs({"creds": {"token": secret}}, schema)
    # ``allOf`` merges its branches; the merged object carries the typed ``token`` leaf.
    assert revealed == {"creds": {"token": "s3cr3t"}}


def test_reveal_unwraps_a_secret_through_an_allof_member_that_is_itself_a_union() -> None:
    secret = SecretValue("s3cr3t")
    schema = {
        "properties": {
            "creds": {
                "allOf": [
                    {"anyOf": [{"type": "object", "properties": {"token": {"type": "string"}}}, {"type": "null"}]}
                ]
            }
        }
    }
    revealed = reveal_typed_refs({"creds": {"token": secret}}, schema)
    # An ``allOf`` member that expands to several branches is carried through, so its
    # typed object branch still reveals the leaf.
    assert revealed == {"creds": {"token": "s3cr3t"}}


def test_reveal_walks_tuple_prefix_items_then_the_items_tail() -> None:
    a, b, c = SecretValue("a"), SecretValue("b"), SecretValue("c")
    schema = {
        "properties": {"row": {"type": "array", "prefixItems": [{"type": "string"}, {}], "items": {"type": "string"}}}
    }
    revealed = reveal_typed_refs({"row": [a, b, c]}, schema)
    # ``prefixItems[0]`` is typed → revealed; ``prefixItems[1]`` is permissive → wrapped;
    # the tail element falls to ``items`` (typed) → revealed.
    assert revealed["row"][0] == "a"
    assert revealed["row"][1] is b
    assert revealed["row"][2] == "c"


def test_reveal_keeps_the_wrapper_for_an_unresolvable_ref() -> None:
    secret = SecretValue("s3cr3t")
    schema = {"properties": {"token": {"$ref": "#/$defs/Missing"}}, "$defs": {}}
    revealed = reveal_typed_refs({"token": secret}, schema)
    # A ``$ref`` that does not resolve is treated as permissive — the wrapper survives,
    # never a crash.
    assert revealed["token"] is secret


def test_reveal_keeps_the_wrapper_for_an_external_ref() -> None:
    secret = SecretValue("s3cr3t")
    schema = {"properties": {"token": {"$ref": "https://example.test/schema"}}}
    revealed = reveal_typed_refs({"token": secret}, schema)
    # A non-local ``$ref`` cannot be resolved within the tool's own schema → permissive.
    assert revealed["token"] is secret


def test_reveal_keeps_the_wrapper_when_the_schema_has_no_properties() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"token": secret}, {})
    # An input schema with no ``properties`` types no parameter — every value is unknown
    # and stays wrapped.
    assert revealed["token"] is secret


def test_reveal_descends_a_dict_whose_type_is_a_list_including_object() -> None:
    secret = SecretValue("s3cr3t")
    schema = {"properties": {"creds": {"type": ["object", "null"], "properties": {"token": {"type": "string"}}}}}
    revealed = reveal_typed_refs({"creds": {"token": secret}}, schema)
    # A JSON-schema ``type`` list that includes ``object`` still marks a descendable
    # object container.
    assert revealed == {"creds": {"token": "s3cr3t"}}


def test_reveal_unwraps_across_two_object_branches_that_both_type_the_leaf() -> None:
    secret = SecretValue("s3cr3t")
    schema = {
        "properties": {
            "creds": {
                "anyOf": [
                    {"type": "object", "properties": {"token": {"type": "string"}}},
                    {"type": "object", "properties": {"token": {"type": "string"}, "extra": {"type": "integer"}}},
                ]
            }
        }
    }
    revealed = reveal_typed_refs({"creds": {"token": secret}}, schema)
    # Both branches type ``token``; the child schema unions them and the leaf is revealed.
    assert revealed == {"creds": {"token": "s3cr3t"}}


def test_reveal_keeps_the_wrapper_for_a_list_under_a_permissive_param() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"tokens": [secret]}, {"properties": {"tokens": {}}})
    # A list value under a permissive schema is not descended — the element stays wrapped.
    assert revealed["tokens"][0] is secret


def test_reveal_walks_a_legacy_tuple_items_list_form() -> None:
    a, b = SecretValue("a"), SecretValue("b")
    schema = {"properties": {"row": {"type": "array", "items": [{"type": "string"}, {}]}}}
    revealed = reveal_typed_refs({"row": [a, b]}, schema)
    # The draft-07 tuple form (``items`` as a list) types position 0 and leaves position 1
    # permissive.
    assert revealed["row"][0] == "a"
    assert revealed["row"][1] is b


def test_reveal_keeps_the_wrapper_on_a_direct_ref_cycle() -> None:
    secret = SecretValue("s3cr3t")
    schema = {
        "properties": {"token": {"$ref": "#/$defs/A"}},
        "$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}},
    }
    revealed = reveal_typed_refs({"token": secret}, schema)
    # A ``$ref`` chain that loops back on itself terminates at the guard and is treated as
    # permissive — the wrapper stays, never a hang.
    assert revealed["token"] is secret


def test_reveal_keeps_the_wrapper_for_a_ref_to_a_non_object_node() -> None:
    secret = SecretValue("s3cr3t")
    schema = {"properties": {"token": {"$ref": "#/title"}}, "title": "not a schema"}
    revealed = reveal_typed_refs({"token": secret}, schema)
    # A ``$ref`` resolving to a non-schema node is treated as permissive, never a crash.
    assert revealed["token"] is secret


def test_reveal_keeps_a_dict_wrapped_under_a_typed_non_object_schema() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"payload": {"token": secret}}, {"properties": {"payload": {"type": "string"}}})
    # A dict baked into a ``str`` parameter is rejected loudly by validation, not leaked:
    # there is no object candidate to descend, so the nested wrapper stays wrapped.
    assert revealed["payload"]["token"] is secret


def test_reveal_keeps_a_list_wrapped_under_a_typed_non_array_schema() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"tokens": [secret]}, {"properties": {"tokens": {"type": "string"}}})
    # A list baked into a ``str`` parameter likewise has no array candidate to descend.
    assert revealed["tokens"][0] is secret


def test_reveal_unwraps_across_object_branches_with_distinct_typed_child_schemas() -> None:
    secret = SecretValue("s3cr3t")
    schema = {
        "properties": {
            "creds": {
                "anyOf": [
                    {"type": "object", "properties": {"token": {"type": "string"}}},
                    {"type": "object", "properties": {"token": {"type": "integer"}}},
                ]
            }
        }
    }
    revealed = reveal_typed_refs({"creds": {"token": secret}}, schema)
    # The two branches type ``token`` differently (string vs integer); both reject the
    # wrapper, so the union child schema stays a real ``anyOf`` and the leaf is revealed.
    assert revealed == {"creds": {"token": "s3cr3t"}}


def test_reveal_keeps_the_wrapper_for_a_typed_or_permissive_union_leaf() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"token": secret}, _STR_OR_ANY_PARAM)
    # ``str | Any`` carries a permissive branch that accepts the wrapper unchanged, so
    # validation would NOT reject it — the leaf stays wrapped and is masked.
    assert revealed["token"] is secret


def test_reveal_keeps_the_wrapper_for_a_typed_container_or_permissive_union() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"payload": {"token": secret}}, _TYPED_DICT_OR_ANY_PARAM)
    # ``dict[str, str] | Any``: the permissive branch accepts the whole dict-with-wrapper,
    # so the container is never descended and the nested leaf stays wrapped.
    assert revealed["payload"]["token"] is secret


def test_reveal_unwraps_a_secret_when_every_union_branch_is_typed() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_typed_refs({"token": secret}, _STR_OR_DICT_PARAM)
    # ``str | dict[str, str]``: both branches reject the wrapper, so it must be revealed.
    assert revealed == {"token": "s3cr3t"}


def test_reveal_deeply_nested_object_union_is_bounded_not_exponential() -> None:
    import time

    depth = 40
    schema = {
        "properties": {"node": {"$ref": "#/$defs/A"}},
        "$defs": {
            name: {
                "type": "object",
                "properties": {
                    "child": {"anyOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}]},
                    "token": {"type": "string"},
                },
            }
            for name in ("A", "B")
        },
    }
    node: dict = {"token": SecretValue("s3cr3t")}
    cursor = node
    for _ in range(depth):
        child: dict = {"token": SecretValue("s3cr3t")}
        cursor["child"] = child
        cursor = child

    start = time.perf_counter()
    revealed = reveal_typed_refs({"node": node}, schema)
    elapsed = time.perf_counter() - start
    # A binary object union nested this deep must not blow up (candidate dedup bounds it).
    assert elapsed < 1.0
    # Every typed ``token`` leaf along the chain is revealed.
    assert revealed["node"]["token"] == "s3cr3t"
    assert revealed["node"]["child"]["token"] == "s3cr3t"


def test_reveal_passes_non_secret_and_unknown_params_through() -> None:
    revealed = reveal_typed_refs({"token": "plain", "extra": 7}, _STR_PARAM)
    assert revealed == {"token": "plain", "extra": 7}


def test_reveal_does_not_mutate_the_input() -> None:
    secret = SecretValue("s3cr3t")
    original = {"payload": {"token": secret}}
    reveal_typed_refs(original, _TYPED_DICT_PARAM)
    assert original["payload"]["token"] is secret
