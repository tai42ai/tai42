"""The process-wide conversation target-bind-validator registry — the body behind
``app.conversations.register_target_validator``.

A plugin registers a validator under a target kind when its module loads (importing the
module runs its ``tai42_app.conversations.register_target_validator(...)`` call). Route
creation consults the registered validator for a route's target kind after the target
exists but before the row is written; a validator returning message lines refuses the
create with them (a 422), so a defect the target carries — a flow reading a state no
binding supplies — is caught at bind, not deferred to run time.

The registry is reset on every ``start()`` (like the preset write-validator registry) so
a reload re-imports the plugin modules and re-registers cleanly; a duplicate kind within
one load raises loudly (a silent overwrite could swap a target kind's bind gate out from
under it).
"""

from __future__ import annotations

from tai42_contract.conversations import ConversationTargetKind, TargetBindValidator


class TargetBindValidatorRegistry:
    def __init__(self) -> None:
        self._validators: dict[str, TargetBindValidator] = {}

    def register(self, target_kind: ConversationTargetKind, validator: TargetBindValidator) -> None:
        if target_kind in self._validators:
            raise ValueError(f"conversation target validator for kind {target_kind!r} is already registered")
        self._validators[target_kind] = validator

    def get(self, target_kind: str) -> TargetBindValidator | None:
        return self._validators.get(target_kind)

    def reset(self) -> None:
        self._validators.clear()
