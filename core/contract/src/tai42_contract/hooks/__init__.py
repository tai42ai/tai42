"""Hooks contract: request body, stored models, and manager protocol.

The ``HookRegister`` request body, the stored ``HookParams`` /
``TopicVerifierBinding`` models, and the ``HooksManager`` protocol.
"""

from __future__ import annotations

from tai42_contract.hooks.manager import HooksManager
from tai42_contract.hooks.models import HOOK_DELIVERY_BLURB, HookParams, HookRegister, HookSubject, TopicVerifierBinding

__all__ = ["HOOK_DELIVERY_BLURB", "HookParams", "HookRegister", "HookSubject", "HooksManager", "TopicVerifierBinding"]
