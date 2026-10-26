"""The per-tool template-references registry: a base tool's declared ``template_refs`` extractor."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from tai42_contract.tools import TemplateRef

from tai42_skeleton.app import server as server_module
from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding import ToolBinding
from tai42_skeleton.tools.template_refs import TemplateRefsRegistry


def _extractor(kwargs: dict[str, Any]) -> list[TemplateRef]:
    return [TemplateRef(id="t/one", site="node 'a' expr")]


def test_the_registry_answers_the_extractor_per_tool_name_and_resets() -> None:
    registry = TemplateRefsRegistry()
    registry.register("composed", _extractor)
    assert registry.get("composed") is _extractor
    assert registry.get("other") is None
    registry.reset()
    assert registry.get("composed") is None


def test_a_duplicate_registration_raises() -> None:
    registry = TemplateRefsRegistry()
    registry.register("composed", _extractor)
    with pytest.raises(ValueError, match=r"^template-references extractor for tool 'composed' is already registered$"):
        registry.register("composed", _extractor)


def _binding(monkeypatch: pytest.MonkeyPatch, *, included: bool) -> tuple[ToolBinding, TemplateRefsRegistry]:
    app_mock = MagicMock(spec=server_module.TaiMCP)
    registry = TemplateRefsRegistry()
    app_mock._template_refs_registry = registry
    app_mock._manifest.should_include_tool.return_value = included
    binding = ToolBinding(app_mock)
    monkeypatch.setattr(binding, "bind_tool_func", lambda *_a, **_k: lambda fn: fn)
    return binding, registry


def test_the_decorator_registers_under_the_bound_name(monkeypatch: pytest.MonkeyPatch) -> None:
    binding, registry = _binding(monkeypatch, included=True)

    @binding.tool(template_refs=_extractor, name="composed")
    def composed_impl() -> int:
        return 1

    assert registry.get("composed") is _extractor
    assert binding.template_refs_extractor("composed") is _extractor


def test_a_manifest_excluded_tool_registers_no_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    binding, registry = _binding(monkeypatch, included=False)

    @binding.tool(template_refs=_extractor, name="composed")
    def composed_impl() -> int:
        return 1

    assert registry.get("composed") is None


def test_start_resets_the_registry_and_the_facet_reads_it() -> None:
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            app._template_refs_registry.register("composed", _extractor)
            assert app.tools.template_refs_extractor("composed") is _extractor
        async with app.app_context(Manifest.model_validate({})):
            assert app.tools.template_refs_extractor("composed") is None

    asyncio.run(run())
