"""Hooks feature — webhook-topic -> tool dispatch.

The ``HookParams`` model and ``HooksManager`` protocol are the contract
(``tai42_contract.hooks``); the classes here implement them. ``managers`` holds the
in-memory and redis-backed registries, ``cache`` the manager singleton accessor,
``payload_parser`` the any-content-type webhook body parser, and ``settings`` the
``HOOKS_*`` env configuration.

The registry is Redis-backed when ``HOOKS_REDIS_URL`` is set, and hooks are then
shared across every worker. With no ``HOOKS_REDIS_URL`` the registry is in-memory
and per-process: registrations and deliveries never leave the worker that made
them, so this mode is valid ONLY for a single worker — with more than one worker
(or a separate backend worker) sibling processes will not see each other's hooks.
Set ``HOOKS_REDIS_URL`` (or ``TAI_DEFAULT_REDIS_URL``) for shared state; under
``tai serve`` with more than one worker, each worker boot, reload and config write
refuses it without the store
(:func:`~tai42_skeleton.app.boot_rules.require_hooks_store_for_workers`), and every
boot, reload and config write refuses a registered task backend without it
(:func:`~tai42_skeleton.app.boot_rules.require_hooks_store_for_backend`).
"""

from tai42_skeleton.hooks.cache import get_hooks_manager
from tai42_skeleton.hooks.payload_parser import parse_any_payload
from tai42_skeleton.hooks.settings import HooksSettings

__all__ = ["HooksSettings", "get_hooks_manager", "parse_any_payload"]
