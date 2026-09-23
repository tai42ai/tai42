"""The one door-layer state BINDING shape, identical on every runnable definition.

A binding is OPTIONAL on a preset, a channel route, a hook and a schedule; it attaches one
or more states to a run and, for each, injects template-input values into the run's input
BEFORE the dispatch and applies template/custom updates AFTER it. The SAME document is
repeated per NODE by a flow engine consuming the platform. Tools stay PURE — they never see
the binding; the door applies it around them at the shared dispatch chokepoint.

Only the SHAPE lives here (a contract holds models, never logic): every authored jq slot is a
:class:`~tai42_contract.template.TemplatedText` — inline ``content`` or a stored ``id`` — that
the binding door RENDERS to its jq program before compiling/evaluating it; the render never
runs inside a model validator. The resolve/merge/apply seam and the save-time
validate-and-attach live in the skeleton, and the wire shape mirrors the Studio api-client
``stateBinding`` schema exactly.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tai42_contract.states.models import STATE_NAME_RE
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY, TemplatedText, expression_annotation


class StateInjection(BaseModel):
    """One input injection placed into the run input under ``into`` before the dispatch.

    Exactly one source is set: ``template_jq`` names an ``input``-purpose template jq
    (``name`` or ``<template>.<name>``) evaluated over the record, or ``jq`` is a templated
    text rendering to a custom program over the record (its ``.``) with the run input bound as
    ``$input``. ``into`` IS the adapter for an injection — the run-input field the value lands
    under. Frozen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_jq: str | None = None
    jq: Annotated[
        TemplatedText | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="injection jq",
                    blurb="the attached record's subtree, or {} when no record exists yet",
                    variables=[("input", "the run input the dispatch is about to run on", {"value": 2})],
                    returns="the value placed into the run input at 'into'",
                    sample={"count": 3},
                )
            }
        ),
    ] = None
    into: str = Field(min_length=1)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> StateInjection:
        if (self.template_jq is None) == (self.jq is None):
            raise ValueError("an injection sets exactly one of 'template_jq' or 'jq'")
        return self


class StateUpdate(BaseModel):
    """One update applied through the store after the dispatch.

    Exactly one source is set: ``template_jq`` names an ``update``-purpose template jq
    (``name`` or ``<template>.<name>``) whose ``.input`` object the optional ``adapter`` (a
    templated text rendering to a jq over the tool output, its ``.``, with the run input bound
    as ``$input``) shapes from the run's output/input; or ``jq`` is a templated text rendering
    to a custom program over the tool output (its ``.``) with the run input bound as ``$input``
    and the record as ``$record``, authoring the whole op batch itself (a custom update carries
    no adapter). ``op_id`` is an optional templated text rendering to an idempotency-key
    expression. Frozen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_jq: str | None = None
    jq: Annotated[
        TemplatedText | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="custom update jq",
                    blurb="the run's output the update runs after",
                    variables=[
                        ("input", "the run input the dispatch ran on", {"value": 2}),
                        ("record", "the attached record's subtree, or {} when no record exists yet", {"count": 3}),
                    ],
                    returns="the op batch (a list of ops) applied to the record",
                    sample={"ok": True},
                )
            }
        ),
    ] = None
    adapter: Annotated[
        TemplatedText | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="update adapter",
                    blurb="the run's output the update runs after",
                    variables=[("input", "the run input the dispatch ran on", {"value": 2})],
                    returns="the template program's '.input' object",
                    sample={"ok": True},
                )
            }
        ),
    ] = None
    op_id: Annotated[
        TemplatedText | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="op_id expression",
                    blurb="the run's output the update runs after",
                    variables=[("input", "the run input the dispatch ran on", {"value": 2})],
                    returns="a string idempotency key, or null for no key",
                    sample={"ok": True},
                )
            }
        ),
    ] = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> StateUpdate:
        if (self.template_jq is None) == (self.jq is None):
            raise ValueError("an update sets exactly one of 'template_jq' or 'jq'")
        if self.jq is not None and self.adapter is not None:
            raise ValueError(
                "a custom-jq update carries no 'adapter' — it authors the batch over the tool output "
                "(its .) with $input and $record bound"
            )
        return self


class StateAttach(BaseModel):
    """One state attached by a binding.

    Carries the ``state`` name, the ``templates`` to attach on it (attach-on-use, idempotent at save),
    the ``subject_expr`` (a templated text rendering to a jq over the run input → a full subject object
    or the record KEY, its scope taken from the ambient door context and its kind from the state's
    declared subject kind), an optional ``scope_expr`` (a templated text rendering to a BOOLEAN predicate
    over the run input evaluated first — ``false`` skips this state for the run, a non-boolean is a loud
    error, absent engages), and the ordered ``input_injections`` / ``updates``. Frozen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: str
    templates: list[str] = Field(default_factory=list[str])
    subject_expr: Annotated[
        TemplatedText,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="subject expression",
                    blurb="the run input the dispatch is about to run on",
                    returns="a full subject object {target_kind, target_name, kind, key}, or a bare key string",
                    sample={"account_id": "acct_42"},
                )
            }
        ),
    ]
    scope_expr: Annotated[
        TemplatedText | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="scope expression",
                    blurb="the run input the dispatch is about to run on",
                    returns="a boolean — false skips this state for the run, true (or absent) engages it",
                    sample={"account_id": "acct_42"},
                )
            }
        ),
    ] = None
    input_injections: list[StateInjection] = Field(default_factory=list[StateInjection])
    updates: list[StateUpdate] = Field(default_factory=list[StateUpdate])

    @model_validator(mode="after")
    def _check_state_name(self) -> StateAttach:
        if not STATE_NAME_RE.fullmatch(self.state):
            raise ValueError(f"state name {self.state!r} must match {STATE_NAME_RE.pattern}")
        return self


class StateBinding(BaseModel):
    """The optional binding a door carries: one or more :class:`StateAttach` entries. Frozen.

    The same shape is stored on ``PresetBody``/``PresetSeed``, ``TargetConversationConfig``,
    ``HookRegister`` and ``ScheduleCreate``, deposited on ``ToolInvocation`` through the
    ambient dispatch context, and repeated per node by a flow engine.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    states: list[StateAttach] = Field(min_length=1)


__all__ = ["StateAttach", "StateBinding", "StateInjection", "StateUpdate"]
