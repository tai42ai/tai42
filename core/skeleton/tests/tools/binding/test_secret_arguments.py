"""Revealing the ``SecretValue`` arguments a tool's input validation would reject.

``reveal_for_validation`` walks the tool's input JSON schema alongside the argument values and
reveals every secret sitting under a TYPED string leaf — a top-level ``token: str``, a
``list[str]`` element, a typed model field, a ``dict[str, str]`` value — so each receives the
plain string that pydantic validation would otherwise reject. A leaf under a permissive schema
(``Any`` / ``object``), and every value under a permissive container schema, stays wrapped.
"""

from __future__ import annotations

from tai42_contract.secrets import SecretValue

from tai42_skeleton.tools.binding.secret_arguments import reveal_for_validation

# -- reveal a secret value under every TYPED string leaf --------------------------------

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
    revealed = reveal_for_validation({"token": secret}, _STR_PARAM)
    # A ``str`` parameter would reject the wrapper at pydantic validation, so it is
    # revealed to its plain value.
    assert revealed == {"token": "s3cr3t"}
    assert not isinstance(revealed["token"], SecretValue)


def test_reveal_unwraps_a_secret_baked_into_an_optional_str_param() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"token": secret}, _OPTIONAL_STR_PARAM)
    assert revealed == {"token": "s3cr3t"}


def test_reveal_keeps_the_wrapper_for_a_permissive_param() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"token": secret}, _PERMISSIVE_PARAM)
    # A permissive (``Any`` / ``object``) parameter accepts the wrapper unchanged, so it
    # stays wrapped and is masked wherever the run is recorded.
    assert revealed["token"] is secret


def test_reveal_unwraps_a_secret_in_a_typed_list_element() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"tokens": [secret, "plain"]}, _TYPED_LIST_PARAM)
    # Each ``list[str]`` element is validated as a ``str``, so the wrapped element is
    # revealed and the plain one passes through.
    assert revealed == {"tokens": ["s3cr3t", "plain"]}


def test_reveal_unwraps_a_secret_in_a_typed_dict_value() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"payload": {"token": secret}}, _TYPED_DICT_PARAM)
    # ``dict[str, str]`` types every value as a ``str`` via ``additionalProperties``, so the
    # nested wrapper is revealed.
    assert revealed == {"payload": {"token": "s3cr3t"}}


def test_reveal_unwraps_a_secret_in_a_typed_model_field() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"creds": {"token": secret}}, _MODEL_PARAM)
    # A ``$ref`` model field resolves within the schema's ``$defs`` down to a typed
    # ``token: str`` leaf, which is revealed.
    assert revealed == {"creds": {"token": "s3cr3t"}}


def test_reveal_unwraps_a_secret_through_a_union_of_object_and_null() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"creds": {"token": secret}}, _UNION_OBJECT_PARAM)
    # The object branch of the union carries the typed leaf; the null branch does not
    # match a dict value. The leaf is revealed.
    assert revealed == {"creds": {"token": "s3cr3t"}}


def test_reveal_keeps_the_wrapper_under_a_permissive_container_schema() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"payload": {"token": secret}}, _PERMISSIVE_DICT_PARAM)
    # ``additionalProperties: true`` types the object's VALUES permissively, so a nested
    # wrapper stays wrapped and is masked wherever the run is recorded.
    assert revealed["payload"]["token"] is secret


def test_reveal_keeps_the_wrapper_for_an_any_typed_model_field() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"creds": {"token": secret}}, _ANY_FIELD_PARAM)
    # The model's ``token`` field is ``Any`` (an empty leaf schema): permissive, so the
    # wrapper survives even though its container is typed.
    assert revealed["creds"]["token"] is secret


def test_reveal_leaves_a_typed_looking_subtree_under_a_permissive_container_wrapped() -> None:
    secret = SecretValue("s3cr3t")
    permissive = {"properties": {"payload": {}}}
    revealed = reveal_for_validation({"payload": {"inner": {"token": secret}}}, permissive)
    # A permissive schema admits anything, so nothing beneath it is typed: the whole
    # subtree, however typed-looking, is left wrapped and never descended.
    assert revealed["payload"] is not None
    assert revealed["payload"]["inner"]["token"] is secret


def test_reveal_does_not_hang_on_a_cyclic_ref() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation(
        {"node": {"child": {"child": {}, "token": secret}, "token": secret}}, _CYCLIC_PARAM
    )
    # A self-referential ``$ref`` terminates (the finite value drives the walk; the ref
    # cycle is guarded); the ``Any`` ``token`` leaves stay wrapped.
    assert revealed["node"]["token"] is secret
    assert revealed["node"]["child"]["token"] is secret


def test_reveal_unwraps_a_secret_under_a_oneof_leaf() -> None:
    secret = SecretValue("s3cr3t")
    schema = {"properties": {"token": {"oneOf": [{"type": "string"}, {"type": "integer"}]}}}
    revealed = reveal_for_validation({"token": secret}, schema)
    # A ``oneOf`` leaf is typed when at least one branch is; the string branch reveals it.
    assert revealed == {"token": "s3cr3t"}


def test_reveal_unwraps_a_secret_through_an_allof_merged_object() -> None:
    secret = SecretValue("s3cr3t")
    schema = {
        "properties": {"creds": {"allOf": [{"type": "object"}, {"properties": {"token": {"type": "string"}}}]}},
    }
    revealed = reveal_for_validation({"creds": {"token": secret}}, schema)
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
    revealed = reveal_for_validation({"creds": {"token": secret}}, schema)
    # An ``allOf`` member that expands to several branches is carried through, so its
    # typed object branch still reveals the leaf.
    assert revealed == {"creds": {"token": "s3cr3t"}}


def test_reveal_walks_tuple_prefix_items_then_the_items_tail() -> None:
    a, b, c = SecretValue("a"), SecretValue("b"), SecretValue("c")
    schema = {
        "properties": {"row": {"type": "array", "prefixItems": [{"type": "string"}, {}], "items": {"type": "string"}}}
    }
    revealed = reveal_for_validation({"row": [a, b, c]}, schema)
    # ``prefixItems[0]`` is typed → revealed; ``prefixItems[1]`` is permissive → wrapped;
    # the tail element falls to ``items`` (typed) → revealed.
    assert revealed["row"][0] == "a"
    assert revealed["row"][1] is b
    assert revealed["row"][2] == "c"


def test_reveal_keeps_the_wrapper_for_an_unresolvable_ref() -> None:
    secret = SecretValue("s3cr3t")
    schema = {"properties": {"token": {"$ref": "#/$defs/Missing"}}, "$defs": {}}
    revealed = reveal_for_validation({"token": secret}, schema)
    # A ``$ref`` that does not resolve is treated as permissive — the wrapper survives,
    # never a crash.
    assert revealed["token"] is secret


def test_reveal_keeps_the_wrapper_for_an_external_ref() -> None:
    secret = SecretValue("s3cr3t")
    schema = {"properties": {"token": {"$ref": "https://example.test/schema"}}}
    revealed = reveal_for_validation({"token": secret}, schema)
    # A non-local ``$ref`` cannot be resolved within the tool's own schema → permissive.
    assert revealed["token"] is secret


def test_reveal_keeps_the_wrapper_when_the_schema_has_no_properties() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"token": secret}, {})
    # An input schema with no ``properties`` types no parameter — every value is unknown
    # and stays wrapped.
    assert revealed["token"] is secret


def test_reveal_descends_a_dict_whose_type_is_a_list_including_object() -> None:
    secret = SecretValue("s3cr3t")
    schema = {"properties": {"creds": {"type": ["object", "null"], "properties": {"token": {"type": "string"}}}}}
    revealed = reveal_for_validation({"creds": {"token": secret}}, schema)
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
    revealed = reveal_for_validation({"creds": {"token": secret}}, schema)
    # Both branches type ``token``; the child schema unions them and the leaf is revealed.
    assert revealed == {"creds": {"token": "s3cr3t"}}


def test_reveal_keeps_the_wrapper_for_a_list_under_a_permissive_param() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"tokens": [secret]}, {"properties": {"tokens": {}}})
    # A list value under a permissive schema is not descended — the element stays wrapped.
    assert revealed["tokens"][0] is secret


def test_reveal_walks_a_legacy_tuple_items_list_form() -> None:
    a, b = SecretValue("a"), SecretValue("b")
    schema = {"properties": {"row": {"type": "array", "items": [{"type": "string"}, {}]}}}
    revealed = reveal_for_validation({"row": [a, b]}, schema)
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
    revealed = reveal_for_validation({"token": secret}, schema)
    # A ``$ref`` chain that loops back on itself terminates at the guard and is treated as
    # permissive — the wrapper stays, never a hang.
    assert revealed["token"] is secret


def test_reveal_keeps_the_wrapper_for_a_ref_to_a_non_object_node() -> None:
    secret = SecretValue("s3cr3t")
    schema = {"properties": {"token": {"$ref": "#/title"}}, "title": "not a schema"}
    revealed = reveal_for_validation({"token": secret}, schema)
    # A ``$ref`` resolving to a non-schema node is treated as permissive, never a crash.
    assert revealed["token"] is secret


def test_reveal_keeps_a_dict_wrapped_under_a_typed_non_object_schema() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"payload": {"token": secret}}, {"properties": {"payload": {"type": "string"}}})
    # A dict baked into a ``str`` parameter is rejected loudly by validation, not leaked:
    # there is no object candidate to descend, so the nested wrapper stays wrapped.
    assert revealed["payload"]["token"] is secret


def test_reveal_keeps_a_list_wrapped_under_a_typed_non_array_schema() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"tokens": [secret]}, {"properties": {"tokens": {"type": "string"}}})
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
    revealed = reveal_for_validation({"creds": {"token": secret}}, schema)
    # The two branches type ``token`` differently (string vs integer); both reject the
    # wrapper, so the union child schema stays a real ``anyOf`` and the leaf is revealed.
    assert revealed == {"creds": {"token": "s3cr3t"}}


def test_reveal_keeps_the_wrapper_for_a_typed_or_permissive_union_leaf() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"token": secret}, _STR_OR_ANY_PARAM)
    # ``str | Any`` carries a permissive branch that accepts the wrapper unchanged, so
    # validation would NOT reject it — the leaf stays wrapped and is masked.
    assert revealed["token"] is secret


def test_reveal_keeps_the_wrapper_for_a_typed_container_or_permissive_union() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"payload": {"token": secret}}, _TYPED_DICT_OR_ANY_PARAM)
    # ``dict[str, str] | Any``: the permissive branch accepts the whole dict-with-wrapper,
    # so the container is never descended and the nested leaf stays wrapped.
    assert revealed["payload"]["token"] is secret


def test_reveal_unwraps_a_secret_when_every_union_branch_is_typed() -> None:
    secret = SecretValue("s3cr3t")
    revealed = reveal_for_validation({"token": secret}, _STR_OR_DICT_PARAM)
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
    revealed = reveal_for_validation({"node": node}, schema)
    elapsed = time.perf_counter() - start
    # A binary object union nested this deep must not blow up (candidate dedup bounds it).
    assert elapsed < 1.0
    # Every typed ``token`` leaf along the chain is revealed.
    assert revealed["node"]["token"] == "s3cr3t"
    assert revealed["node"]["child"]["token"] == "s3cr3t"


def test_reveal_passes_non_secret_and_unknown_params_through() -> None:
    revealed = reveal_for_validation({"token": "plain", "extra": 7}, _STR_PARAM)
    assert revealed == {"token": "plain", "extra": 7}


def test_reveal_does_not_mutate_the_input() -> None:
    secret = SecretValue("s3cr3t")
    original = {"payload": {"token": secret}}
    reveal_for_validation(original, _TYPED_DICT_PARAM)
    assert original["payload"]["token"] is secret
