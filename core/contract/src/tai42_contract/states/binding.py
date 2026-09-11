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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tai42_contract.states.models import STATE_NAME_RE
from tai42_contract.template import TemplatedText


class StateInjection(BaseModel):
    """One input injection placed into the run input under ``into`` before the dispatch.

    Exactly one source is set: ``template_jq`` names an ``input``-purpose template jq
    (``name`` or ``<template>.<name>``) evaluated over the record, or ``jq`` is a templated
    text rendering to a custom program over ``{record, input}``. ``into`` IS the adapter for
    an injection — the run-input field the value lands under. Frozen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_jq: str | None = None
    jq: TemplatedText | None = None
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
    templated text rendering to a jq over ``{output, input}``) shapes from the run's
    output/input; or ``jq`` is a templated text rendering to a custom program over ``{record,
    output, input}`` authoring the whole op batch itself (a custom update carries no adapter).
    ``op_id`` is an optional templated text rendering to an idempotency-key expression.
    Frozen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_jq: str | None = None
    jq: TemplatedText | None = None
    adapter: TemplatedText | None = None
    op_id: TemplatedText | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> StateUpdate:
        if (self.template_jq is None) == (self.jq is None):
            raise ValueError("an update sets exactly one of 'template_jq' or 'jq'")
        if self.jq is not None and self.adapter is not None:
            raise ValueError(
                "a custom-jq update carries no 'adapter' — it authors the batch over {record, output, input}"
            )
        return self


class StateAttach(BaseModel):
    """One state attached by a binding: the ``state`` name, the ``templates`` to attach on it
    (attach-on-use, idempotent at save), the ``subject_expr`` (a templated text rendering to a
    jq over the run input → a full subject object or the record KEY, its scope taken from the
    ambient door context and its kind from the state's declared subject kind), an optional
    ``scope_expr`` (a templated text rendering to a BOOLEAN predicate over the run input
    evaluated first — ``false`` skips this state for the run, a non-boolean is a loud error,
    absent engages), and the ordered ``input_injections`` / ``updates``. Frozen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: str
    templates: list[str] = Field(default_factory=list[str])
    subject_expr: TemplatedText
    scope_expr: TemplatedText | None = None
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
    ambient dispatch context, and repeated per node by a flow engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    states: list[StateAttach] = Field(min_length=1)


__all__ = ["StateAttach", "StateBinding", "StateInjection", "StateUpdate"]
