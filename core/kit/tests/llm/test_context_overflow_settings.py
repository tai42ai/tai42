import pytest

pytest.importorskip("langgraph")

from pydantic import ValidationError

from tai42_kit.llm.settings import ContextOverflowSettings


class TestContextOverflowMethodsValidator:
    def test_default_is_trimming(self):
        assert ContextOverflowSettings().methods == ["trimming"]

    def test_single_method_accepted(self):
        assert ContextOverflowSettings(methods=["summarization"]).methods == ["summarization"]

    def test_summarization_and_context_editing_compose(self):
        settings = ContextOverflowSettings(methods=["context_editing", "summarization"])
        assert settings.methods == ["context_editing", "summarization"]

    def test_empty_methods_allowed(self):
        # Explicitly empty = no context management; normalized, not rejected.
        assert ContextOverflowSettings(methods=[]).methods == []

    def test_duplicate_methods_deduped(self):
        settings = ContextOverflowSettings(methods=["context_editing", "summarization", "context_editing"])
        assert settings.methods == ["context_editing", "summarization"]

    def test_duplicate_trimming_deduped_to_single(self):
        # ["trimming","trimming"] dedupes to ["trimming"] -> valid, no conflict.
        assert ContextOverflowSettings(methods=["trimming", "trimming"]).methods == ["trimming"]

    def test_trimming_cannot_compose_with_others(self):
        with pytest.raises(ValidationError, match="cannot be composed"):
            ContextOverflowSettings(methods=["trimming", "summarization"])

    def test_unknown_method_rejected(self):
        with pytest.raises(ValidationError):
            ContextOverflowSettings(methods=["bogus"])  # type: ignore[list-item]  # intentionally invalid
