"""Every toolbox tool declares its native tags at registration."""

from __future__ import annotations

import pytest

# Importing the tool modules runs their registration through the null app bound in
# conftest, which records the declared tags.
from tai42_toolbox.tools import (  # noqa: F401
    current_time_info,
    generate_embeddings,
    generate_uuid,
    pad_embeddings,
    request,
)

_EXPECTED_TAGS = {
    "current_time_info": {"time"},
    "generate_embeddings": {"embeddings"},
    "pad_embeddings": {"embeddings"},
    "generate_uuid": {"uuid"},
    "request": {"http"},
}


@pytest.mark.parametrize(("name", "tags"), sorted(_EXPECTED_TAGS.items()))
def test_toolbox_tool_tagged(name: str, tags: set[str], null_app) -> None:
    assert null_app.tools.tags[name] == tags
