"""Conversation bridge contract types.

The messaging bridge turns an inbound message — from an authed API caller or a
registered channel adapter — into an agent turn whose answer is durably stored and
delivered back. This package holds only the wire/record shapes that surface crosses;
the routing-row manager, turn engine, answer store and delivery executor are the
skeleton's, reached through its ``AppConversations`` facet.
"""

from __future__ import annotations

from tai42_contract.conversation_target import ConversationTargetKind
from tai42_contract.conversations.answers import (
    AnswerPart,
    AnswerStatus,
    ConversationAnswer,
    joined_answer_text,
)
from tai42_contract.conversations.inbound import (
    EVENT_ID_MAX_CHARS,
    EVENT_KIND_RE,
    BlankInboundTextError,
    ConversationEvent,
    ConversationEventSubmission,
    ConversationMessage,
)
from tai42_contract.conversations.inbound_form import (
    INBOUND_FORM_MAX_BYTES,
    INBOUND_FORM_MAX_DEPTH,
    validate_bounded_object,
    validate_inbound_form,
)
from tai42_contract.conversations.overlap import OverlapPolicy, TurnSupersededError
from tai42_contract.conversations.persons import Person, PersonAddress
from tai42_contract.conversations.receipts import DeliveryReceipt
from tai42_contract.conversations.routes import (
    CONVERSATION_MODES,
    ROUTE_NAME_RE,
    ConversationDoor,
    ConversationMode,
    ConversationRoute,
    ConversationRouteCreate,
    TargetBindValidator,
)
from tai42_contract.conversations.targets import (
    GREETING_PLACEHOLDER,
    CrossTargetMergeError,
    MultichannelDisabledError,
    NotLinkedError,
    PairCodeInvalidError,
    TargetConversationConfig,
)
from tai42_contract.conversations.turn_ref import (
    ConversationTurnRef,
    current_conversation_turn,
    reset_conversation_turn,
    set_conversation_turn,
)
from tai42_contract.entry_params import (
    ENTRY_PARAM_KEY_RE,
    ENTRY_PARAM_VALUE_MAX_CHARS,
    ENTRY_PARAMS_MAX_COUNT,
    ENTRY_PARAMS_MAX_TOTAL_BYTES,
    validate_entry_params,
)

__all__ = [
    "CONVERSATION_MODES",
    "ENTRY_PARAMS_MAX_COUNT",
    "ENTRY_PARAMS_MAX_TOTAL_BYTES",
    "ENTRY_PARAM_KEY_RE",
    "ENTRY_PARAM_VALUE_MAX_CHARS",
    "EVENT_ID_MAX_CHARS",
    "EVENT_KIND_RE",
    "GREETING_PLACEHOLDER",
    "INBOUND_FORM_MAX_BYTES",
    "INBOUND_FORM_MAX_DEPTH",
    "ROUTE_NAME_RE",
    "AnswerPart",
    "AnswerStatus",
    "BlankInboundTextError",
    "ConversationAnswer",
    "ConversationDoor",
    "ConversationEvent",
    "ConversationEventSubmission",
    "ConversationMessage",
    "ConversationMode",
    "ConversationRoute",
    "ConversationRouteCreate",
    "ConversationTargetKind",
    "ConversationTurnRef",
    "CrossTargetMergeError",
    "DeliveryReceipt",
    "MultichannelDisabledError",
    "NotLinkedError",
    "OverlapPolicy",
    "PairCodeInvalidError",
    "Person",
    "PersonAddress",
    "TargetBindValidator",
    "TargetConversationConfig",
    "TurnSupersededError",
    "current_conversation_turn",
    "joined_answer_text",
    "reset_conversation_turn",
    "set_conversation_turn",
    "validate_bounded_object",
    "validate_entry_params",
    "validate_inbound_form",
]
