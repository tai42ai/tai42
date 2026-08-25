"""Every platform event topic must be SUBSCRIBABLE: a topic a hook cannot
register on fires into a namespace nobody can occupy (the original P39 gap —
dotted topics vs the hook door's single-segment charset). The pin builds a real
``HookRegister`` on each emitted topic, so a new event topic that hooks cannot
subscribe to fails here at introduction, not in production."""

from tai42_contract.hooks.models import HookRegister

from tai42_skeleton.channels.inbound import ANSWER_REJECTED_EVENT_TOPIC, CALLBACK_DISCARDED_EVENT_TOPIC
from tai42_skeleton.interactions.helper import DELIVERY_FAILED_EVENT_TOPIC
from tai42_skeleton.interactions.reaper import ASK_EXPIRED_UNANSWERED_EVENT_TOPIC

PLATFORM_EVENT_TOPICS = (
    ANSWER_REJECTED_EVENT_TOPIC,
    CALLBACK_DISCARDED_EVENT_TOPIC,
    DELIVERY_FAILED_EVENT_TOPIC,
    ASK_EXPIRED_UNANSWERED_EVENT_TOPIC,
)


def test_every_platform_event_topic_is_hook_subscribable():
    for topic in PLATFORM_EVENT_TOPICS:
        hook = HookRegister(
            name="watchdog",
            topic=topic,
            tool="notify_user",
            tool_kwargs={"message": "x"},
            execution_key="usr-watchdog",
        )
        assert hook.topic == topic
