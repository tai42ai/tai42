import heapq
import time
from typing import Any

from tai42_contract.hooks.models import HookParams, TopicVerifierBinding

from tai42_skeleton.hooks.managers.base_hooks_manager import BaseHooksManager
from tai42_skeleton.hooks.settings import HooksSettings


class InMemoryHooksManager(BaseHooksManager):
    def __init__(self, settings: HooksSettings):
        super().__init__(settings)
        self._hooks: dict[str, dict[str, HookParams]] = {}
        self._name_topic_map: dict[str, str] = {}
        self._topic_verifiers: dict[str, dict[str, Any]] = {}
        # Full seen-set key -> monotonic expiry. Per-process, matching this manager's
        # single-worker validity (siblings do not share it, exactly as they do not
        # share registrations here); the Redis manager holds the cross-worker seen-set.
        self._webhook_seen: dict[str, float] = {}
        # A min-heap of ``(expiry, key)`` mirroring ``_webhook_seen`` in expiry order, so
        # a claim pops only the already-lapsed prefix instead of scanning every id. TTLs
        # vary per claim, so expiry order is NOT insertion order — hence a heap, not a
        # queue. A key holds one live heap entry at a time (a re-claim happens only after
        # its prior entry lapses and is popped), so the heap stays bounded by the live
        # window, not history.
        self._webhook_seen_expiry: list[tuple[float, str]] = []

    @property
    def hook_count(self) -> int:
        """Number of live in-memory hooks across every topic bucket.

        A synchronous read (no async API needed) so the settings-reset hook can name
        how many hooks a config reload is about to drop from this manager.
        """
        return sum(len(bucket) for bucket in self._hooks.values())

    async def register(self, params: HookParams) -> bool:
        self.validate_jq_fields(params)
        key = self.settings.get_hook_key(params.topic)

        prev_topic = self._name_topic_map.get(params.name)
        if prev_topic is not None and prev_topic != params.topic:
            # Re-registering under a new topic: drop the entry from the old
            # topic's bucket, or the stale hook keeps firing there forever.
            prev_key = self.settings.get_hook_key(prev_topic)
            bucket = self._hooks.get(prev_key)
            if bucket is not None:
                bucket.pop(params.name, None)
                if not bucket:
                    del self._hooks[prev_key]

        self._hooks.setdefault(key, {})[params.name] = params
        self._name_topic_map[params.name] = params.topic
        return True

    async def unregister(self, name: str) -> bool:
        topic = self._name_topic_map.pop(name, None)
        if not topic:
            return False

        key = self.settings.get_hook_key(topic)

        if key in self._hooks and name in self._hooks[key]:
            del self._hooks[key][name]
            if not self._hooks[key]:
                del self._hooks[key]
        return True

    async def list_hooks_by_topic(self, topic: str) -> dict[str, HookParams]:
        key = self.settings.get_hook_key(topic)
        # Return a copy, not the live bucket: ``on_event`` iterates this map
        # across an await (condition render), so a concurrent register/unregister
        # mutating the live dict mid-iteration would raise "dictionary changed
        # size during iteration".
        return dict(self._hooks.get(key, {}))

    async def list_hooks(self) -> dict[str, HookParams]:
        all_hooks: dict[str, HookParams] = {}
        for _, hooks in self._hooks.items():
            all_hooks.update(hooks)
        return all_hooks

    async def set_topic_verifier(self, topic: str, binding: dict[str, Any]) -> None:
        # Validate the shape on write (loud on a wrong shape) so both backends
        # enforce the same binding contract; store the canonical dict.
        self._topic_verifiers[topic] = TopicVerifierBinding.model_validate(binding).model_dump()

    async def get_topic_verifier(self, topic: str) -> dict[str, Any] | None:
        binding = self._topic_verifiers.get(topic)
        # Return a copy so a caller cannot mutate the stored binding in place.
        return dict(binding) if binding is not None else None

    async def delete_topic_verifier(self, topic: str) -> bool:
        return self._topic_verifiers.pop(topic, None) is not None

    async def all_topic_verifiers(self) -> dict[str, dict[str, Any]]:
        return {topic: dict(binding) for topic, binding in self._topic_verifiers.items()}

    async def claim_webhook_delivery(self, topic: str, replay_key: str, ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            raise ValueError(f"webhook replay claim requires a positive ttl_seconds, got {ttl_seconds!r}")
        # No await between the presence check and the write, so the check-and-set is
        # atomic under the single-threaded loop — the in-memory analogue of SET NX EX.
        now = time.monotonic()
        # Pop only the already-lapsed prefix off the expiry heap, not the whole set.
        # A popped entry is stale by construction; drop its id iff the id's CURRENT
        # expiry is also lapsed (a re-claim after a lapse holds a fresh, later expiry
        # whose own heap entry is untouched here).
        while self._webhook_seen_expiry and self._webhook_seen_expiry[0][0] <= now:
            _, lapsed_key = heapq.heappop(self._webhook_seen_expiry)
            current_expiry = self._webhook_seen.get(lapsed_key)
            if current_expiry is not None and current_expiry <= now:
                del self._webhook_seen[lapsed_key]
        key = self.settings.webhook_seen_key(topic, replay_key)
        if key in self._webhook_seen:
            return False
        expiry = now + ttl_seconds
        self._webhook_seen[key] = expiry
        heapq.heappush(self._webhook_seen_expiry, (expiry, key))
        return True
