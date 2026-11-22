"""The render seam's error set: one family, one kind, never a transport failure."""

import pytest

import tai42_contract.template as template
from tai42_contract.errors import ErrorKind, error_kind, is_transport_error
from tai42_contract.template import (
    TemplateBodyError,
    TemplateNotFoundError,
    TemplateNotTextError,
    TemplateRenderError,
)

MEMBERS = (TemplateNotFoundError, TemplateNotTextError, TemplateBodyError)


@pytest.mark.parametrize("cls", [TemplateRenderError, *MEMBERS])
def test_every_member_is_the_authors_fault(cls: type[TemplateRenderError]) -> None:
    exc = cls("references stored id 'x', which could not be fetched: not stored")
    assert isinstance(exc, TemplateRenderError)
    assert error_kind(exc) is ErrorKind.BAD_INPUT
    assert is_transport_error(exc) is False
    assert str(exc) == "references stored id 'x', which could not be fetched: not stored"


def test_the_family_is_exported() -> None:
    for name in ("TemplateRenderError", "TemplateNotFoundError", "TemplateNotTextError", "TemplateBodyError"):
        assert name in template.__all__
