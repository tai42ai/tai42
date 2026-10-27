"""Response models for the hooks operations: hook registrations, topic verifiers, trigger links and hook runs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, JsonValue
from tai42_contract.hooks import HookParams

from tai42_skeleton.hooks.trigger_auth import TriggerAuth


class HookListView(BaseModel):
    """The registered hooks plus the live per-topic verifier bindings and derived trigger-auth axis.

    ``topic_verifiers`` values are dumped binding metadata (an open,
    secret-adjacent map, kept opaque so a bound config is not widened onto the wire);
    ``trigger_auth`` maps each visible topic to how its webhook ingress door
    authenticates, derived live, never stored.
    """

    items: list[HookParams]
    total: int
    topic_verifiers: dict[str, JsonValue]
    trigger_auth: dict[str, TriggerAuth]


class HookRegisterResult(BaseModel):
    """A hook upsert confirmation — ``registered`` is ``True`` for a create and a replace alike."""

    registered: bool
    name: str


class RemovedByName(BaseModel):
    """A remove-by-name confirmation (hook unregister, trigger-link delete)."""

    removed: bool
    name: str


class TopicVerifierView(BaseModel):
    """A topic-to-verifier binding confirmation."""

    topic: str
    verifier: str


class RemovedByTopic(BaseModel):
    """A remove-by-topic confirmation (topic-verifier unbind)."""

    removed: bool
    topic: str


class TriggerLinkCreated(BaseModel):
    """A freshly minted trigger link.

    ``token`` is a ONE-TIME secret — it appears only here (nothing else stores or lists it); typing must not
    widen where it is logged. ``expires_at`` is an ISO-8601 timestamp string, or ``null`` for a permanent link.
    """

    name: str
    trigger_path: str
    token: str
    topic: str
    expires_at: str | None


class TriggerLinkView(BaseModel):
    """One listed trigger link: the stored record plus its token-hash PREFIX and derived ``trigger_auth`` axis.

    Never a raw token — none is stored. ``created_at`` is an ISO-8601 timestamp string; ``expires_at`` is one
    or ``null`` for a permanent link.
    """

    name: str
    topic: str
    execution_key: str
    execution_key_fingerprint: str
    require_api_key: bool
    tool_kwargs: dict[str, JsonValue] | None
    created_by: str | None
    created_at: str
    expires_at: str | None
    token_hash_prefix: str
    trigger_auth: TriggerAuth


class TriggerLinkListView(BaseModel):
    """Every live trigger link."""

    items: list[TriggerLinkView]
    total: int


class HookRunClaimIntentView(BaseModel):
    """A resume or take claim a hook run's hook announced and did not confirm."""

    action: Literal["resume", "take"]
    ids: list[str]


class HookRunView(BaseModel):
    """One kept hook run, without its event payload or its start spec (either may carry secrets of the sender).

    ``status`` is ``pending`` (due at ``next_attempt_at``), ``running`` (an attempt holds its lease) or
    ``failed`` (held until an operator retries or discards it). ``acted`` says whether and how the
    hook acted on its parked entries: ``""`` not at all, ``start`` started its tool, ``cancel``,
    ``resume`` or ``take`` on ``acted_ids``. ``claim_intent`` is set while a claim is still to be
    settled, and on a failed run whose claim could not be told.
    """

    id: str
    topic: str
    hook: str
    status: Literal["pending", "running", "failed"]
    attempts: int
    next_attempt_at: datetime
    acted: Literal["", "start", "cancel", "resume", "take"]
    acted_ids: list[str]
    claim_intent: HookRunClaimIntentView | None
    last_error: str | None
    last_error_kind: str | None
    created_at: datetime
    failed_at: datetime | None


class HookRunsPage(BaseModel):
    """One page of hook runs with the totals of every outstanding and every failed run.

    Outstanding runs come first, the soonest due first; failed runs follow, the most recently
    failed first. ``next_cursor`` is the cursor the next page reads from, ``null`` on the last page.
    """

    items: list[HookRunView]
    next_cursor: str | None
    outstanding: int
    failed: int


class HookRunDiscarded(BaseModel):
    """A discarded failed hook run's id."""

    discarded: str
