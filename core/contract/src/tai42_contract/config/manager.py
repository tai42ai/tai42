"""ConfigManager abstract base class.

The :class:`ConfigManager` ABC is the canonical pluggable-provider seam for
configuration: it reads/writes env (key-value, including secrets) and the tool
manifest. The ``file`` provider (the zero-dep default) is built in; every other
provider ships downstream and is selected by config mode, following the same
pattern as ``TaiApp``: the contract provides the interface, providers the
implementation. No tenant / hosted-SaaS coupling lives here.

A provider makes each single write atomic and exclusive, and also grants an exclusive write
span (:meth:`ConfigManager.exclusive_write`) a writer holds across a whole read → validate →
write sequence, so every process sharing the store, and every writer of one process, sees the
writes of the others one at a time.

The connectors token store is not a config-manager concern: it is reached
through the ``AppConnectors`` facet (``tai42_app.connectors.token_store``),
independent of the active config mode.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any, Final

CONFIG_MODE_ENV: Final = "TAI_CONFIG_MODE"
"""The env name selecting the deployment's config provider."""

FILE_CONFIG_MODE: Final = "file"
"""The built-in provider: env and manifest read from local files (a local ``.env`` included)."""

CONFIG_MODE_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]*")
"""A mode name (fullmatch): any mode but ``file`` resolves to the module ``tai42_config_<mode>.manager``,
so it must be a valid module-name segment; the pattern also fixes the canonical lowercase spelling."""

CONFIG_DIR_ENV: Final = "TAI_CONFIG_DIR_PATH"
"""The env name of the ``file`` provider's config dir, which holds its stored env and its manifest."""

DEFAULT_CONFIG_DIR: Final = "/app"
"""The ``file`` provider's config dir when ``TAI_CONFIG_DIR_PATH`` is unset or blank."""

FILE_STORED_ENV_NAME: Final = ".env"
"""The ``file`` provider's stored env: the file of this name in its config dir."""


def file_config_dir(environ: Mapping[str, str]) -> str:
    """The ``file`` provider's config dir under ``environ``: ``TAI_CONFIG_DIR_PATH`` stripped, else ``/app``."""
    return environ.get(CONFIG_DIR_ENV, "").strip() or DEFAULT_CONFIG_DIR


def file_stored_env_path(environ: Mapping[str, str]) -> str:
    """The ``file`` provider's stored env under ``environ``: ``.env`` in :func:`file_config_dir`."""
    return os.path.join(file_config_dir(environ), FILE_STORED_ENV_NAME)


class ConfigManager(ABC):
    """Abstract base for environment and manifest configuration backends.

    Each config mode (``file`` built in, or an external provider) supplies a concrete implementation.
    Consumers access the active manager via ``tai42_app.config.config_manager``.
    """

    # -- Write exclusion -------------------------------------------------------

    @abstractmethod
    def exclusive_write(self) -> AbstractContextManager[None]:
        """Hold exclusive write access to this store for the whole span the caller wraps.

        A writer holds the span across a whole read → validate → build → write sequence, so no
        other write lands between its reads and its writes. While a span is held, no other span
        over the same store is held: not by another process sharing the store, and not by
        another thread or task of the same process; the provider excludes both. Entering blocks
        until the span is exclusive and raises when the store cannot grant exclusion — a write
        never proceeds unserialized. The span is released on exit and when the holding process
        terminates.

        Exiting releases the span. A release the provider cannot complete raises from the exit; the
        writes made inside the span stand regardless — an exit never undoes a write — and the store
        may still hold the span until the holding process terminates, so a later entry blocks or
        raises as described above. A provider never swallows a failed release: the caller records
        and reports it.

        The span is entered and exited on worker threads of the calling process, never on its
        event loop, and the exiting thread may differ from the entering one: the span is entered
        on one worker-thread call and exited on another. A span whose waiter was abandoned before
        its entry completed is exited exactly once, by the entering thread, as soon as entry
        completes; a span whose waiter's cancellation lands as its entry completes is exited once,
        by the waiter. The span is therefore not bound to the thread that entered it.
        """

    # -- Environment configuration -------------------------------------------

    @abstractmethod
    def read_env(self) -> dict[str, str]:
        """Read environment key-value pairs.

        Raises:
            FileNotFoundError: If the env source does not exist.
        """

    @abstractmethod
    def write_env(self, config: dict[str, str]) -> None:
        """Write / merge environment key-value pairs.

        Merges *config* with the existing configuration, preserving keys
        that are not present in *config*. Filters out empty values.
        """

    @abstractmethod
    def replace_env(self, config: dict[str, str]) -> None:
        """Atomically replace the whole persisted env map.

        ``config`` becomes the entire stored env: a key absent from ``config``
        is DELETED — nothing from the old env survives uninvited (this is a
        whole-map replace, NOT a merge). Empty values are filtered out, so a
        key mapped to ``""`` is likewise absent from the result. Its
        transactional / atomicity guarantee matches :meth:`write_env`.
        """

    # -- Manifest configuration ----------------------------------------------

    @abstractmethod
    def read_manifest(self) -> dict[str, Any]:
        """Read the active manifest.

        Raises:
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

        Raises:
            FileNotFoundError: If the manifest does not exist.
        """

    @abstractmethod
    def read_defaults_manifest(self) -> dict[str, Any]:
        """Read the defaults / template manifest (empty dict if not found)."""

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
