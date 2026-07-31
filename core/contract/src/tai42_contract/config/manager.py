"""ConfigManager abstract base class.

The :class:`ConfigManager` ABC is the canonical pluggable-provider seam for
configuration: it reads/writes env (key-value, including secrets) and the tool
manifest. Concrete providers — ``file`` (the zero-dep default), ``k8s``, a future
``vault`` — live downstream, following the same pattern as ``TaiApp``: the
contract provides the interface, providers the implementation. No tenant /
hosted-SaaS coupling lives here.

The connectors token store is not a config-manager concern: it is reached
through the ``AppConnectors`` facet (``tai42_app.connectors.token_store``),
independent of the file/k8s CONFIG mode.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any


class ConfigManager(ABC):
    """Abstract base for environment and manifest configuration backends.

    Each config mode (file, k8s, ...) provides a concrete implementation.
    Consumers access the active manager via ``tai42_app.config.config_manager``.
    """

    # -- Environment configuration -------------------------------------------

    @abstractmethod
    def read_env(self) -> dict[str, str]:
        """Read environment key-value pairs.

        raises:
            FileNotFoundError: If the env source does not exist.
        """

    @abstractmethod
    def write_env(self, config: dict[str, str]) -> None:
        """Write / merge environment key-value pairs.

        Merges *config* with the existing configuration, preserving keys
        that are not present in *config*. Filters out empty values.
        """

    # -- Manifest configuration ----------------------------------------------

    @abstractmethod
    def read_manifest(self) -> dict[str, Any]:
        """Read the active manifest.

        raises:
            FileNotFoundError: If the manifest does not exist.
        """

    @abstractmethod
    def read_manifest_preserved(self) -> dict[str, Any]:
        """Read the active manifest with ``!ENV`` tags preserved.

        Unlike :meth:`read_manifest`, which returns the runtime view with
        every ``!ENV`` reference resolved to its environment value, this
        returns the manifest with each ``!ENV <expr>`` node kept as its literal
        ``"!ENV <expr>"`` marker string — the placeholders are NOT resolved, so
        no secret values are read. The marker strings are plain (JSON-safe)
        strings.

        raises:
            FileNotFoundError: If the manifest does not exist.
        """

    @abstractmethod
    def read_defaults_manifest(self) -> dict[str, Any]:
        """Read the defaults / template manifest (empty dict if not found)."""

    @abstractmethod
    def write_manifest(self, manifest: dict[str, Any]) -> None:
        """Write manifest with three-way merge (defaults + current + new).

        Preserves YAML ``!ENV`` tags in the output.

        Ownership rule: feature code mutates the manifest through
        :meth:`mutate_manifest` / :meth:`replace_manifest`, whose whole-span
        transaction protects a read-modify-write against a concurrent writer. A
        direct ``write_manifest`` is reserved for config-layer internals
        (startup/bootstrap and the defaults-merge path).
        """

    @abstractmethod
    def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Atomically read-modify-write the manifest.

        Reads the current persisted manifest (the PRESERVED view — every
        ``!ENV <expr>`` kept as its literal marker string, never resolved to a
        secret) inside the write transaction, runs ``mutator`` to edit that
        document IN PLACE, and persists the edited document — untouched keys
        keep their values (and, in a round-trip implementation, their
        comments); no other writer's change can interleave between the read and
        the write. Returns the persisted dict. A ``mutator`` exception aborts
        with nothing written and propagates unchanged.

        The implementation holds exclusive access across the whole read → mutate
        → write span: a file-backed manager takes a lock; an API-backed manager
        uses optimistic-concurrency retry, re-reading and re-running ``mutator``
        on a conflict — so ``mutator`` MUST be re-runnable / pure (no side
        effects beyond editing the passed document). Persistence is atomic. The
        document is never resolved, so a secret can never bake to disk;
        validation against the resolved view is the caller's concern (the
        contract stays schema-free).
        """

    @abstractmethod
    def replace_manifest(self, document: dict[str, Any]) -> dict[str, Any]:
        """Atomically replace the whole persisted manifest.

        ``document`` becomes the entire stored manifest: a key absent from
        ``document`` is DELETED — nothing from the old document survives
        uninvited (defaults may backfill missing keys per the implementation's
        defaults contract). The seam persists ``document`` verbatim; it does
        not read or re-preserve it. Its transactional guarantee matches
        :meth:`mutate_manifest` — exclusive access across the whole span and
        atomic persistence. The PRESERVED view is a CALLER obligation here, not
        a seam guarantee: the caller MUST build ``document`` from the preserved
        view (``!ENV`` marker strings, never values obtained via
        :meth:`read_manifest`), because a document carrying resolved secrets
        would be persisted verbatim and bake those secrets to disk. Returns the
        persisted dict.
        """
