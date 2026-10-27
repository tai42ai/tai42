"""Response models returned by operations across several domains: bare string lists and document-version lists."""

from __future__ import annotations

from pydantic import RootModel
from tai42_contract.versioning.models import DocumentVersion


class StringList(RootModel[list[str]]):
    """A bare list of strings — the public-route pins and the marketplace category / item-kind vocabularies."""


class DocumentVersionList(RootModel[list[DocumentVersion]]):
    """A document's append-only version history — the policy history and the preset history serve this shape."""


class StringListResponse(RootModel[list[str]]):
    """A bare array of strings (verifier names, template ids, tool names)."""
