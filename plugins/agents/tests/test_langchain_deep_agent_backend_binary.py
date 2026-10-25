"""``langchain_deep_agent`` skills backend: binary and non-UTF-8 files in a skill bundle.

A binary-classified file (by deepagents' own read classification) reads as its
full base64 payload and downloads as its exact bytes; a text-classified file whose
bytes are not UTF-8 reads as a per-read error; text search covers only the text
files. Covered on the template backend and through the inline overlay.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest
from tai42_contract.app import tai42_app

from tai42_agents.langchain_deep_agent.backend import SKILLS_ROOT, build_backend

from ._langchain_deep_agent_backend_support import _FakeResourceManager

_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
_NOT_UTF8_BYTES = b"\xff\xfe\x00pipe"

_SKILL_MD = """---
name: jq
description: Query and transform JSON documents with the jq language.
---

Use a pipe to chain filters. See `assets/logo.png`.
"""


@pytest.fixture
def fake_tm(monkeypatch: pytest.MonkeyPatch) -> _FakeResourceManager:
    tm = _FakeResourceManager()
    monkeypatch.setattr(tai42_app.storage, "resource_manager", tm)
    return tm


async def _seed(fake_tm: _FakeResourceManager) -> None:
    """A bundle with SKILL.md, a PNG asset, and a text-suffixed non-UTF-8 file."""
    await fake_tm.upload_template("skills/jq/SKILL.md", _SKILL_MD)
    await fake_tm.upload_template("skills/jq/assets/logo.png", _PNG_BYTES)
    await fake_tm.upload_template("skills/jq/assets/data.bin", _NOT_UTF8_BYTES)


_BACKENDS = {
    "template": lambda: build_backend().routes[SKILLS_ROOT],
    "inline": lambda: build_backend(inline_skills={"demo": "INLINE pipe"}).routes[SKILLS_ROOT],
}


@pytest.mark.parametrize("variant", sorted(_BACKENDS))
def test_png_read_returns_full_base64_payload(fake_tm: _FakeResourceManager, variant: str) -> None:
    """A PNG reads as the base64 of its exact bytes, whole: offset/limit apply to text only."""

    async def go() -> Any:
        await _seed(fake_tm)
        return await _BACKENDS[variant]().aread("/jq/assets/logo.png", offset=3, limit=1)

    result = asyncio.run(go())
    assert result.error is None
    assert result.file_data["encoding"] == "base64"
    assert result.file_data["content"] == base64.standard_b64encode(_PNG_BYTES).decode("ascii")


@pytest.mark.parametrize("variant", sorted(_BACKENDS))
def test_png_download_returns_exact_bytes(fake_tm: _FakeResourceManager, variant: str) -> None:
    """Downloading a PNG returns its stored bytes unchanged."""

    async def go() -> Any:
        await _seed(fake_tm)
        return await _BACKENDS[variant]().adownload_files(["/jq/assets/logo.png", "/jq/assets/data.bin"])

    png, data_bin = asyncio.run(go())
    assert png.error is None
    assert png.content == _PNG_BYTES
    assert data_bin.error is None
    assert data_bin.content == _NOT_UTF8_BYTES


@pytest.mark.parametrize("variant", sorted(_BACKENDS))
def test_grep_searches_text_files_only(fake_tm: _FakeResourceManager, variant: str) -> None:
    """A bundle's binary files never stop a search: the PNG (binary by suffix) and the
    non-UTF-8 ``.bin`` (text by suffix, not text by content) are not searched, while
    the text files are, so the hit is exactly the SKILL.md line."""

    async def go() -> Any:
        await _seed(fake_tm)
        return await _BACKENDS[variant]().agrep("pipe")

    result = asyncio.run(go())
    assert result.error is None
    paths = {match["path"] for match in result.matches}
    assert "/jq/SKILL.md" in paths
    assert not paths & {"/jq/assets/logo.png", "/jq/assets/data.bin"}


@pytest.mark.parametrize("variant", sorted(_BACKENDS))
def test_non_utf8_text_classified_read_is_per_read_error(fake_tm: _FakeResourceManager, variant: str) -> None:
    """A text-classified file whose bytes are not UTF-8 reads as an error result for
    that one read, worded as deepagents' filesystem backend words a read failure."""

    async def go() -> Any:
        await _seed(fake_tm)
        return await _BACKENDS[variant]().aread("/jq/assets/data.bin")

    result = asyncio.run(go())
    assert result.file_data is None
    assert result.error == (
        "Error reading file '/jq/assets/data.bin': "
        "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
    )


def test_png_read_through_composite_route(fake_tm: _FakeResourceManager) -> None:
    """The agent's composite backend routes a ``/skills/`` read of a PNG to the
    skills backend and returns the base64 payload."""

    async def go() -> Any:
        await _seed(fake_tm)
        return await build_backend().aread(f"{SKILLS_ROOT}jq/assets/logo.png")

    result = asyncio.run(go())
    assert result.error is None
    assert result.file_data["encoding"] == "base64"
    assert base64.standard_b64decode(result.file_data["content"]) == _PNG_BYTES
