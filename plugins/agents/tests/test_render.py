"""Tests for :func:`tai42_agents._internal.render.render_message`.

The shared seam every agent face renders its system/user message through. The
default ``allow_empty=True`` maps an unset optional slot to the empty string; a
required slot (``allow_empty=False``) that is absent OR renders to only whitespace
is refused loudly, naming the field, rather than run on a blank prompt.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.template import TemplatedText

from tai42_agents._internal.render import render_message


def test_optional_unset_slot_renders_empty(resource_manager: Any) -> None:
    """An absent optional message (``None``, ``allow_empty`` default true) renders to
    the empty string rather than raising."""
    assert asyncio.run(render_message(None)) == ""


def test_optional_empty_content_renders_empty(resource_manager: Any) -> None:
    """An optional message authored as explicitly empty inline content renders to the
    empty string — an optional slot never demands a non-empty body."""
    assert asyncio.run(render_message(TemplatedText(content=""))) == ""


def test_required_absent_slot_raises(resource_manager: Any) -> None:
    """A required message left unset (``None``) raises, naming the field."""
    with pytest.raises(ValueError, match="user_message: required message was not provided"):
        asyncio.run(render_message(None, allow_empty=False, field="user_message"))


def test_required_empty_content_raises(resource_manager: Any) -> None:
    """A required message authored as explicitly empty inline content
    (``{"content": ""}``) renders to nothing and is refused — it must not run the face
    on a blank prompt."""
    with pytest.raises(ValueError, match="user_message: required message was not provided"):
        asyncio.run(render_message(TemplatedText(content=""), allow_empty=False, field="user_message"))


def test_required_whitespace_only_content_raises(resource_manager: Any) -> None:
    """A required message that renders to only whitespace carries no instruction and is
    as effectively absent as an empty string — it is refused too."""
    with pytest.raises(ValueError, match="judge_message: required message was not provided"):
        asyncio.run(render_message(TemplatedText(content="  \n\t "), allow_empty=False, field="judge_message"))


def test_required_present_content_renders(resource_manager: Any) -> None:
    """A required message with real inline content renders to that content."""
    rendered = asyncio.run(render_message(TemplatedText(content="do it"), allow_empty=False, field="user_message"))
    assert rendered == "do it"
