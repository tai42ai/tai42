"""``LLMSettings.reasoning_effort`` + the post-construction effort validation at ``get_llm``.

The generic cross-provider effort lever is carried into the constructor kwargs like
``max_tokens``/``temperature``; a level the built model does not declare is refused
loudly against its profile, while a model declaring no levels lets the value pass
through to the provider.
"""

from __future__ import annotations

from typing import Any

import pytest

from tai42_kit.llm import models
from tai42_kit.llm.settings import LLMSettings


class _Fake:
    """A built chat model carrying only a ``profile`` and a label for effort validation."""

    def __init__(self, levels: list[str] | None) -> None:
        self.profile = {} if levels is None else {"reasoning_effort_levels": levels}
        self.model_name = "fake-model"


def test_reasoning_effort_is_carried_through_with_fallbacks() -> None:
    merged = LLMSettings(reasoning_effort="high").with_fallbacks({})
    assert merged["reasoning_effort"] == "high"
    # Unset stays absent (exclude_none), so nothing reaches the constructor.
    assert "reasoning_effort" not in LLMSettings().with_fallbacks({})


def _patch_build(monkeypatch: pytest.MonkeyPatch, levels: list[str] | None) -> None:
    models._cached_llm.cache_clear()

    def _build(provider: str, **kwargs: Any) -> Any:
        return _Fake(levels)

    monkeypatch.setattr(models, "_build_llm", _build)


def test_effort_outside_declared_levels_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_build(monkeypatch, ["low", "medium", "high"])
    with pytest.raises(ValueError, match="reasoning_effort 'max'") as excinfo:
        models.get_llm("openai", reasoning_effort="max")
    assert "fake-model" in str(excinfo.value)
    assert "low" in str(excinfo.value)


def test_declared_level_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_build(monkeypatch, ["low", "medium", "high"])
    llm = models.get_llm("openai", reasoning_effort="high")
    assert llm is not None


def test_no_declared_levels_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_build(monkeypatch, None)
    llm = models.get_llm("openai", reasoning_effort="anything")
    assert llm is not None
