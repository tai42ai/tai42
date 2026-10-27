"""Response models for the templates operations: template entries, upload, delete, render and cache clear."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, model_validator
from tai42_contract.app.responses import FanoutSummary

from tai42_skeleton.operations.response_models.storage import StoredBinaryEntry, StoredDocumentEntry, StoredTextEntry


class TemplateTextEntry(StoredTextEntry):
    """A text entry with its inferred input schema, or the template error that stopped the inference.

    The ``schema`` object is an inferred JSON schema (it may carry an ``x-tai42-inference: partial``
    marker when type inference is incomplete). Exactly one of ``schema`` and ``schema_error`` is set.
    The Python attribute is suffixed to avoid shadowing a ``BaseModel`` member; the wire key stays
    ``schema`` via the alias.
    """

    model_config = ConfigDict(populate_by_name=True)

    schema_: dict[str, JsonValue] | None = Field(alias="schema")
    schema_error: str | None

    @model_validator(mode="after")
    def _one_of_schema_or_error(self) -> TemplateTextEntry:
        if (self.schema_ is None) == (self.schema_error is None):
            raise ValueError("a text template entry carries exactly one of 'schema' and 'schema_error'")
        return self


class TemplateDocumentEntry(StoredDocumentEntry):
    """A document entry; its extracted text is not a template, so it has no schema."""

    model_config = ConfigDict(populate_by_name=True)

    schema_: None = Field(default=None, alias="schema")


class TemplateBinaryEntry(StoredBinaryEntry):
    """A binary entry; it is not a template, so it has no schema."""

    model_config = ConfigDict(populate_by_name=True)

    schema_: None = Field(default=None, alias="schema")


TemplateEntryView = Annotated[
    TemplateTextEntry | TemplateDocumentEntry | TemplateBinaryEntry, Field(discriminator="kind")
]


class TemplateEntry(RootModel[TemplateEntryView]):
    """One stored object as a template entry: the stored entry plus its inferred input schema."""


class TemplateUploadResult(BaseModel):
    """A template upload confirmation with the fleet cache-eviction summary."""

    path: str
    uploaded: bool
    fanout: FanoutSummary


class TemplateDeleteResult(BaseModel):
    """A template (or template-directory) delete confirmation with the fleet cache-eviction summary."""

    path: str
    deleted: bool
    fanout: FanoutSummary


class RenderedTemplate(BaseModel):
    """A rendered template."""

    rendered: str


class CacheClearResult(BaseModel):
    """A template-cache clear confirmation with the fleet cache-eviction summary."""

    cleared: bool
    fanout: FanoutSummary
