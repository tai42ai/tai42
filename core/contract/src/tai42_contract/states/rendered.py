"""The rendered and resolved state-template models the states facet serves.

A stored template rendered by the platform: every program body as jq text, the fragment as a
plain schema, and the input programs in dependency order. Frozen, ``extra="forbid"``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tai42_contract.states.models import (
    PathSegment,
    StateRegimeRule,
    StateTemplateParameter,
    StateTemplateTrace,
)


class RenderedTemplateJq(BaseModel):
    """One ``template_jq`` program with its body rendered to jq text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: Literal["input", "update"]
    jq: str
    params: list[str]
    reads: list[list[PathSegment]] = Field(default_factory=list[list[PathSegment]])
    writes: list[list[PathSegment]] = Field(default_factory=list[list[PathSegment]])


class RenderedTemplateDeclarations(BaseModel):
    """A template's ``declarations`` section with its ``check`` rendered to jq text."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True)

    schema_: dict[str, Any] = Field(alias="schema")
    check: str | None = None


class RenderedStateTemplate(BaseModel):
    """A stored template as the platform renders it: every body is jq text, the fragment a plain schema.

    ``input_order`` lists the input programs dependency-first (a program after every sibling it
    calls). ``version`` is an opaque token that changes whenever anything the rendering read
    changes; compare it for equality only.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True)

    name: str
    description: str
    version: str
    parameters: dict[str, StateTemplateParameter]
    schema_: dict[str, Any] = Field(alias="schema")
    regimes: list[StateRegimeRule]
    declarations: RenderedTemplateDeclarations | None
    trace: StateTemplateTrace
    template_jq: dict[str, RenderedTemplateJq]
    input_order: list[str]


class RenderedAttachment(BaseModel):
    """One attachment of a rendered template on a state: its ``path``, effective ``parameters`` and ``declarations``.

    ``version`` is an opaque token that changes whenever the attachment or its rendered template
    changes; compare it for equality only.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: str
    template: RenderedStateTemplate
    path: list[PathSegment]
    parameters: dict[str, Any]
    declarations: dict[str, Any]
    version: str


class ResolvedTemplateJq(BaseModel):
    """A ``template_jq`` reference resolved on a state: the declaring ``template``, its ``program`` name and facts.

    ``purpose`` is the program's purpose (the one the resolution asked for), ``params`` its declared
    parameter names and ``path`` the attachment path the program's record subtree sits under.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    template: str
    program: str
    purpose: Literal["input", "update"]
    params: list[str]
    path: list[PathSegment]


class TemplateJqReference(BaseModel):
    """A ``template_jq`` reference to resolve on ``state``: the reference ``name``, the ``purpose`` it is used for.

    ``declared`` names templates the caller is about to attach (see ``AppStates.resolve_template_jq``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: str
    name: str
    purpose: Literal["input", "update"]
    declared: list[str] = Field(default_factory=list)
