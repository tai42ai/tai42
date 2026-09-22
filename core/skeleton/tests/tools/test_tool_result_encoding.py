"""A tool result JSON cannot encode is refused with a named error at the shared seam.

``find_lone_surrogate`` is the one pure detector every door shares: it walks a reduced
JSON-native value and returns the dotted JSON path of the first ``str`` leaf holding a
lone UTF-16 surrogate — the value ``to_jsonable_python`` passes through but the wire encode
``str.encode("utf-8")`` refuses. The ``run_tool`` seam calls it after the result is reduced
and raises a named :class:`ToolResultEncodingError` (tool + path), so every door that flows
through the seam refuses loudly instead of reaching its encode.
"""

import asyncio
from collections.abc import Callable

import pytest
from pydantic import BaseModel
from tai42_contract.interactions import ResumeBuffered, SuspendedInteraction
from tai42_contract.secrets import SecretValue

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding.result import (
    ToolResultEncodingError,
    UnencodableLeafError,
    _jsonable_or_keep_walkable,
    find_lone_surrogate,
)

# A real lone high surrogate: a Python ``str`` code point in U+D800-U+DFFF that
# ``json.dumps(...).encode("utf-8")`` rejects with "surrogates not allowed".
_LONE_SURROGATE = "\ud83d"


class _SurrogateKeyModel(BaseModel):
    """A model whose ``data`` field is a dict keyed by a surrogate — un-encodable, not deep-walked."""

    data: dict


# -- find_lone_surrogate (the pure detector) --------------------------------


def test_detector_finds_surrogate_in_scalar() -> None:
    assert find_lone_surrogate(_LONE_SURROGATE) == "$"


def test_detector_none_for_clean_value_including_a_real_emoji() -> None:
    # A real multi-byte emoji is a single non-surrogate code point — encodable, not flagged.
    assert find_lone_surrogate({"a": 1, "b": ["ok", "😀", {"c": "plain"}]}) is None


def test_detector_names_the_path_through_dict_and_list() -> None:
    value = {"items": [{"name": "ok"}, {"name": _LONE_SURROGATE}]}
    assert find_lone_surrogate(value) == "$.items[1].name"


def test_detector_short_circuits_on_the_first_hit() -> None:
    # Two offending leaves: the walk returns the FIRST in document order, never the second.
    value = {"a": _LONE_SURROGATE, "b": _LONE_SURROGATE}
    assert find_lone_surrogate(value) == "$.a"


def test_detector_descends_into_a_secret_and_names_the_field_not_the_value() -> None:
    # A door that reveals the secret later would encode the surrogate; the detector reveals
    # it here and names the FIELD path — never the value.
    value = {"token": SecretValue(_LONE_SURROGATE)}
    assert find_lone_surrogate(value) == "$.token"


def test_detector_flags_a_surrogate_dict_key_named_ascii_safely() -> None:
    # A KEY holding a surrogate is un-encodable exactly as a value is; it is named via
    # ``ascii()`` so the path itself carries no surrogate and stays encodable.
    path = find_lone_surrogate({_LONE_SURROGATE: "ok"})
    assert path == f"$.{_LONE_SURROGATE!a}"
    assert path is not None
    path.encode("utf-8")  # the path is ASCII-safe — no surrogate leaks into it


def test_detector_flags_a_surrogate_dict_key_nested_in_a_list() -> None:
    path = find_lone_surrogate({"items": ["ok", {_LONE_SURROGATE: "v"}]})
    assert path == f"$.items[1].{_LONE_SURROGATE!a}"
    assert path is not None
    path.encode("utf-8")


def test_reducer_signals_an_unencodable_model_leaf_with_its_path() -> None:
    # A leaf ``to_jsonable_python`` cannot render (a model holding a surrogate dict key) surfaces
    # as ``UnencodableLeafError`` carrying the leaf's path — without deep-reducing the model.
    with pytest.raises(UnencodableLeafError) as top:
        _jsonable_or_keep_walkable(_SurrogateKeyModel(data={_LONE_SURROGATE: "v"}))
    assert top.value.path == "$"

    with pytest.raises(UnencodableLeafError) as nested:
        _jsonable_or_keep_walkable({"payload": _SurrogateKeyModel(data={_LONE_SURROGATE: "v"})})
    assert nested.value.path == "$.payload"


# -- the run_tool seam ------------------------------------------------------


def _dispatch(tool_fn: Callable[[], object]) -> object:
    """Register ``tool_fn`` on a fresh app and dispatch it through the ``run_tool`` seam."""

    async def run() -> object:
        async with app.app_context(Manifest.model_validate({})):
            app.tools.tool(force=True)(tool_fn)
            return await app.tools.run_tool(tool_fn.__name__, {})

    return asyncio.run(run())


async def emit_scalar() -> str:
    """A tool whose scalar return holds a lone surrogate."""
    return _LONE_SURROGATE


async def emit_nested() -> dict:
    """A tool whose nested dict value holds a lone surrogate."""
    return {"outer": {"inner": _LONE_SURROGATE}}


async def emit_list() -> dict:
    """A tool whose list element holds a lone surrogate."""
    return {"items": ["ok", _LONE_SURROGATE]}


async def emit_secret() -> dict:
    """A tool whose wrapped-secret field, once revealed, holds a lone surrogate."""
    return {"token": SecretValue(_LONE_SURROGATE)}


async def emit_emoji() -> dict:
    """A tool whose return is a real, encodable emoji."""
    return {"emoji": "😀"}


async def emit_park() -> SuspendedInteraction:
    """A tool that async-parks and returns the suspension sentinel."""
    return SuspendedInteraction(interaction_id="i1")


async def emit_surrogate_key() -> dict:
    """A tool whose dict KEY holds a lone surrogate."""
    return {_LONE_SURROGATE: "v"}


async def emit_model_leaf() -> dict:
    """A tool whose result field is a model the encoder cannot render (a surrogate dict key inside)."""
    return {"payload": _SurrogateKeyModel(data={_LONE_SURROGATE: "v"})}


async def emit_buffered() -> ResumeBuffered:
    """A tool that returns the non-terminal resume-buffered park signal."""
    return ResumeBuffered(remaining_ids=["i1"])


@pytest.mark.parametrize(
    ("tool_fn", "expected_path"),
    [
        (emit_scalar, "$"),
        (emit_nested, "$.outer.inner"),
        (emit_list, "$.items[1]"),
        (emit_secret, "$.token"),
    ],
)
def test_seam_raises_named_error_with_tool_and_path(tool_fn: Callable[[], object], expected_path: str) -> None:
    with pytest.raises(ToolResultEncodingError) as excinfo:
        _dispatch(tool_fn)
    assert excinfo.value.tool_name == tool_fn.__name__
    assert excinfo.value.json_path == expected_path
    # The message names the tool and the path, never the offending value.
    assert tool_fn.__name__ in str(excinfo.value)
    assert expected_path in str(excinfo.value)


def test_seam_flags_a_surrogate_key_with_a_safe_encodable_path_and_body() -> None:
    import json

    with pytest.raises(ToolResultEncodingError) as excinfo:
        _dispatch(emit_surrogate_key)
    assert excinfo.value.json_path == f"$.{_LONE_SURROGATE!a}"
    # The error and the JSON body a door builds from it are themselves encodable — no 500
    # from the very refusal that guards against one.
    body = {"error": str(excinfo.value), "tool": excinfo.value.tool_name, "path": excinfo.value.json_path}
    json.dumps(body).encode("utf-8")


def test_seam_flags_an_unencodable_model_leaf_with_the_field_path() -> None:
    import json

    with pytest.raises(ToolResultEncodingError) as excinfo:
        _dispatch(emit_model_leaf)
    assert excinfo.value.json_path == "$.payload"
    body = {"error": str(excinfo.value), "tool": excinfo.value.tool_name, "path": excinfo.value.json_path}
    json.dumps(body).encode("utf-8")


def test_seam_passes_an_encodable_emoji_unchanged() -> None:
    assert _dispatch(emit_emoji) == {"emoji": "😀"}


def test_seam_does_not_flag_a_suspended_interaction_park() -> None:
    result = _dispatch(emit_park)
    assert isinstance(result, SuspendedInteraction)
    assert result.interaction_id == "i1"


def test_seam_does_not_flag_a_resume_buffered_park() -> None:
    result = _dispatch(emit_buffered)
    assert isinstance(result, ResumeBuffered)
    assert result.remaining_ids == ["i1"]
