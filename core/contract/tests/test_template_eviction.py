"""Which template ids a template eviction covers."""

import pytest

from tai42_contract.template import TemplateEviction


@pytest.mark.parametrize(
    ("eviction", "template_id", "covered"),
    [
        (TemplateEviction(path=None), "any/id", True),
        (TemplateEviction(path="docs/a"), "docs/a", True),
        (TemplateEviction(path="docs/a"), "docs/a@he", False),
        (TemplateEviction(path="docs/a"), "docs/ab", False),
        (TemplateEviction(path="docs", prefix=True), "docs/a", True),
        (TemplateEviction(path="docs/", prefix=True), "docs/deep/a", True),
        (TemplateEviction(path="docs", prefix=True), "docs", False),
        (TemplateEviction(path="docs", prefix=True), "docsx/a", False),
    ],
)
def test_covers(eviction: TemplateEviction, template_id: str, covered: bool) -> None:
    assert eviction.covers(template_id) is covered
