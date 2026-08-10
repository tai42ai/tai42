"""mcp_output_util: pure reduction of an MCP tool response to a plain value."""

import json
from types import SimpleNamespace

from tai42_kit.utils.data.mcp_output_util import extract_tool_error, extract_tool_output, tool_has_error


def test_tool_has_error_dict_camel_and_snake():
    assert tool_has_error({"isError": True}) is True
    assert tool_has_error({"is_error": True}) is True
    assert tool_has_error({}) is False


def test_tool_has_error_object_attr():
    assert tool_has_error(SimpleNamespace(isError=True)) is True
    assert tool_has_error(SimpleNamespace(is_error=False, isError=False)) is False


def test_extract_tool_error_from_dict_content():
    resp = {"content": [{"text": "boom"}]}
    assert extract_tool_error(resp) == "boom"


def test_extract_tool_error_from_object_with_text():
    item = SimpleNamespace(text="kaboom")
    assert extract_tool_error({"content": [item]}) == "kaboom"


def test_extract_tool_error_from_plain_string_item():
    assert extract_tool_error({"content": ["just text"]}) == "just text"


def test_extract_tool_error_default_when_empty():
    assert extract_tool_error({"content": []}) == "Unknown error"
    assert extract_tool_error({}) == "Unknown error"


def test_extract_output_error_returned_unchanged():
    resp = {"isError": True, "content": [{"text": "nope"}]}
    assert extract_tool_output(resp) is resp


def test_extract_output_prefers_structured_content():
    resp = {"structuredContent": {"answer": 42}, "content": [{"text": "ignored"}]}
    assert extract_tool_output(resp) == {"answer": 42}


def test_extract_output_string_content_passthrough():
    assert extract_tool_output({"content": "plain"}) == "plain"


def test_extract_output_none_content():
    assert extract_tool_output({"content": None}) is None


def test_extract_output_single_item_json_parsed():
    # One JSON text item (a TextContent-like object) -> the parsed object, not a
    # one-element list.
    item = SimpleNamespace(type="text", text=json.dumps({"k": "v"}))
    assert extract_tool_output({"content": [item]}) == {"k": "v"}


def test_extract_output_single_non_json_item_kept_as_string():
    item = SimpleNamespace(type="text", text="hello world")
    assert extract_tool_output({"content": [item]}) == "hello world"


def test_extract_output_multiple_items_returns_list():
    items = [
        SimpleNamespace(type="text", text=json.dumps({"a": 1})),
        SimpleNamespace(type="text", text="raw"),
    ]
    out = extract_tool_output({"content": items})
    assert out == [{"a": 1}, "raw"]


def test_extract_output_tuple_content():
    item = SimpleNamespace(type="text", text="42")
    # JSON-parses the numeric text -> int 42.
    assert extract_tool_output({"content": (item,)}) == 42


def test_extract_output_dict_content_returned_as_is():
    # Content that is neither None/str nor a list/tuple (here a dict) falls
    # through unchanged.
    content = {"raw": "value"}
    assert extract_tool_output({"content": content}) is content
