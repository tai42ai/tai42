"""The ONE mid-form reaction chokepoint every reaction door reaches.

A reacting form names a ``reaction_tool`` on its durable request and declares, in
``format_payload["reactions"]``, which events fire it (a field changed, a page advanced, the
form submitted). While the form is open a channel (or the in-app surface) routes one of those
events, with the values filled so far, to :func:`react` — the single seam that:

* asserts the interaction is OPEN (a reacting FORM still pending) — raising otherwise;
* type-validates the partial values against the stored schema, reusing the answer path's own
  schema caps (``schema_mismatch``), never a reinvented check;
* runs the asker's ``reaction_tool`` through ``run_tool`` under the stored rebound identity and
  state context (exactly as the continuation does), bounded by ONE deadline here, so a handler
  that raises or times out surfaces LOUDLY — never a stale or silent value;
* validates the returned FORM UPDATE against the form's own schema (values/errors only for
  declared fields, options only for option-bearing fields, display only for declared slots);
* returns the update, STATELESS: it never records an answer, resolves the interaction, or
  rewrites the stored request. The half-filled form's state lives in the channel until submit.

Every reaction transport (the channel callback sibling door, the authenticated in-app door)
calls this one function, so the open-check, the deadline, and the update validation all live
once, covering every door.

The consumer's ``submitted`` check is ENFORCED BY THE SERVER, not the client. :func:`react`
stays stateless; the answer PATH records. Each answer door calls :func:`enforce_submitted_check`
after the schema check and before its atomic record: for a form that declares ``submitted`` it
runs the handler through this same chokepoint with the effective answer, refusing the answer on
the handler's per-field errors (the form stays open) or loudly on a handler raise/timeout. It is
a check, never a second recorder — the door that follows is the one recorder. A client that ran
``submitted`` first only shows errors early; the server never trusts it and never skips the check.
The expiry reaper records no consumer answer (it resolves an expired ask with an expiry marker,
never a submitted value), so no submitted check applies there — nothing unvetted can be recorded.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.interactions import AnswerFormat, FormOption, FormReactions
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.interactions.answer_check import schema_mismatch
from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.states.context import state_context

logger = logging.getLogger(__name__)

# The event kinds a reaction fires on, mirroring the contract ``FormReactions`` triggers.
_FIELD_CHANGED = "field_changed"
_PAGE_ADVANCED = "page_advanced"
_SUBMITTED = "submitted"


class FormReactionError(Exception):
    """Base for a mid-form reaction that could not complete."""


class FormReactionClosedError(FormReactionError):
    """The interaction is not an OPEN reacting form (missing, answered, or not reacting)."""


class FormReactionRequestError(FormReactionError):
    """The caller's reaction request is malformed (an undeclared event trigger, bad partial values)."""


class FormReactionHandlerError(FormReactionError):
    """The reaction handler raised, missed its deadline, or returned an invalid form update."""


class SubmittedCheckRejectedError(FormReactionError):
    """The form's ``submitted`` reaction returned per-field errors: the answer is refused and the form stays open.

    Carries the handler's ``errors`` map (field name -> message) so the door can surface it. Distinct from
    :class:`FormReactionHandlerError` (a handler that raised/timed out) — here the handler ran cleanly and
    DECLINED the submission.
    """

    def __init__(self, errors: dict[str, Any]) -> None:
        """Carry the handler's per-field ``errors`` (field name -> message) so the door can surface them."""
        self.errors = errors
        super().__init__(f"submission rejected by the form's submitted check: {errors}")


def _types_only_schema(schema: dict[str, Any]) -> dict[str, Any]:
    # ``schema`` reduced to a TYPE-only object schema: every ``enum`` stripped (membership is
    # not checked mid-form — a reaction may replace a choice list), nothing required (the
    # values are partial), unknown properties refused. The one shape both the partial values
    # and a handler's returned ``values`` are type-checked against.
    properties = schema.get("properties")
    props: dict[str, Any] = {}
    if isinstance(properties, dict):
        for name, prop in properties.items():
            if not isinstance(prop, dict):
                props[str(name)] = prop
                continue
            stripped = {key: value for key, value in prop.items() if key != "enum"}
            items = stripped.get("items")
            if stripped.get("type") == "array" and isinstance(items, dict):
                stripped = {**stripped, "items": {k: v for k, v in items.items() if k != "enum"}}
            props[str(name)] = stripped
    return {"type": "object", "properties": props, "required": [], "additionalProperties": False}


def _is_option_bearing(prop: dict[str, Any]) -> bool:
    # A field whose choice list a reaction may replace: a string with an enum, or an
    # array of strings.
    if prop.get("type") == "string":
        return isinstance(prop.get("enum"), list)
    items = prop.get("items")
    return prop.get("type") == "array" and isinstance(items, dict) and items.get("type") == "string"


def _declared_slots(payload: dict[str, Any]) -> set[str]:
    # Every display ``slot`` name declared across the form's pages — the only slots a
    # reaction's ``display`` update may fill.
    slots: set[str] = set()
    pages = payload.get("pages")
    if not isinstance(pages, list):
        return slots
    for page in pages:
        if not isinstance(page, dict):
            continue
        display = page.get("display")
        if not isinstance(display, list):
            continue
        for block in display:
            if isinstance(block, dict) and isinstance(block.get("slot"), str):
                slots.add(block["slot"])
    return slots


def _normalize_event(event: Any, reactions: FormReactions) -> dict[str, Any]:
    # Validate the inbound event against the form's declared triggers and return the exact
    # ``{kind, field?|page?}`` the handler receives. An event kind or field/page that is not a
    # declared trigger is a caller bug, refused loudly.
    if not isinstance(event, dict):
        raise FormReactionRequestError("reaction event must be an object with a 'kind'")
    kind = event.get("kind")
    if kind == _FIELD_CHANGED:
        field = event.get("field")
        if not isinstance(field, str) or field not in reactions.field_changed:
            raise FormReactionRequestError(f"field_changed event names {field!r}, not a declared field trigger")
        return {"kind": kind, "field": field}
    if kind == _PAGE_ADVANCED:
        page = event.get("page")
        if not isinstance(page, str) or page not in reactions.page_advanced:
            raise FormReactionRequestError(f"page_advanced event names {page!r}, not a declared page trigger")
        return {"kind": kind, "page": page}
    if kind == _SUBMITTED:
        if not reactions.submitted:
            raise FormReactionRequestError("submitted is not a declared reaction trigger")
        return {"kind": kind}
    raise FormReactionRequestError(f"unknown reaction event kind {kind!r}")


def _validate_update_values(values: Any, schema: dict[str, Any], declared: set[str]) -> None:
    if not isinstance(values, dict):
        raise FormReactionHandlerError("reaction update 'values' must be an object")
    unknown = set(values) - declared
    if unknown:
        raise FormReactionHandlerError(f"reaction update values name undeclared fields {sorted(unknown)}")
    mismatch = schema_mismatch(values, _types_only_schema(schema))
    if mismatch is not None:
        raise FormReactionHandlerError(f"reaction update values do not fit the schema: {mismatch[0]}")


def _validate_update_options(options: Any, properties: dict[str, Any]) -> None:
    if not isinstance(options, dict):
        raise FormReactionHandlerError("reaction update 'options' must be an object")
    for name, option_list in options.items():
        prop = properties.get(name)
        if not isinstance(prop, dict) or not _is_option_bearing(prop):
            raise FormReactionHandlerError(
                f"reaction update options for {name!r} require a string-enum or array-of-strings field"
            )
        if not isinstance(option_list, list) or not option_list:
            raise FormReactionHandlerError(f"reaction update options for {name!r} must be a non-empty list")
        try:
            for option in option_list:
                FormOption.model_validate(option)
        except Exception as exc:
            raise FormReactionHandlerError(f"reaction update options for {name!r} are malformed: {exc}") from exc


def _validate_update_errors(errors: Any, declared: set[str]) -> None:
    if not isinstance(errors, dict):
        raise FormReactionHandlerError("reaction update 'errors' must be an object")
    unknown = set(errors) - declared
    if unknown:
        raise FormReactionHandlerError(f"reaction update errors name undeclared fields {sorted(unknown)}")
    if not all(isinstance(message, str) for message in errors.values()):
        raise FormReactionHandlerError("reaction update error messages must be strings")


def _validate_update_display(display: Any, payload: dict[str, Any]) -> None:
    if not isinstance(display, dict):
        raise FormReactionHandlerError("reaction update 'display' must be an object")
    unknown = set(display) - _declared_slots(payload)
    if unknown:
        raise FormReactionHandlerError(f"reaction update display names undeclared slots {sorted(unknown)}")


def _validate_update(update: Any, schema: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    # Validate a handler's returned FORM UPDATE against the form's own schema: only the four
    # known keys; values/errors only for declared fields (values additionally type-fit the
    # schema); options only for option-bearing fields, each a non-empty list of FormOption;
    # display only for declared display slots. A violation is a handler bug, surfaced loudly.
    if not isinstance(update, dict):
        raise FormReactionHandlerError("reaction handler must return a form update object")
    extra = set(update) - {"values", "options", "errors", "display"}
    if extra:
        raise FormReactionHandlerError(f"reaction update carries unknown keys {sorted(extra)}")
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    declared = set(properties)
    if update.get("values") is not None:
        _validate_update_values(update["values"], schema, declared)
    if update.get("options") is not None:
        _validate_update_options(update["options"], properties)
    if update.get("errors") is not None:
        _validate_update_errors(update["errors"], declared)
    if update.get("display") is not None:
        _validate_update_display(update["display"], payload)
    return update


async def _run_reaction(
    *,
    reaction_tool: str,
    identity: str,
    fingerprint: str | None,
    state_ctx: Any,
    asked_by: list[str],
    interaction_id: str,
    event: dict[str, Any],
    values: dict[str, Any],
    deadline: float,
) -> Any:
    # Run the asker's reaction handler through ``run_tool`` under the stored rebound identity
    # and state context (the continuation's identity), bounded by the one deadline. A raise or a
    # deadline miss surfaces LOUDLY as a handler error — never a stale/silent value. STATELESS:
    # nothing is recorded, resolved, or rewritten.
    from tai42_skeleton.authz.execution import bind_execution_identity

    args = {"interaction_id": interaction_id, "event": event, "values": values}
    park_ctx: AbstractContextManager[Any] = state_context(state_ctx) if state_ctx is not None else nullcontext()
    try:
        async with bind_execution_identity(identity, bound_fingerprint=fingerprint or ""):
            with park_ctx:
                return await asyncio.wait_for(
                    tai42_app.tools.run_tool(reaction_tool, args, continues_chain=asked_by),
                    timeout=deadline,
                )
    except TimeoutError as exc:
        logger.exception(
            "reaction handler %r for interaction %s exceeded the %ss deadline", reaction_tool, interaction_id, deadline
        )
        raise FormReactionHandlerError(f"reaction handler {reaction_tool!r} exceeded the {deadline}s deadline") from exc
    except Exception as exc:
        logger.exception("reaction handler %r for interaction %s failed", reaction_tool, interaction_id)
        raise FormReactionHandlerError(f"reaction handler {reaction_tool!r} failed") from exc


async def enforce_submitted_check(
    interaction_id: str,
    answer_format: AnswerFormat,
    format_payload: dict[str, Any] | None,
    answer: Any,
) -> None:
    """Run the form's ``submitted`` reaction ON THE SERVER before the answer records; refuse if it declines.

    The authoritative enforcement of the consumer's submit check: every answer door calls this after the
    schema check (``check_answer``) and BEFORE the atomic record. A no-op unless the interaction is a FORM
    declaring the ``submitted`` trigger (the contract couples that trigger to a ``reaction_tool``). It reuses
    the one :func:`react` chokepoint (the same identity, deadline and update validation), so a client that
    ran ``submitted`` first is only an optimisation for early errors, never the enforcement — the server
    never trusts it and never skips.

    Raises :class:`SubmittedCheckRejectedError` (carrying the handler's per-field ``errors``) when the handler
    declines the submission — the door refuses the answer and the form stays open. Raises
    :class:`FormReactionHandlerError` when the handler raises or times out, and
    :class:`FormReactionClosedError` when the form is no longer open — both refuse loudly; nothing records.
    Returns normally (the answer may record) only when the handler ran cleanly with no errors.
    """
    if answer_format is not AnswerFormat.FORM:
        return
    reactions_raw = (format_payload or {}).get("reactions")
    if reactions_raw is None or not FormReactions.model_validate(reactions_raw).submitted:
        return
    update = await react(interaction_id, {"kind": _SUBMITTED}, answer if isinstance(answer, dict) else {})
    errors = update.get("errors")
    if errors:
        raise SubmittedCheckRejectedError(errors)


async def react(interaction_id: str, event: Any, partial_values: Any) -> dict[str, Any]:
    """Run the open form's reaction for ``event`` with the values filled so far; return its FORM UPDATE.

    Raises :class:`FormReactionClosedError` when the interaction is not an open reacting form,
    :class:`FormReactionRequestError` for a malformed event / partial values, and
    :class:`FormReactionHandlerError` when the handler raises, times out, or returns an invalid
    update. STATELESS — the durable record is never touched.
    """
    if not interactions_store_configured():
        # With no store configured no interaction can exist — the same closed signal a
        # missing interaction gives, so the door is no configured oracle.
        raise FormReactionClosedError("interaction is not open")
    store = InteractionStore(interactions_settings().key_prefix)
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        state = await store.get_state(r, interaction_id)
        if state is None or state.status != "pending":
            raise FormReactionClosedError("interaction is not open")
        request = state.request
        if request.reaction_tool is None or request.answer_format is not AnswerFormat.FORM:
            raise FormReactionClosedError("interaction is not a reacting form")
        payload = request.format_payload or {}
        schema = payload.get("schema")
        reactions_raw = payload.get("reactions")
        if not isinstance(schema, dict) or request.continuation_identity is None or reactions_raw is None:
            # A persisted reacting form always carries a schema, a continuation identity, and a
            # reactions block (the request model enforces it); reaching here is a platform bug.
            raise RuntimeError(f"reacting interaction {interaction_id!r} is missing its schema/identity/reactions")
        reactions = FormReactions.model_validate(reactions_raw)
        normalized_event = _normalize_event(event, reactions)
        if not isinstance(partial_values, dict):
            raise FormReactionRequestError("reaction values must be an object")
        mismatch = schema_mismatch(partial_values, _types_only_schema(schema))
        if mismatch is not None:
            raise FormReactionRequestError(f"reaction values do not fit the schema: {mismatch[0]}")
        fingerprint = await store.continuation_fingerprint(r, interaction_id)
        identity = request.continuation_identity
        state_ctx = request.continuation_state_context
        reaction_tool = request.reaction_tool
        asked_by = list(request.asked_by)
    # The handler runs OUTSIDE the store connection: a reaction touches no durable state.
    update = await _run_reaction(
        reaction_tool=reaction_tool,
        identity=identity,
        fingerprint=fingerprint,
        state_ctx=state_ctx,
        asked_by=asked_by,
        interaction_id=interaction_id,
        event=normalized_event,
        values=partial_values,
        deadline=interactions_settings().reaction_deadline_seconds,
    )
    return _validate_update(update, schema, payload)
