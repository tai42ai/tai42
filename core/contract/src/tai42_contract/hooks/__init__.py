"""Hooks contract: the ``HookRegister`` request body, the stored ``HookParams`` /
``TopicVerifierBinding`` models + the ``HooksManager`` protocol."""

from __future__ import annotations

from tai42_contract.hooks.manager import HooksManager
from tai42_contract.hooks.models import HookParams, HookRegister, HookSubject, TopicVerifierBinding

__all__ = ["HookParams", "HookRegister", "HookSubject", "HooksManager", "TopicVerifierBinding"]
